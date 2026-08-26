"""Prepare and watch immutable agent source state."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
import logging
from pathlib import Path

from watchfiles import Change, awatch

from toolang.common.layout import AgentLayout
from toolang.common.version import base_toolang_version

from .state import AgentState
from .errors import StateDiagnostic, StatePreparationError
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
logger = logging.getLogger(__name__)
_RELEVANT_CHANGES = {Change.added, Change.modified, Change.deleted}


class StateWatcher:
    """Publish new immutable agent state when authored files change."""

    def __init__(self, layout: AgentLayout) -> None:
        self.layout = layout
        self._state: AgentState | None = None
        self._diagnostics: tuple[StateDiagnostic, ...] = ()
        self._toolang_version = base_toolang_version()
        self._refresh_lock = asyncio.Lock()

    def current(self) -> AgentState:
        """Return the latest immutable state snapshot."""

        if self._state is None:
            raise RuntimeError("state watcher has not been refreshed")
        return self._state

    def diagnostics(self) -> tuple[StateDiagnostic, ...]:
        """Return diagnostics for the latest rejected candidate, if any."""

        return self._diagnostics

    async def refresh(self, *, force: bool = False) -> AgentState:
        """Prepare a fresh state snapshot, optionally refreshing remote sources."""

        async with self._refresh_lock:
            try:
                candidate = await asyncio.to_thread(
                    prepare_agent_state,
                    self.layout,
                    toolang_version=self._toolang_version,
                    force=force,
                )
            except StatePreparationError as exc:
                self._diagnostics = exc.diagnostics
                if self._state is None:
                    raise
                logger.warning(
                    "watch.rejected agent=%s diagnostics=%s",
                    self.layout.name,
                    len(exc.diagnostics),
                )
                return self._state
            except Exception as exc:
                if self._state is None:
                    raise
                self._diagnostics = (
                    StateDiagnostic(
                        layer="state-composition",
                        module_kind="agent",
                        authored_path="",
                        line=None,
                        code="candidate-preparation",
                        message=str(exc) or type(exc).__name__,
                    ),
                )
                logger.warning(
                    "watch.rejected agent=%s diagnostics=1",
                    self.layout.name,
                )
                return self._state
            self._state = candidate
            self._diagnostics = ()
            return self._state

    async def updates(
        self,
        *,
        stop_signal: asyncio.Event,
        interval_ms: float = DEFAULT_INTERVAL_MS,
        debounce_ms: float = DEFAULT_DEBOUNCE_MS,
    ) -> AsyncIterator[AgentState]:
        if self._state is None:
            await self.refresh()
        logger.debug(
            "watch.started root=%s agent=%s interval_ms=%s debounce_ms=%s",
            self.layout.root,
            self.layout.name,
            int(interval_ms),
            int(debounce_ms),
        )
        timeout_ms = max(int(interval_ms), 50)
        async for changes in awatch(
            self.layout.root,
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
                    is_source_path(self.layout.root, self.layout.name, Path(path))
                    or _is_prepared_current_path(self.layout, Path(path))
                )
            }
            if changes and not paths:
                continue
            if not changes and not self._needs_refresh():
                continue
            previous = self.current().fingerprint
            state = await self.refresh()
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
        if self._state is None:
            return True
        state = self._state
        try:
            return (
                load_current_version(self.layout, "root") != state.root_version
                or load_current_version(self.layout, "home") != state.home_version
                or scan_root_source(self.layout.root)
                != load_version_source(
                    prepared_version_dir(
                        self.layout,
                        "root",
                        state.root_version,
                    )
                )
                or scan_home_source(self.layout.root, self.layout.name)
                != load_version_source(
                    prepared_version_dir(
                        self.layout,
                        "home",
                        state.home_version,
                    )
                )
            )
        except (FileNotFoundError, TypeError, ValueError):
            return True


def _is_prepared_current_path(layout: AgentLayout, path: Path) -> bool:
    candidate = path.resolve(strict=False)
    return candidate in {
        prepared_current_path(layout, "root").resolve(strict=False),
        prepared_current_path(layout, "home").resolve(strict=False),
    }
