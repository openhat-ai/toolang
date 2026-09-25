"""Keep the current installed runtime setup synchronized."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
import logging
from pathlib import Path

from toolang.base.protocols.model import ModelAdapter, ModelCatalog
from toolang.base.protocols.tool import Tool
from toolang.base.types.model import ModelCatalogSnapshot, ModelOverride
from toolang.base.types.policy import AgentCeiling, RunDefaults, RunLimits
from toolang.common.layout import AgentLayout
from toolang.common.cache import digest
from toolang.plugin.config import merge_plugin_configs
from toolang.plugin.loading import (
    load_model_adapters,
    load_model_catalogs,
    plugin_provenance,
)
from toolang.plugin.catalogs.models_dev.catalog import (
    FileObservation,
    ModelCatalogSource,
    ModelsDevModelCatalog,
)
from toolang.plugin.catalogs.models_dev.path import resolve_model_catalog_path
from toolang.plugin.models.config import validate_models_config
from toolang.plugin.models.collections import ModelCollection
from toolang.setup.routes import (
    resolve_catalog_providers,
    catalog_environment_names,
)
from toolang.plugin.models.resolution import resolve_model_reasoning
from toolang.plugin.toolsets.collections import ToolCollection
from toolang.plugin.toolsets.loading import load_tools

from .cache import (
    ModelCatalogCache,
    environment_identity,
    model_projection_key,
)
from .catalog import MergedModelCatalog, assemble_catalog
from .config import (
    capture_setup_config,
    load_root_setup_envs,
    load_setup_envs,
    project_model_setup_config,
    project_setup_config,
    resolve_compact_model,
    resolve_run_defaults,
    resolve_run_limits,
    resolve_setup_allow,
)
from .errors import SetupDiagnostic
from .models import select_compact_model
from .model_listing import ModelListing, build_model_listing
from .records import ModelListingCache
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
    tools: dict[str, Tool]
    catalogs: dict[str, ModelCatalog]
    observation: FileObservation
    source_revisions: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class _CatalogLoad:
    """Every catalog source of one refresh, with each source's own revision."""

    observation: FileObservation
    source: ModelCatalogSource
    additional: tuple[tuple[str, str, ModelCatalogSnapshot], ...]


