"""Deprecated provider-discovery compatibility without persistent caching."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path

from toolang.base.protocols.model import ModelProvider
from toolang.base.types.model import ModelInfo
from toolang.plugin.models.discovery import missing_provider_env_vars


class ModelListCache:
    """One-cycle compatibility facade that never reads or writes disk."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    async def get(
        self,
        provider: ModelProvider,
        *,
        envs: Mapping[str, str],
        refresh: bool = False,
    ) -> tuple[ModelInfo, ...]:
        del refresh
        try:
            return await asyncio.to_thread(provider.list_models, environ=dict(envs))
        except Exception:
            return ()

    def read(
        self,
        provider: ModelProvider,
        *,
        envs: Mapping[str, str],
    ) -> None:
        del provider, envs
        return None


async def discover_models(
    providers: Mapping[str, ModelProvider],
    *,
    envs: Mapping[str, str],
    cache: ModelListCache,
    refresh: bool = False,
) -> tuple[ModelInfo, ...]:
    """Run legacy providers once in memory for compatibility callers."""

    available = tuple(
        provider
        for provider in providers.values()
        if provider.name != "custom"
        and not missing_provider_env_vars(provider, environ=envs)
    )
    discovered = await asyncio.gather(
        *(cache.get(provider, envs=envs, refresh=refresh) for provider in available)
    )
    models: dict[tuple[str, str], ModelInfo] = {}
    for provider, provider_models in zip(available, discovered, strict=True):
        for model in provider_models:
            if model.provider != provider.name:
                raise ValueError(
                    f"provider {provider.name!r} returned model for "
                    f"{model.provider!r}: {model.ref}"
                )
            models[(model.provider, model.ref)] = model
    return tuple(models[key] for key in sorted(models))
