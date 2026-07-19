"""Prepare and watch immutable agent source state."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
import logging
from pathlib import Path

from watchfiles import Change, awatch

from .state import AgentState
from .cache import (
    load_current_version,
    load_version_source,
    prepared_current_path,
    prepared_version_dir,
)
from .prepare import prepare_agent_state
from .source import scan_home_source, scan_root_source
from toolang.state.source import is_source_path

DEFAULT_INTERVAL_MS = 1_000.0
DEFAULT_DEBOUNCE_MS = 500.0
logger = logging.getLogger("toolang.watch")
_RELEVANT_CHANGES = {Change.added, Change.modified, Change.deleted}


class StateWatcher:
    """Publish new immutable agent state when authored files change."""

    def __init__(
        self,
        root: Path,
        name: str,
        state: AgentState,
        *,
        transform: Callable[[AgentState], AgentState] | None = None,
    ) -> None:
        self.root = root
        self.name = name
        self._state = state
        self._toolang_version = state.toolang_version
        self._transform = transform or (lambda value: value)

    def current(self) -> AgentState:
        return self._state

    def refresh(self) -> AgentState:
        self._state = self._transform(
            prepare_agent_state(
                self.root,
                self.name,
                toolang_version=self._toolang_version,
            )
        )
        return self._state

    async def updates(
        self,
        *,
        stop_signal: asyncio.Event,
        interval_ms: float = DEFAULT_INTERVAL_MS,
        debounce_ms: float = DEFAULT_DEBOUNCE_MS,
    ) -> AsyncIterator[AgentState]:
        logger.debug(
            "watch.started root=%s agent=%s interval_ms=%s debounce_ms=%s",
            self.root,
            self.name,
            int(interval_ms),
            int(debounce_ms),
        )
        timeout_ms = max(int(interval_ms), 50)
        async for changes in awatch(
            self.root,
            debounce=max(int(debounce_ms), 50),
            step=timeout_ms,
            rust_timeout=timeout_ms,
            yield_on_timeout=True,
            stop_event=stop_signal,
        ):
            paths = {
                Path(path)
                for kind, path in changes
                if kind in _RELEVANT_CHANGES
                and (
                    is_source_path(self.root, self.name, Path(path))
                    or _is_prepared_current_path(
                        self.root, self.name, Path(path)
                    )
                )
            }
            if changes and not paths:
                continue
            if not changes and not self._needs_refresh():
                continue
            previous = self._state.fingerprint
            state = self.refresh()
            if state.fingerprint != previous:
                yield state

    async def run(
        self,
        *,
        stop_signal: asyncio.Event,
        interval_ms: float = DEFAULT_INTERVAL_MS,
        debounce_ms: float = DEFAULT_DEBOUNCE_MS,
    ) -> None:
        """Keep the current state synchronized until the caller stops watching."""

        async for _ in self.updates(
            stop_signal=stop_signal,
            interval_ms=interval_ms,
            debounce_ms=debounce_ms,
        ):
            pass

    def _needs_refresh(self) -> bool:
        try:
            return (
                load_current_version(self.root) != self._state.root_version
                or load_current_version(self.root, self.name)
                != self._state.home_version
                or scan_root_source(self.root)
                != load_version_source(
                    prepared_version_dir(self.root, self._state.root_version)
                )
                or scan_home_source(self.root, self.name)
                != load_version_source(
                    prepared_version_dir(
                        self.root,
                        self._state.home_version,
                        self.name,
                    )
                )
            )
        except (FileNotFoundError, TypeError, ValueError):
            return True


def _is_prepared_current_path(root: Path, name: str, path: Path) -> bool:
    candidate = path.resolve(strict=False)
    return candidate in {
        prepared_current_path(root).resolve(strict=False),
        prepared_current_path(root, name).resolve(strict=False),
    }
