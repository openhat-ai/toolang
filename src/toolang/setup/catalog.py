"""Catalog snapshot merging."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from toolang.base.protocols.model import ModelCatalog
from toolang.base.types.model import Model, ModelCatalogSnapshot, Provider


@dataclass(frozen=True, slots=True)
class MergedModelCatalog(ModelCatalog):
    """Merge exact provider/model records from ordered catalog sources."""

    sources: tuple[ModelCatalog, ...]
    name: str = "merged"

    async def snapshot(self) -> ModelCatalogSnapshot:
        """Load sources in order and reject conflicting exact identities."""

        snapshots = list(
            await asyncio.gather(*(source.snapshot() for source in self.sources))
        )
        if not snapshots:
            return ModelCatalogSnapshot(providers={}, models=(), revision="sha256:0")
        providers: dict[str, Provider] = {}
        models: dict[tuple[str, str], Model] = {}
        for source, raw_snapshot in zip(self.sources, snapshots, strict=True):
            snapshot = raw_snapshot
            for provider_id, provider in snapshot.providers.items():
                if provider_id in providers:
                    raise ValueError(f"duplicate catalog provider: {provider_id}")
                providers[provider_id] = provider
            for model in snapshot.models:
                identity = (model._toolang.provider, model.id)
                if identity in models:
                    raise ValueError(f"duplicate catalog model: {model.identity}")
                models[identity] = model
        return ModelCatalogSnapshot(
            providers=providers,
            models=tuple(models[key] for key in sorted(models)),
            revision=snapshots[0].revision,
            source=snapshots[0].source,
        )


__all__ = ["MergedModelCatalog"]
