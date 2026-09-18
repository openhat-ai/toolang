"""llama.cpp model catalog discovered from a configured local runtime."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import httpx

from toolang.base.protocols.model import ModelCatalog
from toolang.base.types.model import Model, ModelCatalogSnapshot

from ._local import (
    compact_mapping,
    config_environ,
    config_timeout,
    local_snapshot,
    mapping,
    model_entries,
    optional_string,
    positive_int,
    replace_guest_loopback,
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
                    entries = model_entries(raw_models)
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
        provider_runtime = compact_mapping(
            {
                "kind": "llama_cpp",
                "endpoint": endpoint,
                "status": "ready" if online else "offline",
                "build_info": props.get("build_info"),
            }
        )
        return local_snapshot(
            provider_id="llama_cpp",
            provider_name="llama.cpp",
            endpoint=endpoint,
            models=models,
            provider_runtime=provider_runtime,
        )


def _llama_cpp_model(
    model_id: str,
    entry: Mapping[str, object],
    props: Mapping[str, object],
) -> Model:
    meta = mapping(entry.get("meta"))
    settings = mapping(props.get("default_generation_settings"))
    params = mapping(settings.get("params"))
    context = positive_int(settings.get("n_ctx")) or positive_int(
        meta.get("n_ctx_train")
    )
    output = positive_int(params.get("n_predict")) or positive_int(
        params.get("max_tokens")
    )
    modalities = _llama_cpp_modalities(props.get("modalities"))
    caps = mapping(props.get("chat_template_caps"))
    tool_call = _true_capability(caps, "supports_tools", "supports_tool_calls")
    reasoning = _true_capability(
        caps,
        "supports_thinking",
        "supports_reasoning",
        "supports_reasoning_content",
    )
    limit = compact_mapping({"context": context, "output": output})
    runtime = compact_mapping(
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
        family=optional_string(meta.get("architecture"))
        or optional_string(meta.get("general_architecture")),
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


def create_llama_cpp_model_catalog(config: Mapping[str, object]) -> ModelCatalog:
    """Create the built-in llama.cpp catalog plugin."""

    return LlamaCppModelCatalog(
        config_environ(config),
        endpoint=optional_string(config.get("endpoint")),
        timeout=config_timeout(config),
    )


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


def _props_match(
    model_id: str,
    entries: tuple[tuple[str, dict[str, object]], ...],
    props: Mapping[str, object],
) -> bool:
    if not props:
        return False
    if len(entries) == 1:
        return True
    model_path = optional_string(props.get("model_path"))
    return model_path == model_id


def _true_capability(caps: Mapping[str, object], *names: str) -> bool | None:
    values = [caps[name] for name in names if name in caps]
    if not values:
        return None
    return any(value is True for value in values)


def _llama_cpp_description(meta: Mapping[str, object]) -> str:
    parameters = positive_int(meta.get("n_params"))
    suffix = f" ({parameters:,} parameters)" if parameters is not None else ""
    return f"Local llama.cpp model{suffix}."


def _llama_cpp_endpoint(endpoint: str | None, environ: Mapping[str, str]) -> str:
    value = endpoint or environ.get("LLAMA_CPP_HOST")
    if value is None:
        host = environ.get("TOOLANG_HOST_GATEWAY", "127.0.0.1")
        value = f"http://{host}:8080"
    elif endpoint is None:
        value = replace_guest_loopback(value, environ)
    value = value.rstrip("/")
    return value if value.endswith("/v1") else f"{value}/v1"


def _llama_cpp_host(endpoint: str) -> str:
    return endpoint.removesuffix("/v1")
