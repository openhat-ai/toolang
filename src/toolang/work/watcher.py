"""Watch ready authored task and chore files."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

from watchfiles import Change, awatch

from toolang.common.layout import AgentLayout

from .state import Job, load_ready_jobs


class JobWatcher:
    """Publish immutable ready-job snapshots when authored files change."""

    def __init__(self, layout: AgentLayout) -> None:
        self.layout = layout
        self._jobs = load_ready_jobs(layout)

    def current(self) -> tuple[Job, ...]:
        """Return the latest completed snapshot without filesystem access."""

        return self._jobs

    def refresh(self) -> tuple[Job, ...]:
        """Read and publish one complete ready-job snapshot."""

        self._jobs = load_ready_jobs(self.layout)
        return self._jobs

    async def updates(
        self,
        *,
        stop_signal: asyncio.Event,
        interval_ms: float = 1_000.0,
        debounce_ms: float = 500.0,
    ) -> AsyncIterator[tuple[Job, ...]]:
        """Yield different stable snapshots until the owner loop stops."""

        directories = (
            self.layout.home / "tasks",
            self.layout.home / "chores",
        )
        for directory in directories:
            directory.mkdir(parents=True, exist_ok=True)
        async for changes in awatch(
            *directories,
            debounce=max(int(debounce_ms), 50),
            step=max(int(interval_ms), 50),
            stop_event=stop_signal,
        ):
            if not any(
                kind in {Change.added, Change.modified, Change.deleted}
                and _is_ready_job_path(directories, Path(path))
                for kind, path in changes
            ):
                continue
            previous = self._jobs
            current = self.refresh()
            if current != previous:
                yield current


def _is_ready_job_path(directories: tuple[Path, Path], path: Path) -> bool:
    return path.suffix == ".md" and path.parent in directories
