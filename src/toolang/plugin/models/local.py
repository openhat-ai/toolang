"""Ephemeral model catalogs discovered from explicit local runtimes."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256

import httpx
from typing import cast

from toolang.base.protocols.model import ModelCatalog
from toolang.base.types.model import Model, ModelCatalogSnapshot, Provider


@dataclass(frozen=True, slots=True)
class OllamaModels(ModelCatalog):
    """Models currently reported by one configured Ollama endpoint."""

    environ: Mapping[str, str]
    endpoint: str | None = None
    timeout: float = 2.0

    async def snapshot(self) -> ModelCatalogSnapshot:
        host = _ollama_host(self.endpoint, self.environ)
        names: tuple[str, ...] = ()
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(f"{host}/api/tags")
                response.raise_for_status()
            payload = response.json()
            raw_models = payload.get("models") if isinstance(payload, dict) else None
            if isinstance(raw_models, list):
                names = _model_names(raw_models)
        except (httpx.HTTPError, TypeError, ValueError):
            names = ()
        return _local_snapshot(
            provider_id="ollama",
            provider_name="Ollama",
            endpoint=f"{host}/v1",
            model_ids=names,
        )


@dataclass(frozen=True, slots=True)
class LlamaCppModels(ModelCatalog):
    """Models currently reported by one configured llama.cpp endpoint."""

    environ: Mapping[str, str]
    endpoint: str | None = None
    timeout: float = 2.0

    async def snapshot(self) -> ModelCatalogSnapshot:
        endpoint = _llama_cpp_endpoint(self.endpoint, self.environ)
        names: tuple[str, ...] = ()
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(f"{endpoint}/models")
                response.raise_for_status()
            payload = response.json()
            raw_models = payload.get("data") if isinstance(payload, dict) else None
            if isinstance(raw_models, list):
                names = _model_names(raw_models)
        except (httpx.HTTPError, TypeError, ValueError):
            names = ()
        return _local_snapshot(
            provider_id="llama_cpp",
            provider_name="llama.cpp",
            endpoint=endpoint,
            model_ids=names,
        )


def _local_snapshot(
    *,
    provider_id: str,
    provider_name: str,
    endpoint: str,
    model_ids: tuple[str, ...],
) -> ModelCatalogSnapshot:
    models = {
        model_id: Model(
            provider_id=provider_id,
            id=model_id,
            name=model_id,
            modalities={"input": ("text",), "output": ("text",)},
            limit={},
            local=True,
        )
        for model_id in model_ids
    }
    provider = Provider(
        id=provider_id,
        name=provider_name,
        env=(),
        npm="@ai-sdk/openai-compatible",
        api=endpoint,
        models=models,
        local=True,
    )
    identity = "\n".join((provider_id, endpoint, *model_ids))
    return ModelCatalogSnapshot(
        providers={provider_id: provider},
        models=tuple(models[key] for key in sorted(models)),
        revision=f"runtime:{sha256(identity.encode()).hexdigest()}",
    )


def _model_names(items: list[object]) -> tuple[str, ...]:
    names: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        data = cast(dict[str, object], item)
        raw = data.get("model") or data.get("name") or data.get("id")
        if isinstance(raw, str) and raw.strip():
            names.add(raw.strip())
    return tuple(sorted(names))


def _ollama_host(endpoint: str | None, environ: Mapping[str, str]) -> str:
    value = endpoint or environ.get("OLLAMA_HOST") or "http://127.0.0.1:11434"
    value = value.rstrip("/")
    return value.removesuffix("/v1")


def _llama_cpp_endpoint(endpoint: str | None, environ: Mapping[str, str]) -> str:
    value = endpoint or environ.get("LLAMA_CPP_HOST") or "http://127.0.0.1:8080"
    value = value.rstrip("/")
    return value if value.endswith("/v1") else f"{value}/v1"
