"""Prepare and watch immutable agent source state."""

from __future__ import annotations

from toolang.catalog import source as source_owner

import asyncio
from collections import deque
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
import logging
from pathlib import Path
from weakref import WeakValueDictionary

from toolang.common.watching import directory_changes

from toolang.common.layout import AgentLayout
from toolang.common.progress import ProgressSink
from .config import normalize_cap_overrides
from .state import AgentState
from .errors import StateDiagnostic, StatePreparationError
from .types import StateSyncError, StateSyncResult
from .cache import (
    LayerScope,
    AGENT_STATE_FORMAT,
    load_current_agent_revision,
    load_current_revision,
    load_layer_source,
)
from .prepare import load_agent_state, prepare_agent_state
from .source_context import source_config_context
from .source import (
    SourceManifest,
    SourceObservation,
    ROOT_WORKSPACE_SOURCE,
    home_source_manifest,
    observe_home_source,
    observe_root_source,
    root_source_manifest,
    source_path_scope,
)

DEFAULT_INTERVAL_MS = 1_000.0
DEFAULT_DEBOUNCE_MS = 500.0
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _CheckRequest:
    requested: bool
    force: bool
    invalidated_root: frozenset[str]
    invalidated_home: frozenset[str]
    future: asyncio.Future[StateRefresh]
    progress: ProgressSink | None


@dataclass(frozen=True, slots=True)
class _CheckFailure:
    """Keep a rejected check's classification and diagnostics together."""

    error: StateSyncError
    diagnostics: tuple[StateDiagnostic, ...]

    @classmethod
    def from_exception(cls, exc: Exception) -> _CheckFailure:
        return cls(
            "io_error" if isinstance(exc, OSError) else "state_rejected",
            exc.diagnostics
            if isinstance(exc, StatePreparationError)
            else (_candidate_diagnostic(exc),),
        )


@dataclass(frozen=True, slots=True)
class StateRefresh:
    """One completed watcher check and its exact last-valid result."""

    state: AgentState
    failure: _CheckFailure | None = None

    @property
    def diagnostics(self) -> tuple[StateDiagnostic, ...]:
        return (
            self.failure.diagnostics
            if self.failure is not None
            else _source_diagnostics(self.state)
        )

    @property
    def error(self) -> StateSyncError | None:
        return self.failure.error if self.failure is not None else None