class SetupWatcher:
    """Publish immutable setup snapshots when inputs or model probes change."""

    def __init__(
        self,
        layout: AgentLayout,
        *,
        sandbox: str = "host",
        model_catalog: Path | None = None,
        allow_overrides: Mapping[str, tuple[str, ...] | None] | None = None,
        default_overrides: Mapping[str, ModelOverride | str | None] | None = None,
        limit_overrides: Mapping[str, int | float | None] | None = None,
        compact_override: ModelOverride | None = None,
        agent_context: bool = True,
        validate_defaults: bool = True,
    ) -> None:
        self.layout = layout
        self._sandbox = sandbox
        self._agent_context = agent_context
        self._validate_defaults = validate_defaults
        self._model_catalog_override = model_catalog
        self._allow_overrides = dict(allow_overrides or {})
        self._default_overrides = dict(default_overrides or {})
        self._limit_overrides = dict(limit_overrides or {})
        self._compact_override = compact_override
        self._config: tuple[dict[str, object], dict[str, object]] | None = None
        self._adapter_configs: dict[str, dict[str, object]] | None = None
        self._toolset_configs: dict[str, dict[str, object]] | None = None
        self._catalog_configs: dict[str, dict[str, object]] | None = None
        self._adapters: dict[str, ModelAdapter] = {}
        self._tools: dict[str, Tool] = {}
        self._catalogs: dict[str, ModelCatalog] = {}
        self._catalog_identity: FileObservation | None = None
        self._source_revisions: tuple[tuple[str, str], ...] | None = None
        cache_directory = (
            layout.home_model_cache if agent_context else layout.root_model_cache
        )
        self._model_cache = ModelCatalogCache(cache_directory / "sources")
        self.model_listing_cache = ModelListingCache(cache_directory)
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

    def _publish(self, setup: AgentSetup) -> None:
        """Publish one setup version.

        Only the current version is retained. A run keeps its own reference to the
        version it started with, so an older version stays alive exactly as long
        as something still uses it.
        """

        self._setup = setup

    async def _load_catalog_records(
        self,
        loaded: _LoadedInputs,
        load: _CatalogLoad,
        *,
        adapters: Mapping[str, ModelAdapter],
        adapter_configs: Mapping[str, object],
        catalog_configs: Mapping[str, Mapping[str, object]],
        allow_models: tuple[str, ...] | None,
    ) -> ModelListing:
        """Reuse the catalog projection owned by this runtime setup candidate."""

        inputs: dict[str, object] = {
            "sources": [
                ["models_dev", load.source.content_revision],
                *[
                    [name, self._model_cache.content_revision(snapshot)]
                    for name, _revision, snapshot in load.additional
                ],
            ],
            "config_files": list(loaded.fingerprints),
            "adapter_config": digest(adapter_configs),
            "loaded_adapters": _adapter_identity(adapters),
            "catalog_config": digest(
                {
                    name: {
                        key: value
                        for key, value in config.items()
                        if key != "environ"
                        # The captured bytes identify the static source across mounts.
                        and not (name == "models_dev" and key == "path")
                    }
                    for name, config in catalog_configs.items()
                }
            ),
            "allow_models": None if allow_models is None else list(allow_models),
            "plugins": list(self._model_plugin_provenance),
        }
        records = self.model_listing_cache.load(inputs=inputs, environ=loaded.envs)
        if records is not None:
            return ModelListing(records)

        static = await asyncio.to_thread(load.source.snapshot)
        merged = await _merge_catalogs(
            static, tuple((name, snapshot) for name, _, snapshot in load.additional)
        )
        names = catalog_environment_names(merged, adapters=adapters)
        resolved = _resolve_catalog(merged, adapters=adapters, envs=loaded.envs)
        listing = build_model_listing(resolved, allow_models=allow_models)
        try:
            self.model_listing_cache.store(
                listing.records,
                inputs=inputs,
                environment_names=names,
                environ=loaded.envs,
            )
        except Exception:
            logger.warning(
                "setup.model_listing_cache_write_failed agent=%s", self.layout.name
            )
        return listing

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
        compact_model = resolve_compact_model(configs, override=self._compact_override)
        limits = resolve_run_limits(configs, overrides=self._limit_overrides)
        validate_models_config(configs)
        adapter_configs = merge_plugin_configs(configs, family="model_adapter")
        toolset_configs = merge_plugin_configs(configs, family="toolset")
        catalog_path = resolve_model_catalog_path(
            self.layout,
            explicit=self._model_catalog_override,
            environ=inputs.envs,
            include_agent=self._agent_context,
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
        if not isinstance(models_dev, ModelsDevModelCatalog):
            raise RuntimeError("models_dev catalog plugin is not installed")
        load = await self._load_sources(
            models_dev,
            catalogs,
        )
        source_revisions = (
            ("models_dev", load.source.revision),
            *((name, revision) for name, revision, _ in load.additional),
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
            observation=load.observation,
            source_revisions=source_revisions,
        )
        if self._candidate_is_unchanged(candidate):
            self._commit_candidate(candidate)
            self._diagnostics = ()
            return self.current()
        projection_key = _projection_key(
            source_revisions=source_revisions,
            environment=environment_identity(inputs.envs),
            setup_inputs={
                "catalogs": catalog_configs,
                "config": tuple(
                    project_model_setup_config(config) for config in configs
                ),
                "adapters": adapter_configs,
                "loaded_adapters": _adapter_identity(adapters),
                "tools": toolset_configs,
                "allow": allow,
                "defaults": defaults,
                "limits": limits,
                "compact_model": compact_model,
            },
            allow_models=allow.models,
            plugin_provenance=self._model_plugin_provenance,
            scope=f"agent:{self.layout.name}" if self._agent_context else "root",
        )
        if self._setup is not None and self._setup.revision == projection_key:
            self._commit_candidate(candidate)
            self._diagnostics = ()
            return self._setup
        listing = await self._load_catalog_records(
            inputs,
            load,
            adapters=adapters,
            adapter_configs=adapter_configs,
            catalog_configs=catalog_configs,
            allow_models=allow.models,
        )
        catalog_sources = {
            provider.id: ("models_dev", load.source.revision)
            for provider in listing.records.providers
        }
        catalog_sources.update(
            (provider_id, (name, revision))
            for name, revision, snapshot in load.additional
            for provider_id in snapshot.providers
        )
        setup = _build_setup(
            layout=self.layout,
            sandbox=self._sandbox,
            revision=projection_key,
            validate_defaults=self._validate_defaults,
            listing=listing,
            catalog_sources=catalog_sources,
            adapters=adapters,
            tools=tools,
            envs=inputs.envs,
            allow=allow,
            defaults=defaults,
            compact_model=compact_model,
            limits=limits,
        )
        self._publish(setup)
        self._commit_candidate(candidate)
        self._diagnostics = ()
        return setup

    def _load_inputs(self) -> _LoadedInputs:
        paths = (
            self.layout.root_config,
            *((self.layout.config,) if self._agent_context else ()),
        )
        captured = tuple(capture_setup_config(path) for path in paths)
        return _LoadedInputs(
            fingerprints=tuple(revision for _config, revision in captured),
            root_config=captured[0][0],
            agent_config=captured[1][0] if self._agent_context else {},
            envs=dict(
                load_setup_envs(self.layout)
                if self._agent_context
                else load_root_setup_envs(self.layout)
            ),
        )

    def _runtime_catalog_configs(
        self,
        configs: Sequence[Mapping[str, object]],
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

    async def _load_sources(
        self,
        models_dev: ModelsDevModelCatalog,
        catalogs: Mapping[str, ModelCatalog],
    ) -> _CatalogLoad:
        """Capture static bytes and fresh probes before checking the merged cache."""

        observation, source = await asyncio.to_thread(models_dev.capture)
        ordered = _ordered_additional_catalogs(catalogs)
        probes = await asyncio.gather(*(catalog.snapshot() for catalog in ordered))
        additional = tuple(
            await asyncio.gather(
                *(
                    self._probe_revision(catalog.name, assemble_catalog(probe))
                    for catalog, probe in zip(ordered, probes, strict=True)
                )
            )
        )
        return _CatalogLoad(
            observation=observation,
            source=source,
            additional=additional,
        )

    async def _probe_revision(
        self,
        name: str,
        probe: ModelCatalogSnapshot,
    ) -> tuple[str, str, ModelCatalogSnapshot]:
        """Persist one probe result and return it with that source's revision."""

        try:
            revision = await asyncio.to_thread(
                self._model_cache.store_probe,
                name,
                snapshot=probe,
            )
        except Exception:
            logger.warning("setup.model_cache_write_failed agent=%s", self.layout.name)
            # A failed write must not reuse a stale stamp: a changed probe would
            # then collide with the previous revision and be dropped.
            revision = self._model_cache.content_revision(probe)
        return (name, revision, probe)

    def _candidate_is_unchanged(self, candidate: _Candidate) -> bool:
        return (
            self._setup is not None
            and candidate.config_value == self._config
            and candidate.inputs.envs == self._setup.envs
            and candidate.observation == self._catalog_identity
            and candidate.adapter_configs == self._adapter_configs
            and candidate.toolset_configs == self._toolset_configs
            and candidate.catalog_configs == self._catalog_configs
            and candidate.source_revisions == self._source_revisions
        )

    def _commit_candidate(self, candidate: _Candidate) -> None:
        self._config = candidate.config_value
        self._adapter_configs = candidate.adapter_configs
        self._toolset_configs = candidate.toolset_configs
        self._catalog_configs = candidate.catalog_configs
        self._adapters = candidate.adapters
        self._tools = candidate.tools
        self._catalogs = candidate.catalogs
        self._catalog_identity = candidate.observation
        self._source_revisions = candidate.source_revisions

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
) -> ModelCatalogSnapshot:
    return resolve_catalog_providers(merged, adapters=adapters, environ=envs)


def _build_setup(
    *,
    layout: AgentLayout,
    sandbox: str,
    revision: str,
    listing: ModelListing,
    catalog_sources: Mapping[str, tuple[str, str]],
    adapters: dict[str, ModelAdapter],
    tools: dict[str, Tool],
    envs: dict[str, str],
    allow: AgentCeiling,
    defaults: RunDefaults,
    limits: RunLimits,
    compact_model: ModelOverride | None = None,
    validate_defaults: bool = True,
) -> AgentSetup:
    # Retain this version's declarations and environment for lazy full inspection.
    # Only ready, allowed models need runtime routes and query views at startup.
    adapters = dict(adapters)
    envs = dict(envs)

    def load_catalog(*, all: bool = False) -> ModelCatalogSnapshot:
        return listing.resolve(
            listing.records.models if all else listing.effective_models,
            adapters=adapters,
            environ=envs,
            revision=revision,
            include_empty_providers=all,
        )

    snapshot = load_catalog()
    models = ModelCollection(snapshot.models, _lazy_queries=True)
    if (
        validate_defaults
        and compact_model is not None
        and compact_model.identity != "unset"
    ):
        select_compact_model(models, compact_model)
    all_tools = ToolCollection.from_tools(tools)
    tool_collection = all_tools
    if allow.tools is not None:
        selected = (
            tool_collection.user.match(allow.tools) if allow.tools else ToolCollection()
        )
        tool_collection = tool_collection.subset(
            (*tool_collection.runtime, *selected)
        ).compact()
    if validate_defaults and defaults.model is not None:
        model = models.resolve(defaults.model.ref)
        resolve_model_reasoning(model, defaults.model.reasoning)
    provider_ids = {model._toolang.provider for model in models.entries}
    providers = {
        provider_id: snapshot.providers[provider_id]
        for provider_id in sorted(provider_ids)
    }
    return AgentSetup(
        layout=layout,
        revision=revision,
        providers=providers,
        adapters=adapters,
        models=models,
        tools=tool_collection,
        envs=envs,
        environment=AgentEnvironment.capture(layout, sandbox=sandbox),
        defaults=defaults,
        limits=limits,
        compact_model=compact_model,
        catalog_sources=catalog_sources,
        _catalog_loader=lambda: load_catalog(all=True),
        _model_listing=listing,
        _all_tools=all_tools,
        _allowed_model_refs=frozenset(
            f"{model.provider}/{model.id}"
            for model in listing.records.models
            if model.allowed_order is not None
        ),
    )


def _adapter_identity(adapters: Mapping[str, ModelAdapter]) -> str:
    """Include loadable implementations and defaults, not only installed metadata."""

    return digest({name: adapter.default_api for name, adapter in adapters.items()})


def _projection_key(
    *,
    source_revisions: tuple[tuple[str, str], ...],
    environment: Mapping[str, str],
    setup_inputs: Mapping[str, object],
    allow_models: tuple[str, ...] | None,
    plugin_provenance: tuple[object, ...],
    scope: str,
) -> str:
    return model_projection_key(
        kind="runtime",
        scope=scope,
        catalog_revisions=source_revisions,
        setup_config=setup_inputs,
        environment=environment,
        plugin_provenance=plugin_provenance,
        allow_models=allow_models,
    )


def _ordered_additional_catalogs(
    catalogs: Mapping[str, ModelCatalog],
) -> tuple[ModelCatalog, ...]:
    additional = {
        name: catalog for name, catalog in catalogs.items() if name != "models_dev"
    }
    return tuple(
        additional.pop(name) for name in ("ollama", "llama_cpp") if name in additional
    ) + tuple(additional[name] for name in sorted(additional))


def _candidate_diagnostic(exc: Exception) -> SetupDiagnostic:
    name = type(exc).__name__
    code = "".join(
        ("-" + character.lower()) if character.isupper() else character
        for character in name
    ).lstrip("-")
    return SetupDiagnostic(code=code, message=str(exc) or name)


async def load_setup(
    layout: AgentLayout,
    *,
    model_catalog: Path | None = None,
    sandbox: str = "host",
    agent_context: bool = True,
    validate_defaults: bool = True,
) -> AgentSetup:
    """Build one setup version once, without a running watcher."""

    watcher = SetupWatcher(
        layout,
        sandbox=sandbox,
        model_catalog=model_catalog,
        agent_context=agent_context,
        validate_defaults=validate_defaults,
    )
    return await watcher.refresh()
