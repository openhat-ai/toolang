"""Ollama model catalog discovered from a configured local runtime."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
import logging
import shlex
from typing import cast

import httpx

from toolang.base.protocols.model import ModelCatalog
from toolang.base.types.model import (
    CatalogModel,
    CatalogSnapshot,
)

from toolang.plugin.values import (
    mapping,
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
class OllamaModelCatalog(ModelCatalog):
    """Models and metadata currently reported by one Ollama endpoint."""

    environ: Mapping[str, str]
    endpoint: str | None = None
    timeout: float = 2.0
    name: str = "ollama"
    headers: Mapping[str, str] = field(default_factory=dict)

    async def snapshot(self) -> CatalogSnapshot:
        host = _ollama_host(self.endpoint, self.environ)
        entries: tuple[tuple[str, dict[str, object]], ...] = ()
        models: tuple[CatalogModel, ...] = ()
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout, headers=self.headers
            ) as client:
                response = await client.get(f"{host}/api/tags")
                response.raise_for_status()
                payload = response.json()
                raw_models = (
                    payload.get("models") if isinstance(payload, dict) else None
                )
                if isinstance(raw_models, list):
                    entries = model_entries(raw_models)
                loaded = await _loaded_contexts(client, host)
                models = tuple(
                    await asyncio.gather(
                        *(
                            _ollama_model(
                                client, host, model_id, entry, loaded.get(model_id)
                            )
                            for model_id, entry in entries
                        )
                    )
                )
        except httpx.HTTPError as exc:
            logger.debug(
                "catalog.ollama.unreachable endpoint=%s error=%s",
                host,
                type(exc).__name__,
            )
            models = ()
        except (TypeError, ValueError) as exc:
            logger.warning(
                "catalog.ollama.invalid_response endpoint=%s error=%s", host, exc
            )
            models = ()
        return local_snapshot(
            provider_id="ollama",
            provider_name="Ollama",
            endpoint=f"{host}/v1",
            models=models,
        )


async def _ollama_model(
    client: httpx.AsyncClient,
    host: str,
    model_id: str,
    tag: Mapping[str, object],
    loaded_context: int | None = None,
) -> CatalogModel:
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
    except httpx.HTTPError as exc:
        logger.debug(
            "catalog.ollama.show_failed model=%s error=%s",
            model_id,
            type(exc).__name__,
        )
        show = {}
    except (TypeError, ValueError) as exc:
        logger.warning("catalog.ollama.show_invalid model=%s error=%s", model_id, exc)
        show = {}

    tag_details = mapping(tag.get("details"))
    show_details = mapping(show.get("details"))
    details = {**tag_details, **show_details}
    capabilities = string_tuple(show.get("capabilities", tag.get("capabilities")))
    capability_set = {value.lower() for value in capabilities}
    model_info = mapping(show.get("model_info"))
    family = optional_text(details.get("family")) or optional_text(
        model_info.get("general.architecture")
    )
    parameters = _ollama_parameters(show.get("parameters"))
    configured_context = positive_count(parameters.get("num_ctx"), name="num_ctx")
    context = loaded_context or configured_context
    if loaded_context and configured_context and loaded_context != configured_context:
        logger.debug(
            "catalog.ollama.context_disagrees model=%s loaded=%s configured=%s",
            model_id,
            loaded_context,
            configured_context,
        )
    output = positive_count(parameters.get("num_predict"), name="num_predict")
    input_modalities = _ollama_modalities(capability_set)
    completion = "completion" in capability_set
    modified_at = optional_text(show.get("modified_at")) or optional_text(
        tag.get("modified_at")
    )
    return CatalogModel(
        id=model_id,
        provider_id="ollama",
        name=model_id,
        description=_ollama_description(details),
        family=family,
        attachment=len(input_modalities) > 1 if capabilities else None,
        reasoning="thinking" in capability_set if capabilities else None,
        reasoning_options=({"type": "effort", "exhaustive": False},)
        if "thinking" in capability_set
        else None,
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
        limit={
            k: v
            for k, v in {"context": context, "output": output}.items()
            if v is not None
        },
        cost=dict(LOCAL_ZERO_COST),
    )


def create_model_catalog(config: Mapping[str, object]) -> ModelCatalog:
    """Create the built-in Ollama catalog plugin."""

    return OllamaModelCatalog(
        config_environ(config),
        endpoint=config_endpoint(config),
        timeout=config_timeout(config),
        headers=config_headers(config),
    )


async def _loaded_contexts(client: httpx.AsyncClient, host: str) -> dict[str, int]:
    try:
        response = await client.get(f"{host}/api/ps")
        response.raise_for_status()
        payload = mapping(response.json())
        raw = payload.get("models")
        entries = (
            model_entries(cast(list[object], raw)) if isinstance(raw, list) else ()
        )
        return {
            model_id: count
            for model_id, entry in entries
            if (
                count := positive_count(
                    entry.get("context_length"), name="context_length"
                )
            )
            is not None
        }
    except (httpx.HTTPError, TypeError, ValueError):
        logger.debug("catalog.ollama.ps_unavailable")
        return {}


def _ollama_parameters(value: object) -> dict[str, object]:
    if not isinstance(value, str):
        return {}
    result: dict[str, object] = {}
    for line in value.splitlines():
        head = line.split(maxsplit=1)
        if not head or head[0] not in {"num_ctx", "num_predict"}:
            continue
        key = head[0]
        result[key] = None
        try:
            tokens = shlex.split(line, comments=True)
        except ValueError:
            logger.debug("catalog.ollama.invalid_parameter field=%s", key)
            continue
        if len(tokens) == 2:
            try:
                result[key] = int(tokens[1])
            except ValueError:
                result[key] = tokens[1]
    return result


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


def _ollama_description(details: Mapping[str, object]) -> str:
    attributes = [
        value
        for key in ("parameter_size", "quantization_level", "format")
        for value in (optional_text(details.get(key)),)
        if value is not None
    ]
    suffix = f" ({', '.join(attributes)})" if attributes else ""
    return f"Local Ollama model{suffix}."


def _ollama_host(endpoint: str | None, environ: Mapping[str, str]) -> str:
    return resolve_local_endpoint(
        endpoint,
        environ=environ,
        env_name="OLLAMA_HOST",
        default_port=11434,
    ).removesuffix("/v1")