class StateWatcher:
    """Publish new immutable agent state when authored files change."""

    def __init__(
        self,
        layout: AgentLayout,
        *,
        allow_overrides: Mapping[str, tuple[str, ...] | None] | None = None,
        initial_state: AgentState | None = None,
        workspace_additions: Mapping[str, str] | None = None,
    ) -> None:
        self.layout = layout
        self._workspace_additions = dict(workspace_additions or {})
        self._allow_overrides = normalize_cap_overrides(allow_overrides)
        # Active Runs own their snapshots; history can reload released revisions.
        self._states: WeakValueDictionary[str, AgentState] = WeakValueDictionary()
        self._state: AgentState | None = None
        self._checked_root_observation: SourceObservation | None = None
        self._checked_home_observation: SourceObservation | None = None
        self._checked_root_source: SourceManifest | None = None
        self._checked_home_source: SourceManifest | None = None
        self._checked_layer_revisions: tuple[str | None, str | None] | None = None
        self._checked_agent_revision: str | None = None
        if (
            initial_state is not None
            and initial_state.allow_overrides == self._allow_overrides
            and initial_state.workspace_additions == self._workspace_additions
        ):
            self._state = self._remember(initial_state)
            self._record_persisted_baseline(initial_state)
        else:
            try:
                state = load_agent_state(layout)
            except (OSError, KeyError, TypeError, ValueError):
                pass
            else:
                if (
                    state.allow_overrides == self._allow_overrides
                    and state.workspace_additions == self._workspace_additions
                ):
                    self._state = self._remember(state)
                    self._record_persisted_baseline(state)
        self._failure: _CheckFailure | None = None
        self._check_requests: deque[_CheckRequest] = deque()
        self._check_task: asyncio.Task[None] | None = None
        self._monitoring = False
        self._checked_origins: dict[str, Path] | None = None

    def current(self) -> AgentState:
        """Return the latest immutable State publication."""

        if self._state is None:
            raise RuntimeError("state watcher has not been refreshed")
        return self._state

    def diagnostics(self) -> tuple[StateDiagnostic, ...]:
        """Return diagnostics for the latest rejected candidate, if any."""

        if self._failure is not None:
            return self._failure.diagnostics
        if self._state is None:
            return ()
        return _source_diagnostics(self._state)

    def load(self, revision: str) -> AgentState:
        """Load the exact State identified by a persisted composition."""

        cached = self._states.get(revision)
        return (
            cached
            if cached is not None
            else self._remember(load_agent_state(self.layout, revision))
        )

    async def refresh(
        self, *, force: bool = False, progress: ProgressSink | None = None
    ) -> AgentState:
        """Request one serialized check and wait until that check completes."""

        return (
            await self._request_check(requested=True, force=force, progress=progress)
        ).state

    async def refresh_result(
        self, *, force: bool = False, progress: ProgressSink | None = None
    ) -> StateRefresh:
        """Return one serialized check with diagnostics from that exact check."""

        return await self._request_check(requested=True, force=force, progress=progress)

    async def sync(self) -> StateSyncResult:
        """Check once; callers keep authored sources unchanged until return."""

        state: AgentState | None = None
        try:
            result = await self.refresh_result(force=False)
            state = result.state
            failure = result.failure
            if failure is None:
                return StateSyncResult(state.revision, state.files)
        except Exception as exc:
            failure = _CheckFailure.from_exception(exc)
        message = "; ".join(item.message for item in failure.diagnostics)
        return StateSyncResult(
            revision=state.revision if state is not None else None,
            files=(),
            error=failure.error,
            message=message,
        )

    async def _request_check(
        self,
        *,
        requested: bool,
        force: bool = False,
        invalidated_root: frozenset[str] = frozenset(),
        invalidated_home: frozenset[str] = frozenset(),
        progress: ProgressSink | None = None,
    ) -> StateRefresh:
        loop = asyncio.get_running_loop()
        task = self._check_task
        if task is not None and not task.done() and task.get_loop() is not loop:
            raise RuntimeError("State watcher check is running on another event loop")
        future = loop.create_future()
        self._check_requests.append(
            _CheckRequest(
                requested=requested,
                force=force,
                invalidated_root=invalidated_root,
                invalidated_home=invalidated_home,
                future=future,
                progress=progress,
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
                        invalidated_root=request.invalidated_root,
                        invalidated_home=request.invalidated_home,
                        progress=request.progress,
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
        invalidated_root: frozenset[str] = frozenset(),
        invalidated_home: frozenset[str] = frozenset(),
        progress: ProgressSink | None = None,
    ) -> StateRefresh:
        """Run the sole candidate check and publication path."""

        try:
            context = source_config_context(self.layout)
            origins = dict(context.origins) if context else {}
            if self._checked_origins is not None and origins != self._checked_origins:
                requested = True
            self._checked_origins = origins
            root_observation = observe_root_source(self.layout.agent_root)
            home_observation = observe_home_source(
                self.layout.agent_root,
                self.layout.name,
                root_config=self.layout.root_config,
                workspace_key=self.layout.config_key,
            )
        except Exception as exc:
            return self._reject_check(exc)
        if (
            not requested
            and not invalidated_root
            and not invalidated_home
            and not self._observation_needs_check(
                root_observation=root_observation,
                home_observation=home_observation,
            )
        ):
            return StateRefresh(self.current(), self._failure)
        if requested:
            invalidated_root = frozenset(item.path for item in root_observation.files)
            invalidated_home = frozenset(item.path for item in home_observation.files)
        try:
            if (
                context is not None
                and self.layout.runtime_config is not None
                and context.origins != self.layout.runtime_config.origins
            ):
                invalidated_home = invalidated_home | {ROOT_WORKSPACE_SOURCE}
            root_source = root_source_manifest(
                root_observation,
                previous_observation=self._checked_root_observation,
                previous_manifest=self._checked_root_source,
                invalidated=invalidated_root,
            )
            home_source = home_source_manifest(
                home_observation,
                previous_observation=self._checked_home_observation,
                previous_manifest=self._checked_home_source,
                context=context,
                invalidated=invalidated_home,
            )
        except Exception as exc:
            return self._reject_check(exc)
        if (
            not requested
            and not force
            and not self._manifest_needs_check(
                root_source=root_source,
                home_source=home_source,
            )
        ):
            self._record_checked_candidate(
                root_observation,
                home_observation,
                root_source,
                home_source,
            )
            return StateRefresh(self.current(), self._failure)
        try:
            candidate = await asyncio.to_thread(
                prepare_agent_state,
                self.layout,
                force=force,
                allow_overrides=self._allow_overrides,
                previous=self._state,
                workspace_additions=self._workspace_additions,
                progress=progress,
            )
            loaded_root_source = load_layer_source(
                self.layout,
                "root",
                candidate.root_revision,
            )
            loaded_home_source = load_layer_source(
                self.layout,
                "home",
                candidate.home_revision,
            )
            if not isinstance(loaded_root_source, SourceManifest) or not isinstance(
                loaded_home_source, SourceManifest
            ):
                raise ValueError(
                    "prepared State layers require portable source manifests"
                )
        except Exception as exc:
            self._record_checked_candidate(
                root_observation,
                home_observation,
                root_source,
                home_source,
            )
            return self._reject_check(exc)
        self._state = self._remember(candidate)
        # Retain the observations that led to preparation. A source change after
        # preparation returned must remain visible to the next watcher check.
        self._checked_root_observation = root_observation
        self._checked_home_observation = home_observation
        self._checked_root_source = loaded_root_source
        self._checked_home_source = loaded_home_source
        self._checked_layer_revisions = (
            candidate.root_revision,
            candidate.home_revision,
        )
        self._failure = None
        try:
            self._checked_agent_revision = load_current_agent_revision(self.layout)
        except (OSError, TypeError, ValueError):
            self._checked_agent_revision = None
        return StateRefresh(self._state)

    def _reject_check(self, exc: Exception) -> StateRefresh:
        self._failure = _CheckFailure.from_exception(exc)
        if self._state is None:
            raise exc
        logger.warning(
            "watch.rejected agent=%s diagnostics=%s",
            self.layout.name,
            len(self._failure.diagnostics),
        )
        return StateRefresh(self._state, self._failure)

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
                "watch.started place=%s agent=%s interval_ms=%s debounce_ms=%s",
                self.layout.agent_root,
                self.layout.name,
                int(interval_ms),
                int(debounce_ms),
            )
            async for paths in directory_changes(
                lambda: self.layout.watch_directories(AGENT_STATE_FORMAT),
                stop_signal=stop_signal,
                interval_ms=interval_ms,
                debounce_ms=debounce_ms,
            ):
                invalidated_root: set[str] = set()
                invalidated_home: set[str] = set()
                for path in paths:
                    if path in {
                        self.layout.source_receipt,
                        self.layout.root_source_receipt,
                    }:
                        invalidated_root.add("config.toml")
                        invalidated_home.add(ROOT_WORKSPACE_SOURCE)
                    if path == self.layout.root_config:
                        invalidated_home.add(ROOT_WORKSPACE_SOURCE)
                    source = source_path_scope(
                        self.layout.agent_root,
                        self.layout.name,
                        path,
                    )
                    if source is None:
                        continue
                    scope, relative = source
                    (invalidated_root if scope == "root" else invalidated_home).add(
                        relative
                    )
                previous = self.current()
                state = (
                    await self._request_check(
                        requested=False,
                        invalidated_root=frozenset(invalidated_root),
                        invalidated_home=frozenset(invalidated_home),
                    )
                ).state
                if state is not previous:
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

    def _observation_needs_check(
        self,
        *,
        root_observation: SourceObservation,
        home_observation: SourceObservation,
    ) -> bool:
        if self._state is None:
            return True
        state = self._state
        try:
            return (
                not self._sources_reusable(state)
                or bool(self._checked_root_source and self._checked_root_source.issues)
                or bool(self._checked_home_source and self._checked_home_source.issues)
                or load_current_agent_revision(self.layout)
                != self._checked_agent_revision
                or _current_layer_revisions(self.layout)
                != self._checked_layer_revisions
                or root_observation != self._checked_root_observation
                or home_observation != self._checked_home_observation
            )
        except (OSError, TypeError, ValueError):
            return True

    def _manifest_needs_check(
        self,
        *,
        root_source: SourceManifest,
        home_source: SourceManifest,
    ) -> bool:
        if self._state is None:
            return True
        state = self._state
        try:
            return (
                self._checked_home_observation is None
                or not self._sources_reusable(state)
                or load_current_agent_revision(self.layout)
                != self._checked_agent_revision
                or _current_layer_revisions(self.layout)
                != self._checked_layer_revisions
                or root_source != self._checked_root_source
                or home_source != self._checked_home_source
            )
        except (OSError, TypeError, ValueError):
            return True

    @staticmethod
    def _sources_reusable(state: AgentState) -> bool:
        return all(
            source_owner.reusable(kind, declared, resolved)
            for kind, declared, resolved in state.source_requests
        )

    def _remember(self, state: AgentState) -> AgentState:
        existing = self._states.get(state.revision)
        if existing is not None:
            return existing
        self._states[state.revision] = state
        return state

    def _record_persisted_baseline(self, state: AgentState) -> None:
        """Load portable manifests without reconstructing the supplied State."""

        try:
            self._checked_agent_revision = load_current_agent_revision(self.layout)
            root_source = load_layer_source(
                self.layout,
                "root",
                state.root_revision,
            )
            home_source = load_layer_source(
                self.layout,
                "home",
                state.home_revision,
            )
        except (OSError, KeyError, TypeError, ValueError):
            return
        if not isinstance(root_source, SourceManifest) or not isinstance(
            home_source, SourceManifest
        ):
            return
        self._checked_root_source = root_source
        self._checked_home_source = home_source
        self._checked_layer_revisions = (
            state.root_revision,
            state.home_revision,
        )

    def _record_checked_candidate(
        self,
        root_observation: SourceObservation,
        home_observation: SourceObservation,
        root_source: SourceManifest,
        home_source: SourceManifest,
    ) -> None:
        self._checked_root_observation = root_observation
        self._checked_home_observation = home_observation
        self._checked_root_source = root_source
        self._checked_home_source = home_source
        self._checked_layer_revisions = _current_layer_revisions(self.layout)


def _current_layer_revisions(
    layout: AgentLayout,
) -> tuple[str | None, str | None]:
    def load(scope: LayerScope) -> str | None:
        try:
            return load_current_revision(layout, scope)
        except (OSError, TypeError, ValueError):
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


def _source_diagnostics(state: AgentState) -> tuple[StateDiagnostic, ...]:
    return tuple(
        StateDiagnostic(
            layer="runnable-extension"
            if file.code in {"invalid-runnable-export", "invalid-runnable-filename"}
            else "program"
            if file.code == "invalid-program"
            else "state-composition",
            module_kind="runnable" if file.key.startswith("runnables/") else "agent",
            authored_path=file.key,
            line=file.line,
            code=file.code or "invalid-source",
            message=file.message or "Invalid source",
        )
        for file in state.files
        if not file.accepted
    )


def _consume_future_exception(future: asyncio.Future[StateRefresh]) -> None:
    if not future.cancelled():
        future.exception()
