"""Catalog snapshot merging."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, fields

from toolang.base.protocols.model import ModelCatalog
from toolang.base.types.model import (
    CatalogSnapshot,
    Model,
    ModelFacts,
    ModelToolang,
    ModelCatalogSnapshot,
    Provider,
    ProviderToolang,
)


@dataclass(frozen=True, slots=True)
class MergedModelCatalog(ModelCatalog):
    """Merge exact provider/model records from ordered catalog sources."""

    sources: tuple[ModelCatalog, ...]
    name: str = "merged"

    async def snapshot(self) -> ModelCatalogSnapshot:
        """Load sources in order and reject conflicting exact identities."""

        snapshots = [
            assemble_catalog(s)
            for s in await asyncio.gather(
                *(source.snapshot() for source in self.sources)
            )
        ]
        if not snapshots:
            return ModelCatalogSnapshot(providers={}, models=(), revision="sha256:0")
        providers: dict[str, Provider] = {}
        models: dict[tuple[str, str], Model] = {}
        for snapshot in snapshots:
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


def assemble_catalog(
    snapshot: CatalogSnapshot | ModelCatalogSnapshot,
) -> ModelCatalogSnapshot:
    """Translate portable plugin facts into host records at the setup boundary."""

    if isinstance(snapshot, ModelCatalogSnapshot):
        return snapshot
    return ModelCatalogSnapshot(
        providers={
            key: Provider(
                id=p.id,
                name=p.name,
                api=p.api,
                _toolang=ProviderToolang(env=p.env, adapter=p.adapter),
            )
            for key, p in snapshot.providers.items()
        },
        models=tuple(
            Model(
                **{f.name: getattr(model, f.name) for f in fields(ModelFacts)},
                _toolang=ModelToolang(provider=model.provider_id),
            )
            for model in snapshot.models
        ),
        revision=snapshot.revision,
        local=snapshot.local,
    )
