"""Watch authored task and chore files."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

from watchfiles import Change, awatch

from .state import HomeJobs


class JobWatcher:
    """Publish immutable home-job snapshots when authored files change."""

    def __init__(self, root: Path, name: str) -> None:
        self.root = root
        self.name = name
        self._jobs = HomeJobs.load(root, name)

    def current(self) -> HomeJobs:
        return self._jobs

    def refresh(self) -> HomeJobs:
        self._jobs = HomeJobs.load(self.root, self.name)
        return self._jobs

    def start(
        self,
        *,
        stop_signal: asyncio.Event,
        interval_ms: float = 1_000.0,
        debounce_ms: float = 500.0,
    ) -> asyncio.Task[None]:
        async def watch() -> None:
            async for _ in self.updates(
                stop_signal=stop_signal,
                interval_ms=interval_ms,
                debounce_ms=debounce_ms,
            ):
                pass

        return asyncio.create_task(watch())

    async def updates(
        self,
        *,
        stop_signal: asyncio.Event,
        interval_ms: float = 1_000.0,
        debounce_ms: float = 500.0,
    ) -> AsyncIterator[HomeJobs]:
        home = self.root / "agents" / self.name
        home.mkdir(parents=True, exist_ok=True)
        async for changes in awatch(
            home,
            debounce=max(int(debounce_ms), 50),
            step=max(int(interval_ms), 50),
            stop_event=stop_signal,
        ):
            if not any(
                kind in {Change.added, Change.modified, Change.deleted}
                and Path(path).suffix == ".md"
                and Path(path).parent.name in {"tasks", "chores"}
                for kind, path in changes
            ):
                continue
            previous = self._jobs
            current = self.refresh()
            if current != previous:
                yield current
