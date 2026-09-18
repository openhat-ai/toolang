"""Ollama model catalog discovered from a configured local runtime."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
import logging

import httpx

from toolang.base.protocols.model import ModelCatalog
from toolang.base.types.model import (
    LOCAL_RUNTIME_STATUS,
    LOCAL_STATUS_OFFLINE,
    LOCAL_STATUS_READY,
    Model,
    ModelCatalogSnapshot,
)

from toolang.plugin.values import (
    compact_mapping,
    mapping,
    optional_int,
    optional_text,
    string_tuple,
)

from ._local import (
    config_environ,
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
                    entries = model_entries(raw_models)
                models = tuple(
                    await asyncio.gather(
                        *(
                            _ollama_model(client, host, model_id, entry)
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
            provider_runtime={
                "kind": "ollama",
                "endpoint": host,
                LOCAL_RUNTIME_STATUS: (
                    LOCAL_STATUS_READY if online else LOCAL_STATUS_OFFLINE
                ),
            },
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
    capabilities = string_tuple(show.get("capabilities"))
    capability_set = {value.lower() for value in capabilities}
    model_info = mapping(show.get("model_info"))
    family = optional_text(details.get("family"))
    context = _ollama_context(model_info, family=family)
    input_modalities = _ollama_modalities(capability_set)
    completion = "completion" in capability_set
    modified_at = optional_text(show.get("modified_at")) or optional_text(
        tag.get("modified_at")
    )
    runtime = compact_mapping(
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
    )


def create_ollama_model_catalog(config: Mapping[str, object]) -> ModelCatalog:
    """Create the built-in Ollama catalog plugin."""

    return OllamaModelCatalog(
        config_environ(config),
        endpoint=optional_text(config.get("endpoint")),
        timeout=config_timeout(config),
    )


def _ollama_context(
    model_info: Mapping[str, object], *, family: str | None
) -> int | None:
    if family is not None:
        exact = optional_int(model_info.get(f"{family}.context_length"), minimum=1)
        if exact is not None:
            return exact
    values = [
        value
        for key, raw in model_info.items()
        if str(key).endswith(".context_length")
        for value in (optional_int(raw, minimum=1),)
        if value is not None
    ]
    return max(values, default=None)


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
