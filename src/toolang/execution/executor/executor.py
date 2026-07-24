"""Run acceptance, control, and recursive execution."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Generator, Mapping, Sequence
from dataclasses import dataclass, field
import logging
from pathlib import Path
import threading
import time
from typing import Any, Literal

from toolang.base.protocols.model import ModelProvider
from toolang.base.types.message import Message, Part, TextPart
from toolang.common.errors import ToolangError
from toolang.common.ids import IdIssuer
from toolang.common.time import utc_now
from toolang.lang.ast import AgicDecl, FlowDecl, FlowStmt
from toolang.plugin.models.config import parse_default_models, parse_model_aliases
from toolang.state.state import AgentState
from toolang.up.setup import AgentSetup

from ..events import RunBegin, RunEnd, RunEvent, RunTracer, StepBegin, StepEnd
from ..records import (
    OutputRef,
    RunControlRecord,
    RunControlRef,
    RunRecord,
    trace_child_path,
    trace_index,
    trace_parent,
    trace_run,
)
from ..store import RunStore
from ..types import ControlTiming, RunControlKind, StepPath
from .common import (
    BoundRun,
    Local,
    control_text,
    initial_locals,
    statement_has_call,
    value_text,
)
from .prepare import effective_agics
from .persist import PersistSink

_LOGGER = logging.getLogger("toolang.run")
_CONTROL_POLL_INTERVAL = 0.05


class _RunStopped(asyncio.CancelledError):
    def __init__(self, control: RunControlRecord) -> None:
        super().__init__(control_text(control) or "canceled")
        self.control = control


@dataclass(slots=True)
class _ActiveRun:
    task: asyncio.Task[RunRecord]
    tracer: RunTracer | None
    event_lock: Any = field(default_factory=threading.RLock, repr=False)
    ended: set[str] = field(default_factory=set, repr=False)


@dataclass(frozen=True, slots=True)
class RunSpec:
    """Immutable inputs required to execute one runnable."""

    setup: AgentSetup
    state: AgentState
    thread: str
    runnable: str
    input: tuple[Part, ...] = ()
    model: str | None = None
    args: Mapping[str, object] | None = None


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
        self._persist = PersistSink(self.store)
        self._control_poll_interval = _CONTROL_POLL_INTERVAL
        self._active: dict[str, _ActiveRun] = {}
        self._tasks: dict[
            asyncio.Task[RunRecord], tuple[str, _ActiveRun]
        ] = {}
        self._active_lock = threading.Lock()
        self._monitor_task: asyncio.Task[None] | None = None
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
        asyncio.get_running_loop()
        executable = _require_runnable(spec.state, spec.runnable)
        _validate_call(spec, executable)
        bound = _bind_run(
            spec,
            run_id=run_id or self.ids.issue_run(),
        )
        self.store.accept_start(
            run_id=bound.run_id,
            parent=None,
            thread=bound.thread,
            input=bound.input,
            context=_run_context(bound, executable),
            request_id=request_id,
            created_at=bound.created_at,
        )
        task = asyncio.create_task(
            self._execute_owned(bound, executable, tracer=tracer),
            name=f"toolang-run-{bound.run_id}",
        )
        active = _ActiveRun(task=task, tracer=tracer)
        with self._active_lock:
            self._active[bound.run_id] = active
        self._tasks[task] = (bound.run_id, active)
        task.add_done_callback(self._task_done)
        self._ensure_monitor(bound.setup.name)
        return RunHandle(bound.run_id, self, task)

    async def _execute_owned(
        self,
        bound: BoundRun,
        executable: AgicDecl | FlowDecl,
        *,
        tracer: RunTracer | None,
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
        try:
            await _Execution(
                self,
                root=bound,
                emit=emit,
            ).execute(
                bound,
                executable,
            )
        except asyncio.CancelledError:
            self._ensure_terminal(bound.run_id, emit=emit, status="canceled")
        except Exception as exc:
            self._ensure_terminal(
                bound.run_id,
                emit=emit,
                status="failed",
                error=str(exc) or type(exc).__name__,
            )
        finally:
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
        control = self.store.accept_run_control(
            run_id=run_id,
            kind="steer",
            timing=timing,
            input=message,
            context={},
            request_id=request_id,
            created_at=utc_now(),
        )
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
            self._ensure_terminal(run_id, emit=self._handler(active), status="canceled")
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
        if task.cancelled():
            self._ensure_terminal(
                run_id,
                emit=self._handler(active),
                status="canceled",
            )
        elif (error := task.exception()) is not None:
            self._ensure_terminal(
                run_id,
                emit=self._handler(active),
                status="failed",
                error=str(error) or type(error).__name__,
            )
        with self._active_lock:
            for active_run_id, candidate in tuple(self._active.items()):
                if candidate is active:
                    self._active.pop(active_run_id, None)

    def _handler(self, active: _ActiveRun) -> Callable[[RunEvent], None]:
        def emit(event: RunEvent) -> None:
            with active.event_lock:
                event_run = _run_event_id(event)
                if event_run in active.ended:
                    return
                self._persist.on_event(event)
                self._update_control_state(event)
                self._track_active_run(event, active)
                if isinstance(event, RunEnd):
                    active.ended.add(event.run)
                if active.tracer is not None:
                    try:
                        active.tracer.on_event(event)
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
                item.index for item in event.input if isinstance(item, RunControlRef)
            )
            self.store.finish_run_controls(
                run_id=trace_run(event.step),
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

    def _ensure_monitor(self, agent_name: str) -> None:
        if self._monitor_task is None or self._monitor_task.done():
            self._monitor_task = asyncio.create_task(
                self._monitor_controls(), name=f"toolang-controls-{agent_name}"
            )

    async def _monitor_controls(self) -> None:
        while True:
            await asyncio.sleep(self._control_poll_interval)
            with self._active_lock:
                active = tuple(self._active.items())
            canceled: set[asyncio.Task[RunRecord]] = set()
            for run_id, owner in active:
                if owner.task.done() or owner.task in canceled:
                    continue
                controls = self.store.pending_run_controls(run_id=run_id, kind="stop")
                if any(control.timing == "immediate" for control in controls):
                    canceled.add(owner.task)
                    owner.task.cancel()

    def _ensure_terminal(
        self,
        run_id: str,
        *,
        emit: Callable[[RunEvent], None],
        status: Literal["failed", "canceled"],
        error: str | None = None,
    ) -> None:
        record = self.store.get_run(run_id=run_id)
        if record is not None and record.status not in {"pending", "running"}:
            return
        stop = next(
            iter(self.store.pending_run_controls(run_id=run_id, kind="stop")), None
        )
        emit(
            RunEnd(
                run=run_id,
                status=status,
                input=RunControlRef(index=stop.index) if stop is not None else None,
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
        emit: Callable[[RunEvent], None],
    ) -> None:
        self.executor = executor
        self.setup = root.setup
        config_layers = (root.state.root_config, root.state.home_config)
        self.model_aliases = parse_model_aliases(config_layers)
        self.default_models = parse_default_models(config_layers)
        self._emit_trace = emit
        self._run_outputs: dict[str, StepPath] = {}
        self._last_step_index: dict[str, int] = {}
        self._failed_runs: set[str] = set()

    @property
    def store(self) -> RunStore:
        return self.executor.store

    @property
    def model_providers(self) -> Mapping[str, ModelProvider]:
        return self.setup.model_providers

    @property
    def model_environ(self) -> Mapping[str, str]:
        return self.setup.model_environ

    @property
    def model_cache_dir(self) -> Path | None:
        return self.setup.model_cache_dir

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

        current = (
            dict(locals) if locals is not None else initial_locals(binding, executable)
        )
        self.emit(
            RunBegin(
                run=binding.run_id,
                input=RunControlRef(index=0),
                context=_run_context(binding, executable),
                started_at=utc_now(),
            )
        )
        try:
            if isinstance(executable, AgicDecl):
                result = await agic_run.execute(self, binding, executable, current)
                current["_"] = result
            else:
                result = await flow_run.execute(self, binding, executable, current)
        except asyncio.CancelledError as exc:
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
            self.emit(
                RunEnd(
                    run=binding.run_id,
                    status="canceled",
                    input=RunControlRef(index=control.index)
                    if control is not None
                    else None,
                    output=self.run_output(binding.run_id),
                    error=control_text(control) or "canceled",
                    finished_at=utc_now(),
                )
            )
            raise
        except Exception as exc:
            error = str(exc) or type(exc).__name__
            self._emit_system_failure(binding.run_id, error)
            self.emit(
                RunEnd(
                    run=binding.run_id,
                    status="failed",
                    output=self.run_output(binding.run_id),
                    error=error,
                    finished_at=utc_now(),
                )
            )
            raise
        self.emit(
            RunEnd(
                run=binding.run_id,
                status="finished",
                output=self.run_output(binding.run_id),
                finished_at=utc_now(),
            )
        )
        return result

    async def execute_child(
        self,
        parent: BoundRun,
        locals: Mapping[str, Local],
        step: StepPath,
        name: str,
        placement: Mapping[str, object] | None,
    ) -> Local:
        """Accept and execute one recursive child agic or flow run."""

        executable = _require_runnable(parent.state, name)
        binding = _child_binding(self, parent, executable, locals, placement)
        context = _run_context(binding, executable)
        self.store.accept_start(
            run_id=binding.run_id,
            parent=step,
            thread=binding.thread,
            input=binding.input,
            context=context,
            request_id=None,
            created_at=binding.created_at,
        )
        return await self.execute(binding, executable, locals=locals)

    async def parallel_children(
        self,
        binding: BoundRun,
        locals: Mapping[str, Local],
        parent: StepPath,
        runnable: str,
        inputs: Sequence[Any],
        *,
        limit: int | None,
    ) -> list[Any]:
        """Execute child runs concurrently with stable placement metadata."""

        semaphore = asyncio.Semaphore(limit or max(len(inputs), 1))
        lanes = limit or max(len(inputs), 1)

        async def execute(index: int, value: Any) -> Any:
            async with semaphore:
                child_locals = dict(locals)
                child_locals["_"] = Local(value, "item")
                result = await self.execute_child(
                    binding,
                    child_locals,
                    parent,
                    runnable,
                    {
                        "item": index,
                        "items": len(inputs),
                        "lane": index % lanes,
                        "lanes": lanes,
                    },
                )
                return result.value

        tasks = [
            asyncio.create_task(execute(index, value))
            for index, value in enumerate(inputs)
        ]
        try:
            return list(await asyncio.gather(*tasks))
        except BaseException:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

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
        return tuple(self.store.pending_run_controls(run_id=run_id, kind=kind))

    def steer_controls(
        self, run_id: str, statement: FlowStmt
    ) -> tuple[RunControlRecord, ...]:
        allowed = {"immediate", "next_step"}
        if statement_has_call(statement):
            allowed.add("next_call")
        return tuple(
            control
            for control in self.pending_controls(run_id, "steer")
            if control.timing in allowed
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
        if control is not None:
            raise _RunStopped(control)

    def record_output(self, run_id: str, path: StepPath) -> None:
        self._run_outputs[run_id] = path

    def run_output(self, run_id: str) -> OutputRef | None:
        path = self._run_outputs.get(run_id)
        return OutputRef(step=path) if path is not None else None

    def emit(self, event: RunEvent) -> None:
        self._emit_trace(event)
        if (
            isinstance(event, StepEnd)
            and event.kind == "model"
            and event.status == "finished"
        ):
            self.record_output(trace_run(event.step), event.step)
        if isinstance(event, StepBegin | StepEnd):
            run_id = trace_run(event.step)
            parent = trace_parent(event.step)
            index = trace_index(event.step)
            if parent == run_id and index is not None:
                self._last_step_index[run_id] = max(
                    self._last_step_index.get(run_id, -1),
                    index,
                )
            if isinstance(event, StepEnd) and event.status == "failed":
                self._failed_runs.add(run_id)

    def _emit_system_failure(self, run_id: str, error: str) -> None:
        if run_id in self._failed_runs:
            return
        path = trace_child_path(run_id, self._last_step_index.get(run_id, -1) + 1)
        started_at = utc_now()
        self.emit(
            StepBegin(
                step=path,
                kind="system",
                given={"runtime": "failure"},
                started_at=started_at,
            )
        )
        self.emit(
            StepEnd(
                step=path,
                kind="system",
                status="failed",
                output=(TextPart(text=error),),
                error=error,
                finished_at=utc_now(),
            )
        )


def _require_runnable(state: AgentState, name: str) -> AgicDecl | FlowDecl:
    if not name or name != name.strip():
        raise ValueError("run spec requires a canonical runnable name")
    program = state.program
    matches: tuple[AgicDecl | FlowDecl, ...] = (
        *(agic for agic in effective_agics(program) if agic.name == name),
        *(flow for flow in program.flows if flow.name == name),
    )
    if not matches:
        raise ToolangError(f"Runnable not found: {name}")
    if len(matches) > 1:
        raise ToolangError(f"Runnable name is not unique: {name}")
    return matches[0]


def _child_binding(
    context: _Execution,
    parent: BoundRun,
    executable: AgicDecl | FlowDecl,
    locals: Mapping[str, Local],
    placement: Mapping[str, object] | None,
) -> BoundRun:
    primary = locals.get("_", Local())
    text = value_text(primary.value) if primary.shape != "none" else ""
    return BoundRun(
        run_id=context.executor.ids.issue_run(),
        root_run_id=parent.root_run_id,
        thread=parent.thread,
        input=Message.user(text),
        args={
            name: local.value for name, local in locals.items() if name != "_"
        },
        model=parent.model,
        state=parent.state,
        setup=parent.setup,
        created_at=utc_now(),
        call="run",
        placement=dict(placement or {}),
    )


def _bind_run(spec: RunSpec, *, run_id: str) -> BoundRun:
    if not spec.thread or spec.thread != spec.thread.strip():
        raise ValueError("run spec requires a canonical thread id")
    return BoundRun(
        run_id=run_id,
        root_run_id=run_id,
        thread=spec.thread,
        input=Message(role="user", parts=tuple(spec.input)),
        args=dict(spec.args or {}),
        model=spec.model,
        state=spec.state,
        setup=spec.setup,
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
    if binding.args:
        context["args"] = dict(binding.args)
    if binding.placement:
        context["placement"] = dict(binding.placement)
    return context


def _validate_call(spec: RunSpec, executable: AgicDecl | FlowDecl) -> None:
    args = dict(spec.args or {})
    params = {param.name: param for param in executable.params}
    unknown = sorted(set(args) - set(params))
    if unknown:
        joined = ", ".join(unknown)
        raise ValueError(f"unknown arguments for {executable.name}: {joined}")
    missing = sorted(
        name for name, param in params.items() if not param.optional and name not in args
    )
    if missing:
        joined = ", ".join(missing)
        raise ValueError(f"missing arguments for {executable.name}: {joined}")
    if executable.input is None and spec.input:
        raise ValueError(f"{executable.name} does not accept primary input")
    if (
        executable.input is not None
        and not executable.input.optional
        and not spec.input
    ):
        raise ValueError(f"{executable.name} requires primary input")


def _run_event_id(event: RunEvent) -> str:
    if isinstance(event, RunBegin | RunEnd):
        return event.run
    return trace_run(event.step)
