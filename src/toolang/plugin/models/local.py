"""Ephemeral model catalogs discovered from explicit local runtimes."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
import json
from typing import cast
from urllib.parse import urlsplit, urlunsplit

import httpx

from toolang.base.protocols.model import ModelCatalog
from toolang.base.types.model import Model, ModelCatalogSnapshot, Provider


@dataclass(frozen=True, slots=True)
class OllamaModelCatalog(ModelCatalog):
    """Models and metadata currently reported by one Ollama endpoint."""

    environ: Mapping[str, str]
    endpoint: str | None = None
    timeout: float = 2.0
    name: str = "ollama"

    async def snapshot(self) -> ModelCatalogSnapshot:
        host = _ollama_host(self.endpoint, self.environ)
        entries: tuple[tuple[str, dict[str, object]], ...] = ()
        models: tuple[Model, ...] = ()
        online = False
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(f"{host}/api/tags")
                response.raise_for_status()
                online = True
                payload = response.json()
                raw_models = (
                    payload.get("models") if isinstance(payload, dict) else None
                )
                if isinstance(raw_models, list):
                    entries = _model_entries(raw_models)
                models = tuple(
                    await asyncio.gather(
                        *(
                            _ollama_model(client, host, model_id, entry)
                            for model_id, entry in entries
                        )
                    )
                )
        except (httpx.HTTPError, TypeError, ValueError):
            models = ()
        return _local_snapshot(
            provider_id="ollama",
            provider_name="Ollama",
            endpoint=f"{host}/v1",
            models=models,
            provider_runtime={
                "kind": "ollama",
                "endpoint": host,
                "status": "ready" if online else "offline",
            },
        )


@dataclass(frozen=True, slots=True)
class LlamaCppModelCatalog(ModelCatalog):
    """Models and metadata currently reported by one llama.cpp endpoint."""

    environ: Mapping[str, str]
    endpoint: str | None = None
    timeout: float = 2.0
    name: str = "llama_cpp"

    async def snapshot(self) -> ModelCatalogSnapshot:
        endpoint = _llama_cpp_endpoint(self.endpoint, self.environ)
        entries: tuple[tuple[str, dict[str, object]], ...] = ()
        props: dict[str, object] = {}
        online = False
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(f"{endpoint}/models")
                response.raise_for_status()
                online = True
                payload = response.json()
                raw_models = payload.get("data") if isinstance(payload, dict) else None
                if isinstance(raw_models, list):
                    entries = _model_entries(raw_models)
                props = await _optional_json(
                    client, f"{_llama_cpp_host(endpoint)}/props"
                )
        except (httpx.HTTPError, TypeError, ValueError):
            entries = ()
            props = {}
        models = tuple(
            _llama_cpp_model(
                model_id,
                entry,
                props if _props_match(model_id, entries, props) else {},
            )
            for model_id, entry in entries
        )
        provider_runtime = _compact_mapping(
            {
                "kind": "llama_cpp",
                "endpoint": endpoint,
                "status": "ready" if online else "offline",
                "build_info": props.get("build_info"),
            }
        )
        return _local_snapshot(
            provider_id="llama_cpp",
            provider_name="llama.cpp",
            endpoint=endpoint,
            models=models,
            provider_runtime=provider_runtime,
        )


async def _ollama_model(
    client: httpx.AsyncClient,
    host: str,
    model_id: str,
    tag: Mapping[str, object],
) -> Model:
    show: dict[str, object] = {}
    try:
        response = await client.post(
            f"{host}/api/show",
            json={"model": model_id, "verbose": False},
        )
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, dict):
            show = payload
    except (httpx.HTTPError, TypeError, ValueError):
        show = {}

    tag_details = _mapping(tag.get("details"))
    show_details = _mapping(show.get("details"))
    details = {**tag_details, **show_details}
    capabilities = _string_tuple(show.get("capabilities"))
    capability_set = {value.lower() for value in capabilities}
    model_info = _mapping(show.get("model_info"))
    family = _optional_string(details.get("family"))
    context = _ollama_context(model_info, family=family)
    input_modalities = _ollama_modalities(capability_set)
    completion = "completion" in capability_set
    modified_at = _optional_string(show.get("modified_at")) or _optional_string(
        tag.get("modified_at")
    )
    runtime = _compact_mapping(
        {
            "digest": tag.get("digest"),
            "size": tag.get("size"),
            "modified_at": modified_at,
            "details": details or None,
            "capabilities": capabilities or None,
            "model_info": model_info or None,
            "parameters": show.get("parameters"),
        }
    )
    return Model(
        provider_id="ollama",
        id=model_id,
        name=model_id,
        description=_ollama_description(details),
        family=family,
        attachment=len(input_modalities) > 1 if capabilities else None,
        reasoning="thinking" in capability_set if capabilities else None,
        tool_call="tools" in capability_set if capabilities else None,
        structured_output=True if completion else None,
        temperature=True if completion else None,
        last_updated=modified_at[:10]
        if modified_at and len(modified_at) >= 10
        else None,
        modalities={
            "input": input_modalities,
            "output": ("text",),
        },
        limit={"context": context} if context is not None else {},
        cost={"input": 0, "output": 0},
        extra={"runtime": runtime},
        local=True,
    )


def _llama_cpp_model(
    model_id: str,
    entry: Mapping[str, object],
    props: Mapping[str, object],
) -> Model:
    meta = _mapping(entry.get("meta"))
    settings = _mapping(props.get("default_generation_settings"))
    params = _mapping(settings.get("params"))
    context = _positive_int(settings.get("n_ctx")) or _positive_int(
        meta.get("n_ctx_train")
    )
    output = _positive_int(params.get("n_predict")) or _positive_int(
        params.get("max_tokens")
    )
    modalities = _llama_cpp_modalities(props.get("modalities"))
    caps = _mapping(props.get("chat_template_caps"))
    tool_call = _true_capability(caps, "supports_tools", "supports_tool_calls")
    reasoning = _true_capability(
        caps,
        "supports_thinking",
        "supports_reasoning",
        "supports_reasoning_content",
    )
    limit = _compact_mapping({"context": context, "output": output})
    runtime = _compact_mapping(
        {
            "created": entry.get("created"),
            "owned_by": entry.get("owned_by"),
            "meta": meta or None,
            "model_path": props.get("model_path"),
            "build_info": props.get("build_info"),
            "total_slots": props.get("total_slots"),
            "chat_template_caps": caps or None,
            "modalities": props.get("modalities"),
            "context": context,
            "max_output_tokens": output,
        }
    )
    return Model(
        provider_id="llama_cpp",
        id=model_id,
        name=model_id,
        description=_llama_cpp_description(meta),
        family=_optional_string(meta.get("architecture"))
        or _optional_string(meta.get("general_architecture")),
        attachment="image" in modalities
        if props.get("modalities") is not None
        else None,
        reasoning=reasoning,
        tool_call=tool_call,
        structured_output=True,
        temperature=True,
        modalities={"input": modalities, "output": ("text",)},
        limit={key: value for key, value in limit.items() if isinstance(value, int)},
        cost={"input": 0, "output": 0},
        extra={"runtime": runtime},
        local=True,
    )


async def _optional_json(client: httpx.AsyncClient, url: str) -> dict[str, object]:
    try:
        response = await client.get(url)
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def create_ollama_model_catalog(config: Mapping[str, object]) -> ModelCatalog:
    """Create the built-in Ollama catalog plugin."""

    return OllamaModelCatalog(
        _config_environ(config),
        endpoint=_optional_string(config.get("endpoint")),
        timeout=_config_timeout(config),
    )


def create_llama_cpp_model_catalog(config: Mapping[str, object]) -> ModelCatalog:
    """Create the built-in llama.cpp catalog plugin."""

    return LlamaCppModelCatalog(
        _config_environ(config),
        endpoint=_optional_string(config.get("endpoint")),
        timeout=_config_timeout(config),
    )


def _config_environ(config: Mapping[str, object]) -> Mapping[str, str]:
    value = config.get("environ")
    if not isinstance(value, Mapping):
        return {}
    return {
        str(key): str(item)
        for key, item in value.items()
        if isinstance(key, str) and isinstance(item, str)
    }


def _config_timeout(config: Mapping[str, object]) -> float:
    value = config.get("timeout", 2.0)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError("local model catalog timeout must be numeric")
    return float(value)


def _local_snapshot(
    *,
    provider_id: str,
    provider_name: str,
    endpoint: str,
    models: tuple[Model, ...],
    provider_runtime: Mapping[str, object],
) -> ModelCatalogSnapshot:
    by_id = {model.id: model for model in models}
    provider = Provider(
        id=provider_id,
        name=provider_name,
        env=(),
        npm="@ai-sdk/openai-compatible",
        api=endpoint,
        models=by_id,
        extra={"runtime": dict(provider_runtime)},
        local=True,
    )
    identity = json.dumps(
        provider.to_data(),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return ModelCatalogSnapshot(
        providers={provider_id: provider},
        models=tuple(by_id[key] for key in sorted(by_id)),
        revision=f"runtime:{sha256(identity.encode()).hexdigest()}",
    )


def _model_entries(items: list[object]) -> tuple[tuple[str, dict[str, object]], ...]:
    entries: dict[str, dict[str, object]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        data = cast(dict[str, object], item)
        raw = data.get("model") or data.get("name") or data.get("id")
        if isinstance(raw, str) and raw.strip():
            entries.setdefault(raw.strip(), data)
    return tuple(sorted(entries.items()))


def _ollama_context(
    model_info: Mapping[str, object], *, family: str | None
) -> int | None:
    if family is not None:
        exact = _positive_int(model_info.get(f"{family}.context_length"))
        if exact is not None:
            return exact
    values = [
        value
        for key, raw in model_info.items()
        if str(key).endswith(".context_length")
        for value in (_positive_int(raw),)
        if value is not None
    ]
    return max(values, default=None)


def _llama_cpp_modalities(value: object) -> tuple[str, ...]:
    modalities = ["text"]
    if isinstance(value, Mapping):
        for name, enabled in value.items():
            normalized = "image" if str(name).lower() == "vision" else str(name).lower()
            if enabled is True and normalized not in modalities:
                modalities.append(normalized)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            if not isinstance(item, str):
                continue
            normalized = "image" if item.lower() == "vision" else item.lower()
            if normalized not in modalities:
                modalities.append(normalized)
    return tuple(modalities)


def _ollama_modalities(capabilities: set[str]) -> tuple[str, ...]:
    modalities = ["text"]
    for capability, modality in (
        ("vision", "image"),
        ("video", "video"),
        ("audio", "audio"),
    ):
        if capability in capabilities:
            modalities.append(modality)
    return tuple(modalities)


def _props_match(
    model_id: str,
    entries: tuple[tuple[str, dict[str, object]], ...],
    props: Mapping[str, object],
) -> bool:
    if not props:
        return False
    if len(entries) == 1:
        return True
    model_path = _optional_string(props.get("model_path"))
    return model_path == model_id


def _true_capability(caps: Mapping[str, object], *names: str) -> bool | None:
    values = [caps[name] for name in names if name in caps]
    if not values:
        return None
    return any(value is True for value in values)


def _mapping(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): item for key, item in value.items()}


def _compact_mapping(values: Mapping[str, object | None]) -> dict[str, object]:
    return {key: value for key, value in values.items() if value is not None}


def _string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return ()
    return tuple(
        item.strip() for item in value if isinstance(item, str) and item.strip()
    )


def _optional_string(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _positive_int(value: object) -> int | None:
    return (
        value
        if isinstance(value, int) and not isinstance(value, bool) and value > 0
        else None
    )


def _ollama_description(details: Mapping[str, object]) -> str:
    attributes = [
        value
        for key in ("parameter_size", "quantization_level", "format")
        for value in (_optional_string(details.get(key)),)
        if value is not None
    ]
    suffix = f" ({', '.join(attributes)})" if attributes else ""
    return f"Local Ollama model{suffix}."


def _llama_cpp_description(meta: Mapping[str, object]) -> str:
    parameters = _positive_int(meta.get("n_params"))
    suffix = f" ({parameters:,} parameters)" if parameters is not None else ""
    return f"Local llama.cpp model{suffix}."


def _ollama_host(endpoint: str | None, environ: Mapping[str, str]) -> str:
    value = endpoint or environ.get("OLLAMA_HOST")
    if value is None:
        host = environ.get("TOOLANG_HOST_GATEWAY", "127.0.0.1")
        value = f"http://{host}:11434"
    elif endpoint is None:
        value = _replace_guest_loopback(value, environ)
    value = value.rstrip("/")
    return value.removesuffix("/v1")


def _llama_cpp_endpoint(endpoint: str | None, environ: Mapping[str, str]) -> str:
    value = endpoint or environ.get("LLAMA_CPP_HOST")
    if value is None:
        host = environ.get("TOOLANG_HOST_GATEWAY", "127.0.0.1")
        value = f"http://{host}:8080"
    elif endpoint is None:
        value = _replace_guest_loopback(value, environ)
    value = value.rstrip("/")
    return value if value.endswith("/v1") else f"{value}/v1"


def _replace_guest_loopback(value: str, environ: Mapping[str, str]) -> str:
    gateway = environ.get("TOOLANG_HOST_GATEWAY")
    if not gateway:
        return value
    try:
        parsed = urlsplit(value)
        if parsed.hostname not in {
            "0.0.0.0",
            "127.0.0.1",
            "localhost",
            "::",
            "::1",
        }:
            return value
        port = parsed.port
    except ValueError:
        return value
    gateway_host = f"[{gateway}]" if ":" in gateway else gateway
    netloc = f"{gateway_host}:{port}" if port is not None else gateway_host
    return urlunsplit(parsed._replace(netloc=netloc))


def _llama_cpp_host(endpoint: str) -> str:
    return endpoint.removesuffix("/v1")
