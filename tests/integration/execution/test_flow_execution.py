from __future__ import annotations

import asyncio
from multiprocessing import get_context
from pathlib import Path
import sqlite3
import threading
from types import SimpleNamespace
from typing import Any, cast

import pytest

from toolang.base.types.message import ImagePart, Message, TextPart
from toolang.base.types.run import ModelCall
from toolang.common.errors import ToolangError
from toolang.common.ids import IdIssuer
from toolang.execution.events import (
    RunBegin,
    RunEnd,
    RunEvent,
    RunTracer,
    StepBegin,
    StepEnd,
    ThreadEvent,
    ThreadListener,
)
from toolang.execution.executor import RunExecutor, RunSpec
from toolang.execution.executor.common import BoundRun, Local
from toolang.execution.executor.executor import _Execution
from toolang.execution.executor.runs import agic as agic_run
from toolang.execution.inspection import ExecutionInspection
from toolang.execution.records import OutputRef, RunControlRef, ThreadControlRef
from toolang.execution.executor.persist import PersistSink
from toolang.execution.store import RunStore
from toolang.execution.threads import ThreadManager
from toolang.execution.types import ThreadPrefix
from toolang.lang.ast import (
    AgicDecl,
    FlowDecl,
    LetStmt,
    Parameter,
    Program,
    RunStmt,
    Span,
)
from toolang.up.setup import AgentSetup


class _RecordingTracer(RunTracer):
    def __init__(self, store: RunStore, *, fail: bool = False) -> None:
        self.store = store
        self.fail = fail
        self.events: list[RunEvent] = []
        self.thread_ids: set[int] = set()
        self._handling = False

    async def on_event(self, event: RunEvent) -> None:
        assert not self._handling
        self._handling = True
        run_id = (
            event.run
            if isinstance(event, RunBegin | RunEnd)
            else event.step.split("/", 1)[0]
        )
        assert self.store.get_run(run_id=run_id) is not None
        self.thread_ids.add(threading.get_ident())
        try:
            await asyncio.sleep(0)
            self.events.append(event)
            if self.fail:
                raise RuntimeError("tracer failed")
        finally:
            self._handling = False


class _RecordingThreadListener(ThreadListener):
    def __init__(self) -> None:
        self.events: list[ThreadEvent] = []

    def on_event(self, event: ThreadEvent) -> None:
        self.events.append(event)


def _executor(tmp_path: Path) -> RunExecutor:
    store = RunStore(tmp_path / ".runtime" / "runs.db")
    store.create_thread(thread_id="term_test")
    return RunExecutor(store, IdIssuer(tmp_path / ".runtime" / "ids.json"))


def _state(*flows: FlowDecl) -> Any:
    return cast(
        Any,
        SimpleNamespace(
            program=Program(span=Span(line=1), flows=flows),
            program_source="agents/alice/agent.too",
            root_config={},
            home_config={},
            fingerprint="state-test",
        ),
    )


def _setup() -> AgentSetup:
    return AgentSetup(
        home=Path("/agent/alice"),
        name="alice",
        tools={},
        model_providers={},
        model_adapters={},
        model_environ={},
    )


def _capture_model_text(store: RunStore, body: str) -> str:
    captured = store.capture_model_call(
        target={
            "ref": "test/model",
            "provider": "test",
            "name": "model",
            "model": "model",
            "adapter": "test",
        },
        call=ModelCall(instructions=body, messages=[]),
    )
    call = captured.get("call")
    if not isinstance(call, dict) or not isinstance(call.get("instructions"), str):
        raise AssertionError("captured model instructions are missing")
    return call["instructions"]


async def _start(
    executor: RunExecutor,
    setup: AgentSetup,
    state: Any,
    name: str,
    *,
    run_id: str | None = None,
    thread_id: str = "term_test",
    request_id: str | None = None,
    tracer: RunTracer | None = None,
) -> Any:
    return await executor.start(
        RunSpec(
            setup=setup,
            state=state,
            thread=thread_id,
            runnable=name,
        ),
        run_id=run_id,
        request_id=request_id,
        tracer=tracer,
    )


def test_run_executor_persists_before_tracing(tmp_path: Path) -> None:
    flow = FlowDecl(
        name="pipeline",
        stmts=(LetStmt(value="done", span=Span(line=2)),),
        span=Span(line=1),
    )
    executor = _executor(tmp_path)
    store = executor.store
    tracer = _RecordingTracer(store)
    owner_thread = threading.get_ident()

    record = asyncio.run(
        _start(executor, _setup(), _state(flow), flow.name, tracer=tracer)
    )

    assert record.status == "finished"
    assert store.run_output_text(run_id=record.id) == "done"
    assert [event.type for event in tracer.events] == [
        "run_begin",
        "step_begin",
        "step_end",
        "run_end",
    ]
    assert tracer.thread_ids == {owner_thread}
    start = store.get_run_control(run_id=record.id, index=0)
    assert start is not None and start.status == "finished"
    detail = ExecutionInspection(store).run_detail(record.id)
    assert detail is not None
    assert detail.runnable_kind == "flow"
    assert detail.runnable_name == "pipeline"
    assert detail.input == Message(role="user")
    assert [control.index for control in detail.controls] == [0]
    assert detail.controls[0].message == detail.input
    assert [step.kind for step in detail.steps] == ["system"]
    assert detail.steps[0].output == [TextPart(text="done")]
    assert not hasattr(detail.steps[0], "message")
    asyncio.run(executor.shutdown())


