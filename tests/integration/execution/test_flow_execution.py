from __future__ import annotations

import asyncio
from multiprocessing import get_context
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from toolang.base.types.message import Message, TextPart
from toolang.common.ids import allocate_run_id, allocate_thread_id
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
from toolang.execution.executor import RunExecutor
from toolang.execution.executor.common import Local
from toolang.execution.executor.executor import _Execution
from toolang.execution.executor.runs import agic as agic_run
from toolang.execution.inspection import ExecutionInspection
from toolang.execution.records import OutputRef, RunControlRef, ThreadControlRef
from toolang.execution.executor.request import RunRequest
from toolang.execution.executor.persist import PersistSink
from toolang.execution.store import RunStore
from toolang.execution.threads import ThreadManager
from toolang.lang.ast import AgicDecl, FlowDecl, LetStmt, Program, RunStmt, Span
from toolang.up.setup import AgentSetup


class _RecordingTracer(RunTracer):
    def __init__(self, store: RunStore, *, fail: bool = False) -> None:
        self.store = store
        self.fail = fail
        self.events: list[RunEvent] = []

    def on_event(self, event: RunEvent) -> None:
        run_id = (
            event.run
            if isinstance(event, RunBegin | RunEnd)
            else event.step.split("/", 1)[0]
        )
        assert self.store.get_run(run_id=run_id) is not None
        self.events.append(event)
        if self.fail:
            raise RuntimeError("tracer failed")


class _RecordingThreadListener(ThreadListener):
    def __init__(self) -> None:
        self.events: list[ThreadEvent] = []

    def on_event(self, event: ThreadEvent) -> None:
        self.events.append(event)


def _executor(tmp_path: Path, store: RunStore) -> RunExecutor:
    return RunExecutor(
        store=store,
        id_state_path=tmp_path / "ids.json",
    )


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
        name="alice",
        home=Path("/agents/alice"),
        tools={},
        model_providers={},
        model_adapters={},
        model_environ={},
    )


def _request(
    name: str,
    *,
    run_id: str | None = None,
    thread_id: str | None = None,
    request_id: str | None = None,
) -> RunRequest:
    return RunRequest(
        origin="chat",
        run_id=run_id,
        thread_id=thread_id,
        executable_kind="flow",
        executable_name=name,
        request_id=request_id,
    )


def test_run_executor_persists_before_tracing(tmp_path: Path) -> None:
    flow = FlowDecl(
        name="pipeline",
        stmts=(LetStmt(value="done", span=Span(line=2)),),
        span=Span(line=1),
    )
    store = RunStore(tmp_path / "runs.db")
    executor = _executor(tmp_path, store)
    tracer = _RecordingTracer(store)

    record = asyncio.run(
        executor.start(_setup(), _state(flow), _request(flow.name), tracer=tracer)
    )

    assert record.status == "finished"
    assert store.run_output_text(run_id=record.id) == "done"
    assert [event.type for event in tracer.events] == [
        "run_begin",
        "step_begin",
        "step_end",
        "run_end",
    ]
    start = store.get_run_control(run_id=record.id, index=0)
    assert start is not None and start.status == "finished"
    detail = ExecutionInspection(store).run_detail(record.id)
    assert detail is not None
    assert detail.input == Message.user("")
    assert [control.index for control in detail.inputs] == [0]
    assert detail.inputs[0].message == detail.input
    assert [step.kind for step in detail.output.steps] == ["system"]
    assert detail.output.steps[0].output == [TextPart(text="done")]
    assert not hasattr(detail.output.steps[0], "message")
    asyncio.run(executor.shutdown())
    store.close()


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
    store = RunStore(tmp_path / "runs.db")
    executor = _executor(tmp_path, store)
    tracer = _RecordingTracer(store)

    async def execute_agic(*_args: Any, **_kwargs: Any) -> Local:
        return Local("done", "item")

    monkeypatch.setattr(agic_run, "execute", execute_agic)
    record = asyncio.run(
        executor.start(
            _setup(),
            state,
            RunRequest(origin="chat"),
            tracer=tracer,
        )
    )

    assert record.status == "finished"
    assert [event.type for event in tracer.events] == ["run_begin", "run_end"]
    assert store.list_steps(run_id=record.id) == []
    asyncio.run(executor.shutdown())
    store.close()


