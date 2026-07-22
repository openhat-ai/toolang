"""Run acceptance, control, and recursive execution."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import logging
from pathlib import Path
import threading
import time
from typing import Any, Literal, cast

from toolang.base.protocols.model import ModelProvider
from toolang.base.types.message import Message
from toolang.base.types.model import ModelAlias
from toolang.common.errors import ToolangError
from toolang.common.ids import allocate_run_id
from toolang.common.time import utc_now
from toolang.lang.ast import AgicDecl, FlowDecl, FlowStmt
from toolang.state.state import AgentState
from toolang.up.setup import AgentSetup

from ..events import RunBegin, RunEnd, RunEvent, RunTracer, StepBegin, StepEnd
from ..records import (
    OutputRef,
    RunControlRecord,
    RunControlRef,
    RunRecord,
    trace_run,
)
from ..store import RunStore
from ..types import RunControlKind, RunControlTiming, StepPath
from .common import (
    BoundRun,
    Local,
    bind_run_request,
    control_text,
    initial_locals,
    statement_has_call,
    value_text,
)
from .prepare import effective_agics, require_agic, select_origin_agic
from .request import ExecutableKind, RunRequest
from .persist import PersistSink

_LOGGER = logging.getLogger("toolang.run")


class _RunStopped(asyncio.CancelledError):
    def __init__(self, control: RunControlRecord) -> None:
        super().__init__(control_text(control) or "canceled")
        self.control = control


@dataclass(frozen=True, slots=True)
class _ActiveRun:
    task: asyncio.Task[object]
    tracer: RunTracer | None


class RunExecutor:
    """Accept, control, and execute runs against durable execution truth."""

    def __init__(
        self,
        *,
        root: Path,
        name: str,
        home: Path,
        id_state_path: Path,
        store: RunStore,
        model_aliases: Mapping[str, ModelAlias],
        default_models: Sequence[str],
        model_environ: Mapping[str, str],
        default_model_selector: str | None = None,
        allowed_model_selectors: Sequence[str] = (),
        control_poll_interval: float = 0.05,
    ) -> None:
        self.root = root
        self.name = name
        self.home = home
        self.id_state_path = id_state_path
        self.store = store
        self.model_aliases = dict(model_aliases)
        self.default_models = tuple(default_models)
        self.model_environ = dict(model_environ)
        self.model_cache_dir = root / ".runtime" / "model-cache"
        self.model_cache_refresh = False
        self.default_model_selector = default_model_selector
        self.allowed_model_selectors = tuple(allowed_model_selectors)
        self._persist = PersistSink(store)
        self._control_poll_interval = control_poll_interval
        self._active: dict[str, _ActiveRun] = {}
        self._active_lock = threading.Lock()
        self._monitor_task: asyncio.Task[None] | None = None

    def allocate_run_id(self) -> str:
        """Allocate one process-safe run id."""

        return allocate_run_id(self.id_state_path)

    async def close(self) -> None:
        """Cancel and await all runs owned by this process."""

        with self._active_lock:
            tasks = {active.task for active in self._active.values()}
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        monitor = self._monitor_task
        self._monitor_task = None
        if monitor is not None and not monitor.done():
            monitor.cancel()
            await asyncio.gather(monitor, return_exceptions=True)

    async def start(
        self,
        setup: AgentSetup,
        state: AgentState,
        request: RunRequest,
        *,
        tracer: RunTracer | None = None,
    ) -> RunRecord:
        """Accept and execute one top-level run to completion."""

        bound = bind_run_request(
            request,
            id_state_path=self.id_state_path,
            state=state,
            setup=setup,
        )
        if self.store.get_thread(thread_id=bound.thread_id) is None:
            self.store.create_thread(
                thread_id=bound.thread_id,
                origin=bound.origin,
                request_id=None,
                context={"source": request.origin},
                created_at=bound.created_at,
            )
        record, _control, owner = self.store.accept_start(
            run_id=bound.run_id,
            parent=None,
            thread=bound.thread_id,
            input=bound.input,
            context=_top_run_context(bound),
            request_id=request.request_id,
            created_at=bound.created_at,
        )
        if not owner:
            return record
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("run start requires an asyncio task")
        active = _ActiveRun(task=task, tracer=tracer)
        with self._active_lock:
            if bound.run_id in self._active:
                raise ValueError(f"run is already active: {bound.run_id}")
            self._active[bound.run_id] = active
        self._ensure_monitor()
        started_at = time.perf_counter()
        emit = self._handler(active)
        try:
            await _Execution(self, setup=setup, emit=emit).execute(
                bound,
                _resolve_executable(bound),
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

    def steer(
        self,
        *,
        run_id: str,
        message: Message,
        timing: RunControlTiming,
        request_id: str | None = None,
    ) -> RunControlRecord:
        """Persist one steer control for the process that owns the run."""

        control, _created = self.store.accept_run_control(
            run_id=run_id,
            kind="steer",
            timing=timing,
            input=message,
            context={},
            request_id=request_id,
            created_at=utc_now(),
        )
        return control

    def stop(
        self,
        *,
        run_id: str,
        timing: RunControlTiming = "immediate",
        request_id: str | None = None,
        reason: str | None = None,
    ) -> RunControlRecord:
        """Persist one stop control for the process that owns the run."""

        control, _created = self.store.accept_run_control(
            run_id=run_id,
            kind="stop",
            timing=timing,
            input=Message.user(reason) if reason else None,
            context={},
            request_id=request_id,
            created_at=utc_now(),
        )
        return control

    def _handler(self, active: _ActiveRun) -> Callable[[RunEvent], None]:
        def emit(event: RunEvent) -> None:
            if _event_is_after_canceled_run(self.store, event):
                return
            self._persist.on_event(event)
            self._update_control_state(event)
            self._track_active_run(event, active)
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

    def _ensure_monitor(self) -> None:
        if self._monitor_task is None or self._monitor_task.done():
            self._monitor_task = asyncio.create_task(
                self._monitor_controls(), name=f"toolang-controls-{self.name}"
            )

    async def _monitor_controls(self) -> None:
        while True:
            await asyncio.sleep(self._control_poll_interval)
            with self._active_lock:
                active = tuple(self._active.items())
            canceled: set[asyncio.Task[object]] = set()
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
        setup: AgentSetup,
        emit: Callable[[RunEvent], None],
    ) -> None:
        self.executor = executor
        self.setup = setup
        self._emit_trace = emit
        self._run_outputs: dict[str, StepPath] = {}

    def __getattr__(self, name: str) -> Any:
        return getattr(self.executor, name)

    @property
    def model_providers(self) -> Mapping[str, ModelProvider]:
        return self.setup.model_providers

    async def execute(
        self,
        binding: BoundRun,
        executable: AgicDecl | FlowDecl,
        *,
        parent: StepPath | None = None,
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
                context=_run_context(binding, executable, parent=parent),
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
            self.emit(
                RunEnd(
                    run=binding.run_id,
                    status="failed",
                    output=self.run_output(binding.run_id),
                    error=str(exc) or type(exc).__name__,
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

        executable = _resolve_runnable(parent, name)
        binding = _child_binding(self, parent, executable, locals, placement)
        context = _run_context(binding, executable, parent=step)
        _run, _control, owner = self.store.accept_start(
            run_id=binding.run_id,
            parent=step,
            thread=binding.thread_id,
            input=binding.input,
            context=context,
            request_id=None,
            created_at=binding.created_at,
        )
        if not owner:
            raise RuntimeError(f"child run already exists: {binding.run_id}")
        return await self.execute(binding, executable, parent=step, locals=locals)

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
        if (
            isinstance(event, StepEnd)
            and event.kind == "model"
            and event.status == "finished"
        ):
            self.record_output(trace_run(event.step), event.step)
        self._emit_trace(event)


def _resolve_executable(binding: BoundRun) -> AgicDecl | FlowDecl:
    program = binding.state.program
    if binding.executable_kind == "flow":
        flow_name = binding.executable_name or "main"
        flow = program.find_flow(flow_name)
        if flow is None:
            raise ToolangError(f"Flow not found: {flow_name}")
        return flow
    if binding.executable_name is not None:
        return require_agic(program, binding.executable_name)
    return select_origin_agic(program, origin=binding.origin, agic_name=None)


def _resolve_runnable(binding: BoundRun, name: str) -> AgicDecl | FlowDecl:
    program = binding.state.program
    for agic in effective_agics(program):
        if agic.name == name:
            return agic
    for flow in program.flows:
        if flow.name == name:
            return flow
    raise ToolangError(f"Runnable not found: {name}")


def _child_binding(
    context: _Execution,
    parent: BoundRun,
    executable: AgicDecl | FlowDecl,
    locals: Mapping[str, Local],
    placement: Mapping[str, object] | None,
) -> BoundRun:
    primary = locals.get("_", Local())
    metadata = {
        key: value for key, value in parent.context.items() if key != "invoke_params"
    }
    metadata.update(
        {
            "root": parent.context.get("root") or parent.run_id,
            "call": "run",
            "invoke_params": {
                name: local.value for name, local in locals.items() if name != "_"
            },
            "placement": dict(placement or {}),
        }
    )
    text = value_text(primary.value) if primary.shape != "none" else ""
    return BoundRun(
        run_id=allocate_run_id(context.id_state_path),
        origin=parent.origin,
        thread_id=parent.thread_id,
        executable_kind=cast(ExecutableKind, executable.kind),
        executable_name=executable.name,
        input=Message.user(text),
        input_text=text,
        model_selector=parent.model_selector,
        context=metadata,
        state=parent.state,
        setup=parent.setup,
        created_at=utc_now(),
    )


def _top_run_context(binding: BoundRun) -> dict[str, object]:
    return {
        **dict(binding.context),
        "origin": binding.origin,
        "root": binding.run_id,
        "state_fingerprint": binding.state.fingerprint,
        "executable": {
            "kind": binding.executable_kind,
            "name": binding.executable_name,
        },
        "call": "top",
    }


def _run_context(
    binding: BoundRun,
    executable: AgicDecl | FlowDecl,
    *,
    parent: StepPath | None,
) -> dict[str, object]:
    root = (
        str(binding.context.get("root") or trace_run(parent))
        if parent is not None
        else binding.run_id
    )
    return {
        **dict(binding.context),
        "origin": binding.origin,
        "root": root,
        "state_fingerprint": binding.state.fingerprint,
        "executable": {"kind": executable.kind, "name": executable.name},
        "call": "top" if parent is None else "run",
    }


def _event_is_after_canceled_run(store: RunStore, event: RunEvent) -> bool:
    if isinstance(event, RunBegin):
        return False
    event_run = (
        event.run
        if isinstance(event, RunEnd)
        else trace_run(getattr(event, "step", ""))
    )
    if not event_run:
        event_run = getattr(event, "run", "")
    record = store.get_run(run_id=event_run)
    return record is not None and record.status == "canceled"
