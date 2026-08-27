"""Prepare and watch immutable agent source state."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass
import logging
from pathlib import Path

from watchfiles import Change, awatch

from toolang.common.layout import AgentLayout
from .state import AgentState
from .errors import StateDiagnostic, StatePreparationError
from .cache import (
    LayerScope,
    agent_current_path,
    layer_current_path,
    load_current_agent_revision,
    load_current_revision,
    load_layer_source,
)
from .prepare import load_agent_state, prepare_agent_state
from .source import SourceTree, scan_home_source, scan_root_source
from toolang.state.source import is_source_path

DEFAULT_INTERVAL_MS = 1_000.0
DEFAULT_DEBOUNCE_MS = 500.0
logger = logging.getLogger(__name__)
_RELEVANT_CHANGES = {Change.added, Change.modified, Change.deleted}


@dataclass(frozen=True, slots=True)
class _CheckRequest:
    requested: bool
    force: bool
    future: asyncio.Future[AgentState]


class StateWatcher:
    """Publish new immutable agent state when authored files change."""

    def __init__(self, layout: AgentLayout) -> None:
        self.layout = layout
        self._state: AgentState | None = None
        self._checked_root_source: SourceTree | None = None
        self._checked_home_source: SourceTree | None = None
        self._checked_layer_revisions: tuple[str | None, str | None] | None = None
        try:
            state = load_agent_state(layout)
            root_source = load_layer_source(
                layout,
                "root",
                state.root_revision,
            )
            home_source = load_layer_source(
                layout,
                "home",
                state.home_revision,
            )
        except (FileNotFoundError, KeyError, TypeError, ValueError):
            pass
        else:
            self._state = state
            self._checked_root_source = root_source
            self._checked_home_source = home_source
            self._checked_layer_revisions = (
                state.root_revision,
                state.home_revision,
            )
        self._diagnostics: tuple[StateDiagnostic, ...] = ()
        self._check_requests: deque[_CheckRequest] = deque()
        self._check_task: asyncio.Task[None] | None = None
        self._monitoring = False

    def current(self) -> AgentState:
        """Return the latest immutable state snapshot."""

        if self._state is None:
            raise RuntimeError("state watcher has not been refreshed")
        return self._state

    def diagnostics(self) -> tuple[StateDiagnostic, ...]:
        """Return diagnostics for the latest rejected candidate, if any."""

        return self._diagnostics

    def load(self, revision: str) -> AgentState:
        """Load a durable Agent State revision without publishing it."""

        return load_agent_state(self.layout, revision)

    async def refresh(self, *, force: bool = False) -> AgentState:
        """Request one serialized check and wait until that check completes."""

        return await self._request_check(requested=True, force=force)

    async def _request_check(
        self,
        *,
        requested: bool,
        force: bool = False,
    ) -> AgentState:
        loop = asyncio.get_running_loop()
        task = self._check_task
        if task is not None and not task.done() and task.get_loop() is not loop:
            raise RuntimeError("State watcher check is running on another event loop")
        future = loop.create_future()
        self._check_requests.append(
            _CheckRequest(
                requested=requested,
                force=force,
                future=future,
            )
        )
        if task is None or task.done():
            self._check_task = asyncio.create_task(
                self._run_checks(),
                name=f"toolang-state-check-{self.layout.name}",
            )
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            future.add_done_callback(_consume_future_exception)
            raise

    async def _run_checks(self) -> None:
        """Complete queued requests on the sole State checking task."""

        try:
            while self._check_requests:
                request = self._check_requests.popleft()
                try:
                    state = await self._perform_check(
                        requested=request.requested,
                        force=request.force,
                    )
                except asyncio.CancelledError:
                    request.future.cancel()
                    raise
                except Exception as exc:
                    if not request.future.done():
                        request.future.set_exception(exc)
                else:
                    if not request.future.done():
                        request.future.set_result(state)
        finally:
            while self._check_requests:
                self._check_requests.popleft().future.cancel()
            self._check_task = None

    async def _perform_check(
        self,
        *,
        requested: bool,
        force: bool = False,
    ) -> AgentState:
        """Run the sole candidate check and publication path."""

        try:
            root_source = scan_root_source(self.layout.root)
            home_source = scan_home_source(self.layout.root, self.layout.name)
        except Exception as exc:
            if self._state is None:
                raise
            self._diagnostics = (_candidate_diagnostic(exc),)
            logger.warning(
                "watch.rejected agent=%s diagnostics=1",
                self.layout.name,
            )
            return self._state
        if not requested and not self._needs_check(
            root_source=root_source,
            home_source=home_source,
        ):
            return self.current()
        try:
            candidate = await asyncio.to_thread(
                prepare_agent_state,
                self.layout,
                force=force,
            )
        except StatePreparationError as exc:
            self._record_checked_candidate(root_source, home_source)
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
            self._record_checked_candidate(root_source, home_source)
            if self._state is None:
                raise
            self._diagnostics = (_candidate_diagnostic(exc),)
            logger.warning(
                "watch.rejected agent=%s diagnostics=1",
                self.layout.name,
            )
            return self._state
        self._state = candidate
        self._checked_root_source = load_layer_source(
            self.layout,
            "root",
            candidate.root_revision,
        )
        self._checked_home_source = load_layer_source(
            self.layout,
            "home",
            candidate.home_revision,
        )
        self._checked_layer_revisions = (
            candidate.root_revision,
            candidate.home_revision,
        )
        self._diagnostics = ()
        return self._state

    async def updates(
        self,
        *,
        stop_signal: asyncio.Event,
        interval_ms: float = DEFAULT_INTERVAL_MS,
        debounce_ms: float = DEFAULT_DEBOUNCE_MS,
    ) -> AsyncIterator[AgentState]:
        if self._monitoring:
            raise RuntimeError("State watcher is already monitoring")
        self._monitoring = True
        try:
            if self._state is None:
                await self._request_check(requested=True)
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
                        or _is_state_current_path(self.layout, Path(path))
                    )
                }
                if changes and not paths:
                    continue
                previous = self.current().revision
                state = await self._request_check(requested=False)
                if state.revision != previous:
                    yield state
        finally:
            self._monitoring = False

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

    def _needs_check(
        self,
        *,
        root_source: SourceTree,
        home_source: SourceTree,
    ) -> bool:
        if self._state is None:
            return True
        state = self._state
        try:
            return (
                load_current_agent_revision(self.layout) != state.revision
                or _current_layer_revisions(self.layout)
                != self._checked_layer_revisions
                or root_source != self._checked_root_source
                or home_source != self._checked_home_source
            )
        except (FileNotFoundError, TypeError, ValueError):
            return True

    def _record_checked_candidate(
        self,
        root_source: SourceTree,
        home_source: SourceTree,
    ) -> None:
        self._checked_root_source = root_source
        self._checked_home_source = home_source
        self._checked_layer_revisions = _current_layer_revisions(self.layout)


def _is_state_current_path(layout: AgentLayout, path: Path) -> bool:
    candidate = path.resolve(strict=False)
    return candidate in {
        layer_current_path(layout, "root").resolve(strict=False),
        layer_current_path(layout, "home").resolve(strict=False),
        agent_current_path(layout).resolve(strict=False),
    }


def _current_layer_revisions(
    layout: AgentLayout,
) -> tuple[str | None, str | None]:
    def load(scope: LayerScope) -> str | None:
        try:
            return load_current_revision(layout, scope)
        except (FileNotFoundError, TypeError, ValueError):
            return None

    return load("root"), load("home")


def _candidate_diagnostic(exc: Exception) -> StateDiagnostic:
    return StateDiagnostic(
        layer="state-composition",
        module_kind="agent",
        authored_path="",
        line=None,
        code="candidate-preparation",
        message=str(exc) or type(exc).__name__,
    )


def _consume_future_exception(future: asyncio.Future[AgentState]) -> None:
    if not future.cancelled():
        future.exception()
