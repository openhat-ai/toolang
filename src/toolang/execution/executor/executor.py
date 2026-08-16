"""Run acceptance, control, and recursive execution."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Generator, Mapping, Sequence
from dataclasses import dataclass, field, replace
import logging
from decimal import Decimal, InvalidOperation
import threading
import time
from typing import Any, Literal, cast

from toolang.base.protocols.model import ModelProvider
from toolang.base.types.model import ModelInfo, ModelTarget
from toolang.base.types.policy import AgentCeiling, RunBindings, RunLimits
from toolang.base.types.run import ModelUsage
from toolang.base.types.message import (
    Message,
    Percept,
    TextPart,
    part_from_data,
)
from toolang.common.ids import IdIssuer
from toolang.common.time import utc_now
from toolang.lang.ast import AgicDecl, FlowDecl, FlowStmt, Parameter
from toolang.lang.format import format_statement_head
from toolang.lang.input import coerce_input, validate_value
from toolang.plugin.models.config import parse_default_models, parse_model_aliases
from toolang.state.state import AgentState
from toolang.setup import AgentSetup

from ..events import RunBegin, RunEnd, RunEvent, RunTracer, StepBegin
from ..records import (
    RunControlRecord,
    RunInputRef,
    RunRecord,
    StepOutputRef,
    StepRecord,
    ValueRef,
    run_limits_to_data,
)
from ..store import RunStore
from ..types import (
    ControlTiming,
    ExecutionError,
    RunControlKind,
    StepPath,
)
from ..runnables import parse_runnable_ref, resolve_runnable
from .common import (
    BoundRun,
    EventEmitter,
    Local,
    Shape,
    _StepFailed,
    control_text,
    initial_locals,
    json_value,
    statement_has_call,
    value_percept,
    value_text,
)
from .ceiling import (
    _ResolvedAgentCeiling,
    restrict_agent_ceiling,
    resolve_agent_ceiling,
    resolve_run_ceiling,
    validate_root_run_resources,
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


class _RunStopped(asyncio.CancelledError):
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
    primary: Percept = ()
    named: Mapping[str, object] | None = None


@dataclass(frozen=True, slots=True)
class RunHandle(Awaitable[RunRecord]):
    """One locally started run that can be controlled and awaited."""

    run_id: str
    executor: RunExecutor = field(repr=False)
    task: asyncio.Task[RunRecord] = field(repr=False)

    def stop(
        self,
        *,
        timing: ControlTiming = "immediate",
        request_id: str | None = None,
        reason: str | None = None,
    ) -> RunControlRecord:
        """Persist a stop control for this run."""

        return self.executor.stop(
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
        """Cancel one pending steer or stop control for this run."""

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

    def __init__(self, store: RunStore, ids: IdIssuer) -> None:
        self.store = store
        self.ids = ids
        self._persist = _PersistSink(self.store)
        self._control_poll_interval = _CONTROL_POLL_INTERVAL
        self._active: dict[str, _ActiveRun] = {}
        self._tasks: dict[asyncio.Task[RunRecord], tuple[str, _ActiveRun]] = {}
        self._active_lock = threading.Lock()
        self._monitor_task: asyncio.Task[None] | None = None
        self._control_revision = self.store.latest_run_control_revision()
        self._shutdown = False

    def start(
        self,
        spec: RunSpec,
        *,
        run_id: str | None = None,
        request_id: str | None = None,
        tracer: RunTracer | None = None,
    ) -> RunHandle:
        """Accept one top-level run and immediately return its local handle."""

        self._require_available()
        loop = asyncio.get_running_loop()
        executable, agent_ceiling = _validate_start_spec(spec)
        if not isinstance(spec.limits, RunLimits):
            raise TypeError("run limits must be RunLimits")
        bound = _bind_run(
            spec,
            run_id=run_id or self.ids.issue_run(),
            agent_ceiling=agent_ceiling,
        )
        self.store.accept_start(
            run_id=bound.run_id,
            parent=None,
            thread=bound.thread,
            input=bound.input,
            context=_run_context(bound, executable),
            request_id=request_id,
            created_at=bound.created_at,
            control_context={"limits": run_limits_to_data(spec.limits)},
        )
        return self._launch(bound, executable, loop=loop, tracer=tracer)

    def rerun(
        self,
        source: str,
        *,
        setup: AgentSetup,
        state: AgentState,
        ceiling: AgentCeiling = AgentCeiling(),
        model: str | None = None,
        limits: RunLimits | None = None,
        run_id: str | None = None,
        request_id: str | None = None,
        tracer: RunTracer | None = None,
    ) -> RunHandle:
        """Start a new root run from one visible source run's invocation."""

        self._require_available()
        loop = asyncio.get_running_loop()
        spec = self._source_spec(
            source,
            setup=setup,
            state=state,
            ceiling=ceiling,
            model=model,
            limits=limits if limits is not None else setup.limits,
        )
        executable, agent_ceiling = _validate_start_spec(spec)
        bound = _bind_run(
            spec,
            run_id=run_id or self.ids.issue_run(),
            agent_ceiling=agent_ceiling,
        )
        self.store.accept_start(
            run_id=bound.run_id,
            parent=None,
            thread=bound.thread,
            input=bound.input,
            context=_run_context(bound, executable),
            request_id=request_id,
            created_at=bound.created_at,
            control_context={"limits": run_limits_to_data(spec.limits)},
            kind="rerun",
            source=source,
        )
        return self._launch(bound, executable, loop=loop, tracer=tracer)

    def retry(
        self,
        run_id: str,
        *,
        setup: AgentSetup,
        state: AgentState,
        anchor: StepPath | str | None = None,
        ceiling: AgentCeiling = AgentCeiling(),
        model: str | None = None,
        limits: RunLimits | None = None,
        request_id: str | None = None,
        tracer: RunTracer | None = None,
    ) -> RunHandle:
        """Reopen one terminal root run from a durable step boundary."""

        self._require_available()
        loop = asyncio.get_running_loop()
        spec = self._source_spec(
            run_id,
            setup=setup,
            state=state,
            ceiling=ceiling,
            model=model,
            limits=limits if limits is not None else setup.limits,
        )
        executable, agent_ceiling = _validate_start_spec(spec)
        bound = _bind_run(
            spec,
            run_id=run_id,
            agent_ceiling=agent_ceiling,
        )
        _reopened, control, _ejected = self.store.accept_retry(
            run_id=run_id,
            anchor=StepPath.parse(anchor) if anchor is not None else None,
            context={"limits": run_limits_to_data(spec.limits)},
            request_id=request_id,
            created_at=bound.created_at,
        )
        return self._launch(
            bound,
            executable,
            loop=loop,
            tracer=tracer,
            retry=control,
        )

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
        control = self.store.get_run_control(run_id=run_id, index=0)
        if control is None or control.input is None:
            raise ValueError(f"run input not found: {run_id}")
        runnable = run.runnable_name
        if not runnable:
            raise ValueError(f"run runnable not found: {run_id}")
        executable = resolve_runnable(state.program, runnable)
        return RunSpec(
            setup=setup,
            state=state,
            thread=run.thread,
            bindings=RunBindings(
                runnable=f"{executable.kind}:{runnable}",
                model=model if model is not None else _source_model(run),
            ),
            limits=limits,
            ceilings=(
                (ceiling,)
                if any(
                    value is not None
                    for value in (ceiling.models, ceiling.tools, ceiling.caps)
                )
                else ()
            ),
            primary=control.input.percept,
            named=_source_args(run, executable),
        )

    def _launch(
        self,
        bound: BoundRun,
        executable: AgicDecl | FlowDecl,
        *,
        loop: asyncio.AbstractEventLoop,
        tracer: RunTracer | None,
        retry: RunControlRecord | None = None,
    ) -> RunHandle:
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
        return RunHandle(bound.run_id, self, task)

    def validate(self, spec: RunSpec) -> None:
        """Validate one immutable run spec without accepting a run."""

        _validate_start_spec(spec)

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

    def stop(
        self,
        *,
        run_id: str,
        timing: ControlTiming = "immediate",
        request_id: str | None = None,
        reason: str | None = None,
    ) -> RunControlRecord:
        """Persist one stop control for the process that owns the run."""

        self._require_available()
        control = self.store.accept_run_control(
            run_id=run_id,
            kind="stop",
            timing=timing,
            input=Message.user(reason) if reason else None,
            context={},
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
        _ = message.percept
        control = self.store.accept_run_control(
            run_id=run_id,
            kind="steer",
            timing=timing,
            input=message,
            context={},
            request_id=request_id,
            created_at=utc_now(),
        )
        self._observe_control(control)
        return control

    def cancel_control(self, *, run_id: str, index: int) -> RunControlRecord:
        """Cancel one pending steer or stop control from any local process."""

        self._require_available()
        control = self.store.cancel_run_control(
            run_id=run_id,
            index=index,
            canceled_at=utc_now(),
        )
        self._observe_control(control)
        return control

    async def shutdown(self) -> None:
        """Cancel and await all runs owned by this executor."""

        if self._shutdown:
            return
        self._shutdown = True
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
        if self._shutdown:
            raise RuntimeError("run executor is shut down")

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
                indexes=(event.input.index,),
                finished_at=event.started_at,
            )
            return
        if isinstance(event, StepBegin):
            indexes = tuple(
                item.index for item in event.input if isinstance(item, RunInputRef)
            )
            self.store.finish_run_controls(
                run_id=event.step.run,
                indexes=indexes,
                finished_at=event.started_at,
            )
            return
        if isinstance(event, RunEnd):
            if event.input is not None:
                self.store.finish_run_controls(
                    run_id=event.run,
                    indexes=(event.input.index,),
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
        if control.kind == "start":
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
                    and control.kind == "stop"
                    and control.timing == "immediate"
                ):
                    cancel = active.task
                    loop = active.loop
            else:
                controls.pop(control.index, None)
                if not controls:
                    active.controls.pop(control.run, None)
        if cancel is not None and loop is not None and not cancel.done():
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
        kind: RunControlKind,
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
            indexes = {
                item.index for item in event.input if isinstance(item, RunInputRef)
            }
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
        stop = next(
            iter(self.store.pending_run_controls(run_id=run_id, kind="stop")), None
        )
        await emit(
            RunEnd(
                run=run_id,
                status=status,
                input=RunInputRef(index=stop.index) if stop is not None else None,
                error=error or control_text(stop) or status,
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
        if root.agent_ceiling is None:
            raise RuntimeError(f"agent ceiling missing: {root.run_id}")
        self._agent_ceiling = root.agent_ceiling
        self.date = root.created_at.partition("T")[0]
        self.timezone = "UTC"
        self._emit_trace = emit
        self._limits = _RunLimitState(root.limits)
        self._retry = retry
        if retry is not None:
            self._restore_model_limits(root.run_id)
        self._run_outputs: dict[str, ValueRef] = {}

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
        runs = [
            run
            for run in self.store.list_runs(limit=None)
            if run.root_run_id == root_run_id
        ]
        steps_by_run = self.store.list_steps_for_runs(
            run_ids=tuple(run.id for run in runs)
        )
        for step in (
            step
            for steps in steps_by_run.values()
            for step in steps
            if step.kind == "model" and step.status == "succeeded"
        ):
            tokens = step.noted.get("tokens")
            input_tokens = (
                int(tokens["input"])
                if isinstance(tokens, Mapping) and tokens.get("input") is not None
                else None
            )
            output_tokens = (
                int(tokens["output"])
                if isinstance(tokens, Mapping) and tokens.get("output") is not None
                else None
            )
            raw_cost = step.noted.get("cost")
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
    def providers(self) -> Mapping[str, ModelProvider]:
        return self.setup.providers

    @property
    def models(self) -> tuple[ModelInfo, ...]:
        return self.setup.models

    @property
    def envs(self) -> Mapping[str, str]:
        return self.setup.envs

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

        return _model_accounting(target, self.models, usage)

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
    ) -> Local:
        """Execute one accepted agic or flow run and emit its lifecycle."""

        from .runs import agic as agic_run
        from .runs import flow as flow_run

        ceiling = resolve_run_ceiling(
            self,
            executable=executable,
            agent=self._agent_ceiling,
            flow=binding.flow_ceiling,
            agent_name=binding.setup.layout.name,
        )
        binding = replace(
            binding,
            ceiling=ceiling,
            flow_ceiling=(
                ceiling if isinstance(executable, FlowDecl) else binding.flow_ceiling
            ),
        )
        current = (
            dict(locals) if locals is not None else initial_locals(binding, executable)
        )
        statement_start = 0
        step_start = self.next_step(binding.run_id)
        await self.emit(
            RunBegin(
                run=binding.run_id,
                input=RunInputRef(index=0),
                parent=binding.parent,
                context=_run_context(binding, executable),
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
                if isinstance(exc, _RunStopped)
                else next(
                    (
                        item
                        for item in self.pending_controls(binding.run_id, "stop")
                        if item.timing == "immediate"
                    ),
                    None,
                )
            )
            await self.emit(
                RunEnd(
                    run=binding.run_id,
                    status="canceled",
                    input=RunInputRef(index=control.index)
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
                output=result.ref,
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
            source = step.given.get("source")
            if not isinstance(source, Mapping) or (
                source.get("line") != statement.span.line
                or source.get("head") != format_statement_head(statement)
            ):
                raise ValueError(
                    f"retry prefix no longer matches flow source: {step.path}"
                )
            if step.status != "succeeded":
                raise ValueError(f"retry prefix step is not committed: {step.path}")
            local = _step_local(step)
            if statement.binding is not None:
                current[statement.binding] = local
            if statement.binding == "_":
                self.record_output(
                    binding.run_id, local.ref or StepOutputRef(step.path)
                )
        return current, len(committed)

    async def execute_child(
        self,
        parent: BoundRun,
        locals: Mapping[str, Local],
        step: StepPath,
        name: str,
        placement: Mapping[str, object] | None,
    ) -> Local:
        """Accept and execute one recursive child agic or flow run."""

        executable = resolve_runnable(parent.state.program, name)
        binding = _child_binding(
            self,
            parent,
            executable,
            locals,
            parent_step=step,
            placement=placement,
        )
        _validate_inputs(
            state=binding.state,
            executable=executable,
            input=binding.input.percept,
            args=binding.args,
        )
        context = _run_context(binding, executable)
        self.store.accept_start(
            run_id=binding.run_id,
            parent=step,
            thread=binding.thread,
            input=binding.input,
            context=context,
            request_id=None,
            created_at=binding.created_at,
            control_context={},
        )
        self.executor._register_child_run(
            run_id=binding.run_id,
            root_run_id=step.run,
        )
        return await self.execute(binding, executable)

    async def parallel_children(
        self,
        binding: BoundRun,
        locals: Mapping[str, Local],
        parent: StepPath,
        runnable: str,
        inputs: Sequence[Any],
        *,
        limit: int | None,
    ) -> Local:
        """Execute child runs concurrently and preserve their output type."""

        executable = resolve_runnable(binding.state.program, runnable)
        lanes = limit or max(len(inputs), 1)
        available_lanes: asyncio.Queue[int] = asyncio.Queue()
        for lane in range(lanes):
            available_lanes.put_nowait(lane)
        input_type = locals.get("_", Local()).type_name

        async def execute(index: int, value: Any) -> Local:
            lane = await available_lanes.get()
            try:
                child_locals = dict(locals)
                child_locals["_"] = Local(value, "item", type_name=input_type)
                return await self.execute_child(
                    binding,
                    child_locals,
                    parent,
                    runnable,
                    {
                        "item": index,
                        "items": len(inputs),
                        "lane": lane,
                        "lanes": lanes,
                    },
                )
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
        return Local(
            [result.value for result in results],
            "list",
            type_name=_parallel_output_type(results, executable),
        )

    async def execute_statements(
        self,
        binding: BoundRun,
        statements: Sequence[FlowStmt],
        locals: dict[str, Local],
        *,
        parent: StepPath,
        start: int = 0,
        placement: Mapping[str, object] | None = None,
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
            placement=placement,
        )

    def pending_controls(
        self, run_id: str, kind: RunControlKind
    ) -> tuple[RunControlRecord, ...]:
        return self.executor._pending_controls(run_id=run_id, kind=kind)

    def steer_controls(
        self, run_id: str, statement: FlowStmt
    ) -> tuple[RunControlRecord, ...]:
        allowed = {"immediate", "next_step"}
        if statement_has_call(statement):
            allowed.add("next_call")
        selected = tuple(
            control
            for control in self.pending_controls(run_id, "steer")
            if control.timing in allowed
        )
        return self.executor._claim_controls(
            run_id=run_id,
            controls=selected,
        )

    def steer_controls_for_call(
        self,
        run_id: str,
    ) -> tuple[RunControlRecord, ...]:
        return self.executor._claim_controls(
            run_id=run_id,
            controls=self.pending_controls(run_id, "steer"),
        )

    def raise_if_stopping(self, run_id: str, *, call: bool) -> None:
        allowed = {"immediate", "next_step"}
        if call:
            allowed.add("next_call")
        control = next(
            (
                item
                for item in self.pending_controls(run_id, "stop")
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
            raise _RunStopped(claimed[0])

    def record_output(self, run_id: str, ref: ValueRef) -> None:
        self._run_outputs[run_id] = ref

    def run_output(self, run_id: str) -> ValueRef | None:
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
    placement: Mapping[str, object] | None,
) -> BoundRun:
    if executable.input is None:
        percept: Percept = ()
    else:
        primary = locals.get("_", Local())
        percept = (
            value_percept(primary.value, type_name=primary.type_name)
            if primary.shape == "item"
            else None
        )
        if primary.shape == "none":
            percept = ()
        if percept is None:
            percept = (TextPart(value_text(primary.value)),)
    parameters = {parameter.name: parameter for parameter in executable.params}
    return BoundRun(
        run_id=context.executor.ids.issue_run(),
        root_run_id=parent.root_run_id,
        thread=parent.thread,
        input=Message(role="user", parts=percept),
        args={
            name: _argument_value(local, parameters[name])
            for name, local in locals.items()
            if name in parameters and local.shape != "none"
        },
        model=parent.model,
        state=parent.state,
        setup=parent.setup,
        limits=parent.limits,
        ceiling_restrictions=parent.ceiling_restrictions,
        agent_ceiling=parent.agent_ceiling,
        ceiling=None,
        flow_ceiling=parent.flow_ceiling,
        created_at=utc_now(),
        call="run",
        parent=parent_step,
        placement=dict(placement or {}),
    )


def _argument_value(local: Local, parameter: Parameter) -> object:
    """Represent one child argument according to its declared value type."""

    if (parameter.type_name or "Part[]") != "Part[]":
        return local.value
    percept = value_percept(local.value, type_name=local.type_name)
    if percept is not None:
        return percept
    return (TextPart(value_text(local.value)),)


def _bind_run(
    spec: RunSpec,
    *,
    run_id: str,
    agent_ceiling: _ResolvedAgentCeiling,
) -> BoundRun:
    if not spec.thread or spec.thread != spec.thread.strip():
        raise ValueError("run spec requires a canonical thread id")
    return BoundRun(
        run_id=run_id,
        root_run_id=run_id,
        thread=spec.thread,
        input=Message(role="user", parts=tuple(spec.primary)),
        args=dict(spec.named or {}),
        model=spec.bindings.model,
        state=spec.state,
        setup=spec.setup,
        limits=spec.limits,
        ceiling_restrictions=spec.ceilings,
        agent_ceiling=agent_ceiling,
        ceiling=None,
        flow_ceiling=None,
        created_at=utc_now(),
    )


def _run_context(
    binding: BoundRun,
    executable: AgicDecl | FlowDecl,
) -> dict[str, object]:
    context: dict[str, object] = {
        "root": binding.root_run_id,
        "state_fingerprint": binding.state.fingerprint,
        "runnable": {"kind": executable.kind, "name": executable.name},
        "call": binding.call,
    }
    if binding.model is not None:
        context["model"] = binding.model
    if binding.args:
        context["args"] = {
            name: json_value(value) for name, value in binding.args.items()
        }
    if binding.ceiling_restrictions:
        context["ceilings"] = [
            {
                "models": list(restriction.models)
                if restriction.models is not None
                else None,
                "tools": list(restriction.tools)
                if restriction.tools is not None
                else None,
                "caps": list(restriction.caps)
                if restriction.caps is not None
                else None,
            }
            for restriction in binding.ceiling_restrictions
        ]
    if binding.placement:
        context["placement"] = dict(binding.placement)
    return context


def _source_model(run: RunRecord) -> str | None:
    value = run.context.get("model")
    return str(value) if value is not None else None


def _source_args(
    run: RunRecord,
    executable: AgicDecl | FlowDecl,
) -> dict[str, object]:
    raw = run.context.get("args")
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise ValueError(f"run arguments are invalid: {run.id}")
    parameters = {parameter.name: parameter for parameter in executable.params}
    return {
        str(name): _stored_value(
            value,
            (parameters[str(name)].type_name or "Part[]")
            if str(name) in parameters
            else None,
        )
        for name, value in raw.items()
    }


def _stored_value(value: object, type_name: str | None) -> object:
    if type_name == "Part" and isinstance(value, Mapping):
        return part_from_data(cast(Mapping[str, Any], value))
    if (
        type_name == "Part[]"
        and isinstance(value, Sequence)
        and not isinstance(value, (str, bytes, bytearray))
    ):
        return tuple(
            part_from_data(cast(Mapping[str, Any], item))
            if isinstance(item, Mapping)
            else item
            for item in value
        )
    return value


def _step_local(step: StepRecord) -> Local:
    if "value" not in step.noted:
        raise ValueError(f"retry step result snapshot is missing: {step.path}")
    shape = str(step.noted.get("shape", "none"))
    if shape not in {"none", "item", "list"}:
        raise ValueError(f"retry step shape is invalid: {step.path}")
    raw_type = step.noted.get("type")
    type_name = str(raw_type) if raw_type is not None else None
    value = _stored_value(step.noted.get("value"), type_name)
    return Local(
        value=value,
        shape=cast(Shape, shape),
        ref=StepOutputRef(step.path),
        type_name=type_name,
    )


def _validate_call(spec: RunSpec, executable: AgicDecl | FlowDecl) -> None:
    _validate_inputs(
        state=spec.state,
        executable=executable,
        input=tuple(spec.primary),
        args=dict(spec.named or {}),
    )


def _validate_start_spec(
    spec: RunSpec,
) -> tuple[AgicDecl | FlowDecl, _ResolvedAgentCeiling]:
    if spec.bindings.runnable is None:
        raise ValueError("run spec requires a runnable binding")
    runnable_name, runnable_kind = parse_runnable_ref(spec.bindings.runnable)
    executable = resolve_runnable(
        spec.state.program,
        runnable_name,
        kind=runnable_kind,
    )
    _validate_call(spec, executable)
    setup_ceiling = resolve_agent_ceiling(
        spec.setup,
        spec.state,
        spec.setup.ceiling,
    )
    agent_ceiling = setup_ceiling
    for restriction in spec.ceilings:
        agent_ceiling = restrict_agent_ceiling(
            spec.setup,
            spec.state,
            agent_ceiling,
            restriction,
        )
    validate_root_run_resources(
        spec.setup,
        spec.state,
        executable=executable,
        agent=agent_ceiling,
        model=spec.bindings.model,
    )
    return executable, agent_ceiling


def _validate_inputs(
    *,
    state: AgentState,
    executable: AgicDecl | FlowDecl,
    input: Percept,
    args: Mapping[str, object],
) -> None:
    structs = {item.name: item for item in state.program.structs}
    params = {param.name: param for param in executable.params}
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
    if executable.input is None and input:
        raise ValueError(f"{executable.name} does not accept primary input")
    if executable.input is not None and not executable.input.optional and not input:
        raise ValueError(f"{executable.name} requires primary input")
    if executable.input is not None:
        coerce_input(
            input,
            executable.input.type_name or "Part[]",
            structs=structs,
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
