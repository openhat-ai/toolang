"""Catalog snapshot merging and the models.dev source read."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from toolang.base.protocols.model import ModelCatalog
from toolang.base.types.model import Model, ModelCatalogSnapshot, Provider
from toolang.common.layout import AgentLayout
from toolang.plugin.catalogs.models_dev.catalog import read_model_catalog_snapshot
from toolang.plugin.catalogs.models_dev.path import resolve_model_catalog_path

from .config import load_root_setup_envs, load_setup_envs


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


def load_models_dev_snapshot(
    layout: AgentLayout,
    *,
    model_catalog: Path | None = None,
    agent_context: bool = True,
) -> ModelCatalogSnapshot:
    """Read the selected models.dev source without merging or resolving.

    This is the models.dev-compatible catalog itself, so an export needs no
    local-only guard.
    """

    envs = load_setup_envs(layout) if agent_context else load_root_setup_envs(layout)
    path = resolve_model_catalog_path(
        layout,
        explicit=model_catalog,
        environ=envs,
        include_agent=agent_context,
    )
    return read_model_catalog_snapshot(path)


__all__ = ["MergedModelCatalog", "load_models_dev_snapshot"]
