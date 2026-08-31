"""One-shot raw model catalog inspection loading."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
import logging
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
from toolang.plugin.models.cache import (
    FileFingerprint,
    ModelProjectionCache,
    environment_readiness,
    hydrate_model_infos,
    model_projection_key,
)
from toolang.plugin.models.config import (
    configure_catalog_providers,
    parse_provider_configs,
)
from toolang.plugin.models.loading import load_model_adapters, load_model_catalogs
from toolang.plugin.models.provider_resolver import resolve_catalog_providers
from toolang.plugin.models.resolution import build_model_collection
from toolang.plugin.models.collections import ModelCollection

from .config import (
    load_agent_config,
    load_setup_config,
    load_setup_envs,
    project_setup_config,
)

_LOCAL_CATALOG_ENV = frozenset(
    {
        "LLAMA_CPP_HOST",
        "OLLAMA_HOST",
        "TOOLANG_HOST_GATEWAY",
    }
)
logger = logging.getLogger(__name__)


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
    config_value = tuple(project_setup_config(config) for config in configs)
    envs = load_setup_envs(layout)
    adapters = load_model_adapters(
        merge_plugin_configs(configs, family="model_adapter")
    )
    catalog_configs = merge_plugin_configs(configs, family="model_catalog")
    catalog_path = resolve_model_catalog_path(
        layout,
        explicit=model_catalog,
        environ=envs,
    )
    source = FileFingerprint.capture(catalog_path)
    cache = ModelProjectionCache(layout.model_cache)
    cached = cache.load(source)
    catalog_configs["models_dev"] = {
        **catalog_configs.get("models_dev", {}),
        "path": catalog_path,
    }
    local_env = {
        name: envs[name] for name in sorted(_LOCAL_CATALOG_ENV) if name in envs
    }
    for name in ("ollama", "llama_cpp"):
        catalog_configs[name] = {
            **catalog_configs.get(name, {}),
            "environ": local_env,
        }
    catalogs = load_model_catalogs(catalog_configs)
    ordered = tuple(
        catalogs.pop(name)
        for name in ("models_dev", "ollama", "llama_cpp")
        if name in catalogs
    ) + tuple(catalogs[name] for name in sorted(catalogs))
    if not ordered or ordered[0].name != "models_dev":
        raise RuntimeError("models_dev catalog plugin is not installed")
    static = cached.static if cached is not None else await ordered[0].snapshot()
    additional = ordered[1:]
    additional_snapshots = await asyncio.gather(
        *(catalog.snapshot() for catalog in additional)
    )
    snapshots = (static, *additional_snapshots)
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
    readiness = environment_readiness(merged, envs)
    for config in provider_configs.values():
        if config.key_env is not None:
            readiness[config.key_env] = bool(envs.get(config.key_env, "").strip())
    projection_key = model_projection_key(
        source=source,
        catalog_revisions=tuple(
            (catalog.name, snapshot.revision)
            for catalog, snapshot in zip(ordered, snapshots, strict=True)
        ),
        setup_config=config_value,
        environment_readiness=readiness,
        adapters=tuple(adapters),
    )
    infos = (
        hydrate_model_infos(cached.model_infos, resolved)
        if cached is not None
        and cached.projection_key == projection_key
        and cached.model_infos is not None
        else None
    )
    projection_cache_hit = infos is not None
    cache_entry_valid = (
        cached is not None
        and cached.projection_key == projection_key
        and (cached.model_infos is None or projection_cache_hit)
    )
    if infos is None:
        infos = tuple(model_info_from_catalog(model) for model in resolved.models)
    models = build_model_collection(
        providers=resolved.providers,
        models=infos,
        envs=envs,
        provider_configs=provider_configs,
    )
    inspection = CatalogInspection(
        snapshot=resolved,
        adapters=adapters,
        envs=envs,
        models=models,
    )
    if not cache_entry_valid:
        try:
            cache.store(
                source=source,
                static=static,
                projection_key=projection_key,
                model_infos=infos,
            )
        except Exception:
            logger.exception("catalog.model_cache_write_failed agent=%s", layout.name)
    return inspection


__all__ = ["CatalogInspection", "load_catalog_inspection"]
