"""Committed runtime operations precede execution and survive target failure."""

import asyncio
from pathlib import Path
import sqlite3

import pytest

from tests.support.execution_assertions import (
    assert_replayed,
    assert_run_event_integrity,
)
from tests.support.execution_harness import (
    AsyncGate,
    ExecutionHarness,
    RecordingRunTracer,
)
from toolang.base.types.message import Message, TextPart
from toolang.base.types.run import ModelCallResult, ToolCall
from toolang.execution.events import StepEnd
from toolang.execution.store import RunStore
from toolang.execution.stream import TraceObserver
from toolang.execution.types import ThreadPrefix


@pytest.mark.parametrize("caller", ["flow", "agic"])
@pytest.mark.parametrize("operation", ["spawn", "exec"])
@pytest.mark.parametrize("target_fails", [False, True])
def test_control_and_source_step_commit_before_target_execution(
    tmp_path: Path, monkeypatch, caller: str, operation: str, target_fails: bool
) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source=f"""
flow flow_parent(_: Text) -> Text:
  {operation} child
agic agic_parent() -> Text:
  recall = none
  {"hands" if operation == "spawn" else "handoffs"} = flow:child
  user: Start work.
flow child(_: Text) -> Text:
  let unused = Work
""",
        responses=[
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        "launch",
                        "launch",
                        f"_toolang__{operation}",
                        {"runnable": "flow:child", "input": {"_": "work"}},
                    ),
                )
            ),
            ModelCallResult(message=Message.assistant("Started")),
        ],
    )
    from toolang.execution.executor.stmts import let

    original = let.evaluate_content
    committed = []

    def inspect_target(*args, **kwargs):
        reader = RunStore(harness.store.db_path, read_only=True)
        try:
            control = next(
                c
                for c in reader.list_controls()
                if c.kind == ("create" if operation == "spawn" else "exec")
                and c.triggered_by is not None
            )
            source = control.triggered_by
            assert source is not None
            controls = tuple(
                c for c in reader.list_controls() if c.triggered_by == source
            )
            committed.append((reader.get_step(ref=source), controls))
        finally:
            reader.close()
        if target_fails:
            raise RuntimeError("target execution failed")
        return original(*args, **kwargs)

    monkeypatch.setattr(let, "evaluate_content", inspect_target)
    tracer = RecordingRunTracer()

    async def scenario():
        async with harness:
            observer = TraceObserver(harness.executor.stream.subscribe(), tracer)
            parent = await harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable=f"{caller}:{caller}_parent",
                    primary=(TextPart("work"),) if caller == "flow" else None,
                ),
            )
            await asyncio.gather(*tuple(harness.executor._tasks))
            observer.finish()
            await observer.drain()
            assert parent.status == (
                "failed" if operation == "exec" and target_fails else "succeeded"
            )
            ((source, controls),) = committed
            assert source is not None and source.status == "succeeded"
            assert source.finished_at is not None
            assert source.end_cursor is not None
            assert {c.kind for c in controls} == (
                {"create", "run"} if operation == "spawn" else {"exec"}
            )
            assert all(c.status == "applied" for c in controls)
            assert all(c.finished_at == c.created_at for c in controls)
            assert all(c.finished_at == source.finished_at for c in controls)
            assert harness.store.get_step(ref=source.ref) == source
            assert all(c in harness.store.list_controls() for c in controls)
            end = next(
                e
                for e in tracer.events
                if isinstance(e, StepEnd) and e.step == source.ref
            )
            assert (
                end.status,
                end.output,
                end.noted,
                end.finished_at,
                end.aborted_by,
            ) == (source.status, source.output, source.noted, source.finished_at, None)
            if operation == "spawn":
                root = next(r for r in harness.store.list_runs() if r.id != parent.id)
                assert root.status == ("failed" if target_fails else "succeeded")
        if caller == "agic":
            assert_replayed(harness.store.db_path, tracer.events)

    asyncio.run(scenario())


def test_exec_tool_source_commit_failure_rolls_back_handoff(tmp_path: Path) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic parent() -> Text:
  recall = none
  handoffs = flow:child
  user: Transfer.
