"""One-shot raw model catalog inspection loading."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, replace
import logging
from pathlib import Path
from types import MappingProxyType

from toolang.base.protocols.model import ModelAdapter, ModelCatalog
from toolang.base.types.model import Model, ModelCatalogSnapshot, Provider
from toolang.common.layout import AgentLayout
from toolang.common.query import MatchUnion, QueryDataset
from toolang.plugin.config import merge_plugin_configs
from toolang.plugin.loading import plugin_provenance
from toolang.plugin.adapters.loading import load_model_adapters
from toolang.plugin.catalogs.loading import load_model_catalogs
from toolang.plugin.catalogs.models_dev.catalog import (
    ModelCatalogSource,
    ModelsDevModelCatalog,
)
from toolang.plugin.catalogs.models_dev.path import resolve_model_catalog_path
from toolang.plugin.models.collections import (
    ModelCollection,
    ModelQueryView,
    catalog_model_dataset,
)
from toolang.plugin.models.config import validate_models_config
from toolang.plugin.models.provider_resolver import (
    model_adapter,
    resolve_catalog_providers,
)
from toolang.plugin.models.resolution import build_model_collection

from .cache import (
    ModelProjectionCache,
    environment_readiness,
    model_projection_key,
)
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
    source: ModelCatalogSource
    additional_snapshots: tuple[ModelCatalogSnapshot, ...]
    context_cache: ModelProjectionCache
    plugin_provenance: tuple[dict[str, str | None], ...]

    @property
    def scope(self) -> str:
        return f"agent:{self.layout.name}" if self.agent_context else "root"

    @property
    def catalog_revisions(self) -> tuple[tuple[str, str], ...]:
        return (
            (self.ordered[0].name, self.source.revision),
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
        load.context_cache.catalog_identity_misses,
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
    if not isinstance(models_dev, ModelsDevModelCatalog):
        raise RuntimeError("models_dev catalog plugin is not installed")
    _, source = await asyncio.to_thread(models_dev.capture)
    context_cache = ModelProjectionCache(
        layout.home_model_cache if agent_context else layout.root_model_cache
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
        context_cache=context_cache,
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
    ordered = load.ordered
    context_cache = load.context_cache
    adapters = load_model_adapters(
        merge_plugin_configs(configs, family="model_adapter")
    )
    static = await asyncio.to_thread(load.source.snapshot)
    snapshots = (static, *load.additional_snapshots)
    merged = await MergedModelCatalog(
        tuple(
            _SnapshotCatalog(snapshot, catalog.name)
            for catalog, snapshot in zip(ordered, snapshots, strict=True)
        )
    ).snapshot()
    validate_models_config(configs)
    resolved = resolve_catalog_providers(
        merged,
        adapters=adapters,
        environ=envs,
    )
    readiness = environment_readiness(resolved, envs)
    context_key = model_projection_key(
        kind="inspection",
        scope=load.scope,
        catalog_revisions=load.catalog_revisions,
        setup_config=config_value,
        environment_readiness=readiness,
        plugin_provenance=load.plugin_provenance,
        allow_models=None,
    )
    cached = await asyncio.to_thread(context_cache.load_context, context_key)
    snapshot = cached.snapshot if cached is not None else resolved
    models = build_model_collection(snapshot.models)
    available = {model.ref for model in snapshot.models if model._toolang.ready}
    adapter_by_identity = {
        model.identity: adapter
        for model in snapshot.models
        if model._toolang.provider in snapshot.providers
        for adapter in (
            model_adapter(snapshot.providers[model._toolang.provider], model),
        )
        if adapter is not None
    }
    catalog_models = catalog_model_dataset(
        snapshot,
        available=available,
        adapters=adapter_by_identity,
    )
    inspection = CatalogInspection(
        snapshot=snapshot,
        adapters=adapters,
        envs=envs,
        models=models,
        catalog_models=catalog_models,
    )
    if cached is None:
        try:
            await asyncio.to_thread(
                context_cache.store_context,
                context_key,
                snapshot=resolved,
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
            snapshot = _with_catalog_origin(raw_snapshot)
            for provider_id, provider in snapshot.providers.items():
                existing = providers.get(provider_id)
                if existing is not None and not (
                    existing._toolang.local and provider._toolang.local
                ):
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


def _with_catalog_origin(
    snapshot: ModelCatalogSnapshot,
) -> ModelCatalogSnapshot:
    """Attach the declaring catalog's locality to every provider record."""

    providers: dict[str, Provider] = {}
    for provider_id, provider in snapshot.providers.items():
        providers[provider_id] = (
            provider
            if provider._toolang.local == snapshot.local
            else replace(
                provider,
                _toolang=replace(provider._toolang, local=snapshot.local),
            )
        )
    return ModelCatalogSnapshot(
        providers=providers,
        models=snapshot.models,
        revision=snapshot.revision,
        source=snapshot.source,
        local=snapshot.local,
    )


__all__ = [
    "CatalogInspection",
    "load_catalog_inspection",
    "load_matching_catalog_inspection",
]
