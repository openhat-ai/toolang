"""Keep the current installed runtime setup synchronized."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from decimal import Decimal
import logging
from pathlib import Path

from toolang.base.protocols.model import ModelAdapter, ModelCatalog
from toolang.base.protocols.tool import AgentTool
from toolang.base.types.model import ModelCatalogSnapshot
from toolang.common.layout import AgentLayout
from toolang.plugin.config import merge_named_configs
from toolang.plugin.models.catalog import (
    MergedModelCatalog,
    model_info_from_catalog,
    resolve_model_catalog_path,
)
from toolang.plugin.models.config import (
    ProviderConfig,
    configure_catalog_providers,
    parse_catalog_configs,
    parse_provider_configs,
)
from toolang.plugin.models.loading import load_model_adapters, load_model_catalogs
from toolang.plugin.models.provider_resolver import resolve_catalog_providers
from toolang.plugin.tools.loading import load_runtime_tools

from .config import (
    load_agent_config,
    load_setup_config,
    load_setup_envs,
    resolve_agent_ceiling,
    resolve_run_bindings,
    resolve_run_limits,
)
from .types import AgentEnvironment, AgentSetup

DEFAULT_INTERVAL_MS = 1_000.0
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _SnapshotModels(ModelCatalog):
    value: ModelCatalogSnapshot
    name: str = "models_dev"

    async def snapshot(self) -> ModelCatalogSnapshot:
        return self.value


class SetupWatcher:
    """Publish immutable setup snapshots when inputs or local models change."""

    def __init__(
        self,
        layout: AgentLayout,
        *,
        model_catalog: Path | None = None,
        ceiling_overrides: Mapping[str, tuple[str, ...] | None] | None = None,
        binding_overrides: Mapping[str, str | None] | None = None,
        limit_overrides: Mapping[str, int | Decimal | None] | None = None,
    ) -> None:
        self.layout = layout
        self._model_catalog_override = model_catalog
        self._ceiling_overrides = dict(ceiling_overrides or {})
        self._binding_overrides = dict(binding_overrides or {})
        self._limit_overrides = dict(limit_overrides or {})
        self._config: tuple[dict[str, object], dict[str, object]] | None = None
        self._adapters: dict[str, ModelAdapter] = {}
        self._tools: dict[str, AgentTool] = {}
        self._static_catalog: ModelCatalogSnapshot | None = None
        self._catalog_identity: tuple[Path, int, int, int, int] | None = None
        self._setup: AgentSetup | None = None
        self._refresh_lock = asyncio.Lock()

    def current(self) -> AgentSetup:
        """Return the latest immutable setup snapshot."""

        if self._setup is None:
            raise RuntimeError("setup watcher has not been refreshed")
        return self._setup

    async def refresh(self, *, force: bool = False) -> AgentSetup:
        """Refresh environment and explicit local provider discovery."""

        async with self._refresh_lock:
            root_config = load_setup_config(self.layout)
            agent_config = load_agent_config(self.layout)
            envs = load_setup_envs(self.layout)
            configs = (root_config, agent_config)
            config_value = (root_config, agent_config)
            config_changed = config_value != self._config
            envs_changed = self._setup is None or envs != self._setup.envs
            catalog_path = resolve_model_catalog_path(
                self.layout,
                explicit=self._model_catalog_override,
                environ=envs,
            )
            identity = _catalog_file_identity(catalog_path)
            catalog_changed = identity != self._catalog_identity
            ceiling = resolve_agent_ceiling(
                configs,
                overrides=self._ceiling_overrides,
            )
            bindings = resolve_run_bindings(
                configs,
                overrides=self._binding_overrides,
            )
            limits = resolve_run_limits(
                configs,
                overrides=self._limit_overrides,
            )
            provider_configs = parse_provider_configs(configs)
            if config_changed or not self._adapters:
                self._adapters = load_model_adapters()
            if config_changed or envs_changed or not self._tools:
                self._tools = load_runtime_tools(
                    plugin_config=merge_named_configs(
                        configs,
                        section="tools",
                        environ=envs,
                    )
                )
            ollama = provider_configs.get("ollama")
            llama_cpp = provider_configs.get("llama_cpp")
            catalog_configs = parse_catalog_configs(configs, environ=envs)
            catalog_configs["models_dev"] = {
                **catalog_configs.get("models_dev", {}),
                "path": catalog_path,
            }
            catalog_configs["ollama"] = {
                **catalog_configs.get("ollama", {}),
                "environ": envs,
                **(
                    {"endpoint": _endpoint(ollama)}
                    if _endpoint(ollama) is not None
                    else {}
                ),
            }
            catalog_configs["llama_cpp"] = {
                **catalog_configs.get("llama_cpp", {}),
                "environ": envs,
                **(
                    {"endpoint": _endpoint(llama_cpp)}
                    if _endpoint(llama_cpp) is not None
                    else {}
                ),
            }
            catalogs = load_model_catalogs(catalog_configs)
            models_dev = catalogs.pop("models_dev", None)
            if models_dev is None:
                raise RuntimeError("models_dev catalog plugin is not installed")
            if self._static_catalog is None or catalog_changed:
                self._static_catalog = await models_dev.snapshot()

            static = self._static_catalog
            assert static is not None
            ordered_catalogs = tuple(
                catalogs.pop(name)
                for name in ("ollama", "llama_cpp")
                if name in catalogs
            ) + tuple(catalogs[name] for name in sorted(catalogs))
            merged = await MergedModelCatalog(
                (
                    _SnapshotModels(static),
                    *ordered_catalogs,
                )
            ).snapshot()
            providers = configure_catalog_providers(
                merged.providers,
                provider_configs,
            )
            resolved_catalog = resolve_catalog_providers(
                ModelCatalogSnapshot(
                    providers=providers,
                    models=merged.models,
                    revision=merged.revision,
                    source=merged.source,
                ),
                adapters=self._adapters,
                environ=envs,
                configs=provider_configs,
            )
            providers = dict(resolved_catalog.providers)
            models = tuple(
                model_info_from_catalog(model) for model in resolved_catalog.models
            )
            setup = AgentSetup(
                layout=self.layout,
                providers=providers,
                adapters=self._adapters,
                models=models,
                tools=self._tools,
                envs=envs,
                catalog=resolved_catalog,
                provider_configs=provider_configs,
                environment=AgentEnvironment.capture(self.layout, envs=envs),
                ceiling=ceiling,
                bindings=bindings,
                limits=limits,
            )
            self._config = config_value
            self._catalog_identity = identity
            if self._setup is not None and not force and setup == self._setup:
                return self._setup
            self._setup = setup
            return setup

    async def updates(
        self,
        *,
        stop_signal: asyncio.Event,
        interval_ms: float = DEFAULT_INTERVAL_MS,
    ) -> AsyncIterator[AgentSetup]:
        """Yield each changed setup until the caller stops watching."""

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
            try:
                current = await self.refresh()
            except Exception:
                logger.exception("setup.refresh_failed agent=%s", self.layout.name)
                continue
            if current != previous:
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


def _endpoint(config: ProviderConfig | None) -> str | None:
    return config.endpoint if config is not None else None


def _catalog_file_identity(path: Path) -> tuple[Path, int, int, int, int]:
    resolved = path.resolve(strict=True)
    stat = resolved.stat()
    return (resolved, stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_size)
