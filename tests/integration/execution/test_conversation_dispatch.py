"""Explicit thread reuse, fenced controls, and shared root admission."""

import asyncio

import pytest

from tests.support.execution_assertions import last_tool_result
from tests.support.execution_harness import (
    ExecutionHarness,
    AsyncGate,
    ScriptedModelTurn,
)
from toolang.base.types.message import Message, TextPart, ToolResultPart
from toolang.base.types.run import ModelCallResult, ToolCall
from toolang.execution.errors import ThreadBusy
from toolang.execution.events import ThreadCreated
from toolang.execution.records import RunControlPayload
from toolang.execution.types import ThreadPrefix, ControlRef, RunRef


def call(name, **arguments):
    return ModelCallResult(
        tool_calls=(ToolCall("action", "action", f"runtime__{name}", arguments),)
    )


@pytest.mark.parametrize("prefix", list(ThreadPrefix))
def test_spawn_reuses_any_idle_local_prefix_without_rewriting_metadata(
    tmp_path, prefix
):
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic controller() -> Text:
  user: Start the requested independent work.
flow worker(_: Text) -> Text:
  let untouched = Work
""",
        responses=[],
    )

    async def scenario():
        async with harness:
            destination = harness.threads.create(prefix=prefix)
            previous = await harness.executor.run(
                harness.run_spec(
                    thread=destination,
                    runnable="flow:worker",
                    primary=(TextPart("previous request"),),
                )
            )
            assert previous.status == "succeeded"
            metadata = harness.store.get_thread(thread_id=destination)
            source = harness.threads.create(prefix=ThreadPrefix.CHAT)
            harness.adapter._responses.extend(
                [
                    call(
                        "spawn",
                        runnable="flow:worker",
                        input={"_": "new work"},
                        thread=destination,
                    ),
                    ModelCallResult(message=Message.assistant("Admitted.")),
                ]
            )
            parent = await harness.executor.run(
                harness.run_spec(thread=source, runnable="agic:controller")
            )
            assert parent.status == "succeeded", parent.error
            await asyncio.gather(*tuple(harness.executor._tasks))
            receipt = last_tool_result(harness.adapter.invocations[-1].call)
            assert receipt.error is None and receipt.output["thread"] == destination
            worker = harness.store.get_run(run_id=receipt.output["id"])
            assert (
                worker is not None
                and worker.parent is None
                and worker.status == "succeeded"
            )
            assert worker.thread.id == destination
            after = harness.store.get_thread(thread_id=destination)
            assert after is not None and metadata is not None
            assert (after.origin, after.peer, after.created_at) == (
                metadata.origin,
                metadata.peer,
                metadata.created_at,
            )
            assert len(harness.store.list_threads()) == 2
            preparation = harness.store.get_run_control(run_id=worker.id, index=0)
            assert preparation is not None and isinstance(
                preparation.payload, RunControlPayload
            )
            assert (
                preparation.payload.spawn_thread == destination
                and preparation.payload.conversation is None
            )
            with harness.executor.stream.boundary() as (_, _, frames):
                assert (
                    len(
                        [
                            frame
                            for frame in frames
                            if isinstance(frame.event, ThreadCreated)
                            and frame.event.thread == destination
                        ]
                    )
                    == 0
                )

    asyncio.run(scenario())


def test_busy_thread_rejects_root_and_spawn_without_substituting_work(tmp_path):
    gate = AsyncGate()
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic worker() -> Text:
  user: Perform work.
agic controller() -> Text:
  user: Start a separate task in the requested thread.
flow other() -> Text:
  let result = Done
""",
        responses=[
            ScriptedModelTurn(
                ModelCallResult(message=Message.assistant("done")), gate=gate
            )
        ],
    )

    async def scenario():
        async with harness:
            destination = harness.threads.create(prefix=ThreadPrefix.TALK)
            running = harness.executor.run(
                harness.run_spec(thread=destination, runnable="agic:worker")
            )
            await asyncio.wait_for(gate.wait_until_entered(), 2)
            before = harness.store.list_runs()
            with pytest.raises(ThreadBusy):
                harness.executor.run(
                    harness.run_spec(thread=destination, runnable="flow:other")
                )
            assert harness.store.list_runs() == before
            harness.adapter._responses.extend(
                [
                    call("spawn", runnable="flow:other", thread=destination),
                    ModelCallResult(message=Message.assistant("Thread is busy.")),
                ]
            )
            source = harness.threads.create(prefix=ThreadPrefix.CHAT)
            parent = await harness.executor.run(
                harness.run_spec(thread=source, runnable="agic:controller")
            )
            assert parent.status == "succeeded", parent.error
            receipt = last_tool_result(harness.adapter.invocations[-1].call)
            assert (
                receipt.error is not None
                and "busy" in receipt.error
                and receipt.output == {}
            )
            assert (
                len(harness.store.list_runs()) == 2
                and len(harness.store.list_threads()) == 2
            )
            gate.release()
            assert (await running).status == "succeeded"

    asyncio.run(scenario())


def test_spawn_reuse_keeps_target_compaction_and_caller_launch_snapshot(tmp_path):
    from tests.support.execution_fixtures import project_compaction

    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic controller() -> Text:
  user: Start independent work in the selected thread.
flow worker(_: Text) -> Text:
  let result = {{_}}