def test_run_executor_validates_args_against_runnable_params(
    tmp_path: Path,
) -> None:
    flow = FlowDecl(
        name="pipeline",
        params=(Parameter(name="focus", span=Span(line=1)),),
        span=Span(line=1),
    )
    executor = _executor(tmp_path)
    setup = _setup()
    state = _state(flow)

    async def scenario() -> None:
        with pytest.raises(ValueError, match="missing arguments.*focus"):
            executor.start(
                RunSpec(
                    setup=setup,
                    state=state,
                    thread="term_test",
                    runnable=flow.name,
                )
            )
        with pytest.raises(ValueError, match="unknown arguments.*other"):
            executor.start(
                RunSpec(
                    setup=setup,
                    state=state,
                    thread="term_test",
                    runnable=flow.name,
                    args={
                        "focus": (TextPart("events"),),
                        "other": True,
                    },
                )
            )
        record = await executor.start(
            RunSpec(
                setup=setup,
                state=state,
                thread="term_test",
                runnable=flow.name,
                args={"focus": (TextPart("events"),)},
            )
        )
        assert record.status == "finished"

    asyncio.run(scenario())
    asyncio.run(executor.shutdown())


def test_run_executor_rejects_lossy_input_before_acceptance(
    tmp_path: Path,
) -> None:
    flow = FlowDecl(
        name="pipeline",
        input=Parameter(
            name="_",
            type_name="Text",
            span=Span(line=1),
        ),
        span=Span(line=1),
    )
    executor = _executor(tmp_path)

    async def scenario() -> None:
        with pytest.raises(ToolangError, match="non-text parts"):
            executor.start(
                RunSpec(
                    setup=_setup(),
                    state=_state(flow),
                    thread="term_test",
                    runnable=flow.name,
                    input=(ImagePart(file_id="image-1"),),
                )
            )

    asyncio.run(scenario())
    assert executor.store.list_runs() == []
    asyncio.run(executor.shutdown())


def test_top_level_agic_has_no_containing_step_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agic = AgicDecl(name="default", span=Span(line=1))
    state = cast(
        Any,
        SimpleNamespace(
            program=Program(span=Span(line=1), agics=(agic,)),
            program_source="agents/alice/agent.too",
            root_config={},
            home_config={},
            fingerprint="state-test",
        ),
    )
    executor = _executor(tmp_path)
    store = executor.store
    tracer = _RecordingTracer(store)

    async def execute_agic(*_args: Any, **_kwargs: Any) -> Local:
        return Local("done", "item")

    monkeypatch.setattr(agic_run, "execute", execute_agic)
    record = asyncio.run(
        _start(executor, _setup(), state, "default", tracer=tracer)
    )

    assert record.status == "finished"
    assert [event.type for event in tracer.events] == ["run_begin", "run_end"]
    assert store.list_steps(run_id=record.id) == []
    asyncio.run(executor.shutdown())


def test_runtime_emits_and_persists_system_failure_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agic = AgicDecl(name="default", span=Span(line=1))
    state = cast(
        Any,
        SimpleNamespace(
            program=Program(span=Span(line=1), agics=(agic,)),
            program_source="agents/alice/agent.too",
            root_config={},
            home_config={},
            fingerprint="state-test",
        ),
    )
    executor = _executor(tmp_path)
    tracer = _RecordingTracer(executor.store)

    async def fail_agic(*_args: Any, **_kwargs: Any) -> Local:
        raise RuntimeError("runtime failed")

    monkeypatch.setattr(agic_run, "execute", fail_agic)
    record = asyncio.run(
        _start(executor, _setup(), state, "default", tracer=tracer)
    )

    assert record.status == "failed"
    assert [event.type for event in tracer.events] == [
        "run_begin",
        "step_begin",
        "step_end",
        "run_end",
    ]
    steps = executor.store.list_steps(run_id=record.id)
    assert len(steps) == 1
    assert steps[0].kind == "system"
    assert steps[0].status == "failed"
    assert steps[0].error == "runtime failed"
    assert steps[0].given == {"runtime": "failure"}
    asyncio.run(executor.shutdown())


def test_event_delivery_does_not_read_run_state_per_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    flow = FlowDecl(
        name="pipeline",
        stmts=(LetStmt(value="done", span=Span(line=2)),),
        span=Span(line=1),
    )
    executor = _executor(tmp_path)
    original = executor.store.get_run
    reads = 0

    def get_run(*, run_id: str):
        nonlocal reads
        reads += 1
        return original(run_id=run_id)

    monkeypatch.setattr(executor.store, "get_run", get_run)
    record = asyncio.run(_start(executor, _setup(), _state(flow), flow.name))

    assert record.status == "finished"
    assert reads == 1
    asyncio.run(executor.shutdown())


def test_start_rejects_ambiguous_runnable_name(tmp_path: Path) -> None:
    runnable = "shared"
    state = cast(
        Any,
        SimpleNamespace(
            program=Program(
                span=Span(line=1),
                agics=(AgicDecl(name=runnable, span=Span(line=1)),),
                flows=(FlowDecl(name=runnable, span=Span(line=2)),),
            ),
            program_source="agents/alice/agent.too",
            root_config={},
            home_config={},
            fingerprint="state-test",
        ),
    )
    executor = _executor(tmp_path)

    async def scenario() -> None:
        with pytest.raises(ToolangError, match="Runnable name is not unique"):
            executor.start(
                RunSpec(
                    setup=_setup(),
                    state=state,
                    thread="term_test",
                    runnable=runnable,
                )
            )

    asyncio.run(scenario())
    asyncio.run(executor.shutdown())


def test_tracer_failure_does_not_fail_execution(tmp_path: Path) -> None:
    flow = FlowDecl(name="pipeline", span=Span(line=1))
    executor = _executor(tmp_path)
    store = executor.store

    record = asyncio.run(
        _start(
            executor,
            _setup(),
            _state(flow),
            flow.name,
            tracer=_RecordingTracer(store, fail=True),
        )
    )

    assert record.status == "finished"
    asyncio.run(executor.shutdown())


