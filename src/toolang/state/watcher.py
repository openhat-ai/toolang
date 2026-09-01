"""Prepare and watch immutable agent source state."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
import logging
from pathlib import Path

from watchfiles import Change, awatch

from toolang.common.layout import AgentLayout
from .state import (
    AgentState,
    StatePublication,
    publish_state_resources,
)
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
from .source import (
    SourceManifest,
    SourceObservation,
    home_source_manifest,
    observe_home_source,
    observe_root_source,
    root_source_manifest,
    source_path_scope,
)

DEFAULT_INTERVAL_MS = 1_000.0
DEFAULT_DEBOUNCE_MS = 500.0
logger = logging.getLogger(__name__)
_RELEVANT_CHANGES = {Change.added, Change.modified, Change.deleted}


@dataclass(frozen=True, slots=True)
class _CheckRequest:
    requested: bool
    force: bool
    invalidated_root: frozenset[str]
    invalidated_home: frozenset[str]
    future: asyncio.Future[StateRefresh]


@dataclass(frozen=True, slots=True)
class StateRefresh:
    """One completed watcher check and its exact last-valid result."""

    publication: StatePublication
    diagnostics: tuple[StateDiagnostic, ...] = ()


class StateWatcher:
    """Publish new immutable agent state when authored files change."""

    def __init__(
        self,
        layout: AgentLayout,
        *,
        allow_overrides: Mapping[str, tuple[str, ...] | None] | None = None,
        initial_state: AgentState | None = None,
    ) -> None:
        self.layout = layout
        unknown_overrides = sorted(
            set(allow_overrides or ()) - {"psyches", "skills", "services", "prompts"}
        )
        if unknown_overrides:
            raise ValueError(
                "unknown State allow override: " + ", ".join(unknown_overrides)
            )
        self._allow_overrides = dict(allow_overrides or {})
        self._publications: dict[str, StatePublication] = {}
        self._publication: StatePublication | None = None
        self._checked_root_observation: SourceObservation | None = None
        self._checked_home_observation: SourceObservation | None = None
        self._checked_root_source: SourceManifest | None = None
        self._checked_home_source: SourceManifest | None = None
        self._checked_layer_revisions: tuple[str | None, str | None] | None = None
        if initial_state is not None:
            self._publication = self._publish(initial_state)
            self._record_persisted_baseline(initial_state)
        else:
            try:
                state = load_agent_state(layout)
            except (FileNotFoundError, KeyError, TypeError, ValueError):
                pass
            else:
                self._publication = self._publish(state)
                self._record_persisted_baseline(state)
        self._diagnostics: tuple[StateDiagnostic, ...] = ()
        self._check_requests: deque[_CheckRequest] = deque()
        self._check_task: asyncio.Task[None] | None = None
        self._monitoring = False

    def current(self) -> StatePublication:
        """Return the latest immutable State publication."""

        if self._publication is None:
            raise RuntimeError("state watcher has not been refreshed")
        return self._publication

    def diagnostics(self) -> tuple[StateDiagnostic, ...]:
        """Return diagnostics for the latest rejected candidate, if any."""

        return self._diagnostics

    def load(self, revision: str) -> StatePublication:
        """Load one durable Agent State and derive the frozen startup policy."""

        return self._publish(load_agent_state(self.layout, revision))

    async def refresh(self, *, force: bool = False) -> StatePublication:
        """Request one serialized check and wait until that check completes."""

        return (await self._request_check(requested=True, force=force)).publication

    async def refresh_result(self, *, force: bool = False) -> StateRefresh:
        """Return one serialized check with diagnostics from that exact check."""

        return await self._request_check(requested=True, force=force)

    async def _request_check(
        self,
        *,
        requested: bool,
        force: bool = False,
        invalidated_root: frozenset[str] = frozenset(),
        invalidated_home: frozenset[str] = frozenset(),
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
    ) -> StateRefresh:
        """Run the sole candidate check and publication path."""

        try:
            root_observation = observe_root_source(self.layout.root)
            home_observation = observe_home_source(
                self.layout.root,
                self.layout.name,
            )
        except Exception as exc:
            if self._publication is None:
                raise
            self._diagnostics = (_candidate_diagnostic(exc),)
            logger.warning(
                "watch.rejected agent=%s diagnostics=1",
                self.layout.name,
            )
            return StateRefresh(self._publication, self._diagnostics)
        if (
            not requested
            and not invalidated_root
            and not invalidated_home
            and not self._observation_needs_check(
                root_observation=root_observation,
                home_observation=home_observation,
            )
        ):
            return StateRefresh(self.current(), self._diagnostics)
        if requested:
            invalidated_root = frozenset(item.path for item in root_observation.files)
            invalidated_home = frozenset(item.path for item in home_observation.files)
        try:
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
                invalidated=invalidated_home,
            )
        except Exception as exc:
            if self._publication is None:
                raise
            self._diagnostics = (_candidate_diagnostic(exc),)
            return StateRefresh(self._publication, self._diagnostics)
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
            self._diagnostics = ()
            return StateRefresh(self.current())
        try:
            candidate = await asyncio.to_thread(
                prepare_agent_state,
                self.layout,
                force=force,
            )
        except StatePreparationError as exc:
            self._record_checked_candidate(
                root_observation,
                home_observation,
                root_source,
                home_source,
            )
            self._diagnostics = exc.diagnostics
            if self._publication is None:
                raise
            logger.warning(
                "watch.rejected agent=%s diagnostics=%s",
                self.layout.name,
                len(exc.diagnostics),
            )
            return StateRefresh(self._publication, self._diagnostics)
        except Exception as exc:
            self._record_checked_candidate(
                root_observation,
                home_observation,
                root_source,
                home_source,
            )
            if self._publication is None:
                raise
            self._diagnostics = (_candidate_diagnostic(exc),)
            logger.warning(
                "watch.rejected agent=%s diagnostics=1",
                self.layout.name,
            )
            return StateRefresh(self._publication, self._diagnostics)
        self._publication = self._publish(candidate)
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
            raise ValueError("prepared State layers require portable source manifests")
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
        self._diagnostics = ()
        return StateRefresh(self._publication)

    async def updates(
        self,
        *,
        stop_signal: asyncio.Event,
        interval_ms: float = DEFAULT_INTERVAL_MS,
        debounce_ms: float = DEFAULT_DEBOUNCE_MS,
    ) -> AsyncIterator[StatePublication]:
        if self._monitoring:
            raise RuntimeError("State watcher is already monitoring")
        self._monitoring = True
        try:
            if self._publication is None:
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
                        source_path_scope(
                            self.layout.root,
                            self.layout.name,
                            Path(path),
                        )
                        is not None
                        or _is_state_current_path(self.layout, Path(path))
                    )
                }
                if changes and not paths:
                    continue
                invalidated_root: set[str] = set()
                invalidated_home: set[str] = set()
                for path in paths:
                    source = source_path_scope(
                        self.layout.root,
                        self.layout.name,
                        path,
                    )
                    if source is None:
                        continue
                    scope, relative = source
                    (invalidated_root if scope == "root" else invalidated_home).add(
                        relative
                    )
                previous = self.current().state.revision
                publication = (
                    await self._request_check(
                        requested=False,
                        invalidated_root=frozenset(invalidated_root),
                        invalidated_home=frozenset(invalidated_home),
                    )
                ).publication
                if publication.state.revision != previous:
                    yield publication
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
        if self._publication is None:
            return True
        state = self._publication.state
        try:
            return (
                load_current_agent_revision(self.layout) != state.revision
                or _current_layer_revisions(self.layout)
                != self._checked_layer_revisions
                or root_observation != self._checked_root_observation
                or home_observation != self._checked_home_observation
            )
        except (FileNotFoundError, TypeError, ValueError):
            return True

    def _manifest_needs_check(
        self,
        *,
        root_source: SourceManifest,
        home_source: SourceManifest,
    ) -> bool:
        if self._publication is None:
            return True
        state = self._publication.state
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

    def _publish(self, state: AgentState) -> StatePublication:
        existing = self._publications.get(state.revision)
        if existing is not None:
            return existing
        publication = publish_state_resources(
            state,
            agent_name=self.layout.name,
            allow_overrides=self._allow_overrides,
        )
        self._publications[state.revision] = publication
        return publication

    def _record_persisted_baseline(self, state: AgentState) -> None:
        """Load portable manifests without reconstructing the supplied State."""

        try:
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
        except (FileNotFoundError, KeyError, TypeError, ValueError):
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


def _consume_future_exception(future: asyncio.Future[StateRefresh]) -> None:
    if not future.cancelled():
        future.exception()
