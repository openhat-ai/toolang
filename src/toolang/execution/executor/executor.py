"""Run acceptance, control, and recursive execution."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Generator, Mapping, Sequence
from dataclasses import dataclass, field, replace
import logging
from decimal import Decimal, InvalidOperation
import threading
import time
from typing import Any, Literal, cast

from toolang.base.types.model import ModelInfo, ModelTarget, Provider
from toolang.base.types.policy import AgentCeiling, RunBindings, RunLimits
from toolang.base.types.run import ModelUsage
from toolang.base.types.message import Message, TextPart
from toolang.common.ids import IdIssuer
from toolang.common.time import utc_now
from toolang.lang.ast import (
    AgicDecl,
    FlowDecl,
    FlowStmt,
    Parameter,
    Program,
    RepeatStmt,
)
from toolang.lang.input import (
    RunnableInput,
    resolve_runnable_input,
    validate_value,
)
from toolang.lang.includes import resolve_file_include
from toolang.lang.types import Value
from toolang.plugin.models.config import (
    ProviderConfig,
    parse_default_models,
    parse_model_aliases,
)
from toolang.state.state import AgentState, state_program_module
from toolang.setup import AgentSetup

from ..accounting import selected_usd_cost
from ..calls import IncludeResolver, resolve_restart_request, resolve_run_request
from ..events import RunBegin, RunEnd, RunEvent, RunTracer, StepBegin
from ..records import (
    PreparationControlPayload,
    RunControlRecord,
    RunRecord,
    StepRecord,
)
from ..store import RunStore
from ..schemas import RerunRequest, RetryRequest, RunRequest
from ..types import (
    ControlTiming,
    AgentResources,
    ControlRef,
    ExecutionError,
    Local as RecordLocal,
    ControlKind,
    StepPath,
    Pointer,
    ModelStepNoted,
    Occurrence,
    OccurrencePosition,
    TypedPointer,
)
from ..runnables import parse_runnable_ref, resolve_runnable, resolve_state_runnable
from .common import (
    BoundRun,
    EventEmitter,
    Local,
    _ExecutionFailed,
    _StepFailed,
    control_text,
    initial_locals,
    value_parts,
    value_text,
)
from .resources import (
    apply_agent_ceiling,
    resolve_agent_resources,
    resolve_runnable_resources,
    snapshot_model_selection,
    validate_model_binding,
)
from .limits import (
    _ModelAccounting,
    _RunLimitExceeded,
    _RunLimitState,
    _model_accounting,
)
from ._persist import _PersistSink

_LOGGER = logging.getLogger(__name__)
_CONTROL_POLL_INTERVAL = 0.05

SetupSource = Callable[[], AgentSetup]
StateSource = Callable[[], AgentState]
StateLoad = Callable[[str], AgentState]
IncludeSource = Callable[[AgentSetup], IncludeResolver]


class _RunCanceled(asyncio.CancelledError):
    def __init__(self, control: RunControlRecord) -> None:
        super().__init__(control_text(control) or "canceled")
        self.control = control


@dataclass(slots=True)
class _ActiveRun:
    task: asyncio.Task[RunRecord]
    tracer: RunTracer | None
    loop: asyncio.AbstractEventLoop = field(repr=False)
    controls: dict[str, dict[int, RunControlRecord]] = field(
        default_factory=dict,
        repr=False,
    )
    event_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    ended: set[str] = field(default_factory=set, repr=False)


@dataclass(frozen=True, slots=True)
class RunSpec:
    """Immutable inputs required to execute one runnable."""

    setup: AgentSetup
    state: AgentState
    thread: str
    bindings: RunBindings
    limits: RunLimits
    ceilings: tuple[AgentCeiling, ...] = ()
    input: RunnableInput = RunnableInput()


@dataclass(frozen=True, slots=True)
class LocalRunHandle(Awaitable[RunRecord]):
    """One locally accepted run that can be controlled and awaited."""

    run_id: str
    executor: RunExecutor = field(repr=False)
    task: asyncio.Task[RunRecord] = field(repr=False)

    def cancel(
        self,
        *,
        timing: ControlTiming = "immediate",
        request_id: str | None = None,
        reason: str | None = None,
    ) -> RunControlRecord:
        """Persist a cancel control for this run."""

        return self.executor.cancel(
            run_id=self.run_id,
            timing=timing,
            request_id=request_id,
            reason=reason,
        )

    def steer(
        self,
        message: Message,
        *,
        timing: ControlTiming = "next_step",
        request_id: str | None = None,
    ) -> RunControlRecord:
        """Persist a steer control for this run."""

        return self.executor.steer(
            run_id=self.run_id,
            message=message,
            timing=timing,
            request_id=request_id,
        )

    def cancel_control(self, index: int) -> RunControlRecord:
        """Revoke one pending steer or cancel control for this run."""

        return self.executor.cancel_control(run_id=self.run_id, index=index)

    def __await__(self) -> Generator[Any, None, RunRecord]:
        return self._wait().__await__()

    async def _wait(self) -> RunRecord:
        try:
            return await asyncio.shield(self.task)
        except asyncio.CancelledError:
            if self.task.cancelled():
                record = self.executor.store.get_run(run_id=self.run_id)
                if record is not None and record.status not in {"pending", "running"}:
                    return record
            raise


class RunExecutor:
    """Accept, control, and execute runs against durable execution truth."""

    def __init__(
        self,
        store: RunStore,
        ids: IdIssuer,
        *,
        setup: SetupSource | None = None,
        state: StateSource | None = None,
        load_state: StateLoad | None = None,
        include: IncludeSource | None = None,
    ) -> None:
        if (setup is None) != (state is None) or (setup is None) != (
            load_state is None
        ):
            raise TypeError(
                "run request execution requires setup, state, and load_state together"
            )
        self.store = store
        self.ids = ids
        self._setup = setup
        self._state = state
        self._load_state = load_state
        self._include = include
        self._persist = _PersistSink(self.store)
        self._control_poll_interval = _CONTROL_POLL_INTERVAL
        self._active: dict[str, _ActiveRun] = {}
        self._tasks: dict[asyncio.Task[RunRecord], tuple[str, _ActiveRun]] = {}
        self._active_lock = threading.Lock()
        self._monitor_task: asyncio.Task[None] | None = None
        self._control_revision = self.store.latest_run_control_revision()
        self._stopped = False

    def start(self) -> None:
        """Start this executor lifecycle."""

        self._require_available()

    def run(
        self,
        spec: RunSpec | RunRequest,
        *,
        run_id: str | None = None,
        request_id: str | None = None,
        tracer: RunTracer | None = None,
    ) -> LocalRunHandle:
        """Accept one top-level run and immediately return its local handle."""

        self._require_available()
        if isinstance(spec, RunRequest):
            if run_id is not None or request_id is not None:
                raise ValueError(
                    "resolved run identity cannot override a caller run request"
                )
            setup, state = self._current_snapshots()
            request_id = spec.request_id
            spec = resolve_run_request(
                spec,
                setup=setup,
                state=state,
                include=self._include_resolver(setup),
            )
        loop = asyncio.get_running_loop()
        sandbox = _setup_sandbox(spec.setup)
        executable, input, agent_resources, resources = _prepare_run_spec(spec)
        if not isinstance(spec.limits, RunLimits):
            raise TypeError("run limits must be RunLimits")
        bound = _bind_run(
            spec,
            executable=executable,
            run_id=run_id or self.ids.issue_run(),
            input=input,
            agent_resources=agent_resources,
            resources=resources,
        )
        self.store.accept_run(
            run_id=bound.run_id,
            parent=None,
            thread=bound.thread,
            resources=resources,
            limits=bound.limits,
            state=bound.state.revision,
            runnable=_bound_runnable(bound),
            model=_bound_model(bound),
            locals=bound.control_locals,
            sandbox=sandbox,
            occurrence=bound.occurrence,
            request_id=request_id,
            created_at=bound.created_at,
        )
        return self._launch(bound, executable, loop=loop, tracer=tracer)

    def rerun(
        self,
        source: str | RerunRequest,
        *,
        setup: AgentSetup | None = None,
        state: AgentState | None = None,
        ceiling: AgentCeiling = AgentCeiling(),
        model: str | None = None,
        limits: RunLimits | None = None,
        run_id: str | None = None,
        request_id: str | None = None,
        tracer: RunTracer | None = None,
    ) -> LocalRunHandle:
        """Start a new root run from one visible source run's invocation."""

        self._require_available()
        if isinstance(source, RerunRequest):
            if setup is not None or state is not None or request_id is not None:
                raise ValueError(
                    "resolved rerun inputs cannot override a caller rerun request"
                )
            request = source
            setup, state = self._current_snapshots()
            resolved = resolve_restart_request(request, setup=setup, state=state)
            source = request.source
            setup = resolved.setup
            state = resolved.state
            ceiling = resolved.ceiling
            model = resolved.model
            limits = resolved.limits
            request_id = request.request_id
        if setup is None or state is None:
            raise TypeError("rerun requires resolved setup and state")
        loop = asyncio.get_running_loop()
        sandbox = _setup_sandbox(setup)
        spec = self._source_spec(
            source,
            setup=setup,
            state=state,
            ceiling=ceiling,
            model=model,
            limits=limits if limits is not None else setup.limits,
        )
        executable, input, agent_resources, resources = _prepare_run_spec(spec)
        bound = _bind_run(
            spec,
            executable=executable,
            run_id=run_id or self.ids.issue_run(),
            input=input,
            agent_resources=agent_resources,
            resources=resources,
        )
        self.store.accept_run(
            run_id=bound.run_id,
            parent=None,
            thread=bound.thread,
            resources=resources,
            limits=bound.limits,
            state=bound.state.revision,
            runnable=_bound_runnable(bound),
            model=_bound_model(bound),
            locals=bound.control_locals,
            sandbox=sandbox,
            occurrence=bound.occurrence,
            request_id=request_id,
            created_at=bound.created_at,
            kind="rerun",
            source=source,
        )
        return self._launch(bound, executable, loop=loop, tracer=tracer)

    def retry(
        self,
        run_id: str | RetryRequest,
        *,
        setup: AgentSetup | None = None,
        state: AgentState | None = None,
        anchor: StepPath | str | None = None,
        ceiling: AgentCeiling = AgentCeiling(),
        model: str | None = None,
        limits: RunLimits | None = None,
        request_id: str | None = None,
        tracer: RunTracer | None = None,
    ) -> LocalRunHandle:
        """Reopen one terminal root run from a durable step boundary."""

        self._require_available()
        if isinstance(run_id, RetryRequest):
            if setup is not None or state is not None or request_id is not None:
                raise ValueError(
                    "resolved retry inputs cannot override a caller retry request"
                )
            request = run_id
            setup = self._current_setup()
            state = self._recorded_state(request.source)
            resolved = resolve_restart_request(request, setup=setup, state=state)
            run_id = request.source
            setup = resolved.setup
            state = resolved.state
            anchor = request.anchor
            ceiling = resolved.ceiling
            model = resolved.model
            limits = resolved.limits
            request_id = request.request_id
        if setup is None or state is None:
            raise TypeError("retry requires resolved setup and state")
        loop = asyncio.get_running_loop()
        sandbox = _setup_sandbox(setup)
        self._require_retry_compatible(run_id, state, sandbox=sandbox)
        spec = self._source_spec(
            run_id,
            setup=setup,
            state=state,
            ceiling=ceiling,
            model=model,
            limits=limits if limits is not None else setup.limits,
        )
        executable, input, agent_resources, resources = _prepare_run_spec(spec)
        bound = _bind_run(
            spec,
            executable=executable,
            run_id=run_id,
            input=input,
            agent_resources=agent_resources,
            resources=resources,
        )
        _reopened, control, _trimmed = self.store.accept_retry(
            run_id=run_id,
            anchor=StepPath.parse(anchor) if anchor is not None else None,
            resources=resources,
            limits=bound.limits,
            state=bound.state.revision,
            runnable=_bound_runnable(bound),
            model=_bound_model(bound),
            locals=bound.control_locals,
            sandbox=sandbox,
            request_id=request_id,
            created_at=bound.created_at,
        )
        bound = replace(bound, control_index=control.index)
        return self._launch(
            bound,
            executable,
            loop=loop,
            tracer=tracer,
            retry=control,
        )

    def _current_setup(self) -> AgentSetup:
        source = self._setup
        if source is None:
            raise RuntimeError("run executor has no request snapshot sources")
        return source()

    def _current_snapshots(self) -> tuple[AgentSetup, AgentState]:
        source = self._state
        if source is None:
            raise RuntimeError("run executor has no request snapshot sources")
        return self._current_setup(), source()

    def _include_resolver(self, setup: AgentSetup) -> IncludeResolver:
        if self._include is not None:
            return self._include(setup)
        base = (
            setup.environment.working_directory
            if setup.environment is not None
            else setup.layout.home
        )
        return lambda reference: resolve_file_include(reference, base=base)

    def _recorded_state(self, run_id: str) -> AgentState:
        load = self._load_state
        if load is None:
            raise RuntimeError("run executor has no request snapshot sources")
        run = self.store.get_run(run_id=run_id)
        if run is None or run.parent is not None:
            raise ValueError(f"root run not found: {run_id}")
        control = self.store.get_run_control(
            run_id=run.control.target,
            index=run.control.index,
        )
        if control is None or not isinstance(
            control.payload,
            PreparationControlPayload,
        ):
            raise ValueError(f"run preparation not found: {run_id}")
        revision = control.payload.state
        try:
            state = load(revision)
        except (KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
            raise ValueError(
                f"retry state snapshot is not available: {revision}"
            ) from exc
        if not isinstance(state, AgentState) or state.revision != revision:
            raise ValueError(f"retry state snapshot is not available: {revision}")
        return state

    def _source_spec(
        self,
        run_id: str,
        *,
        setup: AgentSetup,
        state: AgentState,
        ceiling: AgentCeiling,
        model: str | None,
        limits: RunLimits,
    ) -> RunSpec:
        run = self.store.get_run(run_id=run_id)
        if run is None or run.parent is not None:
            raise ValueError(f"root run not found: {run_id}")
        control = self.store.get_run_control(
            run_id=run.control.target,
            index=run.control.index,
        )
        if control is None or not isinstance(
            control.payload, PreparationControlPayload
        ):
            raise ValueError(f"run input not found: {run_id}")
        _kind, _separator, runnable = control.payload.runnable.partition(":")
        if not runnable:
            raise ValueError(f"run runnable not found: {run_id}")
        resolved = resolve_state_runnable(state, runnable)
        return RunSpec(
            setup=setup,
            state=state,
            thread=run.thread,
            bindings=RunBindings(
                runnable=f"{resolved.public.kind}:{resolved.public.name}",
                model=model if model is not None else control.payload.model,
            ),
            limits=limits,
            ceilings=(
                (ceiling,)
                if any(
                    value is not None
                    for value in (
                        ceiling.models,
                        ceiling.tools,
                        ceiling.caps,
                    )
                )
                else ()
            ),
            input=_runnable_input_from_locals(
                self.store,
                _adopted_control_locals(
                    self.store,
                    run_id=run.id,
                    through=control.index,
                ),
            ),
        )

    def _require_retry_compatible(
        self, run_id: str, state: AgentState, *, sandbox: str
    ) -> None:
        """Reject retry before mutation when its execution snapshot changed."""

        run = self.store.get_run(run_id=run_id)
        if run is None or run.parent is not None:
            raise ValueError(f"root run not found: {run_id}")
        control = self.store.get_run_control(
            run_id=run.control.target,
            index=run.control.index,
        )
        if control is None or not isinstance(
            control.payload, PreparationControlPayload
        ):
            raise ValueError(f"run preparation not found: {run_id}")
        if control.payload.state != state.revision:
            raise ValueError(
                f"retry state no longer matches original run: {run_id}; use rerun"
            )
        if control.payload.sandbox is None:
            raise ValueError(f"retry sandbox is unknown for run {run_id}; use rerun")
        if control.payload.sandbox != sandbox:
            raise ValueError(
                f"retry sandbox {sandbox} does not match original sandbox "
                f"{control.payload.sandbox} for run {run_id}; use rerun"
            )

    def _launch(
        self,
        bound: BoundRun,
        executable: AgicDecl | FlowDecl,
        *,
        loop: asyncio.AbstractEventLoop,
        tracer: RunTracer | None,
        retry: RunControlRecord | None = None,
    ) -> LocalRunHandle:
        task = asyncio.create_task(
            self._execute_owned(bound, executable, tracer=tracer, retry=retry),
            name=f"toolang-run-{bound.run_id}",
        )
        active = _ActiveRun(task=task, tracer=tracer, loop=loop)
        with self._active_lock:
            self._active[bound.run_id] = active
        self._tasks[task] = (bound.run_id, active)
        task.add_done_callback(self._task_done)
        self._ensure_monitor(bound.setup.layout.name)
        return LocalRunHandle(bound.run_id, self, task)

    def validate(self, spec: RunSpec) -> None:
        """Validate one immutable run spec without accepting a run."""

        _prepare_run_spec(spec)

    async def _execute_owned(
        self,
        bound: BoundRun,
        executable: AgicDecl | FlowDecl,
        *,
        tracer: RunTracer | None,
        retry: RunControlRecord | None = None,
    ) -> RunRecord:
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("run execution requires an asyncio task")
        with self._active_lock:
            active = self._active.get(bound.run_id)
        if active is None or active.task is not task or active.tracer is not tracer:
            raise RuntimeError(f"run ownership missing: {bound.run_id}")
        started_at = time.perf_counter()
        emit = self._handler(active)
        execution = _Execution(
            self,
            root=bound,
            emit=emit,
            retry=retry,
        )
        timeout = execution.schedule_time_limit(task)
        try:
            await execution.execute(
                bound,
                executable,
            )
        except asyncio.CancelledError:
            await self._ensure_terminal(bound.run_id, emit=emit, status="canceled")
        except Exception as exc:
            await self._ensure_terminal(
                bound.run_id,
                emit=emit,
                status="failed",
                error=str(exc) or type(exc).__name__,
            )
        finally:
            if timeout is not None:
                timeout.cancel()
            with self._active_lock:
                for run_id in tuple(self._active):
                    if self._active.get(run_id) is active:
                        self._active.pop(run_id, None)
        result = self.store.get_run(run_id=bound.run_id)
        if result is None:
            raise RuntimeError(f"run projection missing: {bound.run_id}")
        _LOGGER.info(
            "Run finished thread=%s run=%s status=%s duration_ms=%s",
            result.thread,
            result.id,
            result.status,
            max(0, round((time.perf_counter() - started_at) * 1000)),
        )
        return result

    def cancel(
        self,
        *,
        run_id: str,
        timing: ControlTiming = "immediate",
        request_id: str | None = None,
        reason: str | None = None,
    ) -> RunControlRecord:
        """Persist one cancel control for the process that owns the run."""

        self._require_available()
        control = self.store.accept_run_control(
            run_id=run_id,
            kind="cancel",
            timing=timing,
            locals=(RecordLocal.typed("Text", reason, "_", 0),)
            if reason is not None
            else (),
            request_id=request_id,
            created_at=utc_now(),
        )
        self._observe_control(control)
        return control

    def steer(
        self,
        *,
        run_id: str,
        message: Message,
        timing: ControlTiming,
        request_id: str | None = None,
    ) -> RunControlRecord:
        """Persist one steer control for the process that owns the run."""

        self._require_available()
        if message.role != "user":
            raise ValueError("run steer requires a user message")
        _ = message.parts
        control = self.store.accept_run_control(
            run_id=run_id,
            kind="steer",
            timing=timing,
            locals=(RecordLocal.typed("Part[]", tuple(message.parts), "_", 0),),
            request_id=request_id,
            created_at=utc_now(),
        )
        self._observe_control(control)
        return control

    def cancel_control(self, *, run_id: str, index: int) -> RunControlRecord:
        """Revoke one pending steer or cancel control from any local process."""

        self._require_available()
        control = self.store.cancel_run_control(
            run_id=run_id,
            index=index,
            canceled_at=utc_now(),
        )
        self._observe_control(control)
        return control

    async def stop(self) -> None:
        """Cancel and await all runs owned by this executor."""

        if self._stopped:
            return
        self._stopped = True
        owned = tuple(self._tasks.items())
        for task, _run in owned:
            if not task.done():
                task.cancel()
        if owned:
            await asyncio.gather(
                *(task for task, _run in owned),
                return_exceptions=True,
            )
        for _task, (run_id, active) in owned:
            await self._ensure_terminal(
                run_id,
                emit=self._handler(active),
                status="canceled",
            )
        with self._active_lock:
            owned_tasks = {task for task, _run in owned}
            for run_id, active in tuple(self._active.items()):
                if active.task in owned_tasks:
                    self._active.pop(run_id, None)
        monitor = self._monitor_task
        self._monitor_task = None
        if monitor is not None and not monitor.done():
            monitor.cancel()
            await asyncio.gather(monitor, return_exceptions=True)

    def _require_available(self) -> None:
        if self._stopped:
            raise RuntimeError("run executor is stopped")

    def _task_done(self, task: asyncio.Task[RunRecord]) -> None:
        owned = self._tasks.pop(task, None)
        if owned is None:
            return
        run_id, active = owned
        if not task.cancelled() and (error := task.exception()) is not None:
            _LOGGER.error(
                "Run task failed outside runtime handling run=%s error=%r",
                run_id,
                str(error) or type(error).__name__,
            )
        with self._active_lock:
            for active_run_id, candidate in tuple(self._active.items()):
                if candidate is active:
                    self._active.pop(active_run_id, None)

    def _handler(self, active: _ActiveRun) -> EventEmitter:
        async def emit(event: RunEvent) -> None:
            async with active.event_lock:
                event_run = _run_event_id(event)
                if event_run in active.ended:
                    return
                with self.store.write_transaction():
                    self._persist.on_event(event)
                    self._update_control_state(event)
                self._update_cached_control_state(event)
                self._track_active_run(event, active)
                if isinstance(event, RunEnd):
                    active.ended.add(event.run)
                if active.tracer is not None:
                    try:
                        await active.tracer.on_event(event)
                    except Exception:
                        _LOGGER.exception("run tracer event handling failed")

        return emit

    def _update_control_state(self, event: RunEvent) -> None:
        if isinstance(event, RunBegin):
            self.store.finish_run_controls(
                run_id=event.run,
                indexes=(event.control.index,),
                finished_at=event.started_at,
            )
            return
        if isinstance(event, StepBegin):
            indexes = _control_indexes(event.input, run_id=event.step.run)
            self.store.finish_run_controls(
                run_id=event.step.run,
                indexes=indexes,
                finished_at=event.started_at,
            )
            return
        if isinstance(event, RunEnd):
            if event.control is not None:
                self.store.finish_run_controls(
                    run_id=event.run,
                    indexes=(event.control.index,),
                    finished_at=event.finished_at,
                )
            self.store.fail_pending_run_controls(
                run_id=event.run,
                finished_at=event.finished_at,
                error="run ended before the control could be applied",
            )

    def _track_active_run(self, event: RunEvent, active: _ActiveRun) -> None:
        if isinstance(event, RunBegin):
            with self._active_lock:
                self._active[event.run] = active
        elif isinstance(event, RunEnd):
            with self._active_lock:
                if self._active.get(event.run) is active:
                    self._active.pop(event.run, None)

    def _register_child_run(self, *, run_id: str, root_run_id: str) -> None:
        with self._active_lock:
            active = self._active.get(root_run_id)
            if active is None:
                raise RuntimeError(f"root run ownership missing: {root_run_id}")
            self._active[run_id] = active

    def _observe_control(self, control: RunControlRecord) -> None:
        if control.kind == "run":
            return
        cancel: asyncio.Task[RunRecord] | None = None
        loop: asyncio.AbstractEventLoop | None = None
        with self._active_lock:
            active = self._active.get(control.run)
            if active is None:
                return
            controls = active.controls.setdefault(control.run, {})
            if control.status == "pending":
                observed = control.index in controls
                controls[control.index] = control
                if (
                    not observed
                    and control.kind in {"steer", "cancel"}
                    and control.timing == "immediate"
                ):
                    cancel = active.task
                    loop = active.loop
            else:
                controls.pop(control.index, None)
                if not controls:
                    active.controls.pop(control.run, None)
        if cancel is not None and loop is not None and not cancel.done():
            if control.kind == "cancel":
                claimed = self.store.claim_run_controls(
                    run_id=control.run,
                    indexes=(control.index,),
                )
                if control.index not in claimed:
                    return
            loop.call_soon_threadsafe(cancel.cancel)

    def _pending_controls(
        self,
        *,
        run_id: str,
        kind: ControlKind,
    ) -> tuple[RunControlRecord, ...]:
        self._refresh_controls()
        with self._active_lock:
            active = self._active.get(run_id)
            if active is None:
                return ()
            controls = active.controls.get(run_id, {})
            return tuple(
                control
                for _index, control in sorted(controls.items())
                if control.kind == kind and control.status == "pending"
            )

    def _claim_controls(
        self,
        *,
        run_id: str,
        controls: Sequence[RunControlRecord],
    ) -> tuple[RunControlRecord, ...]:
        if not controls:
            return ()
        claimed = self.store.claim_run_controls(
            run_id=run_id,
            indexes=tuple(control.index for control in controls),
        )
        return tuple(control for control in controls if control.index in claimed)

    def _update_cached_control_state(self, event: RunEvent) -> None:
        if isinstance(event, RunBegin):
            return
        if isinstance(event, StepBegin):
            run_id = event.step.run
            indexes = set(_control_indexes(event.input, run_id=run_id))
        elif isinstance(event, RunEnd):
            run_id = event.run
            indexes = None
        else:
            return
        with self._active_lock:
            active = self._active.get(run_id)
            if active is None:
                return
            if indexes is None:
                active.controls.pop(run_id, None)
                return
            controls = active.controls.get(run_id)
            if controls is None:
                return
            for index in indexes:
                controls.pop(index, None)
            if not controls:
                active.controls.pop(run_id, None)

    def _refresh_controls(self) -> bool:
        revision, controls = self.store.changed_run_controls(
            after_revision=self._control_revision
        )
        if not controls:
            return False
        for control in controls:
            self._observe_control(control)
        self._control_revision = revision
        return True

    def _ensure_monitor(self, agent_name: str) -> None:
        if self._monitor_task is None or self._monitor_task.done():
            self._monitor_task = asyncio.create_task(
                self._monitor_controls(), name=f"toolang-controls-{agent_name}"
            )

    async def _monitor_controls(self) -> None:
        while True:
            await asyncio.sleep(self._control_poll_interval)
            self._refresh_controls()

    async def _ensure_terminal(
        self,
        run_id: str,
        *,
        emit: EventEmitter,
        status: Literal["failed", "canceled"],
        error: ExecutionError | None = None,
    ) -> None:
        record = self.store.get_run(run_id=run_id)
        if record is not None and record.status not in {"pending", "running"}:
            return
        cancellation = next(
            iter(self.store.pending_run_controls(run_id=run_id, kind="cancel")),
            None,
        )
        await emit(
            RunEnd(
                run=run_id,
                status=status,
                control=(
                    ControlRef(run_id, cancellation.index)
                    if cancellation is not None
                    else None
                ),
                error=error or control_text(cancellation) or status,
                finished_at=utc_now(),
            )
        )


class _Execution:
    """Private state for one root run and its recursive child-run tree."""

    def __init__(
        self,
        executor: RunExecutor,
        *,
        root: BoundRun,
        emit: EventEmitter,
        retry: RunControlRecord | None = None,
    ) -> None:
        self.executor = executor
        self.setup = root.setup
        self.layout = root.setup.layout
        config_layers = (root.state.root_config, root.state.home_config)
        self.model_aliases = parse_model_aliases(config_layers)
        self.default_models = parse_default_models(config_layers)
        if root.agent_resources is None:
            raise RuntimeError(f"agent resources missing: {root.run_id}")
        self._agent_resources = root.agent_resources
        self.date = root.created_at.partition("T")[0]
        self.timezone = "UTC"
        self._emit_trace = emit
        self._limits = _RunLimitState(root.limits)
        self._retry = retry
        if retry is not None:
            self._restore_model_limits(root.run_id)
        self._run_outputs: dict[str, RecordLocal] = {}

    def next_step(self, run_id: str) -> int:
        """Return the next unused top-level physical step index."""

        steps = self.store.list_steps(run_id=run_id, include_ejected=True)
        return (
            max(
                (step.index for step in steps if step.parent is None),
                default=-1,
            )
            + 1
        )

    def _restore_model_limits(self, root_run_id: str) -> None:
        runs = self.store.list_run_tree(root_run_id=root_run_id)
        steps_by_run = self.store.list_steps_for_runs(
            run_ids=tuple(run.id for run in runs)
        )
        for step in (
            step
            for steps in steps_by_run.values()
            for step in steps
            if step.kind == "model" and step.status == "succeeded"
        ):
            noted = step.noted if isinstance(step.noted, ModelStepNoted) else None
            input_tokens = (
                noted.accounting.input_tokens
                if noted and noted.accounting
                else noted.tokens.input
                if noted and noted.tokens
                else None
            )
            output_tokens = (
                noted.accounting.output_tokens
                if noted and noted.accounting
                else noted.tokens.output
                if noted and noted.tokens
                else None
            )
            raw_cost = noted.cost if noted is not None else None
            if noted is not None and noted.accounting is not None:
                cost = selected_usd_cost(noted.accounting)
            else:
                try:
                    cost = Decimal(str(raw_cost)) if raw_cost is not None else None
                except InvalidOperation:
                    cost = None
            self._limits.restore(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost=cost,
            )

    @property
    def store(self) -> RunStore:
        return self.executor.store

    @property
    def providers(self) -> Mapping[str, Provider]:
        return self.setup.providers

    @property
    def models(self) -> tuple[ModelInfo, ...]:
        return self.setup.models

    @property
    def envs(self) -> Mapping[str, str]:
        return self.setup.envs

    @property
    def provider_configs(self) -> Mapping[str, ProviderConfig]:
        return cast(Mapping[str, ProviderConfig], self.setup.provider_configs)

    def schedule_time_limit(
        self,
        task: asyncio.Task[RunRecord],
    ) -> asyncio.TimerHandle | None:
        """Cancel the owner task when its root-tree wall-time limit expires."""

        limit = self._limits.limits.time
        if limit is None:
            return None

        def expire() -> None:
            if task.done():
                return
            self._limits.expire_time()
            task.cancel()

        return asyncio.get_running_loop().call_later(limit, expire)

    def require_model_pricing(self, target: ModelTarget) -> None:
        """Require price metadata before a cost-limited model call."""

        self._limits.require_pricing(target, self.models)

    def model_accounting(
        self,
        target: ModelTarget,
        usage: ModelUsage | None,
    ) -> _ModelAccounting:
        """Build accounting facts for one completed model call."""

        return _model_accounting(
            target,
            self.models,
            usage,
            catalog=self.setup.catalog,
        )

    def record_model_accounting(
        self,
        target: ModelTarget,
        accounting: _ModelAccounting,
    ) -> None:
        """Add one model accounting result to root-tree totals."""

        self._limits.record_model(target, accounting)

    async def execute(
        self,
        binding: BoundRun,
        executable: AgicDecl | FlowDecl,
        *,
        locals: Mapping[str, Local] | None = None,
        output_name: str | None = "_",
    ) -> Local:
        """Execute one accepted agic or flow run and emit its lifecycle."""

        from .runs import agic as agic_run
        from .runs import flow as flow_run

        if binding.resources is None:
            raise RuntimeError(f"run resources missing: {binding.run_id}")
        current = (
            dict(locals) if locals is not None else initial_locals(binding, executable)
        )
        statement_start = 0
        step_start = self.next_step(binding.run_id)
        await self.emit(
            RunBegin(
                run=binding.run_id,
                control=ControlRef(binding.run_id, binding.control_index),
                runnable=_bound_runnable(binding),
                parent=binding.parent,
                occurrence=binding.occurrence,
                started_at=utc_now(),
            )
        )
        try:
            if (
                self._retry is not None
                and binding.run_id == self._retry.run
                and isinstance(executable, FlowDecl)
            ):
                current, statement_start = self._resume_flow(
                    binding,
                    executable,
                    current,
                )
            if self._retry is not None and binding.run_id == self._retry.run:
                self._limits.check_restored()
            if isinstance(executable, AgicDecl):
                result = await agic_run.execute(self, binding, executable, current)
                current["_"] = result
            else:
                result = await flow_run.execute(
                    self,
                    binding,
                    executable,
                    current,
                    statement_start=statement_start,
                    step_start=step_start,
                )
        except asyncio.CancelledError as exc:
            if self._limits.error is not None:
                error = self._limits.error
                await self.emit(
                    RunEnd(
                        run=binding.run_id,
                        status="failed",
                        output=self.run_output(binding.run_id),
                        error=error,
                        finished_at=utc_now(),
                    )
                )
                raise _RunLimitExceeded(error) from exc
            control = (
                exc.control
                if isinstance(exc, _RunCanceled)
                else next(
                    (
                        item
                        for item in self.pending_controls(binding.run_id, "cancel")
                        if item.timing == "immediate"
                    ),
                    None,
                )
            )
            await self.emit(
                RunEnd(
                    run=binding.run_id,
                    status="canceled",
                    control=ControlRef(binding.run_id, control.index)
                    if control is not None
                    else None,
                    output=self.run_output(binding.run_id),
                    error=control_text(control) or "canceled",
                    finished_at=utc_now(),
                )
            )
            raise
        except _StepFailed as exc:
            await self.emit(
                RunEnd(
                    run=binding.run_id,
                    status="failed",
                    output=self.run_output(binding.run_id),
                    error=exc.error,
                    finished_at=utc_now(),
                )
            )
            raise
        except Exception as exc:
            error = str(exc) or type(exc).__name__
            await self.emit(
                RunEnd(
                    run=binding.run_id,
                    status="failed",
                    output=self.run_output(binding.run_id),
                    error=error,
                    finished_at=utc_now(),
                )
            )
            raise
        await self.emit(
            RunEnd(
                run=binding.run_id,
                status="succeeded",
                output=_run_result_local(result, name=output_name),
                finished_at=utc_now(),
            )
        )
        return result

    def _resume_flow(
        self,
        binding: BoundRun,
        flow: FlowDecl,
        current: dict[str, Local],
    ) -> tuple[dict[str, Local], int]:
        committed = [
            step
            for step in self.store.list_steps(run_id=binding.run_id)
            if step.parent is None
        ]
        if len(committed) > len(flow.stmts):
            raise ValueError(f"retry prefix exceeds flow body: {binding.run_id}")
        for index, step in enumerate(committed):
            statement = flow.stmts[index]
            if step.given != statement:
                raise ValueError(
                    f"retry prefix no longer matches flow statement: {step.path}"
                )
            if step.status != "succeeded":
                raise ValueError(f"retry prefix step is not committed: {step.path}")
            if isinstance(statement, RepeatStmt):
                for descendant in self.store.list_steps(run_id=binding.run_id):
                    if (
                        len(descendant.path.indices) <= len(step.path.indices)
                        or descendant.path.indices[: len(step.path.indices)]
                        != step.path.indices
                    ):
                        continue
                    if descendant.status != "succeeded":
                        raise ValueError(
                            f"retry prefix step is not committed: {descendant.path}"
                        )
                    self._restore_step_local(binding.run_id, descendant, current)
                continue
            if statement.binding is None:
                continue
            local = _step_local(step, self.store)
            current[statement.binding] = local
            if statement.binding == "_":
                self.record_output(binding.run_id, local.ref or Pointer.step(step.path))
        return current, len(committed)

    def _restore_step_local(
        self,
        run_id: str,
        step: StepRecord,
        current: dict[str, Local],
    ) -> None:
        """Restore one named local produced inside a committed structural step."""

        if step.output is None or step.output.name is None:
            return
        local = _step_local(step, self.store)
        current[step.output.name] = local
        if step.output.name == "_":
            self.record_output(run_id, local.ref or Pointer.step(step.path))

    async def execute_child(
        self,
        parent: BoundRun,
        locals: Mapping[str, Local],
        step: StepPath,
        name: str,
        occurrence: Occurrence | None,
        *,
        output_name: str | None = "_",
    ) -> Local:
        """Accept and execute one recursive child agic or flow run."""

        executable = resolve_runnable(
            state_program_module(parent.state, parent.module).program,
            name,
        )
        binding = _child_binding(
            self,
            parent,
            executable,
            locals,
            parent_step=step,
            occurrence=occurrence,
        )
        binding = _prepare_child_run(binding, executable)
        resources = binding.resources
        if resources is None:
            raise RuntimeError(f"run resources missing: {binding.run_id}")
        self.store.accept_run(
            run_id=binding.run_id,
            parent=step,
            thread=binding.thread,
            resources=resources,
            limits=binding.limits,
            state=binding.state.revision,
            runnable=_bound_runnable(binding),
            model=_bound_model(binding),
            locals=binding.control_locals,
            sandbox=None,
            occurrence=binding.occurrence,
            request_id=None,
            created_at=binding.created_at,
        )
        self.executor._register_child_run(
            run_id=binding.run_id,
            root_run_id=step.run,
        )
        try:
            result = await self.execute(binding, executable, output_name=output_name)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise _ExecutionFailed(Pointer.run(binding.run_id), exc) from exc
        pointer = Pointer.run(binding.run_id)
        item_type = result.type_name or "Json"
        return replace(
            result,
            ref=pointer,
            record=RecordLocal.typed(
                type_name=(f"{item_type}[]" if result.shape == "list" else item_type),
                value=pointer,
                dim=1 if result.shape == "list" else 0,
            ),
        )

    async def parallel_children(
        self,
        binding: BoundRun,
        locals: Mapping[str, Local],
        parent: StepPath,
        runnable: str,
        inputs: Sequence[Any],
        *,
        limit: int | None,
        select_source: bool = True,
    ) -> Local:
        """Execute child runs concurrently and preserve their output type."""

        executable = resolve_runnable(
            state_program_module(binding.state, binding.module).program,
            runnable,
        )
        lanes = limit or max(len(inputs), 1)
        available_lanes: asyncio.Queue[int] = asyncio.Queue()
        for lane in range(lanes):
            available_lanes.put_nowait(lane)
        source_local = locals.get("_", Local())
        input_type = source_local.type_name

        async def execute(index: int, value: Any) -> Local:
            lane = await available_lanes.get()
            try:
                child_locals = dict(locals)
                child_locals["_"] = Local(
                    value,
                    "item",
                    ref=(
                        (
                            source_local.ref.select(index)
                            if select_source
                            else source_local.ref
                        )
                        if source_local.ref is not None
                        else None
                    ),
                    type_name=input_type,
                )
                return await self.execute_child(
                    binding,
                    child_locals,
                    parent,
                    runnable,
                    Occurrence(
                        item=OccurrencePosition(index=index, count=len(inputs)),
                        lane=OccurrencePosition(index=lane, count=lanes),
                    ),
                )
            except _ExecutionFailed as exc:
                raise RuntimeError(
                    f"parallel step stopped because lane {lane} (#{index}) failed"
                ) from exc
            finally:
                available_lanes.put_nowait(lane)

        tasks = [
            asyncio.create_task(execute(index, value))
            for index, value in enumerate(inputs)
        ]
        try:
            results = list(await asyncio.gather(*tasks))
        except BaseException:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        output_type = _parallel_output_type(results, executable) or "Json"
        result_refs = tuple(result.ref for result in results if result.ref is not None)
        return Local(
            [result.value for result in results],
            "list",
            type_name=output_type,
            record=(
                RecordLocal.typed(
                    type_name=f"{output_type}[]",
                    value=result_refs,
                    dim=1,
                )
                if len(result_refs) == len(results)
                else None
            ),
        )

    async def execute_statements(
        self,
        binding: BoundRun,
        statements: Sequence[FlowStmt],
        locals: dict[str, Local],
        *,
        parent: StepPath,
        start: int = 0,
        occurrence: Occurrence | None = None,
    ) -> int:
        """Delegate nested statement execution to the flow run implementation."""

        from .runs.flow import execute_statements

        return await execute_statements(
            self,
            binding,
            statements,
            locals,
            parent=parent,
            start=start,
            occurrence=occurrence,
        )

    def pending_controls(
        self, run_id: str, kind: ControlKind
    ) -> tuple[RunControlRecord, ...]:
        return self.executor._pending_controls(run_id=run_id, kind=kind)

    def steer_controls_for_call(
        self,
        run_id: str,
    ) -> tuple[RunControlRecord, ...]:
        return self.executor._claim_controls(
            run_id=run_id,
            controls=self.pending_controls(run_id, "steer"),
        )

    def steer_before_next_step(self, run_id: str) -> bool:
        """Return whether a steer replaces the next planned non-model step."""

        return any(
            control.timing in {"immediate", "next_step"}
            for control in self.pending_controls(run_id, "steer")
        )

    def immediate_steer(self, run_id: str) -> bool:
        """Return whether an immediate steer interrupted the active step."""

        return any(
            control.timing == "immediate"
            for control in self.pending_controls(run_id, "steer")
        )

    def raise_if_canceling(self, run_id: str, *, call: bool) -> None:
        allowed = {"immediate", "next_step"}
        if call:
            allowed.add("next_call")
        control = next(
            (
                item
                for item in self.pending_controls(run_id, "cancel")
                if item.timing in allowed
            ),
            None,
        )
        if control is None:
            return
        claimed = self.executor._claim_controls(
            run_id=run_id,
            controls=(control,),
        )
        if claimed:
            raise _RunCanceled(claimed[0])

    def record_output(self, run_id: str, ref: Pointer) -> None:
        step = next(
            (
                item
                for item in reversed(self.store.list_steps(run_id=run_id))
                if Pointer.step(item.path) == ref and item.output is not None
            ),
            None,
        )
        if step is not None and step.output is not None:
            self._run_outputs[run_id] = replace(
                step.output,
                value=TypedPointer(step.output.type, ref),
                name="_",
            )

    def run_output(self, run_id: str) -> RecordLocal | None:
        return self._run_outputs.get(run_id)

    async def emit(self, event: RunEvent) -> None:
        await self._emit_trace(event)


def _parallel_output_type(
    results: Sequence[Local],
    executable: AgicDecl | FlowDecl,
) -> str | None:
    actual = {result.type_name for result in results if result.type_name is not None}
    if len(actual) == 1:
        return next(iter(actual))
    if isinstance(executable, AgicDecl):
        return executable.output or "Part[]"
    return executable.output


def _child_binding(
    context: _Execution,
    parent: BoundRun,
    executable: AgicDecl | FlowDecl,
    locals: Mapping[str, Local],
    *,
    parent_step: StepPath,
    occurrence: Occurrence | None,
) -> BoundRun:
    structs = {
        item.name: item
        for item in state_program_module(parent.state, parent.module).program.structs
    }
    source_locals: dict[str, Local] = {}
    primary_value: object | None = None
    if executable.input is not None:
        primary = locals.get("_", Local())
        if primary.shape != "none":
            primary_value = _argument_value(primary, executable.input)
            source_locals["_"] = primary
    named: dict[str, object] = {}
    for parameter in executable.params:
        local = locals.get(parameter.name)
        if local is None or local.shape == "none":
            continue
        named[parameter.name] = _argument_value(local, parameter)
        source_locals[parameter.name] = local
    input = resolve_runnable_input(
        executable,
        primary=primary_value,
        named=named,
        structs=structs,
    )
    declared_types = {
        **(
            {"_": executable.input.type_name or "Part[]"}
            if executable.input is not None
            else {}
        ),
        **{
            parameter.name: parameter.type_name or "Part[]"
            for parameter in executable.params
        },
    }
    control_locals: list[RecordLocal] = []
    resolved_values = {
        **({"_": input.primary} if input.primary is not None else {}),
        **input.named,
    }
    for name, value in resolved_values.items():
        control_locals.append(
            _child_control_local(
                name,
                source_locals[name],
                declared_types[name],
                cast(Value, value),
            )
        )
    return BoundRun(
        run_id=context.executor.ids.issue_run(),
        root_run_id=parent.root_run_id,
        thread=parent.thread,
        bindings=RunBindings(
            model=parent.bindings.model,
            runnable=f"{executable.kind}:{executable.name}",
        ),
        input=input,
        control_locals=tuple(control_locals),
        state=parent.state,
        setup=parent.setup,
        module=parent.module,
        limits=parent.limits,
        ceilings=parent.ceilings,
        agent_resources=parent.agent_resources,
        resources=None,
        flow_resources=parent.flow_resources,
        created_at=utc_now(),
        call="run",
        parent=parent_step,
        occurrence=occurrence,
    )


def _argument_value(local: Local, parameter: Parameter) -> Value:
    """Represent one child argument according to its declared value type."""

    if (parameter.type_name or "Part[]") != "Part[]":
        return cast(Value, local.value)
    parts = value_parts(local.value, type_name=local.type_name)
    if parts is not None:
        return parts
    return (TextPart(value_text(local.value)),)


def _bind_run(
    spec: RunSpec,
    *,
    executable: AgicDecl | FlowDecl,
    run_id: str,
    input: RunnableInput,
    agent_resources: AgentResources,
    resources: AgentResources,
) -> BoundRun:
    if not spec.thread or spec.thread != spec.thread.strip():
        raise ValueError("run spec requires a canonical thread id")
    if spec.bindings.runnable is None:
        raise ValueError("run spec requires a runnable binding")
    runnable_name, runnable_kind = parse_runnable_ref(spec.bindings.runnable)
    resolved = resolve_state_runnable(
        spec.state,
        runnable_name,
        kind=runnable_kind,
    )
    control_locals = _input_locals(input, executable)
    return BoundRun(
        run_id=run_id,
        root_run_id=run_id,
        thread=spec.thread,
        bindings=RunBindings(
            runnable=f"{resolved.public.kind}:{resolved.public.name}",
            model=spec.bindings.model
            or (resources.models[0] if resources.models else "none"),
        ),
        input=_runnable_input_from_values(control_locals),
        control_locals=control_locals,
        state=spec.state,
        setup=spec.setup,
        module=resolved.module.name,
        limits=spec.limits,
        ceilings=spec.ceilings,
        agent_resources=agent_resources,
        resources=resources,
        flow_resources=resources if isinstance(executable, FlowDecl) else None,
        created_at=utc_now(),
    )


def _step_local(step: StepRecord, store: RunStore) -> Local:
    if step.output is None:
        return Local()
    return Local(
        value=store.resolve_value(step.output.value),
        shape="list" if step.output.dim == 1 else "item",
        ref=Pointer.step(step.path),
        type_name=step.output.item_type,
    )


def _prepare_run_spec(
    spec: RunSpec,
) -> tuple[AgicDecl | FlowDecl, RunnableInput, AgentResources, AgentResources]:
    if spec.bindings.runnable is None:
        raise ValueError("run spec requires a runnable binding")
    runnable_name, runnable_kind = parse_runnable_ref(spec.bindings.runnable)
    resolved = resolve_state_runnable(
        spec.state,
        runnable_name,
        kind=runnable_kind,
    )
    executable = resolved.executable
    input = spec.input
    _validate_inputs(
        program=resolved.module.program,
        executable=executable,
        input=input,
    )
    agent_resources = resolve_agent_resources(
        spec.setup,
        spec.state,
        spec.setup.ceiling,
        module=resolved.module.name,
    )
    for ceiling in spec.ceilings:
        agent_resources = apply_agent_ceiling(
            spec.setup,
            spec.state,
            agent_resources,
            ceiling,
            module=resolved.module.name,
        )
    selection = snapshot_model_selection(spec.setup, spec.state)
    resources = resolve_runnable_resources(
        selection,
        executable=executable,
        base=agent_resources,
        setup=spec.setup,
        state=spec.state,
        module=resolved.module.name,
    )
    validate_model_binding(
        selection,
        executable=executable,
        resources=resources,
        model=spec.bindings.model,
    )
    return executable, input, agent_resources, resources


def _setup_sandbox(setup: AgentSetup) -> str:
    environment = setup.environment
    if environment is None:
        raise ValueError("agent setup requires an execution environment")
    sandbox = environment.sandbox
    if not sandbox or sandbox != sandbox.strip():
        raise ValueError("agent setup requires a canonical sandbox")
    return sandbox


def _prepare_child_run(
    binding: BoundRun,
    executable: AgicDecl | FlowDecl,
) -> BoundRun:
    agent_resources = binding.agent_resources
    if agent_resources is None:
        raise RuntimeError(f"agent resources missing: {binding.run_id}")
    base = (
        agent_resources
        if isinstance(executable, FlowDecl)
        else binding.flow_resources or agent_resources
    )
    selection = snapshot_model_selection(binding.setup, binding.state)
    resources = resolve_runnable_resources(
        selection,
        executable=executable,
        base=base,
        setup=binding.setup,
        state=binding.state,
        module=binding.module,
    )
    return replace(
        binding,
        resources=resources,
        flow_resources=(
            resources if isinstance(executable, FlowDecl) else binding.flow_resources
        ),
    )


def _validate_inputs(
    *,
    program: Program,
    executable: AgicDecl | FlowDecl,
    input: RunnableInput,
) -> None:
    structs = {item.name: item for item in program.structs}
    params = {param.name: param for param in executable.params}
    args = input.named
    unknown = sorted(set(args) - set(params))
    if unknown:
        joined = ", ".join(unknown)
        raise ValueError(f"unknown named inputs for {executable.name}: {joined}")
    missing = sorted(
        name
        for name, param in params.items()
        if not param.optional and name not in args
    )
    if missing:
        joined = ", ".join(missing)
        raise ValueError(f"missing named inputs for {executable.name}: {joined}")
    if executable.input is None and input.primary is not None:
        raise ValueError(f"{executable.name} does not accept primary input")
    if (
        executable.input is not None
        and not executable.input.optional
        and input.primary is None
    ):
        raise ValueError(f"{executable.name} requires primary input")
    if executable.input is not None and input.primary is not None:
        validate_value(
            input.primary,
            executable.input.type_name or "Part[]",
            structs=structs,
            path="primary input",
        )
    for name, value in args.items():
        validate_value(
            value,
            params[name].type_name or "Part[]",
            structs=structs,
            path=f"named input {name}",
        )


def _run_event_id(event: RunEvent) -> str:
    if isinstance(event, RunBegin | RunEnd):
        return event.run
    return event.step.run


def _input_locals(
    input: RunnableInput,
    executable: AgicDecl | FlowDecl,
) -> tuple[RecordLocal, ...]:
    parameters = {item.name: item for item in executable.params}
    result: list[RecordLocal] = []
    if executable.input is not None and input.primary is not None:
        type_name = executable.input.type_name or "Part[]"
        result.append(
            RecordLocal.typed(
                type_name=type_name,
                value=input.primary,
                name="_",
            )
        )
    for name, item in input.named.items():
        result.append(
            RecordLocal.typed(
                type_name=parameters[name].type_name or "Part[]",
                value=item,
                name=name,
            )
        )
    return tuple(result)


def _child_control_local(
    name: str,
    local: Local,
    type_name: str,
    value: Value,
) -> RecordLocal:
    source_type = _runtime_local_type(local)
    return RecordLocal.typed(
        type_name=type_name,
        value=(
            local.ref if local.ref is not None and source_type == type_name else value
        ),
        name=name,
        dim=0,
    )


def _runtime_local_type(local: Local) -> str | None:
    if local.record is not None:
        return local.record.type
    if local.type_name is None:
        return None
    return f"{local.type_name}[]" if local.shape == "list" else local.type_name


def _runnable_input_from_locals(
    store: RunStore,
    locals: Sequence[RecordLocal],
) -> RunnableInput:
    primary: Value | None = None
    named: dict[str, Value] = {}
    for local in locals:
        value = cast(Value, store.resolve_value(local.value))
        if local.name == "_":
            primary = value
        elif local.name is not None:
            named[local.name] = value
    return RunnableInput(primary=primary, named=named)


def _runnable_input_from_values(
    locals: Sequence[RecordLocal],
) -> RunnableInput:
    primary: Value | None = None
    named: dict[str, Value] = {}
    for local in locals:
        if isinstance(local.value, TypedPointer):
            raise TypeError("top-level input local cannot be a pointer")
        if local.name == "_":
            primary = local.value
        elif local.name is not None:
            named[local.name] = local.value
    return RunnableInput(primary=primary, named=named)


def _adopted_control_locals(
    store: RunStore,
    *,
    run_id: str,
    through: int,
) -> tuple[RecordLocal, ...]:
    for control in reversed(store.list_run_controls(run_id=run_id)):
        if control.index > through or not isinstance(
            control.payload, PreparationControlPayload
        ):
            continue
        if control.payload.locals is not None:
            return control.payload.locals
    raise ValueError(f"run control locals are missing: {run_id}^{through}")


def _bound_runnable(binding: BoundRun) -> str:
    runnable = binding.bindings.runnable
    if not runnable:
        raise RuntimeError(f"run runnable binding is missing: {binding.run_id}")
    return runnable


def _bound_model(binding: BoundRun) -> str:
    model = binding.bindings.model
    if not model:
        raise RuntimeError(f"run model binding is missing: {binding.run_id}")
    return model


def _run_result_local(
    result: Local,
    *,
    name: str | None = "_",
) -> RecordLocal | None:
    if result.shape == "none":
        return None
    item_type = result.type_name or "Json"
    reference = (
        result.ref
        if result.ref is not None
        and (result.record is not None or result.type_name == "Part[]")
        else None
    )
    concrete = (
        tuple(result.value)
        if item_type.endswith("[]") and isinstance(result.value, list)
        else result.value
    )
    return RecordLocal.typed(
        type_name=f"{item_type}[]" if result.shape == "list" else item_type,
        value=reference if reference is not None else cast(Value, concrete),
        name=name,
        dim=1 if result.shape == "list" else 0,
    )


def _control_indexes(
    pointers: Sequence[Pointer],
    *,
    run_id: str,
) -> tuple[int, ...]:
    indexes: list[int] = []
    for pointer in pointers:
        anchor = pointer.anchor
        if "^" not in anchor:
            continue
        target, raw_index = anchor.split("^", 1)
        if target == run_id:
            indexes.append(int(raw_index))
    return tuple(dict.fromkeys(indexes))