def test_run_handle_shields_execution_from_waiter_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    flow = FlowDecl(name="waiting", span=Span(line=1))
    executor = _executor(tmp_path)

    async def wait_forever(*_args: Any, **_kwargs: Any) -> Any:
        await asyncio.sleep(30)

    monkeypatch.setattr(_Execution, "execute", wait_forever)

    async def scenario() -> Any:
        handle = executor.start(
            RunSpec(
                setup=_setup(),
                state=_state(flow),
                thread="term_test",
                runnable=flow.name,
            )
        )
        assert handle.executor is executor
        accepted = executor.store.get_run(run_id=handle.run_id)
        assert accepted is not None and accepted.status == "pending"
        waiter = asyncio.ensure_future(handle)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert not handle.task.cancelled()
        handle.stop(reason="done")
        return await asyncio.wait_for(handle, timeout=2)

    record = asyncio.run(scenario())

    assert record.status == "canceled"
    asyncio.run(executor.shutdown())


def test_duplicate_start_request_is_rejected(tmp_path: Path) -> None:
    flow = FlowDecl(name="pipeline", span=Span(line=1))
    executor = _executor(tmp_path)
    store = executor.store
    first_tracer = _RecordingTracer(store)
    store.create_thread(thread_id="term_idempotent")

    first = asyncio.run(
        _start(
            executor,
            _setup(),
            _state(flow),
            flow.name,
            run_id="run_unique",
            thread_id="term_idempotent",
            request_id="start-unique",
            tracer=first_tracer,
        )
    )
    with pytest.raises(ValueError, match="run already exists"):
        asyncio.run(
            _start(
                executor,
                _setup(),
                _state(flow),
                flow.name,
                run_id="run_unique",
                thread_id="term_idempotent",
                request_id="start-unique",
            )
        )

    assert first.status == "finished"
    assert len(store.list_run_controls(run_id=first.id)) == 1
    asyncio.run(executor.shutdown())


def test_child_runs_are_persisted_without_starting_event(tmp_path: Path) -> None:
    child = FlowDecl(
        name="child",
        stmts=(LetStmt(value="done", span=Span(line=2)),),
        span=Span(line=1),
    )
    parent = FlowDecl(
        name="parent",
        stmts=(RunStmt(runnable="child", span=Span(line=4)),),
        span=Span(line=3),
    )
    executor = _executor(tmp_path)
    store = executor.store
    tracer = _RecordingTracer(store)

    root = asyncio.run(
        _start(
            executor,
            _setup(),
            _state(parent, child),
            parent.name,
            tracer=tracer,
        )
    )

    runs = store.list_runs(limit=None)
    child_run = next(run for run in runs if run.id != root.id)
    assert child_run.parent == f"{root.id}/0"
    assert child_run.status == "finished"
    assert [event.type for event in tracer.events] == [
        "run_begin",
        "step_begin",
        "run_begin",
        "step_begin",
        "step_end",
        "run_end",
        "step_end",
        "run_end",
    ]
    assert [event.type for event in tracer.events].count("run_begin") == 2
    assert not any(event.type == "run_starting" for event in tracer.events)
    asyncio.run(executor.shutdown())


