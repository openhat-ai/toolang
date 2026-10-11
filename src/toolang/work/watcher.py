"""Watch current authored chore files."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
import logging
from pathlib import Path

from watchfiles import Change, awatch
from yaml import YAMLError

from toolang.common.layout import AgentLayout

from .state import Chore, load_chore_files

_LOGGER = logging.getLogger(__name__)


class ChoreWatcher:
    """Publish immutable current chore snapshots when authored files change."""

    def __init__(self, layout: AgentLayout) -> None:
        self.layout = layout
        self._chores = load_chore_files(layout)

    def current(self) -> tuple[Chore, ...]:
        """Return the latest completed snapshot without filesystem access."""

        return self._chores

    def refresh(self) -> tuple[Chore, ...]:
        """Refresh definitions, retaining the last valid snapshot on a bad edit."""

        try:
            self._chores = load_chore_files(self.layout)
        except (OSError, ValueError, YAMLError) as exc:
            _LOGGER.warning("chores.reload_rejected error=%s", exc)
        return self._chores

    async def updates(
        self,
        *,
        stop_signal: asyncio.Event,
        interval_ms: float = 1_000.0,
        debounce_ms: float = 500.0,
    ) -> AsyncIterator[tuple[Chore, ...]]:
        """Yield different stable snapshots until the owner loop stops."""

        directories = (
            self.layout.root / "chores",
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
                and _is_chore_path(directories, Path(path))
                for kind, path in changes
            ):
                continue
            previous = self._chores
            current = self.refresh()
            if current != previous:
                yield current


def _is_chore_path(directories: tuple[Path, Path], path: Path) -> bool:
    return path.suffix == ".md" and path.parent in directories
