"""Keep the current installed runtime setup synchronized."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, replace
from decimal import Decimal
import logging
from pathlib import Path

from toolang.base.protocols.model import ModelAdapter, ModelCatalog
from toolang.base.protocols.tool import AgentTool
from toolang.base.types.model import ModelCatalogSnapshot, ModelInfo
from toolang.base.types.policy import AgentCeiling, RunDefaults, RunLimits
from toolang.common.layout import AgentLayout
from toolang.plugin.config import merge_plugin_configs
from toolang.plugin.loading import plugin_provenance
from toolang.plugin.models.catalog import (
    MergedModelCatalog,
    ModelsDevModelCatalog,
    model_info_from_catalog,
    resolve_model_catalog_path,
)
from toolang.plugin.models.cache import (
    CachedModelProjection,
    CatalogSource,
    FileObservation,
    ModelProjectionCache,
    capture_catalog_source,
    environment_readiness,
    hydrate_model_infos,
    model_projection_key,
)
from toolang.plugin.models.config import (
    ProviderConfig,
    configure_catalog_providers,
    parse_provider_configs,
)
from toolang.plugin.models.collections import ModelQueryView
from toolang.plugin.models.loading import load_model_adapters, load_model_catalogs
from toolang.plugin.models.provider_resolver import resolve_catalog_providers
from toolang.plugin.models.resolution import build_model_collection
from toolang.plugin.toolsets.collections import ToolCollection
from toolang.plugin.toolsets.loading import load_tools

from .config import (
    load_agent_config,
    load_setup_config,
    load_setup_envs,
    project_model_setup_config,
    project_setup_config,
    resolve_run_defaults,
    resolve_run_limits,
    resolve_setup_allow,
)
from .errors import SetupDiagnostic
from .types import AgentEnvironment, AgentSetup

DEFAULT_INTERVAL_MS = 5_000.0
logger = logging.getLogger(__name__)
_LOCAL_CATALOG_ENV = frozenset(
    {
        "LLAMA_CPP_HOST",
        "OLLAMA_HOST",
        "TOOLANG_HOST_GATEWAY",
    }
)


@dataclass(frozen=True, slots=True)
class _SnapshotModelCatalog(ModelCatalog):
    value: ModelCatalogSnapshot
    name: str = "models_dev"

    async def snapshot(self) -> ModelCatalogSnapshot:
        return self.value


@dataclass(frozen=True, slots=True)
class _LoadedInputs:
    fingerprints: tuple[object, ...]
    root_config: dict[str, object]
    agent_config: dict[str, object]
    envs: dict[str, str]


@dataclass(frozen=True, slots=True)
class _Candidate:
    inputs: _LoadedInputs
    config_value: tuple[dict[str, object], dict[str, object]]
    adapter_configs: dict[str, dict[str, object]]
    toolset_configs: dict[str, dict[str, object]]
    catalog_configs: dict[str, dict[str, object]]
    adapters: dict[str, ModelAdapter]
    tools: dict[str, AgentTool]
    catalogs: dict[str, ModelCatalog]
    observation: FileObservation
    source: CatalogSource
    static: ModelCatalogSnapshot
    additional: tuple[tuple[str, ModelCatalogSnapshot], ...]


@dataclass(frozen=True, slots=True)
class _PendingModelCache:
    projection_key: str
    model_infos: tuple[ModelInfo, ...]
    query_views: tuple[ModelQueryView, ...]
    environment_names: tuple[str, ...]


class SetupWatcher:
    """Publish immutable setup snapshots when inputs or model probes change."""

    def __init__(
        self,
        layout: AgentLayout,
        *,
        sandbox: str = "host",
        model_catalog: Path | None = None,
        allow_overrides: Mapping[str, tuple[str, ...] | None] | None = None,
        default_overrides: Mapping[str, str | None] | None = None,
        limit_overrides: Mapping[str, int | Decimal | None] | None = None,
    ) -> None:
        self.layout = layout
        self._sandbox = sandbox
        self._model_catalog_override = model_catalog
        self._allow_overrides = dict(allow_overrides or {})
        self._default_overrides = dict(default_overrides or {})
        self._limit_overrides = dict(limit_overrides or {})
        self._inputs: _LoadedInputs | None = None
        self._config: tuple[dict[str, object], dict[str, object]] | None = None
        self._adapter_configs: dict[str, dict[str, object]] | None = None
        self._toolset_configs: dict[str, dict[str, object]] | None = None
        self._catalog_configs: dict[str, dict[str, object]] | None = None
        self._adapters: dict[str, ModelAdapter] = {}
        self._tools: dict[str, AgentTool] = {}
        self._catalogs: dict[str, ModelCatalog] = {}
        self._static_catalog: ModelCatalogSnapshot | None = None
        self._additional_catalogs: (
            tuple[tuple[str, ModelCatalogSnapshot], ...] | None
        ) = None
        self._catalog_identity: FileObservation | None = None
        self._catalog_source: CatalogSource | None = None
        self._cache_entry: CachedModelProjection | None = None
        self._pending_catalog_cache: (
            tuple[CatalogSource, ModelCatalogSnapshot] | None
        ) = None
        self._pending_model_cache: _PendingModelCache | None = None
        self._model_cache = ModelProjectionCache(
            layout.root_model_cache,
            layout.home_model_cache,
        )
        self._model_plugin_provenance = tuple(
            item.to_data()
            for group in ("toolang.model_catalog", "toolang.model_adapter")
            for item in plugin_provenance(group=group)
        )
        self._setup: AgentSetup | None = None
        self._diagnostics: tuple[SetupDiagnostic, ...] = ()
        self._refresh_lock = asyncio.Lock()

    def current(self) -> AgentSetup:
        """Return the latest immutable setup snapshot."""

        if self._setup is None:
            raise RuntimeError("setup watcher has not been refreshed")
        return self._setup

    def diagnostics(self) -> tuple[SetupDiagnostic, ...]:
        """Return diagnostics for the latest rejected candidate, if any."""

        return self._diagnostics

    async def refresh(self) -> AgentSetup:
        """Run one serialized candidate check and return the last valid Setup."""

        async with self._refresh_lock:
            try:
                return await self._perform_refresh()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self._setup is None:
                    raise
                self._diagnostics = (_candidate_diagnostic(exc),)
                logger.warning(
                    "setup.refresh_rejected agent=%s error=%s",
                    self.layout.name,
                    type(exc).__name__,
                )
                return self._setup

    async def _perform_refresh(self) -> AgentSetup:
        inputs = self._load_inputs()
        configs = (inputs.root_config, inputs.agent_config)
        config_value = (
            project_setup_config(inputs.root_config),
            project_setup_config(inputs.agent_config),
        )
        allow = resolve_setup_allow(configs, overrides=self._allow_overrides)
        defaults = resolve_run_defaults(configs, overrides=self._default_overrides)
        limits = resolve_run_limits(configs, overrides=self._limit_overrides)
        provider_configs = parse_provider_configs(configs)
        adapter_configs = merge_plugin_configs(configs, family="model_adapter")
        toolset_configs = merge_plugin_configs(configs, family="toolset")
        catalog_path = resolve_model_catalog_path(
            self.layout,
            explicit=self._model_catalog_override,
            environ=inputs.envs,
        )
        catalog_configs = self._runtime_catalog_configs(
            configs,
            inputs.envs,
            catalog_path=catalog_path,
        )
        adapters = (
            self._adapters
            if self._adapter_configs == adapter_configs
            else load_model_adapters(adapter_configs)
        )
        tools = (
            self._tools
            if self._toolset_configs == toolset_configs
            else load_tools(toolset_config=toolset_configs)
        )
        catalogs = (
            self._catalogs
            if self._catalog_configs == catalog_configs
            else load_model_catalogs(catalog_configs)
        )
        models_dev = catalogs.get("models_dev")
        if models_dev is None:
            raise RuntimeError("models_dev catalog plugin is not installed")
        max_source_bytes = (
            models_dev.max_bytes
            if isinstance(models_dev, ModelsDevModelCatalog)
            else None
        )
        observation = FileObservation.capture(catalog_path)
        if max_source_bytes is not None and observation.size > max_source_bytes:
            raise ValueError(
                f"model catalog exceeds {max_source_bytes} bytes: {observation.path}"
            )
        if (
            observation == self._catalog_identity
            and self._catalog_source is not None
            and self._static_catalog is not None
        ):
            source = self._catalog_source
            static = self._static_catalog
        else:
            observation, source = await asyncio.to_thread(
                capture_catalog_source,
                catalog_path,
                max_source_bytes=max_source_bytes,
            )
            static = await asyncio.to_thread(
                self._model_cache.load_catalog,
                source,
                source_path=catalog_path,
            )
            if static is None:
                static = await models_dev.snapshot()
                await self._store_catalog_cache(source, static)
        ordered_catalogs = _ordered_additional_catalogs(catalogs)
        additional_snapshots = tuple(
            await asyncio.gather(*(catalog.snapshot() for catalog in ordered_catalogs))
        )
        additional = tuple(
            (catalog.name, snapshot)
            for catalog, snapshot in zip(
                ordered_catalogs,
                additional_snapshots,
                strict=True,
            )
        )
        candidate = _Candidate(
            inputs=inputs,
            config_value=config_value,
            adapter_configs=adapter_configs,
            toolset_configs=toolset_configs,
            catalog_configs=catalog_configs,
            adapters=adapters,
            tools=tools,
            catalogs=catalogs,
            observation=observation,
            source=source,
            static=static,
            additional=additional,
        )
        if self._candidate_is_unchanged(candidate):
            await self._persist_pending_model_cache()
            self._commit_candidate(candidate)
            self._diagnostics = ()
            return self.current()
        merged = await _merge_catalogs(static, additional)
        resolved_catalog = _resolve_catalog(
            merged,
            adapters=adapters,
            envs=inputs.envs,
            provider_configs=provider_configs,
        )
        projection_key = _projection_key(
            additional=additional,
            config_value=tuple(
                project_model_setup_config(config) for config in configs
            ),
            merged=merged,
            envs=inputs.envs,
            provider_configs=provider_configs,
            allow_models=allow.models,
            plugin_provenance=self._model_plugin_provenance,
            scope=f"agent:{self.layout.name}",
        )
        cache_entry = (
            self._cache_entry
            if self._cache_entry is not None and self._cache_entry.key == projection_key
            else await asyncio.to_thread(
                self._model_cache.load_context,
                projection_key,
            )
        )
        model_infos = (
            hydrate_model_infos(cache_entry.model_infos, resolved_catalog)
            if cache_entry is not None
            else None
        )
        if cache_entry is not None and model_infos is None:
            cache_entry = None
        if model_infos is None:
            model_infos = tuple(
                model_info_from_catalog(model) for model in resolved_catalog.models
            )
        setup = _build_setup(
            layout=self.layout,
            sandbox=self._sandbox,
            resolved_catalog=resolved_catalog,
            model_infos=model_infos,
            adapters=adapters,
            tools=tools,
            envs=inputs.envs,
            provider_configs=provider_configs,
            allow=allow,
            defaults=defaults,
            limits=limits,
            query_views=(cache_entry.query_views if cache_entry is not None else None),
            apply_model_allow=cache_entry is None,
        )
        if self._setup is not None and _setups_equal(setup, self._setup):
            setup = self._setup
        else:
            self._setup = setup
        self._commit_candidate(candidate)
        self._diagnostics = ()
        if cache_entry is None:
            model_infos = tuple(entry.info for entry in setup.models.entries)
            query_views = setup.models.query_views()
            self._cache_entry = CachedModelProjection(
                key=projection_key,
                model_infos=model_infos,
                query_views=query_views,
            )
            self._pending_model_cache = _PendingModelCache(
                projection_key=projection_key,
                model_infos=model_infos,
                query_views=query_views,
                environment_names=tuple(
                    sorted(
                        _projection_environment_readiness(
                            merged,
                            inputs.envs,
                            provider_configs,
                        )
                    )
                ),
            )
            await self._persist_pending_model_cache()
        else:
            self._cache_entry = cache_entry
        return setup

    def _load_inputs(self) -> _LoadedInputs:
        fingerprints = tuple(
            _input_file_fingerprint(path)
            for path in (
                self.layout.root_config,
                self.layout.config,
                self.layout.root_env,
                self.layout.env,
            )
        )
        if self._inputs is not None and self._inputs.fingerprints == fingerprints:
            return self._inputs
        previous = self._inputs
        return _LoadedInputs(
            fingerprints=fingerprints,
            root_config=(
                previous.root_config
                if previous is not None and previous.fingerprints[0] == fingerprints[0]
                else load_setup_config(self.layout)
            ),
            agent_config=(
                previous.agent_config
                if previous is not None and previous.fingerprints[1] == fingerprints[1]
                else load_agent_config(self.layout)
            ),
            envs=(
                previous.envs
                if previous is not None
                and previous.fingerprints[2:] == fingerprints[2:]
                else load_setup_envs(self.layout)
            ),
        )

    def _runtime_catalog_configs(
        self,
        configs: tuple[dict[str, object], dict[str, object]],
        envs: Mapping[str, str],
        *,
        catalog_path: Path,
    ) -> dict[str, dict[str, object]]:
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
        return catalog_configs

    def _candidate_is_unchanged(self, candidate: _Candidate) -> bool:
        return (
            self._setup is not None
            and candidate.config_value == self._config
            and candidate.inputs.envs == self._setup.envs
            and candidate.observation == self._catalog_identity
            and candidate.adapter_configs == self._adapter_configs
            and candidate.toolset_configs == self._toolset_configs
            and candidate.catalog_configs == self._catalog_configs
            and candidate.additional == self._additional_catalogs
        )

    def _commit_candidate(self, candidate: _Candidate) -> None:
        self._inputs = candidate.inputs
        self._config = candidate.config_value
        self._adapter_configs = candidate.adapter_configs
        self._toolset_configs = candidate.toolset_configs
        self._catalog_configs = candidate.catalog_configs
        self._adapters = candidate.adapters
        self._tools = candidate.tools
        self._catalogs = candidate.catalogs
        self._catalog_identity = candidate.observation
        self._catalog_source = candidate.source
        self._static_catalog = candidate.static
        self._additional_catalogs = candidate.additional

    async def _store_catalog_cache(
        self,
        source: CatalogSource,
        static: ModelCatalogSnapshot,
    ) -> None:
        self._pending_catalog_cache = (source, static)
        await self._persist_pending_model_cache()

    async def _persist_pending_model_cache(self) -> None:
        pending_catalog = self._pending_catalog_cache
        if pending_catalog is not None:
            source, static = pending_catalog
            self._pending_catalog_cache = None
            try:
                await asyncio.to_thread(
                    self._model_cache.store_catalog,
                    source=source,
                    snapshot=static,
                )
            except asyncio.CancelledError:
                self._pending_catalog_cache = pending_catalog
                raise
            except Exception:
                self._pending_catalog_cache = pending_catalog
                logger.exception(
                    "setup.catalog_cache_write_failed agent=%s", self.layout.name
                )
        pending = self._pending_model_cache
        if pending is None:
            return
        self._pending_model_cache = None
        try:
            await asyncio.to_thread(
                self._model_cache.store_context,
                key=pending.projection_key,
                model_infos=pending.model_infos,
                query_views=pending.query_views,
                environment_names=pending.environment_names,
            )
        except asyncio.CancelledError:
            self._pending_model_cache = pending
            raise
        except Exception:
            self._pending_model_cache = pending
            logger.exception(
                "setup.model_cache_write_failed agent=%s", self.layout.name
            )

    async def updates(
        self,
        *,
        stop_signal: asyncio.Event,
        interval_ms: float = DEFAULT_INTERVAL_MS,
    ) -> AsyncIterator[AgentSetup]:
        """Yield each newly published setup until the caller stops watching."""

        if self._setup is None:
            await self.refresh()
        interval_sec = max(interval_ms, 50.0) / 1_000
        while not stop_signal.is_set():
            try:
                await asyncio.wait_for(stop_signal.wait(), timeout=interval_sec)
            except TimeoutError:
                pass
            if stop_signal.is_set():
                break
            previous = self.current()
            current = await self.refresh()
            if current is not previous:
                yield current

    async def run(
        self,
        *,
        stop_signal: asyncio.Event,
        interval_ms: float = DEFAULT_INTERVAL_MS,
    ) -> None:
        """Keep the current setup synchronized until stopped."""

        async for _ in self.updates(
            stop_signal=stop_signal,
            interval_ms=interval_ms,
        ):
            pass


async def _merge_catalogs(
    static: ModelCatalogSnapshot,
    additional: tuple[tuple[str, ModelCatalogSnapshot], ...],
) -> ModelCatalogSnapshot:
    return await MergedModelCatalog(
        (
            _SnapshotModelCatalog(static),
            *(
                _SnapshotModelCatalog(snapshot, name=name)
                for name, snapshot in additional
            ),
        )
    ).snapshot()


def _resolve_catalog(
    merged: ModelCatalogSnapshot,
    *,
    adapters: Mapping[str, ModelAdapter],
    envs: Mapping[str, str],
    provider_configs: Mapping[str, ProviderConfig],
) -> ModelCatalogSnapshot:
    providers = configure_catalog_providers(merged.providers, provider_configs)
    return resolve_catalog_providers(
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


def _build_setup(
    *,
    layout: AgentLayout,
    sandbox: str,
    resolved_catalog: ModelCatalogSnapshot,
    model_infos: tuple[ModelInfo, ...],
    adapters: dict[str, ModelAdapter],
    tools: dict[str, AgentTool],
    envs: dict[str, str],
    provider_configs: Mapping[str, ProviderConfig],
    allow: AgentCeiling,
    defaults: RunDefaults,
    limits: RunLimits,
    query_views: tuple[ModelQueryView, ...] | None = None,
    apply_model_allow: bool = True,
) -> AgentSetup:
    models = build_model_collection(
        providers=resolved_catalog.providers,
        models=model_infos,
        envs=envs,
        provider_configs=provider_configs,
        query_views=query_views,
    )
    if apply_model_allow and allow.models is not None:
        models = models.match(allow.models).compact()
    tool_collection = ToolCollection.from_tools(tools)
    if allow.tools is not None:
        tool_collection = tool_collection.match(allow.tools).compact()
    if defaults.model is not None:
        models.resolve(defaults.model)
    all_providers = dict(resolved_catalog.providers)
    provider_models: dict[str, set[str]] = {}
    for entry in models.entries:
        provider_models.setdefault(entry.target.provider, set()).add(entry.info.model)
    providers = {
        provider_id: replace(
            all_providers[provider_id],
            models={
                model_id: model
                for model_id, model in all_providers[provider_id].models.items()
                if model_id in model_ids
            },
        )
        for provider_id, model_ids in provider_models.items()
    }
    return AgentSetup(
        layout=layout,
        providers=providers,
        adapters=adapters,
        models=models,
        tools=tool_collection,
        envs=envs,
        environment=AgentEnvironment.capture(layout, sandbox=sandbox),
        defaults=defaults,
        limits=limits,
    )


def _projection_key(
    *,
    additional: tuple[tuple[str, ModelCatalogSnapshot], ...],
    config_value: object,
    merged: ModelCatalogSnapshot,
    envs: Mapping[str, str],
    provider_configs: Mapping[str, ProviderConfig],
    allow_models: tuple[str, ...] | None,
    plugin_provenance: tuple[object, ...],
    scope: str,
) -> str:
    readiness = _projection_environment_readiness(merged, envs, provider_configs)
    return model_projection_key(
        kind="runtime",
        scope=scope,
        catalog_revisions=(
            ("models_dev", merged.revision),
            *((name, snapshot.revision) for name, snapshot in additional),
        ),
        setup_config=config_value,
        environment_readiness=readiness,
        plugin_provenance=plugin_provenance,
        allow_models=allow_models,
    )


def _projection_environment_readiness(
    merged: ModelCatalogSnapshot,
    envs: Mapping[str, str],
    provider_configs: Mapping[str, ProviderConfig],
) -> dict[str, bool]:
    readiness = environment_readiness(merged, envs)
    for config in provider_configs.values():
        if config.key_env is not None:
            readiness[config.key_env] = bool(envs.get(config.key_env, "").strip())
    return readiness


def _ordered_additional_catalogs(
    catalogs: Mapping[str, ModelCatalog],
) -> tuple[ModelCatalog, ...]:
    additional = {
        name: catalog for name, catalog in catalogs.items() if name != "models_dev"
    }
    return tuple(
        additional.pop(name) for name in ("ollama", "llama_cpp") if name in additional
    ) + tuple(additional[name] for name in sorted(additional))


def _input_file_fingerprint(path: Path) -> object:
    try:
        resolved = path.resolve(strict=True)
        stat = resolved.stat()
    except FileNotFoundError:
        return (path.resolve(strict=False), None)
    return (
        resolved,
        stat.st_dev,
        stat.st_ino,
        stat.st_mtime_ns,
        stat.st_size,
    )


def _setups_equal(left: AgentSetup, right: AgentSetup) -> bool:
    return (
        left.layout == right.layout
        and left.providers == right.providers
        and left.adapters == right.adapters
        and left.models.entries == right.models.entries
        and left.tools.entries == right.tools.entries
        and left.envs == right.envs
        and left.environment == right.environment
        and left.defaults == right.defaults
        and left.limits == right.limits
    )


def _candidate_diagnostic(exc: Exception) -> SetupDiagnostic:
    name = type(exc).__name__
    code = "".join(
        ("-" + character.lower()) if character.isupper() else character
        for character in name
    ).lstrip("-")
    return SetupDiagnostic(code=code, message=str(exc) or name)
