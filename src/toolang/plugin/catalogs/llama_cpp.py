"""llama.cpp model catalog discovered from a configured local runtime."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
import logging
from urllib.parse import urlencode

import httpx

from toolang.base.protocols.model import ModelCatalog
from toolang.base.types.model import (
    CatalogModel,
    CatalogSnapshot,
)

from toolang.plugin.values import (
    compact_mapping,
    mapping,
    optional_int,
    optional_text,
    string_tuple,
)

from ._local import (
    LOCAL_ZERO_COST,
    config_environ,
    config_endpoint,
    config_headers,
    positive_count,
    config_timeout,
    local_snapshot,
    model_entries,
    resolve_local_endpoint,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class LlamaCppModelCatalog(ModelCatalog):
    """Models and metadata currently reported by one llama.cpp endpoint."""

    environ: Mapping[str, str]
    endpoint: str | None = None
    timeout: float = 2.0
    name: str = "llama_cpp"
    headers: Mapping[str, str] = field(default_factory=dict)

    async def snapshot(self) -> CatalogSnapshot:
        endpoint = _llama_cpp_endpoint(self.endpoint, self.environ)
        entries: tuple[tuple[str, dict[str, object]], ...] = ()
        models: tuple[CatalogModel, ...] = ()
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout, headers=self.headers
            ) as client:
                response = await client.get(f"{endpoint}/models")
                response.raise_for_status()
                payload = response.json()
                raw_models = payload.get("data") if isinstance(payload, dict) else None
                if isinstance(raw_models, list):
                    entries = model_entries(raw_models)
                discovered: list[CatalogModel] = []
                for model_id, entry in entries:
                    url = f"{_llama_cpp_host(endpoint)}/props"
                    props = await _optional_json(
                        client,
                        f"{url}?{urlencode({'model': model_id, 'autoload': 'false'})}",
                        fallback=url if len(entries) == 1 else None,
                    )
                    if not _props_match(model_id, entries, props):
                        logger.debug(
                            "catalog.llama_cpp.props_model_mismatch model=%s", model_id
                        )
                        props = {}
                    discovered.append(_llama_cpp_model(model_id, entry, props))
                models = tuple(discovered)
        except httpx.HTTPError as exc:
            logger.debug(
                "catalog.llama_cpp.unreachable endpoint=%s error=%s",
                endpoint,
                type(exc).__name__,
            )
        except (TypeError, ValueError) as exc:
            logger.warning(
                "catalog.llama_cpp.invalid_response endpoint=%s error=%s",
                endpoint,
                type(exc).__name__,
            )
        return local_snapshot(
            provider_id="llama_cpp",
            provider_name="llama.cpp",
            endpoint=endpoint,
            models=models,
        )


def _llama_cpp_model(
    model_id: str,
    entry: Mapping[str, object],
    props: Mapping[str, object],
) -> CatalogModel:
    meta = mapping(entry.get("meta"))
    settings = mapping(props.get("default_generation_settings"))
    params = mapping(settings.get("params"))
    context = positive_count(settings.get("n_ctx"), name="n_ctx") or positive_count(
        meta.get("n_ctx"), name="meta.n_ctx"
    )
    if (
        "n_predict" in params
        and "max_tokens" in params
        and params["n_predict"] != params["max_tokens"]
    ):
        logger.debug("catalog.llama_cpp.prediction_alias_conflict model=%s", model_id)
    output = positive_count(
        params.get("n_predict", params.get("max_tokens")), name="n_predict"
    )
    architecture = mapping(entry.get("architecture"))
    raw_modalities = props.get("modalities", architecture.get("input_modalities"))
    modalities = _llama_cpp_modalities(raw_modalities)
    caps = mapping(props.get("chat_template_caps", entry.get("chat_template_caps")))
    tool_call = _tool_capability(caps)
    reasoning = _true_capability(
        caps,
        "supports_reasoning_effort",
        "supports_thinking",
        "supports_reasoning",
        "supports_reasoning_content",
    )
    limit = compact_mapping({"context": context, "output": output})
    return CatalogModel(
        id=model_id,
        provider_id="llama_cpp",
        name=model_id,
        description=_llama_cpp_description(meta),
        family=optional_text(meta.get("architecture"))
        or optional_text(meta.get("general_architecture")),
        attachment=len(modalities) > 1 if raw_modalities is not None else None,
        reasoning=reasoning,
        reasoning_options=({"type": "effort", "exhaustive": False},)
        if caps.get("supports_reasoning_effort") is True
        else None,
        tool_call=tool_call,
        structured_output=True,
        temperature=True,
        modalities={
            "input": modalities,
            "output": _llama_cpp_modalities(architecture.get("output_modalities")),
        },
        limit={key: value for key, value in limit.items() if isinstance(value, int)},
        cost=dict(LOCAL_ZERO_COST),
    )


async def _optional_json(
    client: httpx.AsyncClient, url: str, *, fallback: str | None = None
) -> dict[str, object]:
    try:
        response = await client.get(url)
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPStatusError as exc:
        if fallback is not None and exc.response.status_code in {404, 405}:
            return await _optional_json(client, fallback)
        logger.debug(
            "catalog.llama_cpp.props_unavailable status=%s", exc.response.status_code
        )
        return {}
    except httpx.HTTPError as exc:
        logger.debug(
            "catalog.llama_cpp.props_unavailable url=%s error=%s",
            url,
            type(exc).__name__,
        )
        return {}
    except (TypeError, ValueError) as exc:
        logger.warning("catalog.llama_cpp.props_invalid url=%s error=%s", url, exc)
        return {}
    return payload if isinstance(payload, dict) else {}


def create_model_catalog(config: Mapping[str, object]) -> ModelCatalog:
    """Create the built-in llama.cpp catalog plugin."""

    return LlamaCppModelCatalog(
        config_environ(config),
        endpoint=config_endpoint(config),
        timeout=config_timeout(config),
        headers=config_headers(config),
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
    entries: tuple[tuple[str, Mapping[str, object]], ...],
    props: Mapping[str, object],
) -> bool:
    """Reject scoped responses that explicitly identify another listed model."""

    names = {key: set(string_tuple(entry.get("aliases"))) for key, entry in entries}
    identity = optional_text(props.get("model_alias"))
    if identity is None:
        path = optional_text(props.get("model_path"))
        # Older servers expose a file path instead of an API identity. Only
        # compare it when it matches an advertised name; never guess basenames.
        if path is None or not any(
            path == key or path in aliases for key, aliases in names.items()
        ):
            return True
        identity = path
    if identity in names:
        return identity == model_id
    owners = [key for key, aliases in names.items() if identity in aliases]
    return owners == [model_id]


def _tool_capability(caps: Mapping[str, object]) -> bool | None:
    values = [caps.get(name) for name in ("supports_tools", "supports_tool_calls")]
    if any(value is False for value in values):
        return False
    return True if all(value is True for value in values) else None


def _true_capability(caps: Mapping[str, object], *names: str) -> bool | None:
    if any(caps.get(name) is True for name in names):
        return True
    explicit = [
        caps[name]
        for name in names
        if name != "supports_reasoning_effort" and type(caps.get(name)) is bool
    ]
    return False if explicit else None


def _llama_cpp_description(meta: Mapping[str, object]) -> str:
    parameters = optional_int(meta.get("n_params"), minimum=1)
    suffix = f" ({parameters:,} parameters)" if parameters is not None else ""
    return f"Local llama.cpp model{suffix}."


def _llama_cpp_endpoint(endpoint: str | None, environ: Mapping[str, str]) -> str:
    value = resolve_local_endpoint(
        endpoint,
        environ=environ,
        env_name="LLAMA_CPP_HOST",
        default_port=8080,
    )
    return value if value.endswith("/v1") else f"{value}/v1"


def _llama_cpp_host(endpoint: str) -> str:
    return endpoint.removesuffix("/v1")
