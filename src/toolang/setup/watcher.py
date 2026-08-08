"""Keep the current installed runtime setup synchronized."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from decimal import Decimal
import logging

from toolang.base.protocols.model import ModelAdapter, ModelProvider
from toolang.base.protocols.tool import AgentTool
from toolang.common.layout import AgentLayout
from toolang.plugin.config import merge_named_configs
from toolang.plugin.models.config import parse_model_provider_configs
from toolang.plugin.models.loading import load_model_adapters, load_model_providers
from toolang.plugin.tools.loading import load_runtime_tools

from .config import (
    load_agent_config,
    load_setup_config,
    load_setup_envs,
    resolve_agent_ceiling,
    resolve_run_bindings,
    resolve_run_limits,
)
from .models import ModelListCache, discover_models
from .types import AgentEnvironment, AgentSetup

DEFAULT_INTERVAL_MS = 1_000.0
logger = logging.getLogger(__name__)


class SetupWatcher:
    """Publish setup snapshots when envs or available models change."""

    def __init__(
        self,
        layout: AgentLayout,
        *,
        ceiling_overrides: Mapping[str, tuple[str, ...] | None] | None = None,
        binding_overrides: Mapping[str, str | None] | None = None,
        limit_overrides: Mapping[str, int | Decimal | None] | None = None,
    ) -> None:
        self.layout = layout
        self._ceiling_overrides = dict(ceiling_overrides or {})
        self._binding_overrides = dict(binding_overrides or {})
        self._limit_overrides = dict(limit_overrides or {})
        self._config: dict[str, object] | None = None
        self._providers: dict[str, ModelProvider] = {}
        self._adapters: dict[str, ModelAdapter] = {}
        self._tools: dict[str, AgentTool] = {}
        self._model_cache = ModelListCache(layout.model_cache)
        self._setup: AgentSetup | None = None
        self._refresh_lock = asyncio.Lock()

    def current(self) -> AgentSetup:
        """Return the latest immutable setup snapshot."""

        if self._setup is None:
            raise RuntimeError("setup watcher has not been refreshed")
        return self._setup

    async def refresh(self, *, force: bool = False) -> AgentSetup:
        """Refresh envs and models, optionally forcing provider discovery."""

        async with self._refresh_lock:
            config = load_setup_config(self.layout)
            agent_config = load_agent_config(self.layout)
            envs = load_setup_envs(self.layout)
            configs = (config, agent_config)
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
            config_changed = config != self._config
            envs_changed = self._setup is None or envs != self._setup.envs
            providers = self._providers
            adapters = self._adapters
            tools = self._tools
            if config_changed:
                providers = load_model_providers(
                    parse_model_provider_configs((config,))
                )
                adapters = load_model_adapters()
            if config_changed or envs_changed:
                tools = load_runtime_tools(
                    plugin_config=merge_named_configs(
                        (config,),
                        section="tools",
                        environ=envs,
                    )
                )
            models = await discover_models(
                providers,
                envs=envs,
                cache=self._model_cache,
                refresh=force,
            )
            setup = AgentSetup(
                layout=self.layout,
                providers=providers,
                adapters=adapters,
                models=tuple(
                    model for model in models if model.adapter in adapters
                ),
                tools=tools,
                envs=envs,
                environment=AgentEnvironment.capture(
                    self.layout,
                    envs=envs,
                ),
                ceiling=ceiling,
                bindings=bindings,
                limits=limits,
            )
            self._providers = providers
            self._adapters = adapters
            self._tools = tools
            self._config = config
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
