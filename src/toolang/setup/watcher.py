"""Keep the current installed runtime setup synchronized."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Mapping

from .models import ModelListCache
from .prepare import prepare_agent_setup
from .types import AgentSetup

DEFAULT_INTERVAL_MS = 1_000.0


class SetupWatcher:
    """Publish setup snapshots when envs or available models change."""

    def __init__(
        self,
        setup: AgentSetup,
        *,
        cache: ModelListCache,
        get_envs: Callable[[], Mapping[str, str]],
    ) -> None:
        self._setup = setup
        self._cache = cache
        self._get_envs = get_envs
        self._refresh_lock = asyncio.Lock()

    def current(self) -> AgentSetup:
        """Return the latest immutable setup snapshot."""

        return self._setup

    async def refresh(self, *, force: bool = False) -> AgentSetup:
        """Refresh envs and models, optionally forcing provider discovery."""

        async with self._refresh_lock:
            current = self._setup
            self._setup = await prepare_agent_setup(
                name=current.name,
                home=current.home,
                providers=current.providers,
                adapters=current.adapters,
                tools=current.tools,
                envs=self._get_envs(),
                cache=self._cache,
                refresh_models=force,
            )
            return self._setup

    async def updates(
        self,
        *,
        stop_signal: asyncio.Event,
        interval_ms: float = DEFAULT_INTERVAL_MS,
    ) -> AsyncIterator[AgentSetup]:
        """Yield each changed setup until the caller stops watching."""

        interval_sec = max(interval_ms, 50.0) / 1_000
        while not stop_signal.is_set():
            try:
                await asyncio.wait_for(stop_signal.wait(), timeout=interval_sec)
            except TimeoutError:
                pass
            if stop_signal.is_set():
                break
            previous = self._setup
            current = await self.refresh()
            if current.models != previous.models or current.envs != previous.envs:
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
