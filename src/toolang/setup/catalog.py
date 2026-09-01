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
from toolang.common.query import MatchUnion, QueryDataset
from toolang.plugin.config import merge_plugin_configs
from toolang.plugin.loading import plugin_provenance
from toolang.plugin.models.cache import (
    CatalogSource,
    ModelProjectionCache,
    capture_catalog_source,
    environment_readiness,
    hydrate_model_infos,
    model_projection_key,
)
from toolang.plugin.models.catalog import (
    MergedModelCatalog,
    ModelsDevModelCatalog,
    model_info_from_catalog,
    resolve_model_catalog_path,
)
from toolang.plugin.models.collections import (
    ModelCollection,
    ModelQueryView,
    catalog_model_dataset,
)
from toolang.plugin.models.config import (
    configure_catalog_providers,
    parse_provider_configs,
)
from toolang.plugin.models.loading import load_model_adapters, load_model_catalogs
from toolang.plugin.models.provider_resolver import resolve_catalog_providers
from toolang.plugin.models.resolution import build_model_collection

from .config import (
    load_agent_config,
    load_root_setup_envs,
    load_setup_config,
    load_setup_envs,
    project_model_setup_config,
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
    catalog_models: QueryDataset[ModelQueryView]

    def __post_init__(self) -> None:
        object.__setattr__(self, "adapters", MappingProxyType(dict(self.adapters)))
        object.__setattr__(self, "envs", MappingProxyType(dict(self.envs)))


@dataclass(frozen=True, slots=True)
class _SnapshotCatalog(ModelCatalog):
    value: ModelCatalogSnapshot
    name: str

    async def snapshot(self) -> ModelCatalogSnapshot:
        return self.value


@dataclass(frozen=True, slots=True)
class _CatalogLoad:
    layout: AgentLayout
    agent_context: bool
    configs: tuple[dict[str, object], ...]
    envs: dict[str, str]
    config_value: tuple[dict[str, object], ...]
    catalog_path: Path
    ordered: tuple[ModelCatalog, ...]
    source: CatalogSource
    additional_snapshots: tuple[ModelCatalogSnapshot, ...]
    cache: ModelProjectionCache
    plugin_provenance: tuple[dict[str, str | None], ...]

    @property
    def scope(self) -> str:
        return f"agent:{self.layout.name}" if self.agent_context else "root"

    @property
    def catalog_revisions(self) -> tuple[tuple[str, str], ...]:
        return (
            (self.ordered[0].name, self.source.digest),
            *(
                (catalog.name, snapshot.revision)
                for catalog, snapshot in zip(
                    self.ordered[1:],
                    self.additional_snapshots,
                    strict=True,
                )
            ),
        )


async def load_catalog_inspection(
    layout: AgentLayout,
    *,
    model_catalog: Path | None = None,
    agent_context: bool = True,
) -> CatalogInspection:
    """Load raw catalogs once without constructing an AgentSetup."""

    load = await _prepare_catalog_load(
        layout,
        model_catalog=model_catalog,
        agent_context=agent_context,
    )
    return await _materialize_catalog_inspection(load)


async def load_matching_catalog_inspection(
    layout: AgentLayout,
    *,
    queries: MatchUnion,
    model_catalog: Path | None = None,
    agent_context: bool = True,
) -> CatalogInspection | None:
    """Load inspection unless a current cached context proves no identity match."""

    load = await _prepare_catalog_load(
        layout,
        model_catalog=model_catalog,
        agent_context=agent_context,
    )
    misses = await asyncio.to_thread(
        load.cache.catalog_identity_misses,
        kind="inspection",
        scope=load.scope,
        catalog_revisions=load.catalog_revisions,
        setup_config=load.config_value,
        environ=load.envs,
        plugin_provenance=load.plugin_provenance,
        allow_models=None,
        queries=queries,
    )
    return None if misses is True else await _materialize_catalog_inspection(load)


async def _prepare_catalog_load(
    layout: AgentLayout,
    *,
    model_catalog: Path | None,
    agent_context: bool,
) -> _CatalogLoad:
    configs, envs = _context_inputs(layout, agent_context=agent_context)
    config_value = tuple(project_model_setup_config(config) for config in configs)
    catalog_path = resolve_model_catalog_path(
        layout,
        explicit=model_catalog,
        environ=envs,
        include_agent=agent_context,
    )
    ordered = _load_ordered_catalogs(configs, envs, catalog_path=catalog_path)
    models_dev = ordered[0]
    max_source_bytes = (
        models_dev.max_bytes if isinstance(models_dev, ModelsDevModelCatalog) else None
    )
    _, source = await asyncio.to_thread(
        capture_catalog_source,
        catalog_path,
        max_source_bytes=max_source_bytes,
    )
    cache = ModelProjectionCache(
        layout.root_model_cache,
        layout.home_model_cache if agent_context else layout.root_model_cache,
    )
    additional_snapshots = tuple(
        await asyncio.gather(*(catalog.snapshot() for catalog in ordered[1:]))
    )
    return _CatalogLoad(
        layout=layout,
        agent_context=agent_context,
        configs=configs,
        envs=envs,
        config_value=config_value,
        catalog_path=catalog_path,
        ordered=ordered,
        source=source,
        additional_snapshots=additional_snapshots,
        cache=cache,
        plugin_provenance=_model_plugin_provenance(),
    )


async def _materialize_catalog_inspection(
    load: _CatalogLoad,
) -> CatalogInspection:
    """Hydrate a prepared catalog load into the complete inspection view."""

    layout = load.layout
    configs = load.configs
    envs = load.envs
    config_value = load.config_value
    catalog_path = load.catalog_path
    ordered = load.ordered
    source = load.source
    cache = load.cache
    adapters = load_model_adapters(
        merge_plugin_configs(configs, family="model_adapter")
    )
    models_dev = ordered[0]
    static = await asyncio.to_thread(
        cache.load_catalog,
        source,
        source_path=catalog_path,
    )
    if static is None:
        static = await models_dev.snapshot()
        if static.revision != source.digest:
            raise ValueError("models_dev revision does not match its source")
        try:
            await asyncio.to_thread(
                cache.store_catalog,
                source=source,
                snapshot=static,
            )
        except Exception:
            logger.exception("catalog.static_cache_write_failed")
    snapshots = (static, *load.additional_snapshots)
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
    context_key = model_projection_key(
        kind="inspection",
        scope=load.scope,
        catalog_revisions=load.catalog_revisions,
        setup_config=config_value,
        environment_readiness=readiness,
        plugin_provenance=load.plugin_provenance,
        allow_models=None,
    )
    cached = await asyncio.to_thread(cache.load_context, context_key)
    infos = (
        hydrate_model_infos(cached.model_infos, resolved)
        if cached is not None
        else None
    )
    if cached is not None and infos is None:
        cached = None
    if infos is None:
        infos = tuple(model_info_from_catalog(model) for model in resolved.models)
    models = build_model_collection(
        providers=resolved.providers,
        models=infos,
        envs=envs,
        provider_configs=provider_configs,
        query_views=cached.query_views if cached is not None else None,
    )
    available = set(models.refs())
    adapter_by_identity = {
        model.identity: model.resolved.adapter
        for model in resolved.models
        if model.resolved is not None and model.resolved.adapter is not None
    }
    cached_catalog_queries = (
        cached.catalog_query_views
        if cached is not None and cached.catalog_query_views
        else None
    )
    catalog_models = catalog_model_dataset(
        resolved,
        available=available,
        adapters=adapter_by_identity,
        query_views=cached_catalog_queries,
    )
    inspection = CatalogInspection(
        snapshot=resolved,
        adapters=adapters,
        envs=envs,
        models=models,
        catalog_models=catalog_models,
    )
    if cached is None or cached_catalog_queries is None:
        try:
            await asyncio.to_thread(
                cache.store_context,
                key=context_key,
                model_infos=tuple(entry.info for entry in models.entries),
                query_views=models.query_views(),
                catalog_query_views=tuple(catalog_models.items),
                environment_names=tuple(readiness),
            )
        except Exception:
            logger.exception("catalog.model_cache_write_failed agent=%s", layout.name)
    return inspection


def _context_inputs(
    layout: AgentLayout,
    *,
    agent_context: bool,
) -> tuple[tuple[dict[str, object], ...], dict[str, str]]:
    configs = (
        (load_setup_config(layout), load_agent_config(layout))
        if agent_context
        else (load_setup_config(layout),)
    )
    envs = load_setup_envs(layout) if agent_context else load_root_setup_envs(layout)
    return configs, envs


def _load_ordered_catalogs(
    configs: tuple[dict[str, object], ...],
    envs: Mapping[str, str],
    *,
    catalog_path: Path,
) -> tuple[ModelCatalog, ...]:
    catalog_configs = merge_plugin_configs(configs, family="model_catalog")
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
    return ordered


def _model_plugin_provenance() -> tuple[dict[str, str | None], ...]:
    return tuple(
        item.to_data()
        for group in ("toolang.model_catalog", "toolang.model_adapter")
        for item in plugin_provenance(group=group)
    )


__all__ = [
    "CatalogInspection",
    "load_catalog_inspection",
    "load_matching_catalog_inspection",
]