""",
        responses=[],
    )

    async def scenario():
        async with harness:
            destination = harness.threads.create(prefix=ThreadPrefix.CHAT)
            previous = await harness.executor.run(
                harness.run_spec(
                    thread=destination,
                    runnable="flow:worker",
                    primary=(TextPart("Old work"),),
                )
            )
            retained = await harness.executor.run(
                harness.run_spec(
                    thread=destination,
                    runnable="flow:worker",
                    primary=(TextPart("Retained work"),),
                )
            )
            horizon = project_compaction(
                harness.store,
                thread=destination,
                begin=previous.id,
                end=retained.id,
                summary="Facts retained by the selected thread.",
            )
            harness.store.publish_compaction(
                horizon, roots=(RunRef(previous.id), RunRef(retained.id))
            )
            harness.adapter._responses.extend(
                [
                    call(
                        "spawn",
                        runnable="flow:worker",
                        input={"_": "New work"},
                        thread=destination,
                    ),
                    ModelCallResult(message=Message.assistant("Admitted.")),
                ]
            )
            source = harness.threads.create(prefix=ThreadPrefix.TALK)
            parent = await harness.executor.run(
                harness.run_spec(thread=source, runnable="agic:controller")
            )
            await asyncio.gather(*tuple(harness.executor._tasks))
            receipt = last_tool_result(harness.adapter.invocations[-1].call)
            assert receipt.error is None
            child_control = harness.store.get_run_control(
                run_id=receipt.output["id"], index=0
            )
            caller_control = harness.store.get_run_control(run_id=parent.id, index=0)
            assert child_control is not None and caller_control is not None
            child, caller = child_control.payload, caller_control.payload
            assert isinstance(child, RunControlPayload) and isinstance(
                caller, RunControlPayload
            )
            assert child.horizon == horizon
            assert child.state == caller.state and child.cwd == caller.cwd
            assert child.launch_context is not None
            assert child.launch_context.workspaces == (
                caller.launch_context.workspaces
                if caller.launch_context is not None
                else {}
            )
            assert child.resources == caller.resources
            assert child.conversation is None

    asyncio.run(scenario())


def test_self_cancel_preserves_accepted_control_receipt_during_delivery(tmp_path):
    gate = AsyncGate()
    harness = ExecutionHarness.create(
        tmp_path,
        source="agic controller() -> Text:\n  user: Cancel this invocation when authorized.\n",
        responses=[ScriptedModelTurn(call("runnables"), gate=gate)],
    )

    async def scenario():
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TALK)
            handle = harness.executor.run(
                harness.run_spec(thread=thread, runnable="agic:controller")
            )
            await asyncio.wait_for(gate.wait_until_entered(), 2)
            harness.adapter._responses.append(
                call(
                    "cancel",
                    target=handle.run_id,
                    expected_control=str(ControlRef.for_run(handle.run_id, 0)),
                    reason="Explicitly requested cancellation",
                )
            )
            gate.release()
            record = await handle
            assert record.status == "canceled"
            (control,) = harness.store.list_run_controls(
                run_id=record.id, kind="cancel"
            )
            assert control.triggered_by is not None
            step = harness.store.get_step(ref=control.triggered_by)
            assert step is not None and step.status == "succeeded"
            assert step.output is not None and isinstance(
                step.output.value, ToolResultPart
            )
            assert step.output.value.error is None
            assert step.output.value.output["command"]["status"] == "pending"
            assert step.output.value.output["command"]["run_id"] == record.id
            assert step.output.value.output["command"]["index"] == control.index

    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["steer", "cancel"])
@pytest.mark.parametrize("stale", [False, True])
def test_control_receipt_is_atomic_and_fences_the_inspected_invocation(
    tmp_path, operation, stale
):
    gate = AsyncGate()
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic worker() -> Text:
  user: Perform the existing task.
agic controller() -> Text:
  user: Control the inspected task as explicitly requested.
""",
        responses=[
            ScriptedModelTurn(
                ModelCallResult(message=Message.assistant("original work")), gate=gate
            )
        ],
    )

    async def scenario():
        async with harness:
            destination = harness.threads.create(prefix=ThreadPrefix.CHORE)
            worker = harness.executor.run(
                harness.run_spec(thread=destination, runnable="agic:worker")
            )
            await asyncio.wait_for(gate.wait_until_entered(), 2)
            expected = str(ControlRef(RunRef(worker.run_id), 1 if stale else 0))
            arguments = {
                "target": worker.run_id,
                "expected_control": expected,
                "input" if operation == "steer" else "reason": "Requested change",
            }
            harness.adapter._responses.extend(
                [
                    call(operation, **arguments),
                    ModelCallResult(message=Message.assistant("Reported.")),
                ]
            )
            source = harness.threads.create(prefix=ThreadPrefix.CHAT)
            parent = await harness.executor.run(
                harness.run_spec(thread=source, runnable="agic:controller")
            )
            assert parent.status == "succeeded", parent.error
            reply = last_tool_result(harness.adapter.invocations[-1].call)
            controls = harness.store.list_run_controls(
                run_id=worker.run_id, kind=operation
            )
            if stale:
                assert reply.error is not None and not controls
            else:
                assert (
                    reply.error is None
                    and reply.output["command"]["status"] == "pending"
                )
                (control,) = controls
                assert (
                    control.triggered_by is not None
                    and control.triggered_by.run_id == parent.id
                )
                step = harness.store.get_step(ref=control.triggered_by)
                assert step is not None and step.status == "succeeded"
                assert (
                    step.output is not None
                    and isinstance(step.output.value, ToolResultPart)
                    and step.output.value.output == reply.output
                )
            if operation == "steer" and not stale:
                harness.adapter._responses.append(
                    ModelCallResult(message=Message.assistant("revised work"))
                )
            gate.release()
            result = await worker
            assert result.status == (
                "canceled" if operation == "cancel" and not stale else "succeeded"
            )
            assert (
                len(harness.store.list_runs()) == 2
                and len(harness.store.list_threads()) == 2
            )

    asyncio.run(scenario())