flow child() -> Text:
  let unused = Work
""",
        responses=[
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        "exec", "exec", "_toolang__exec", {"runnable": "flow:child"}
                    ),
                )
            )
        ],
    )
    with sqlite3.connect(harness.store.db_path) as connection:
        connection.execute("""
            CREATE TRIGGER reject_exec_completion BEFORE UPDATE ON steps
            WHEN OLD.kind = 'tool' AND NEW.status = 'succeeded'
            BEGIN SELECT RAISE(ABORT, 'injected source completion failure'); END
        """)
    tracer = RecordingRunTracer()

    async def scenario():
        async with harness:
            root = await harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="agic:parent",
                ),
                tracer=tracer,
            )
            assert root.status == "failed"
            assert not harness.store.list_run_controls(run_id=root.id, kind="exec")
            steps = harness.store.list_steps(run_id=root.id)
            assert [s.kind for s in steps] == ["model", "tool"]
            assert steps[1].status == "failed"
            assert steps[1].error is not None
            assert (
                harness.store.resolve_error(steps[1].error)
                == "injected source completion failure"
            )
            assert_run_event_integrity(tracer.events)
        assert_replayed(harness.store.db_path, tracer.events)

    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["spawn", "exec"])
@pytest.mark.parametrize("interruption", ["steer", "cancel"])
def test_committed_tool_end_is_delivered_after_interruption_at_event_lock(
    tmp_path: Path, monkeypatch, operation: str, interruption: str
) -> None:
    gate = AsyncGate()
    blockers = []

    async def hold_event_lock(run_id):
        async with harness.executor._active[run_id].event_lock:
            await gate.wait()

    harness = ExecutionHarness.create(
        tmp_path,
        source=f"""
agic parent() -> Text:
  recall = none
  {"hands" if operation == "spawn" else "handoffs"} = agic:child
  user: Start work.
agic child() -> Text:
  recall = none
  user: Child work.
""",
        responses=[
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        "launch",
                        "launch",
                        f"_toolang__{operation}",
                        {"runnable": "agic:child"},
                    ),
                )
            ),
            ModelCallResult(message=Message.assistant("Revised")),
            ModelCallResult(message=Message.assistant("Done")),
        ],
    )
    original_emit = harness.executor._emit_event

    async def interruptible_emit(active, event):
        if isinstance(event, StepEnd) and event.step.index == 1 and not blockers:
            blockers.append(asyncio.create_task(hold_event_lock(event.step.run_id)))
            await asyncio.sleep(0)
        await original_emit(active, event)

    monkeypatch.setattr(harness.executor, "_emit_event", interruptible_emit)
    tracer = RecordingRunTracer()

    async def scenario():
        async with harness:
            handle = harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="agic:parent",
                ),
                tracer=tracer,
            )
            await asyncio.wait_for(gate.wait_until_entered(), 2)
            source = harness.store.list_steps(run_id=handle.run_id)[1]
            assert source.status == "succeeded"
            request = (
                handle.steer(Message.user("Revise"), timing="immediate")
                if interruption == "steer"
                else handle.cancel()
            )
            await asyncio.sleep(0)
            gate.release()
            root = await asyncio.wait_for(handle, 2)
            await asyncio.gather(*blockers, *tuple(harness.executor._tasks))
            assert root.status == (
                "succeeded" if interruption == "steer" else "canceled"
            )
            assert harness.store.get_step(ref=source.ref) == source
            ends = [
                e
                for e in tracer.events
                if isinstance(e, StepEnd) and e.step == source.ref
            ]
            assert len(ends) == 1
            assert ends[0].status == "succeeded" and ends[0].aborted_by is None
            assert ends[0].finished_at == source.finished_at
            applied = harness.store.get_run_control(
                run_id=handle.run_id, index=request.index
            )
            assert applied is not None and applied.status == "applied"
            assert_run_event_integrity(tracer.events)
        assert_replayed(harness.store.db_path, tracer.events)

    asyncio.run(scenario())
