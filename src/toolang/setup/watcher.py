"""Keep the current installed runtime setup synchronized."""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, replace
from types import MappingProxyType
import logging
from pathlib import Path

from toolang.base.protocols.model import ModelAdapter, ModelCatalog
from toolang.base.types.model import ModelCatalogSnapshot, ModelOverride
from toolang.base.types.policy import AgentCeiling, RunDefaults, RunLimits
from toolang.common.layout import AgentLayout
from toolang.common.progress import ProgressSink
from .progress import setup_progress
from .teaming import TeamingSetup, resolve_teaming_setup
from toolang.plugin.config import merge_plugin_configs
from toolang.common.config_sources import ConfigSource, config_sources
from toolang.plugin.loading import (
    load_model_adapters,
    load_model_catalogs,
    plugin_provenance,
)
from toolang.plugin.catalogs.models_dev.catalog import (
    ModelCatalogSource,
    ModelsDevModelCatalog,
)
from toolang.plugin.catalogs.models_dev.path import resolve_model_catalog_path
from toolang.plugin.models.config import validate_models_config
from toolang.plugin.models.resolution import resolve_model_reasoning
from .tools import capture_toolset_loader, materialize_tools
from toolang.setup.routes import RouteAdapter, resolve_catalog_providers

from .revisions import (
    environment_identity,
    model_projection_key,
    source_content_revision,
)
from .catalog import assemble_catalog, merge_catalog_snapshots
from .config import (
    load_root_setup_envs,
    load_setup_envs,
    project_model_setup_config,
    project_setup_config,
    resolve_compact_config,
    resolve_run_defaults,
    resolve_run_limits,
    resolve_setup_allow,
)
from .errors import SetupDiagnostic
from .models import order_models, select_compact_model
from toolang.plugin.models.query import resolve_model
from .types import AgentEnvironment, AgentSetup, CompactConfig, _ModelData

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
class _LoadedInputs:
    sources: tuple[ConfigSource, ...]
    configs: tuple[dict[str, object], ...]
    envs: dict[str, str]