def test_parallel_children_preserve_input_and_output_types(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    child = FlowDecl(
        name="child",
        input=Parameter(name="_", type_name="Text", span=Span(line=1)),
        output="Number",
        span=Span(line=1),
    )
    parent = FlowDecl(name="parent", span=Span(line=2))
    executor = _executor(tmp_path)
    setup = _setup()
    state = _state(parent, child)
    binding = BoundRun(
        run_id="run_root",
        root_run_id="run_root",
        thread="term_test",
        input=Message.user("input"),
        args={},
        model=None,
        state=state,
        setup=setup,
        created_at="2026-01-01T00:00:00Z",
    )

    async def emit(_event: RunEvent) -> None:
        return None

    execution = _Execution(executor, root=binding, emit=emit)
    observed_types: list[str | None] = []

    async def execute_child(
        _parent: BoundRun,
        child_locals: dict[str, Local],
        _step: str,
        _name: str,
        _placement: dict[str, object] | None,
    ) -> Local:
        observed_types.append(child_locals["_"].type_name)
        return Local(1, "item", type_name="Number")

    monkeypatch.setattr(execution, "execute_child", execute_child)

    result = asyncio.run(
        execution.parallel_children(
            binding,
            {"_": Local(["one", "two"], "list", type_name="Text")},
            "run_root/0",
            child.name,
            ["one", "two"],
            limit=2,
        )
    )

    assert result == Local([1, 1], "list", type_name="Number")
    assert observed_types == ["Text", "Text"]
    asyncio.run(executor.shutdown())


def test_flow_step_events_carry_complete_input_references(tmp_path: Path) -> None:
    child = FlowDecl(
        name="child",
        stmts=(LetStmt(value="done", span=Span(line=2)),),
        span=Span(line=1),
    )
    parent = FlowDecl(
        name="parent",
        stmts=(
            RunStmt(runnable="child", span=Span(line=4)),
            RunStmt(runnable="child", span=Span(line=5)),
        ),
        span=Span(line=3),
    )
    executor = _executor(tmp_path)

    root = asyncio.run(
        _start(executor, _setup(), _state(parent, child), parent.name)
    )

    steps = [
        step
        for step in executor.store.list_steps(run_id=root.id)
        if step.parent == root.id
    ]
    assert [step.input for step in steps] == [
        (RunControlRef(),),
        (OutputRef(step=f"{root.id}/0"),),
    ]
    asyncio.run(executor.shutdown())


def test_steer_control_is_finished_when_step_consumes_it(tmp_path: Path) -> None:
    flow = FlowDecl(
        name="pipeline",
        stmts=(LetStmt(value="default", span=Span(line=2)),),
        span=Span(line=1),
    )
    executor = _executor(tmp_path)
    store = executor.store

    class SteeringTracer(RunTracer):
        control_index: int | None = None

        async def on_event(self, event: RunEvent) -> None:
            if isinstance(event, RunBegin) and self.control_index is None:
                control = executor.steer(
                    run_id=event.run,
                    message=Message.user("steered"),
                    timing="next_step",
                    request_id="steer-1",
                )
                self.control_index = control.index

    tracer = SteeringTracer()
    record = asyncio.run(
        _start(executor, _setup(), _state(flow), flow.name, tracer=tracer)
    )

    assert tracer.control_index is not None
    control = store.get_run_control(run_id=record.id, index=tracer.control_index)
    assert control is not None and control.status == "finished"
    steps = store.list_steps(run_id=record.id)
    assert steps[0].input == (RunControlRef(index=tracer.control_index),)
    asyncio.run(executor.shutdown())


def test_unreachable_control_fails_when_run_ends(tmp_path: Path) -> None:
    flow = FlowDecl(name="pipeline", span=Span(line=1))
    executor = _executor(tmp_path)
    store = executor.store

    class SteeringTracer(RunTracer):
        control_index: int | None = None

        async def on_event(self, event: RunEvent) -> None:
            if isinstance(event, RunBegin):
                self.control_index = executor.steer(
                    run_id=event.run,
                    message=Message.user("unused"),
                    timing="next_call",
                ).index

    tracer = SteeringTracer()
    record = asyncio.run(
        _start(executor, _setup(), _state(flow), flow.name, tracer=tracer)
    )

    assert tracer.control_index is not None
    control = store.get_run_control(run_id=record.id, index=tracer.control_index)
    assert control is not None
    assert control.status == "failed"
    assert control.error == "run ended before the control could be applied"
    asyncio.run(executor.shutdown())


def test_run_control_request_id_is_unique_across_runs(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs.db")
    store.create_thread(thread_id="term_test")
    store.create_thread(thread_id="term_other")
    store.accept_start(
        run_id="run_test",
        parent=None,
        thread="term_test",
        input=Message.user("hello"),
        context={},
        request_id="start-1",
        created_at="2026-01-01T00:00:00Z",
    )
    store.accept_start(
        run_id="run_other",
        parent=None,
        thread="term_other",
        input=Message.user("hello"),
        context={},
        request_id=None,
        created_at="2026-01-01T00:00:00Z",
    )

    first = store.accept_run_control(
        run_id="run_test",
        kind="steer",
        timing="next_step",
        input=Message.user("continue"),
        context={},
        request_id="steer-1",
        created_at="2026-01-01T00:00:01Z",
    )
    with pytest.raises(
        ValueError, match="run control request already exists: steer-1"
    ):
        store.accept_run_control(
            run_id="run_other",
            kind="steer",
            timing="next_step",
            input=Message.user("continue"),
            context={},
            request_id="steer-1",
            created_at="2026-01-01T00:00:03Z",
        )

    assert first.run == "run_test"
    store.close()


def test_run_control_acceptance_rejects_invalid_runtime_values(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs.db")
    store.create_thread(thread_id="term_test")
    store.accept_start(
        run_id="run_test",
        parent=None,
        thread="term_test",
        input=Message.user("hello"),
        context={},
        request_id=None,
        created_at="2026-01-01T00:00:00Z",
    )

    with pytest.raises(ValueError, match="unsupported run control kind"):
        store.accept_run_control(
            run_id="run_test",
            kind=cast(Any, "start"),
            timing="immediate",
            input=Message.user("duplicate start"),
            context={},
            request_id=None,
            created_at="2026-01-01T00:00:01Z",
        )
    with pytest.raises(ValueError, match="unsupported run control timing"):
        store.accept_run_control(
            run_id="run_test",
            kind="stop",
            timing=cast(Any, "later"),
            input=None,
            context={},
            request_id=None,
            created_at="2026-01-01T00:00:01Z",
        )
    with pytest.raises(ValueError, match="steer control requires input"):
        store.accept_run_control(
            run_id="run_test",
            kind="steer",
            timing="next_step",
            input=None,
            context={},
            request_id=None,
            created_at="2026-01-01T00:00:01Z",
        )
    store.close()


def test_persist_sink_does_not_update_control_status(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs.db")
    store.create_thread(thread_id="term_test")
    store.accept_start(
        run_id="run_test",
        parent=None,
        thread="term_test",
        input=Message.user("hello"),
        context={},
        request_id=None,
        created_at="2026-01-01T00:00:00Z",
    )
    sink = PersistSink(store)

    sink.on_event(
        RunBegin(
            run="run_test",
            input=RunControlRef(index=0),
            started_at="2026-01-01T00:00:01Z",
        )
    )

    control = store.get_run_control(run_id="run_test", index=0)
    assert control is not None and control.status == "pending"
    store.close()


def test_thread_manager_emits_only_successful_events(tmp_path: Path) -> None:
    executor = _executor(tmp_path)
    listener = _RecordingThreadListener()
    manager = ThreadManager(executor.store, executor.ids, listener=listener)

    thread_id = manager.create(prefix=ThreadPrefix.TERM)
    created = executor.store.get_thread(thread_id=thread_id)

    assert created is not None and created.created_by.index == 0
    assert [event.type for event in listener.events] == ["thread_created"]
    with pytest.raises(ValueError, match="thread has no runs"):
        manager.fork(thread_id=thread_id, run_id="run_missing")
    assert [event.type for event in listener.events] == ["thread_created"]
    asyncio.run(executor.shutdown())


def test_thread_create_rejects_duplicate_request_id(
    tmp_path: Path,
) -> None:
    executor = _executor(tmp_path)
    listener = _RecordingThreadListener()
    manager = ThreadManager(executor.store, executor.ids, listener=listener)

    first = manager.create(prefix=ThreadPrefix.TERM, request_id="create-thread")
    with pytest.raises(ValueError, match="thread control request already exists"):
        manager.create(prefix=ThreadPrefix.TERM, request_id="create-thread")

    assert executor.store.get_thread(thread_id=first) is not None
    assert [event.type for event in listener.events] == ["thread_created"]
    asyncio.run(executor.shutdown())


def test_thread_request_id_is_unique_across_thread_kinds(tmp_path: Path) -> None:
    executor = _executor(tmp_path)
    manager = ThreadManager(executor.store, executor.ids)

    manager.create(prefix=ThreadPrefix.TERM, request_id="create-thread")

    with pytest.raises(ValueError, match="thread control request already exists"):
        manager.create(prefix=ThreadPrefix.WEB, request_id="create-thread")
    asyncio.run(executor.shutdown())


def test_thread_inspection_starts_from_thread_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executor = _executor(tmp_path)
    store = executor.store
    created = ThreadManager(store, executor.ids).create(prefix=ThreadPrefix.TERM)
    inspection = ExecutionInspection(store)
    monkeypatch.setattr(
        store,
        "list_runs",
        lambda **_kwargs: pytest.fail("thread inspection must not scan all runs"),
    )

    threads = inspection.list_threads(limit=None)

    projected = next(thread for thread in threads if thread.id == created)
    assert projected.run_count == 0
    asyncio.run(executor.shutdown())


def test_thread_fork_and_rewind_use_control_refs_without_copying_runs(
    tmp_path: Path,
) -> None:
    executor = _executor(tmp_path)
    store = executor.store
    listener = _RecordingThreadListener()
    manager = ThreadManager(store, executor.ids, listener=listener)
    created = manager.create(prefix=ThreadPrefix.TERM)
    anchor_id = "run_anchor"
    store.accept_start(
        run_id=anchor_id,
        parent=None,
        thread=created,
        input=Message.user("hello"),
        context={"runnable": {"kind": "flow", "name": "test"}},
        request_id=None,
        created_at="2026-01-01T00:00:00Z",
    )
    sink = PersistSink(store)
    sink.on_event(
        RunBegin(
            run=anchor_id,
            input=RunControlRef(index=0),
            started_at="2026-01-01T00:00:01Z",
        )
    )
    store.finish_run_controls(
        run_id=anchor_id, indexes=(0,), finished_at="2026-01-01T00:00:01Z"
    )
    sink.on_event(
        RunEnd(
            run=anchor_id,
            status="finished",
            finished_at="2026-01-01T00:00:02Z",
        )
    )

    forked = manager.fork(thread_id=created)

    assert store.list_runs(thread_id=forked, limit=None) == []
    assert [
        run.id
        for run in store.list_thread_history_chronological(
            thread_id=forked
        )
    ] == [anchor_id]
    assert manager.rewind(thread_id=created, run_id=anchor_id) is None
    rewound = store.get_thread(thread_id=created)
    anchor = store.get_run(run_id=anchor_id)
    assert anchor is not None and rewound is not None
    assert anchor.superseded_by == rewound.head
    assert [event.type for event in listener.events] == [
        "thread_created",
        "thread_forked",
        "thread_rewound",
    ]
    asyncio.run(executor.shutdown())


def test_thread_fork_rejects_duplicate_request_without_starting_runs(
    tmp_path: Path,
) -> None:
    executor = _executor(tmp_path)
    store = executor.store
    listener = _RecordingThreadListener()
    manager = ThreadManager(store, executor.ids, listener=listener)
    created = manager.create(prefix=ThreadPrefix.TERM)
    store.accept_start(
        run_id="run_anchor",
        parent=None,
        thread=created,
        input=Message.user("hello"),
        context={"runnable": {"kind": "flow", "name": "test"}},
        request_id=None,
        created_at="2026-01-01T00:00:00Z",
    )
    sink = PersistSink(store)
    sink.on_event(
        RunBegin(
            run="run_anchor",
            input=RunControlRef(index=0),
            started_at="2026-01-01T00:00:01Z",
        )
    )
    sink.on_event(
        RunEnd(
            run="run_anchor",
            status="finished",
            finished_at="2026-01-01T00:00:02Z",
        )
    )

    forked = manager.fork(
        thread_id=created,
        request_id="fork-thread",
    )
    with pytest.raises(ValueError, match="thread control request already exists"):
        manager.fork(
            thread_id=created,
            request_id="fork-thread",
        )
    manager.rewind(
        thread_id=created,
        request_id="rewind-thread",
    )

    assert store.get_thread(thread_id=forked) is not None
    assert [
        run.id for run in store.list_runs(limit=None, include_superseded=True)
    ] == ["run_anchor"]
    assert [event.type for event in listener.events] == [
        "thread_created",
        "thread_forked",
        "thread_rewound",
    ]
    asyncio.run(executor.shutdown())


def test_rewind_uses_durable_acceptance_order_instead_of_timestamps(
    tmp_path: Path,
) -> None:
    executor = _executor(tmp_path)
    store = executor.store
    manager = ThreadManager(store, executor.ids)
    created = manager.create(prefix=ThreadPrefix.TERM)
    sink = PersistSink(store)
    timestamp = "2026-01-01T00:00:00Z"
    for run_id in ("run_before", "run_anchor", "run_after"):
        store.accept_start(
            run_id=run_id,
            parent=None,
            thread=created,
            input=Message.user(run_id),
            context={"runnable": {"kind": "flow", "name": "test"}},
            request_id=None,
            created_at=timestamp,
        )
        sink.on_event(
            RunBegin(
                run=run_id,
                input=RunControlRef(index=0),
                started_at=timestamp,
            )
        )
        sink.on_event(RunEnd(run=run_id, status="finished", finished_at=timestamp))

    manager.rewind(thread_id=created, run_id="run_anchor")

    before = store.get_run(run_id="run_before")
    anchor = store.get_run(run_id="run_anchor")
    after = store.get_run(run_id="run_after")
    rewound = store.get_thread(thread_id=created)
    assert before is not None and before.superseded_by is None
    assert rewound is not None
    assert anchor is not None and anchor.superseded_by == rewound.head
    assert after is not None and after.superseded_by == rewound.head
    asyncio.run(executor.shutdown())


def test_rewind_can_trim_inherited_fork_history(tmp_path: Path) -> None:
    executor = _executor(tmp_path)
    store = executor.store
    manager = ThreadManager(store, executor.ids)
    source = manager.create(prefix=ThreadPrefix.TERM)
    sink = PersistSink(store)
    for run_id in ("run_a", "run_b"):
        store.accept_start(
            run_id=run_id,
            parent=None,
            thread=source,
            input=Message.user(run_id),
            context={"runnable": {"kind": "flow", "name": "test"}},
            request_id=None,
            created_at="2026-01-01T00:00:00Z",
        )
        sink.on_event(
            RunBegin(
                run=run_id,
                input=RunControlRef(index=0),
                started_at="2026-01-01T00:00:01Z",
            )
        )
        sink.on_event(
            RunEnd(
                run=run_id,
                status="finished",
                finished_at="2026-01-01T00:00:02Z",
            )
        )

    forked = manager.fork(thread_id=source)
    manager.rewind(thread_id=forked)

    assert [
        run.id for run in store.list_thread_history_chronological(thread_id=forked)
    ] == ["run_a"]
    source_anchor = store.get_run(run_id="run_b")
    assert source_anchor is not None and source_anchor.superseded_by is None
    asyncio.run(executor.shutdown())


def test_implicit_thread_anchor_ignores_child_runs(tmp_path: Path) -> None:
    executor = _executor(tmp_path)
    store = executor.store
    manager = ThreadManager(store, executor.ids)
    source = manager.create(prefix=ThreadPrefix.TERM)
    sink = PersistSink(store)
    for run_id, parent in (("run_root", None), ("run_child", "run_root/0")):
        store.accept_start(
            run_id=run_id,
            parent=parent,
            thread=source,
            input=Message.user(run_id),
            context={"runnable": {"kind": "flow", "name": "test"}},
            request_id=None,
            created_at="2026-01-01T00:00:00Z",
        )
        sink.on_event(
            RunBegin(
                run=run_id,
                input=RunControlRef(index=0),
                started_at="2026-01-01T00:00:01Z",
            )
        )
        sink.on_event(
            RunEnd(
                run=run_id,
                status="finished",
                finished_at="2026-01-01T00:00:02Z",
            )
        )

    forked = manager.fork(thread_id=source)
    control = store.get_thread_control(thread_id=forked, index=0)

    assert control is not None and control.anchor_run == "run_root"
    asyncio.run(executor.shutdown())


def _accept_controls(db_path: str, run_id: str, offset: int, count: int) -> list[int]:
    store = RunStore(Path(db_path))
    indexes = [
        store.accept_run_control(
            run_id=run_id,
            kind="steer",
            timing="next_step",
            input=Message.user(str(index)),
            context={},
            request_id=f"worker-{offset + index}",
            created_at="2026-01-01T00:00:01Z",
        ).index
        for index in range(count)
    ]
    store.close()
    return indexes


def _accept_same_start(db_path: str) -> bool:
    store = RunStore(Path(db_path))
    try:
        store.accept_start(
            run_id="run_shared_start",
            parent=None,
            thread="term_shared_start",
            input=Message.user("hello"),
            context={"runnable": {"kind": "flow", "name": "shared"}},
            request_id="shared-start",
            created_at="2026-01-01T00:00:00Z",
        )
        return True
    except ValueError:
        return False
    finally:
        store.close()


def _allocate_execution_ids(state_path: str, count: int) -> tuple[list[str], list[str]]:
    ids = IdIssuer(Path(state_path))
    return (
        [ids.issue_run() for _ in range(count)],
        [ids.issue_thread(ThreadPrefix.TERM.value) for _ in range(count)],
    )


def _accept_remote_stop(db_path: str, run_id: str) -> None:
    store = RunStore(Path(db_path))
    store.accept_run_control(
        run_id=run_id,
        kind="stop",
        timing="immediate",
        input=Message.user("remote stop"),
        context={},
        request_id="remote-stop",
        created_at="2026-01-01T00:00:01Z",
    )
    store.close()


def _rewind_thread(db_path: str, request_id: str) -> int | None:
    store = RunStore(Path(db_path))
    try:
        _thread, control, _superseded = store.rewind_thread(
            thread_id="term_thread_controls",
            anchor_run="run_thread_anchor",
            request_id=request_id,
            expected_head=ThreadControlRef("term_thread_controls", 0),
            context={},
            created_at="2026-01-01T00:00:03Z",
        )
        return control.index
    except ValueError:
        return None
    finally:
        store.close()


def test_run_control_indexes_are_process_safe(tmp_path: Path) -> None:
    db_path = tmp_path / "runs.db"
    store = RunStore(db_path)
    store.create_thread(thread_id="term_test")
    store.accept_start(
        run_id="run_test",
        parent=None,
        thread="term_test",
        input=Message.user("hello"),
        context={},
        request_id=None,
        created_at="2026-01-01T00:00:00Z",
    )
    store.close()
    context = get_context("spawn")
    with context.Pool(4) as pool:
        groups = pool.starmap(
            _accept_controls,
            [(str(db_path), "run_test", worker * 10, 10) for worker in range(4)],
        )

    indexes = sorted(index for group in groups for index in group)
    assert indexes == list(range(1, 41))


def test_run_start_has_one_owner_across_processes(tmp_path: Path) -> None:
    db_path = tmp_path / "runs.db"
    store = RunStore(db_path)
    store.create_thread(thread_id="term_shared_start")
    store.close()
    context = get_context("spawn")
    with context.Pool(4) as pool:
        owners = pool.map(_accept_same_start, [str(db_path)] * 4)

    assert owners.count(True) == 1
    assert owners.count(False) == 3


def test_run_and_thread_ids_are_process_safe(tmp_path: Path) -> None:
    state_path = tmp_path / "ids.json"
    context = get_context("spawn")
    with context.Pool(4) as pool:
        groups = pool.starmap(
            _allocate_execution_ids,
            [(str(state_path), 20) for _ in range(4)],
        )

    run_ids = [run_id for runs, _threads in groups for run_id in runs]
    thread_ids = [thread_id for _runs, threads in groups for thread_id in threads]
    assert len(run_ids) == len(set(run_ids)) == 80
    assert len(thread_ids) == len(set(thread_ids)) == 80


def test_remote_process_can_stop_an_owned_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    flow = FlowDecl(name="waiting", span=Span(line=1))
    executor = _executor(tmp_path)
    store = executor.store

    async def wait_until_canceled(
        runtime: _Execution,
        binding: Any,
        executable: Any,
        **_kwargs: Any,
    ) -> Any:
        await runtime.emit(
            RunBegin(
                run=binding.run_id,
                input=RunControlRef(index=0),
                started_at="2026-01-01T00:00:00Z",
            )
        )
        await asyncio.sleep(30)

    monkeypatch.setattr(_Execution, "execute", wait_until_canceled)

    async def scenario() -> Any:
        handle = executor.start(
            RunSpec(
                setup=_setup(),
                state=_state(flow),
                thread="term_test",
                runnable=flow.name,
            ),
            run_id="run_remote_stop",
        )
        while (
            run := store.get_run(run_id="run_remote_stop")
        ) is None or run.status != "running":
            await asyncio.sleep(0.01)
        process = get_context("spawn").Process(
            target=_accept_remote_stop,
            args=(str(store.db_path), "run_remote_stop"),
        )
        process.start()
        await asyncio.to_thread(process.join, 10)
        assert process.exitcode == 0
        return await asyncio.wait_for(handle, timeout=2)

    record = asyncio.run(scenario())

    control = store.get_run_control(run_id=record.id, index=1)
    assert record.status == "canceled"
    assert control is not None and control.status == "finished"
    asyncio.run(executor.shutdown())


def test_executor_shutdown_cancels_and_persists_active_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    flow = FlowDecl(name="waiting", span=Span(line=1))
    executor = _executor(tmp_path)
    store = executor.store

    async def wait_until_canceled(
        runtime: _Execution,
        binding: Any,
        executable: Any,
        **_kwargs: Any,
    ) -> Any:
        await runtime.emit(
            RunBegin(
                run=binding.run_id,
                input=RunControlRef(index=0),
                started_at="2026-01-01T00:00:00Z",
            )
        )
        await asyncio.sleep(30)

    monkeypatch.setattr(_Execution, "execute", wait_until_canceled)

    async def scenario() -> Any:
        handle = executor.start(
            RunSpec(
                setup=_setup(),
                state=_state(flow),
                thread="term_test",
                runnable=flow.name,
            ),
            run_id="run_shutdown",
        )
        while (
            run := store.get_run(run_id="run_shutdown")
        ) is None or run.status != "running":
            await asyncio.sleep(0.01)
        await executor.shutdown()
        return await handle

    record = asyncio.run(scenario())

    assert record.status == "canceled"
    assert executor._active == {}
    assert executor._monitor_task is None
    with pytest.raises(RuntimeError, match="run executor is shut down"):
        executor.stop(run_id=record.id)
    assert store.get_run(run_id=record.id) == record


def test_executor_shutdown_persists_run_before_owner_task_starts(
    tmp_path: Path,
) -> None:
    flow = FlowDecl(name="never-started", span=Span(line=1))
    executor = _executor(tmp_path)

    async def scenario() -> Any:
        handle = executor.start(
            RunSpec(
                setup=_setup(),
                state=_state(flow),
                thread="term_test",
                runnable=flow.name,
            ),
            run_id="run_never_started",
        )
        await executor.shutdown()
        return await handle

    record = asyncio.run(scenario())

    assert record.status == "canceled"
    assert record.started_at == ""
    assert executor._active == {}


def test_thread_control_indexes_and_head_are_process_safe(tmp_path: Path) -> None:
    db_path = tmp_path / "runs.db"
    store = RunStore(db_path)
    store.create_thread(thread_id="term_thread_controls")
    store.accept_start(
        run_id="run_thread_anchor",
        parent=None,
        thread="term_thread_controls",
        input=Message.user("hello"),
        context={},
        request_id=None,
        created_at="2026-01-01T00:00:00Z",
    )
    sink = PersistSink(store)
    sink.on_event(
        RunBegin(
            run="run_thread_anchor",
            input=RunControlRef(index=0),
            started_at="2026-01-01T00:00:01Z",
        )
    )
    sink.on_event(
        RunEnd(
            run="run_thread_anchor",
            status="finished",
            finished_at="2026-01-01T00:00:02Z",
        )
    )
    store.close()
    context = get_context("spawn")
    with context.Pool(2) as pool:
        results = pool.starmap(
            _rewind_thread,
            [(str(db_path), "rewind-a"), (str(db_path), "rewind-b")],
        )

    assert sorted(results, key=lambda value: value is not None) == [None, 1]
    reopened = RunStore(db_path)
    assert [
        control.index
        for control in reopened.list_thread_controls(thread_id="term_thread_controls")
    ] == [0, 1]
    reopened.close()


def test_persist_sink_projects_run_and_step_records(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs.db")
    store.create_thread(thread_id="term_test")
    store.accept_start(
        run_id="run_test",
        parent=None,
        thread="term_test",
        input=Message.user("hello"),
        context={},
        request_id=None,
        created_at="2026-01-01T00:00:00Z",
    )
    sink = PersistSink(store)
    sink.on_event(
        RunBegin(
            run="run_test",
            input=RunControlRef(index=0),
            started_at="2026-01-01T00:00:01Z",
        )
    )
    sink.on_event(
        StepBegin(
            step="run_test/0",
            kind="system",
            input=(RunControlRef(index=0),),
            started_at="2026-01-01T00:00:02Z",
        )
    )
    sink.on_event(
        StepEnd(
            step="run_test/0",
            kind="system",
            status="finished",
            output=(TextPart(text="done"),),
            finished_at="2026-01-01T00:00:03Z",
        )
    )
    sink.on_event(
        RunEnd(
            run="run_test",
            status="finished",
            output=OutputRef(step="run_test/0"),
            finished_at="2026-01-01T00:00:04Z",
        )
    )

    run = store.get_run(run_id="run_test")
    assert run is not None and run.status == "finished"
    assert store.run_output_text(run_id="run_test") == "done"
    assert len(store.list_steps(run_id="run_test")) == 1
    store.close()


def test_run_store_migrates_schema_without_deleting_history(tmp_path: Path) -> None:
    path = tmp_path / "runs.db"
    store = RunStore(path)
    store.create_thread(thread_id="term_test")
    store.accept_start(
        run_id="run_test",
        parent=None,
        thread="term_test",
        input=Message.user("hello"),
        context={"runnable": {"kind": "flow", "name": "pipeline"}},
        request_id="start-test",
        created_at="2026-01-01T00:00:00Z",
    )
    store.close()

    connection = sqlite3.connect(path)
    connection.execute("DROP INDEX idx_run_controls_request")
    connection.execute(
        """
        CREATE UNIQUE INDEX idx_run_controls_request
        ON run_controls(run, request_id)
        WHERE request_id IS NOT NULL
        """
    )
    connection.execute("PRAGMA user_version=13")
    connection.commit()
    connection.close()

    reopened = RunStore(path)
    try:
        assert reopened.get_thread(thread_id="term_test") is not None
        assert reopened.get_run(run_id="run_test") is not None
        assert len(reopened.list_run_controls(run_id="run_test")) == 1
    finally:
        reopened.close()


def test_run_store_removes_legacy_thread_control_error_without_deleting_history(
    tmp_path: Path,
) -> None:
    path = tmp_path / "runs.db"
    store = RunStore(path)
    store.create_thread(thread_id="term_test")
    store.close()

    connection = sqlite3.connect(path)
    connection.execute("ALTER TABLE thread_controls ADD COLUMN error TEXT")
    connection.execute("PRAGMA user_version=17")
    connection.commit()
    connection.close()

    reopened = RunStore(path)
    try:
        assert reopened.get_thread(thread_id="term_test") is not None
        assert len(reopened.list_thread_controls(thread_id="term_test")) == 1
    finally:
        reopened.close()

    connection = sqlite3.connect(path)
    try:
        columns = {
            str(row[1])
            for row in connection.execute(
                "PRAGMA table_info(thread_controls)"
            ).fetchall()
        }
        assert "error" not in columns
    finally:
        connection.close()


def test_run_store_migrates_step_and_model_text_names_in_place(
    tmp_path: Path,
) -> None:
    path = tmp_path / "runs.db"
    store = RunStore(path)
    store.create_thread(thread_id="term_test")
    store.accept_start(
        run_id="run_test",
        parent=None,
        thread="term_test",
        input=Message.user("hello"),
        context={"runnable": {"kind": "flow", "name": "pipeline"}},
        request_id=None,
        created_at="2026-01-01T00:00:00Z",
    )
    sink = PersistSink(store)
    sink.on_event(
        StepBegin(
            step="run_test/0",
            kind="system",
            given={"runtime": "test"},
            started_at="2026-01-01T00:00:01Z",
        )
    )
    sink.on_event(
        StepEnd(
            step="run_test/0",
            kind="system",
            status="finished",
            noted={"shape": "item"},
            finished_at="2026-01-01T00:00:02Z",
        )
    )
    text_hash = _capture_model_text(store, "stable")
    store.close()

    connection = sqlite3.connect(path)
    connection.execute("ALTER TABLE steps RENAME COLUMN given TO context")
    connection.execute("ALTER TABLE steps RENAME COLUMN noted TO detail")
    connection.execute("ALTER TABLE model_texts RENAME TO prompts")
    connection.execute("PRAGMA user_version=15")
    connection.commit()
    connection.close()

    reopened = RunStore(path)
    try:
        step = reopened.list_steps(run_id="run_test")[0]
        assert step.given == {"runtime": "test"}
        assert step.noted == {"shape": "item"}
        assert reopened.get_model_text(text_hash=text_hash) == "stable"
    finally:
        reopened.close()


def test_run_store_migrates_v16_model_texts_in_place(tmp_path: Path) -> None:
    path = tmp_path / "runs.db"
    store = RunStore(path)
    text_hash = _capture_model_text(store, "stable")
    store.close()

    connection = sqlite3.connect(path)
    connection.execute("ALTER TABLE model_texts RENAME TO templates")
    connection.execute("PRAGMA user_version=16")
    connection.commit()
    connection.close()

    reopened = RunStore(path)
    try:
        assert reopened.get_model_text(text_hash=text_hash) == "stable"
    finally:
        reopened.close()


def test_step_queries_treat_run_ids_as_literal_prefixes(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs.db")
    store.create_thread(thread_id="term_test")
    sink = PersistSink(store)
    for run_id, text in (("run_%", "literal"), ("run_ax", "other")):
        store.accept_start(
            run_id=run_id,
            parent=None,
            thread="term_test",
            input=Message.user(text),
            context={},
            request_id=None,
            created_at="2026-01-01T00:00:00Z",
        )
        sink.on_event(
            RunBegin(
                run=run_id,
                input=RunControlRef(index=0),
                started_at="2026-01-01T00:00:01Z",
            )
        )
        sink.on_event(
            StepBegin(
                step=f"{run_id}/0",
                kind="system",
                started_at="2026-01-01T00:00:02Z",
            )
        )
        sink.on_event(
            StepEnd(
                step=f"{run_id}/0",
                kind="system",
                status="finished",
                output=(TextPart(text=text),),
                finished_at="2026-01-01T00:00:03Z",
            )
        )
        sink.on_event(
            RunEnd(
                run=run_id,
                status="finished",
                finished_at="2026-01-01T00:00:04Z",
            )
        )

    assert [step.parent for step in store.list_steps(run_id="run_%")] == ["run_%"]
    grouped = store.list_steps_for_runs(run_ids=("run_%", "run_ax"))
    assert [step.parent for step in grouped["run_%"]] == ["run_%"]
    assert [step.parent for step in grouped["run_ax"]] == ["run_ax"]
    with pytest.raises(ValueError, match="invalid run id"):
        store.accept_start(
            run_id="run/invalid",
            parent=None,
            thread="term_test",
            input=Message.user("invalid"),
            context={},
            request_id=None,
            created_at="2026-01-01T00:00:05Z",
        )
    store.close()
