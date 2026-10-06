"""Flow and model run calls contain the same synchronous child lifetime."""

import asyncio

import pytest

from tests.support.execution_assertions import (
    assert_replayed,
    assert_run_event_integrity,
)
from tests.support.execution_harness import (
    AsyncGate,
    ExecutionHarness,
    RecordingRunTracer,
    ScriptedModelTurn,
)
from toolang.base.types.message import Message, ToolResultPart
from toolang.base.types.run import ModelCallResult, ToolCall
from toolang.execution.events import RunBegin, RunEnd, StepBegin, StepEnd
from toolang.execution.types import ThreadPrefix


@pytest.mark.parametrize("caller", ["flow", "agic"])
def test_sync_run_step_stays_open_until_child_finishes(tmp_path, caller):
    gate = AsyncGate()
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
flow flow_parent():
  run child
agic agic_parent() -> Text:
  recall = none
  hands = agic:child
  user: Run child.
agic child() -> Text:
  recall = none
  user: Child.
""",
        responses=[
            *(
                [
                    ModelCallResult(
                        tool_calls=(
                            ToolCall(
                                "call",
                                "provider",
                                "_toolang__run",
                                {"runnable": "agic:child"},
                            ),
                        )
                    )
                ]
                if caller == "agic"
                else []
            ),
            ScriptedModelTurn(
                ModelCallResult(message=Message.assistant("child result")), gate=gate
            ),
            ModelCallResult(message=Message.assistant("parent result")),
        ],
    )
    tracer = RecordingRunTracer()

    async def scenario():
        async with harness:
            handle = harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable=f"{caller}:{caller}_parent",
                ),
                tracer=tracer,
            )
            await asyncio.wait_for(gate.wait_until_entered(), 2)
            child = next(
                r
                for r in harness.store.list_run_tree(root_run_id=handle.run_id)
                if r.parent
            )
            assert child.parent is not None
            source = harness.store.get_step(ref=child.parent)
            assert source is not None
            assert source.status == "running" and source.output is None
            entry = harness.store.get_run_control(run_id=child.id, index=0)
            assert entry is not None
            assert entry.status == "applied" and entry.finished_at == entry.created_at
            assert not handle.task.done()
            gate.release()
            assert (await handle).status == "succeeded"
            child_begin = next(
                i
                for i, e in enumerate(tracer.events)
                if isinstance(e, RunBegin) and e.run == child.id
            )
            child_end = next(
                i
                for i, e in enumerate(tracer.events)
                if isinstance(e, RunEnd) and e.run == child.id
            )
            source_end = next(
                i
                for i, e in enumerate(tracer.events)
                if isinstance(e, StepEnd) and e.step == source.ref
            )
            assert child_begin < child_end < source_end
            if caller == "agic":
                messages = harness.adapter.invocations[-1].call.messages
                parts = [
                    p
                    for m in messages
                    for p in m.parts
                    if isinstance(p, ToolResultPart)
                ]
                assert len(parts) == 1
                assert parts[0].output == {"type": "Text", "value": "child result"}
                assert not any(m.tag == "run-result" for m in messages)
            assert_run_event_integrity(tracer.events)
        assert_replayed(harness.store.db_path, tracer.events)

    asyncio.run(scenario())


@pytest.mark.parametrize("caller", ["flow", "agic"])
def test_child_begin_shares_admission_boundary_when_event_lock_is_contended(
    tmp_path, caller
):
    gate = AsyncGate()
    blockers = []

    async def hold_after_admission(run_id):
        async with harness.executor._active[run_id].event_lock:
            await gate.wait()

    async def queue_after_admission(run_id):
        async with harness.executor._active[run_id].event_lock:
            # Admission is waiting behind this owner. Queue another observer
            # behind admission, so a separate RunBegin would have to wait.
            await asyncio.sleep(0)
            blockers.append(asyncio.create_task(hold_after_admission(run_id)))

    class Tracer(RecordingRunTracer):
        async def on_event(self, event):
            await super().on_event(event)
            if isinstance(event, StepBegin) and event.kind == (
                "run" if caller == "flow" else "tool"
            ):
                if not blockers:
                    blockers.append(
                        asyncio.create_task(queue_after_admission(event.step.run_id))
                    )
                    await asyncio.sleep(0)

    harness = ExecutionHarness.create(
        tmp_path,
        source="""
flow flow_parent():
  run child
agic agic_parent():
  recall = none
  hands = agic:child
  user: Run child.
agic child():
  recall = none
  user: Child.
""",
        responses=[
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        "call", "provider", "_toolang__run", {"runnable": "agic:child"}
                    ),
                )
            )
        ]
        if caller == "agic"
        else [],
    )
    tracer = Tracer()

    async def scenario():
        async with harness:
            handle = harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable=f"{caller}:{caller}_parent",
                ),
                tracer=tracer,
            )
            await asyncio.wait_for(gate.wait_until_entered(), 2)
            child = next(
                r
                for r in harness.store.list_run_tree(root_run_id=handle.run_id)
                if r.parent
            )
            began = any(
                isinstance(event, RunBegin) and event.run == child.id
                for event in tracer.events
            )
            handle.cancel()
            gate.release()
            assert (await asyncio.wait_for(handle, 2)).status == "canceled"
            await asyncio.gather(*blockers)
            assert child.started_at and began
            assert_run_event_integrity(tracer.events)
        assert_replayed(harness.store.db_path, tracer.events)

    asyncio.run(scenario())