@dataclass(frozen=True, slots=True)
class _Candidate:
    inputs: _LoadedInputs
    config_value: tuple[dict[str, object], ...]
    adapter_configs: dict[str, dict[str, object]]
    toolset_configs: dict[str, dict[str, object]]
    catalog_configs: dict[str, dict[str, object]]
    catalogs: dict[str, ModelCatalog]
    source_revisions: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class _CatalogLoad:
    """Validated, immutable source snapshots for one candidate revision."""

    source: ModelCatalogSource
    static: ModelCatalogSnapshot
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
        self._config: tuple[dict[str, object], ...] | None = None
        self._adapter_configs: dict[str, dict[str, object]] | None = None
        self._toolset_configs: dict[str, dict[str, object]] | None = None
        self._catalog_configs: dict[str, dict[str, object]] | None = None
        self._catalogs: dict[str, ModelCatalog] = {}
        self._source_revisions: tuple[tuple[str, str], ...] | None = None
        self._setup_plugin_provenance = tuple(
            item.to_data()
            for group in (
                "toolang.model_catalog",
                "toolang.model_adapter",
                "toolang.toolset",
            )
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

    def diagnostics(self) -> tuple[SetupDiagnostic, ...]:
        """Return diagnostics for the latest rejected candidate, if any."""

        return self._diagnostics

    async def refresh(self, *, progress: ProgressSink | None = None) -> AgentSetup:
        """Run one serialized candidate check and return the last valid Setup."""

        async with self._refresh_lock:
            try:
                with setup_progress(
                    progress, target=self.layout.name, resource="setup"
                ):
                    return await self._perform_refresh(progress=progress)
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

    async def _perform_refresh(self, *, progress: ProgressSink | None) -> AgentSetup:
        inputs = self._load_inputs()
        configs = inputs.configs
        config_value = tuple(project_setup_config(config) for config in configs)
        allow = resolve_setup_allow(configs, overrides=self._allow_overrides)
        defaults = resolve_run_defaults(configs, overrides=self._default_overrides)
        compact = resolve_compact_config(configs, override=self._compact_override)
        limits = resolve_run_limits(configs, overrides=self._limit_overrides)
        validate_models_config(configs)
        adapter_configs = merge_plugin_configs(configs, family="model_adapter")
        toolset_configs = merge_plugin_configs(configs, family="toolset")
        if self._setup is None:
            teaming = resolve_teaming_setup(
                inputs.sources, root=self.layout.root_config, home=self.layout.config
            )
        else:
            teaming = self._setup.teaming
        toolset_configs["msg"] = (
            teaming.toolset_config(root=self.layout.root) if teaming else {}
        )
        catalog_path = resolve_model_catalog_path(
            self.layout,
            explicit=self._model_catalog_override,
            environ=inputs.envs,
            include_agent=self._agent_context,
            sources=inputs.sources,
        )
        catalog_configs = self._runtime_catalog_configs(
            configs, inputs.envs, catalog_path=catalog_path
        )
        catalogs = (
            self._catalogs
            if self._catalog_configs == catalog_configs
            else load_model_catalogs(catalog_configs)
        )
        models_dev = catalogs.get("models_dev")
        if not isinstance(models_dev, ModelsDevModelCatalog):
            raise RuntimeError("models_dev catalog plugin is not installed")
        load = await self._load_sources(models_dev, catalogs, progress=progress)
        source_revisions = (
            ("models_dev", load.source.content_revision),
            *((name, revision) for name, revision, _snapshot in load.additional),
        )
        candidate = _Candidate(
            inputs=inputs,
            config_value=config_value,
            adapter_configs=adapter_configs,
            toolset_configs=toolset_configs,
            catalog_configs=catalog_configs,
            catalogs=catalogs,
            source_revisions=source_revisions,
        )
        if self._candidate_is_unchanged(candidate):
            self._commit_candidate(candidate)
            self._diagnostics = ()
            return self.current()

        revision = _projection_key(
            source_revisions=source_revisions,
            environment=environment_identity(inputs.envs),
            setup_inputs={
                "catalogs": catalog_configs,
                "config": tuple(
                    project_model_setup_config(config) for config in configs
                ),
                "adapters": adapter_configs,
                "toolsets": toolset_configs,
                "human": teaming.root.human if teaming else None,
                "allow": allow,
                "defaults": defaults,
                "limits": limits,
                "compact": compact,
            },
            allow_models=allow.models,
            plugin_provenance=self._setup_plugin_provenance,
            scope=f"agent:{self.layout.name}" if self._agent_context else "root",
        )
        if self._setup is not None and self._setup.revision == revision:
            self._commit_candidate(candidate)
            self._diagnostics = ()
            return self._setup

        catalog_sources = {
            provider_id: ("models_dev", load.source.content_revision)
            for provider_id in load.static.providers
        }
        catalog_sources.update(
            (provider_id, (name, source_revision))
            for name, source_revision, snapshot in load.additional
            for provider_id in snapshot.providers
        )
        setup = _build_setup(
            layout=self.layout,
            sandbox=self._sandbox,
            revision=revision,
            load=load,
            catalog_sources=catalog_sources,
            teaming=teaming,
            adapter_configs=adapter_configs,
            toolset_configs=toolset_configs,
            catalog_configs=catalog_configs,
            envs=inputs.envs,
            allow=allow,
            defaults=defaults,
            compact=compact,
            limits=limits,
            validate_defaults=self._validate_defaults,
        )
        self._publish(setup)
        self._commit_candidate(candidate)
        self._diagnostics = ()
        return setup

    def _load_inputs(self) -> _LoadedInputs:
        sources = config_sources(self.layout, include_agent=self._agent_context)
        return _LoadedInputs(
            sources=sources,
            configs=tuple(source.config for source in sources),
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
        *,
        progress: ProgressSink | None,
    ) -> _CatalogLoad:
        """Capture validated source snapshots for watcher revision detection."""

        _observation, source = await asyncio.to_thread(models_dev.capture)
        static = await asyncio.to_thread(source.snapshot)
        ordered = _ordered_additional_catalogs(catalogs)
        with setup_progress(
            progress,
            target=self.layout.name,
            resource="model catalogs",
            stage="discover",
        ):
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
            source=source,
            static=static,
            additional=additional,
        )

    async def _probe_revision(
        self,
        name: str,
        probe: ModelCatalogSnapshot,
    ) -> tuple[str, str, ModelCatalogSnapshot]:
        """Fingerprint one fresh probe in memory; do not persist model data."""

        revision = await asyncio.to_thread(source_content_revision, probe)
        return (name, revision, probe)

    def _candidate_is_unchanged(self, candidate: _Candidate) -> bool:
        return (
            self._setup is not None
            and candidate.config_value == self._config
            and candidate.inputs.envs == self._setup.envs
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
        self._catalogs = candidate.catalogs
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


def _resolve_catalog(
    merged: ModelCatalogSnapshot,
    *,
    adapters: Mapping[str, RouteAdapter],
    envs: Mapping[str, str],
) -> ModelCatalogSnapshot:
    return resolve_catalog_providers(merged, adapters=adapters, environ=envs)


def _build_setup(
    *,
    layout: AgentLayout,
    sandbox: str,
    revision: str,
    load: _CatalogLoad,
    catalog_sources: Mapping[str, tuple[str, str]],
    adapter_configs: Mapping[str, Mapping[str, object]],
    toolset_configs: Mapping[str, Mapping[str, object]],
    catalog_configs: Mapping[str, Mapping[str, object]],
    envs: Mapping[str, str],
    allow: AgentCeiling,
    defaults: RunDefaults,
    limits: RunLimits,
    compact: CompactConfig,
    teaming: TeamingSetup | None,
    validate_defaults: bool,
) -> AgentSetup:
    """Publish captured revisions with per-setup synchronous lazy loaders."""

    captured_envs = dict(envs)
    adapter_config = {name: dict(value) for name, value in adapter_configs.items()}
    catalog_config = {name: dict(value) for name, value in catalog_configs.items()}
    # Hold one generation's input snapshots until its first successful model
    # materialization, then release them so only the resolved records and view
    # reference lists remain attached to the setup.
    source_snapshots = [load.static, *(snapshot for _, _, snapshot in load.additional)]

    def load_adapters() -> Mapping[str, ModelAdapter]:
        return MappingProxyType(load_model_adapters(adapter_config))

    def load_catalog_plugins() -> Mapping[str, ModelCatalog]:
        return MappingProxyType(load_model_catalogs(catalog_config))

    def load_model_data(setup: AgentSetup) -> _ModelData:
        merged = merge_catalog_snapshots(tuple(source_snapshots))
        resolved = _resolve_catalog(
            merged,
            adapters=setup.adapters(),
            envs=captured_envs,
        )
        ordered_models, allowed_refs = order_models(resolved.models, allow.models)
        models = [
            model.with_allowed(model.ref in allowed_refs) for model in ordered_models
        ]
        models_effective = [model for model in models if model._toolang.effective_ready]
        model_counts = Counter(model.provider for model in models)
        ready_counts = Counter(model.provider for model in models_effective)
        providers = [
            replace(
                provider,
                _toolang=replace(
                    provider._toolang,
                    model_count=model_counts[provider.id],
                    ready_count=ready_counts[provider.id],
                ),
            )
            for provider in resolved.providers.values()
        ]
        providers_effective = [
            provider for provider in providers if provider._toolang.ready_count > 0
        ]
        if validate_defaults and compact.model is not None:
            select_compact_model(models_effective, compact.model)
        if validate_defaults and defaults.model is not None:
            model = resolve_model(models_effective, defaults.model.ref)
            resolve_model_reasoning(model, defaults.model.reasoning)
        data = _ModelData(
            models=tuple(models),
            providers=tuple(providers),
            models_effective=tuple(models_effective),
            providers_effective=tuple(providers_effective),
        )
        source_snapshots.clear()
        return data

    return AgentSetup(
        layout=layout,
        envs=captured_envs,
        revision=revision,
        environment=AgentEnvironment.capture(layout, sandbox=sandbox),
        defaults=defaults,
        limits=limits,
        compact=compact,
        teaming=teaming,
        catalog_sources=catalog_sources,
        _load_models=load_model_data,
        _load_tools=materialize_tools,
        _allowed_tools=allow.tools,
        _load_toolset_plugins=capture_toolset_loader(toolset_configs),
        _load_adapters=load_adapters,
        _load_catalogs=load_catalog_plugins,
    )


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
    progress: ProgressSink | None = None,
) -> AgentSetup:
    """Build one setup version once, without a running watcher."""

    watcher = SetupWatcher(
        layout,
        sandbox=sandbox,
        model_catalog=model_catalog,
        agent_context=agent_context,
        validate_defaults=validate_defaults,
    )
    return await watcher.refresh(progress=progress)