def test_tracer_failure_does_not_fail_execution(tmp_path: Path) -> None:
    flow = FlowDecl(name="pipeline", span=Span(line=1))
    store = RunStore(tmp_path / "runs.db")
    executor = _executor(tmp_path, store)

    record = asyncio.run(
        executor.start(
            _setup(),
            _state(flow),
            _request(flow.name),
            tracer=_RecordingTracer(store, fail=True),
        )
    )

    assert record.status == "finished"
    asyncio.run(executor.shutdown())
    store.close()


def test_start_request_is_idempotent_and_executes_once(tmp_path: Path) -> None:
    flow = FlowDecl(name="pipeline", span=Span(line=1))
    store = RunStore(tmp_path / "runs.db")
    executor = _executor(tmp_path, store)
    request = _request(
        flow.name,
        run_id="run_idempotent",
        thread_id="term_idempotent",
        request_id="start-idempotent",
    )
    first_tracer = _RecordingTracer(store)
    replay_tracer = _RecordingTracer(store)

    first = asyncio.run(
        executor.start(_setup(), _state(flow), request, tracer=first_tracer)
    )
    replay = asyncio.run(
        executor.start(_setup(), _state(flow), request, tracer=replay_tracer)
    )

    assert replay == first
    assert first.status == "finished"
    assert replay_tracer.events == []
    assert len(store.list_run_controls(run_id=first.id)) == 1
    asyncio.run(executor.shutdown())
    store.close()


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
    store = RunStore(tmp_path / "runs.db")
    executor = _executor(tmp_path, store)
    tracer = _RecordingTracer(store)

    root = asyncio.run(
        executor.start(
            _setup(), _state(parent, child), _request(parent.name), tracer=tracer
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
    store.close()


def test_steer_control_is_finished_when_step_consumes_it(tmp_path: Path) -> None:
    flow = FlowDecl(
        name="pipeline",
        stmts=(LetStmt(value="default", span=Span(line=2)),),
        span=Span(line=1),
    )
    store = RunStore(tmp_path / "runs.db")
    executor = _executor(tmp_path, store)

    class SteeringTracer(RunTracer):
        control_index: int | None = None

        def on_event(self, event: RunEvent) -> None:
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
        executor.start(_setup(), _state(flow), _request(flow.name), tracer=tracer)
    )

    assert tracer.control_index is not None
    control = store.get_run_control(run_id=record.id, index=tracer.control_index)
    assert control is not None and control.status == "finished"
    steps = store.list_steps(run_id=record.id)
    assert steps[0].input == (RunControlRef(index=tracer.control_index),)
    asyncio.run(executor.shutdown())
    store.close()


def test_unreachable_control_fails_when_run_ends(tmp_path: Path) -> None:
    flow = FlowDecl(name="pipeline", span=Span(line=1))
    store = RunStore(tmp_path / "runs.db")
    executor = _executor(tmp_path, store)

    class SteeringTracer(RunTracer):
        control_index: int | None = None

        def on_event(self, event: RunEvent) -> None:
            if isinstance(event, RunBegin):
                self.control_index = executor.steer(
                    run_id=event.run,
                    message=Message.user("unused"),
                    timing="next_call",
                ).index

    tracer = SteeringTracer()
    record = asyncio.run(
        executor.start(_setup(), _state(flow), _request(flow.name), tracer=tracer)
    )

    assert tracer.control_index is not None
    control = store.get_run_control(run_id=record.id, index=tracer.control_index)
    assert control is not None
    assert control.status == "failed"
    assert control.error == "run ended before the control could be applied"
    asyncio.run(executor.shutdown())
    store.close()


def test_run_control_acceptance_is_idempotent(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs.db")
    store.create_thread(thread_id="term_test")
    store.accept_start(
        run_id="run_test",
        parent=None,
        thread="term_test",
        input=Message.user("hello"),
        context={},
        request_id="start-1",
        created_at="2026-01-01T00:00:00Z",
    )

    first, created = store.accept_run_control(
        run_id="run_test",
        kind="steer",
        timing="next_step",
        input=Message.user("continue"),
        context={},
        request_id="steer-1",
        created_at="2026-01-01T00:00:01Z",
    )
    store.finish_run(
        run_id="run_test",
        status="finished",
        finished_at="2026-01-01T00:00:02Z",
    )
    second, replayed = store.accept_run_control(
        run_id="run_test",
        kind="steer",
        timing="next_step",
        input=Message.user("continue"),
        context={},
        request_id="steer-1",
        created_at="2026-01-01T00:00:03Z",
    )

    assert created is True
    assert replayed is False
    assert second == first
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


def test_pending_control_can_be_canceled_explicitly(tmp_path: Path) -> None:
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
    control, _created = store.accept_run_control(
        run_id="run_test",
        kind="steer",
        timing="next_call",
        input=Message.user("continue"),
        context={},
        request_id="steer-cancel",
        created_at="2026-01-01T00:00:01Z",
    )

    canceled = store.cancel_run_control(
        run_id="run_test",
        index=control.index,
        finished_at="2026-01-01T00:00:02Z",
    )

    assert canceled.status == "canceled"
    assert canceled.finished_at == "2026-01-01T00:00:02Z"
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
    store = RunStore(tmp_path / "runs.db")
    executor = _executor(tmp_path, store)
    listener = _RecordingThreadListener()
    manager = ThreadManager(executor, listener=listener)

    created = manager.create()

    assert created.thread.created_by.index == 0
    assert [event.type for event in listener.events] == ["thread_created"]
    with pytest.raises(FileNotFoundError):
        manager.fork(run_id="run_missing")
    assert [event.type for event in listener.events] == ["thread_created"]
    asyncio.run(executor.shutdown())
    store.close()


def test_thread_create_request_is_idempotent_across_allocated_ids(
    tmp_path: Path,
) -> None:
    store = RunStore(tmp_path / "runs.db")
    executor = _executor(tmp_path, store)
    listener = _RecordingThreadListener()
    manager = ThreadManager(executor, listener=listener)

    first = manager.create(request_id="create-thread")
    replay = manager.create(request_id="create-thread")

    assert replay == first
    assert [event.type for event in listener.events] == ["thread_created"]
    asyncio.run(executor.shutdown())
    store.close()


def test_thread_create_request_rejects_conflicting_replay(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs.db")
    executor = _executor(tmp_path, store)
    manager = ThreadManager(executor)

    manager.create(kind="chat", request_id="create-thread")

    with pytest.raises(ValueError, match="conflicting thread control request"):
        manager.create(kind="support", request_id="create-thread")
    asyncio.run(executor.shutdown())
    store.close()


def test_thread_inspection_starts_from_thread_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = RunStore(tmp_path / "runs.db")
    executor = _executor(tmp_path, store)
    created = ThreadManager(executor).create()
    inspection = ExecutionInspection(store)
    monkeypatch.setattr(
        store,
        "list_runs",
        lambda **_kwargs: pytest.fail("thread inspection must not scan all runs"),
    )

    threads = inspection.list_threads(limit=None)

    assert [thread.id for thread in threads] == [created.thread.thread_id]
    assert threads[0].run_count == 0
    asyncio.run(executor.shutdown())
    store.close()


def test_thread_fork_and_rewind_use_control_refs_without_copying_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = RunStore(tmp_path / "runs.db")
    executor = _executor(tmp_path, store)
    listener = _RecordingThreadListener()
    manager = ThreadManager(executor, listener=listener)
    created = manager.create()
    anchor_id = "run_anchor"
    store.accept_start(
        run_id=anchor_id,
        parent=None,
        thread=created.thread.thread_id,
        input=Message.user("hello"),
        context={"origin": "chat"},
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

    forked = manager.fork(run_id=anchor_id)

    assert store.list_runs(thread_id=forked.thread.thread_id, limit=None) == []
    assert [
        run.id
        for run in store.list_thread_history_chronological(
            thread_id=forked.thread.thread_id
        )
    ] == [anchor_id]
    original_head = created.thread.head
    rewound = manager.rewind(run_id=anchor_id, expected_head=original_head)
    anchor = store.get_run(run_id=anchor_id)
    assert anchor is not None
    assert anchor.superseded_by == rewound.thread.head
    assert [event.type for event in listener.events] == [
        "thread_created",
        "thread_forked",
        "thread_rewound",
    ]
    monkeypatch.setattr(
        manager,
        "_stop_affected_runs",
        lambda _anchor: pytest.fail("stale rewind must not stop runs"),
    )
    with pytest.raises(ValueError, match="thread head changed"):
        manager.rewind(run_id=anchor_id, expected_head=original_head)
    assert [event.type for event in listener.events] == [
        "thread_created",
        "thread_forked",
        "thread_rewound",
    ]
    asyncio.run(executor.shutdown())
    store.close()


def test_thread_fork_and_rewind_replays_return_persisted_result_run(
    tmp_path: Path,
) -> None:
    store = RunStore(tmp_path / "runs.db")
    executor = _executor(tmp_path, store)
    listener = _RecordingThreadListener()
    manager = ThreadManager(executor, listener=listener)
    created = manager.create()
    store.accept_start(
        run_id="run_anchor",
        parent=None,
        thread=created.thread.thread_id,
        input=Message.user("hello"),
        context={"origin": "chat"},
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
        run_id="run_anchor",
        message=Message.user("fork"),
        request_id="fork-thread",
    )
    fork_replay = manager.fork(
        run_id="run_anchor",
        message=Message.user("fork"),
        request_id="fork-thread",
    )
    rewound = manager.rewind(
        run_id="run_anchor",
        message=Message.user("rewind"),
        request_id="rewind-thread",
    )
    rewind_replay = manager.rewind(
        run_id="run_anchor",
        message=Message.user("rewind"),
        request_id="rewind-thread",
    )

    assert forked.control.result_run is not None
    assert fork_replay == forked
    assert rewound.control.result_run is not None
    assert rewind_replay == rewound
    assert [event.type for event in listener.events] == [
        "thread_created",
        "thread_forked",
        "thread_rewound",
    ]
    asyncio.run(executor.shutdown())
    store.close()


def test_rewind_uses_durable_acceptance_order_instead_of_timestamps(
    tmp_path: Path,
) -> None:
    store = RunStore(tmp_path / "runs.db")
    executor = _executor(tmp_path, store)
    manager = ThreadManager(executor)
    created = manager.create()
    sink = PersistSink(store)
    timestamp = "2026-01-01T00:00:00Z"
    for run_id in ("run_before", "run_anchor", "run_after"):
        store.accept_start(
            run_id=run_id,
            parent=None,
            thread=created.thread.thread_id,
            input=Message.user(run_id),
            context={"origin": "chat"},
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

    rewound = manager.rewind(run_id="run_anchor")

    before = store.get_run(run_id="run_before")
    anchor = store.get_run(run_id="run_anchor")
    after = store.get_run(run_id="run_after")
    assert before is not None and before.superseded_by is None
    assert anchor is not None and anchor.superseded_by == rewound.thread.head
    assert after is not None and after.superseded_by == rewound.thread.head
    asyncio.run(executor.shutdown())
    store.close()


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
        )[0].index
        for index in range(count)
    ]
    store.close()
    return indexes


def _accept_same_start(db_path: str) -> bool:
    store = RunStore(Path(db_path))
    _run, _control, owner = store.accept_start(
        run_id="run_shared_start",
        parent=None,
        thread="term_shared_start",
        input=Message.user("hello"),
        context={"origin": "chat"},
        request_id="shared-start",
        created_at="2026-01-01T00:00:00Z",
    )
    store.close()
    return owner


def _allocate_execution_ids(state_path: str, count: int) -> tuple[list[str], list[str]]:
    path = Path(state_path)
    return (
        [allocate_run_id(path) for _ in range(count)],
        [allocate_thread_id(path, "chat") for _ in range(count)],
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


def _rewind_thread(db_path: str, request_id: str) -> tuple[bool, int | None]:
    store = RunStore(Path(db_path))
    try:
        _thread, control, _superseded, created = store.rewind_thread(
            thread_id="term_thread_controls",
            anchor_run="run_thread_anchor",
            result_run=None,
            message=None,
            request_id=request_id,
            expected_head=ThreadControlRef("term_thread_controls", 0),
            context={},
            created_at="2026-01-01T00:00:03Z",
        )
        return created, control.index
    except ValueError:
        return False, None
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
    store = RunStore(tmp_path / "runs.db")
    executor = _executor(tmp_path, store)

    async def wait_until_canceled(
        runtime: _Execution,
        binding: Any,
        executable: Any,
        **_kwargs: Any,
    ) -> Any:
        runtime.emit(
            RunBegin(
                run=binding.run_id,
                input=RunControlRef(index=0),
                started_at="2026-01-01T00:00:00Z",
            )
        )
        await asyncio.sleep(30)

    monkeypatch.setattr(_Execution, "execute", wait_until_canceled)

    async def scenario() -> Any:
        task = asyncio.create_task(
            executor.start(
                _setup(),
                _state(flow),
                _request(flow.name, run_id="run_remote_stop"),
            )
        )
        while (
            run := store.get_run(run_id="run_remote_stop")
        ) is None or run.status != "running":
            await asyncio.sleep(0.01)
        process = get_context("spawn").Process(
            target=_accept_remote_stop,
            args=(str(tmp_path / "runs.db"), "run_remote_stop"),
        )
        process.start()
        await asyncio.to_thread(process.join, 10)
        assert process.exitcode == 0
        return await asyncio.wait_for(task, timeout=2)

    record = asyncio.run(scenario())

    control = store.get_run_control(run_id=record.id, index=1)
    assert record.status == "canceled"
    assert control is not None and control.status == "finished"
    store.close()


def test_executor_shutdown_cancels_and_persists_active_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    flow = FlowDecl(name="waiting", span=Span(line=1))
    store = RunStore(tmp_path / "runs.db")
    executor = _executor(tmp_path, store)

    async def wait_until_canceled(
        runtime: _Execution,
        binding: Any,
        executable: Any,
        **_kwargs: Any,
    ) -> Any:
        runtime.emit(
            RunBegin(
                run=binding.run_id,
                input=RunControlRef(index=0),
                started_at="2026-01-01T00:00:00Z",
            )
        )
        await asyncio.sleep(30)

    monkeypatch.setattr(_Execution, "execute", wait_until_canceled)

    async def scenario() -> Any:
        task = asyncio.create_task(
            executor.start(
                _setup(),
                _state(flow),
                _request(flow.name, run_id="run_shutdown"),
            )
        )
        while (
            run := store.get_run(run_id="run_shutdown")
        ) is None or run.status != "running":
            await asyncio.sleep(0.01)
        await executor.shutdown()
        return await task

    record = asyncio.run(scenario())

    assert record.status == "canceled"
    assert executor._active == {}
    assert executor._monitor_task is None
    with pytest.raises(RuntimeError, match="run executor is shut down"):
        executor.stop(run_id=record.id)
    store.close()


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

    assert sorted(results) == [(False, None), (True, 1)]
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
    assert sink._last_step_index == {}
    assert sink._failed_runs == set()
    assert sink._locals == {}
    assert sink._bindings == {}
    store.close()


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
