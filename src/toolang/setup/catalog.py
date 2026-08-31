"""One-shot raw model catalog inspection loading."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from toolang.base.protocols.model import ModelAdapter, ModelCatalog
from toolang.base.types.model import ModelCatalogSnapshot
from toolang.common.layout import AgentLayout
from toolang.plugin.config import merge_plugin_configs
from toolang.plugin.models.catalog import (
    MergedModelCatalog,
    model_info_from_catalog,
    resolve_model_catalog_path,
)
from toolang.plugin.models.config import (
    configure_catalog_providers,
    parse_provider_configs,
)
from toolang.plugin.models.loading import load_model_adapters, load_model_catalogs
from toolang.plugin.models.provider_resolver import resolve_catalog_providers
from toolang.plugin.models.resolution import build_model_collection
from toolang.plugin.models.collections import ModelCollection

from .config import load_agent_config, load_setup_config, load_setup_envs


@dataclass(frozen=True, slots=True)
class CatalogInspection:
    """Raw resolved catalog plus process-local inspection facts."""

    snapshot: ModelCatalogSnapshot
    adapters: Mapping[str, ModelAdapter]
    envs: Mapping[str, str]
    models: ModelCollection

    def __post_init__(self) -> None:
        object.__setattr__(self, "adapters", MappingProxyType(dict(self.adapters)))
        object.__setattr__(self, "envs", MappingProxyType(dict(self.envs)))


@dataclass(frozen=True, slots=True)
class _SnapshotCatalog(ModelCatalog):
    value: ModelCatalogSnapshot
    name: str

    async def snapshot(self) -> ModelCatalogSnapshot:
        return self.value


async def load_catalog_inspection(
    layout: AgentLayout,
    *,
    model_catalog: Path | None = None,
) -> CatalogInspection:
    """Load raw catalogs once without constructing an AgentSetup."""

    configs = (load_setup_config(layout), load_agent_config(layout))
    envs = load_setup_envs(layout)
    adapters = load_model_adapters(
        merge_plugin_configs(configs, family="model_adapter")
    )
    catalog_configs = merge_plugin_configs(configs, family="model_catalog")
    catalog_configs["models_dev"] = {
        **catalog_configs.get("models_dev", {}),
        "path": resolve_model_catalog_path(
            layout,
            explicit=model_catalog,
            environ=envs,
        ),
    }
    for name in ("ollama", "llama_cpp"):
        catalog_configs[name] = {
            **catalog_configs.get(name, {}),
            "environ": envs,
        }
    catalogs = load_model_catalogs(catalog_configs)
    ordered = tuple(
        catalogs.pop(name)
        for name in ("models_dev", "ollama", "llama_cpp")
        if name in catalogs
    ) + tuple(catalogs[name] for name in sorted(catalogs))
    if not ordered or ordered[0].name != "models_dev":
        raise RuntimeError("models_dev catalog plugin is not installed")
    snapshots = await asyncio.gather(*(catalog.snapshot() for catalog in ordered))
    merged = await MergedModelCatalog(
        tuple(
            _SnapshotCatalog(snapshot, catalog.name)
            for catalog, snapshot in zip(ordered, snapshots, strict=True)
        )
    ).snapshot()
    provider_configs = parse_provider_configs(configs)
    providers = configure_catalog_providers(merged.providers, provider_configs)
    resolved = resolve_catalog_providers(
        ModelCatalogSnapshot(
            providers=providers,
            models=merged.models,
            revision=merged.revision,
            source=merged.source,
        ),
        adapters=adapters,
        environ=envs,
        configs=provider_configs,
    )
    infos = tuple(model_info_from_catalog(model) for model in resolved.models)
    models = build_model_collection(
        providers=resolved.providers,
        models=infos,
        envs=envs,
        provider_configs=provider_configs,
    )
    return CatalogInspection(
        snapshot=resolved,
        adapters=adapters,
        envs=envs,
        models=models,
    )


__all__ = ["CatalogInspection", "load_catalog_inspection"]
