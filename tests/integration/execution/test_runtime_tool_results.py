"""Runtime responses remain execution facts without another Model Call."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from tests.support.execution_assertions import assert_run_event_integrity
from tests.support.execution_harness import (
    AsyncGate,
    ExecutionHarness,
    RecordingRunTracer,
    RecordingTool,
    ScriptedModelTurn,
)
from toolang.base.types.message import Message, ToolResultPart
from toolang.base.types.policy import RunLimits
from toolang.base.types.run import ModelCallResult, ToolCall
from toolang.cli.common.execution_progress import ProgressProjector
from toolang.execution.events import PartBegin, PartEnd, RunEvent, StepBegin, StepEnd
from toolang.execution.history import RunHistory
from toolang.execution.store import RunStore
from toolang.execution.trees import build_execution_tree
from toolang.execution.types import FieldRef, StepRef, ThreadPrefix, ToolStepGiven
from toolang.execution.values import parts_from_local

SOURCE = """
agic parent() -> Text:
  recall = none
  hands = agic:child
  context: none
  instruct: none
  user: Parent task.

agic child() -> Text:
  recall = none
  context: none
  instruct: none
  user: Child task.
"""


def call(name: str, *, id: str = "first") -> ToolCall:
    return ToolCall(
        tool_call_id=id,
        call_id=f"provider-{id}",
        name=name,
        input={"runnable": "agic:child"} if name == "_too__run" else {},
    )


@pytest.mark.parametrize("tool_name", ["_too__run", "_too__unknown"])
def test_runtime_result_survives_restart_without_followup_model(
    tmp_path: Path, tool_name: str
) -> None:
    request = call(tool_name)
    harness = ExecutionHarness.create(
        tmp_path,
        source=SOURCE,
        responses=[
            ModelCallResult(tool_calls=(request,)),
            *(
                [ModelCallResult(message=Message.assistant("child output"))]
                if tool_name == "_too__run"
                else []
            ),
        ],
    )
    tracer = RecordingRunTracer()

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            root = await harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="parent",
                    limits=RunLimits(agic_model_calls=1),
                ),
                tracer=tracer,
            )
            assert root.status == "failed"
            steps = harness.store.list_steps(run_id=root.id)
            assert [step.kind for step in steps] == ["model", "tool"]
            assert_run_event_integrity(tracer.events)
        reopened = RunStore(harness.store.db_path, read_only=True)
        try:
            history = RunHistory(reopened)
            model, tool = history.run_view(root.id).steps()
            assert isinstance(tool.given, ToolStepGiven)
            assert tool.given.call == request
            assert tool.input == (FieldRef.from_path(model.ref, "output", "value", 0),)
            assert tool.output is not None
            (part,) = parts_from_local(tool.output)
            assert isinstance(part, ToolResultPart)
            assert part.tool_call_id == request.tool_call_id
            assert part.call_id == request.call_id
            conversation = reopened.recent_conversation_messages(thread_id=thread)
            results = [
                item
                for message in conversation
                for item in message.parts
                if isinstance(item, ToolResultPart)
            ]
            assert results == [part]
            root_record = history.run_view(root.id).record
            tree = build_execution_tree(reopened.load_execution_snapshot(root=root.id))
            assert [node.pointer for node in tree.nodes[:3]] == [
                root.id,
                str(model.ref),
                str(tool.ref),
            ]
            if tool_name == "_too__run":
                assert tool.status == "succeeded"
                child = reopened.get_run(run_id=part.output["run_id"])
                assert child is not None and child.parent == tool.ref
                assert tree.nodes[3].pointer == child.id
                assert tree.nodes[3].parent == str(tool.ref)
                assert part.output["output"] == "child output"
                assert root_record.output is not None
                assert (
                    reopened.resolve_value(root_record.output.value) == "child output"
                )
                assert all(
                    step.ref.run_id == root.id
                    for step in history.run_view(root.id).steps()
                )
                assert Message.user("Child task.") not in conversation
            else:
                assert tool.status == "failed"
                assert part.error == "unknown inner runtime tool: _too__unknown"
        finally:
            reopened.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("boundary", ["part_begin", "part_end", "step_end"])
@pytest.mark.parametrize("tool_name", ["math__double", "_too__run", "_too__unknown"])
def test_steer_during_result_delivery_preserves_result_once(
    tmp_path: Path, boundary: str, tool_name: str
) -> None:
    gate = AsyncGate()
    tool = RecordingTool("math__double", output={"value": 6})
    requests = (call(tool_name), call("math__double", id="skipped"))

    class DeliveryTracer(RecordingRunTracer):
        async def on_event(self, event: RunEvent) -> None:
            await super().on_event(event)
            if (
                isinstance(event, (PartBegin, PartEnd, StepEnd))
                and event.type == boundary
                and event.step.index == 1
                and not gate.entered
            ):
                await gate.wait()

    harness = ExecutionHarness.create(
        tmp_path,
        source=SOURCE,
        tools={tool.name: tool},
        responses=[
            ModelCallResult(tool_calls=requests),
            *(
                [ModelCallResult(message=Message.assistant("child output"))]
                if tool_name == "_too__run"
                else []
            ),
            ModelCallResult(message=Message.assistant("revised")),
        ],
    )
    tracer = DeliveryTracer()

    async def scenario() -> None:
        async with harness:
            handle = harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="parent",
                ),
                tracer=tracer,
            )
            await asyncio.wait_for(gate.wait_until_entered(), timeout=1)
            steer = handle.steer(Message.user("change direction"), timing="immediate")
            root = await asyncio.wait_for(handle, timeout=2)
            assert root.status == "succeeded", root.error
            steps = harness.store.list_steps(run_id=root.id)
            assert [step.kind for step in steps] == ["model", "tool", "tool", "model"]
            first, skipped = steps[1:3]
            assert first.output is not None and skipped.output is not None
            (first_part,) = parts_from_local(first.output)
            (skipped_part,) = parts_from_local(skipped.output)
            assert isinstance(first_part, ToolResultPart)
            assert first_part.error != "canceled by steer"
            expected_status = (
                ("failed" if tool_name == "_too__unknown" else "succeeded")
                if boundary == "step_end"
                else "canceled"
            )
            assert first.status == expected_status
            assert first.aborted_by == (None if boundary == "step_end" else steer.ref)
            assert skipped.status == "canceled" and skipped.aborted_by == steer.ref
            results = [
                part
                for message in harness.adapter.invocations[-1].call.messages
                for part in message.parts
                if isinstance(part, ToolResultPart)
            ]
            assert results == [first_part, skipped_part]
            assert len(tool.calls) == (1 if tool_name == tool.name else 0)
            assert steps[-1].preceded_by == (steer.ref,)
            assert_run_event_integrity(tracer.events)

    asyncio.run(scenario())


@pytest.mark.parametrize("interruption", ["steer", "cancel"])
def test_interrupting_runtime_child_terminates_owning_tool_step(
    tmp_path: Path, interruption: str
) -> None:
    gate = AsyncGate()
    harness = ExecutionHarness.create(
        tmp_path,
        source=SOURCE,
        responses=[
            ModelCallResult(tool_calls=(call("_too__run"),)),
            ScriptedModelTurn(
                result=ModelCallResult(message=Message.assistant("unused")), gate=gate
            ),
            ModelCallResult(message=Message.assistant("revised")),
        ],
    )
    tracer = RecordingRunTracer()

    async def scenario() -> None:
        async with harness:
            handle = harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="parent",
                ),
                tracer=tracer,
            )
            await asyncio.wait_for(gate.wait_until_entered(), timeout=1)
            control = (
                handle.steer(Message.user("change direction"), timing="immediate")
                if interruption == "steer"
                else handle.cancel()
            )
            root = await asyncio.wait_for(handle, timeout=2)
            assert root.status == (
                "succeeded" if interruption == "steer" else "canceled"
            )
            tool_step = harness.store.list_steps(run_id=root.id)[1]
            assert tool_step.kind == "tool" and tool_step.status == "canceled"
            assert tool_step.aborted_by == control.ref
            child = next(
                run
                for run in harness.store.list_run_tree(root_run_id=root.id)
                if run.parent == tool_step.ref
            )
            assert child.status == "canceled"
            if interruption == "steer":
                assert tool_step.output is not None
                assert parts_from_local(tool_step.output) == (
                    harness.adapter.invocations[-1].call.messages[-2].parts[0],
                )
            else:
                assert tool_step.output is None
            assert_run_event_integrity(tracer.events)

    asyncio.run(scenario())


def test_retry_replaces_runtime_results_and_owned_child(tmp_path: Path) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source=SOURCE,
        responses=[
            ModelCallResult(tool_calls=(call("_too__run", id="old"),)),
            ModelCallResult(message=Message.assistant("old output")),
            RuntimeError("retry me"),
            ModelCallResult(tool_calls=(call("_too__run", id="new"),)),
            ModelCallResult(message=Message.assistant("new output")),
            ModelCallResult(message=Message.assistant("done")),
        ],
    )

    async def scenario() -> None:
        async with harness:
            root = await harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="parent",
                )
            )
            assert root.status == "failed"
            old_child = next(
                run
                for run in harness.store.list_run_tree(root_run_id=root.id)
                if run.parent is not None
            )
            retried = await harness.executor.retry(
                root.id, setup=harness.setup, state=harness.state
            )
            assert retried.status == "succeeded", retried.error
            assert harness.store.get_run(run_id=old_child.id) is None
            view = RunHistory(harness.store).run_view(root.id)
            assert [step.ref for step in view.steps()] == [
                StepRef.from_local(root.id, (index,)) for index in range(3)
            ]
            result = view.steps()[1].output
            assert result is not None
            (part,) = parts_from_local(result)
            assert isinstance(part, ToolResultPart) and part.tool_call_id == "new"
            assert part.output["output"] == "new output"
            assert part.output["run_id"] != old_child.id

    asyncio.run(scenario())


@pytest.mark.parametrize("followup", [False, True])
def test_skipped_batch_is_durable_and_does_not_consume_call_budget(
    tmp_path: Path, followup: bool
) -> None:
    gate = AsyncGate()
    requests = (call("_too__run"), call("math__double", id="second"))
    tool = RecordingTool("math__double", output={"value": 6})
    harness = ExecutionHarness.create(
        tmp_path,
        source=SOURCE,
        tools={tool.name: tool},
        responses=[
            ScriptedModelTurn(result=ModelCallResult(tool_calls=requests), gate=gate),
            *(
                [
                    ModelCallResult(tool_calls=(call(tool.name, id="actual"),)),
                    ModelCallResult(message=Message.assistant("done")),
                ]
                if followup
                else []
            ),
        ],
    )
    tracer = RecordingRunTracer()

    async def scenario() -> None:
        async with harness:
            handle = harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="parent",
                    limits=RunLimits(
                        agic_model_calls=3 if followup else 1, agic_tool_calls=1
                    ),
                ),
                tracer=tracer,
            )
            await asyncio.wait_for(gate.wait_until_entered(), timeout=1)
            steer = handle.steer(Message.user("skip these calls"), timing="next_step")
            gate.release()
            root = await asyncio.wait_for(handle, timeout=2)
            assert root.status == ("succeeded" if followup else "failed")
            assert len(tool.calls) == (1 if followup else 0)
            assert harness.store.list_run_tree(root_run_id=root.id) == [root]
            assert_run_event_integrity(tracer.events)
        reopened = RunStore(harness.store.db_path, read_only=True)
        try:
            steps = RunHistory(reopened).run_view(root.id).steps()
            for step, request in zip(steps[1:3], requests, strict=True):
                assert step.kind == "tool" and step.status == "canceled"
                assert (
                    isinstance(step.given, ToolStepGiven) and step.given.call == request
                )
                assert step.aborted_by == steer.ref
                assert step.output is not None
                (part,) = parts_from_local(step.output)
                assert isinstance(part, ToolResultPart)
                assert part.tool_call_id == request.tool_call_id
                assert part.error == "canceled by steer"
            if followup:
                messages = harness.adapter.invocations[1].call.messages
                assert messages[-2].role == "tool"
                assert len(messages[-2].parts) == 2
                assert messages[-1] == Message.user("skip these calls")
        finally:
            reopened.close()

    asyncio.run(scenario())


def test_steer_during_execute_delivery_keeps_committed_transfer(tmp_path: Path) -> None:
    gate = AsyncGate()

    class DeliveryTracer(RecordingRunTracer):
        async def on_event(self, event: RunEvent) -> None:
            await super().on_event(event)
            if (
                isinstance(event, PartEnd)
                and event.step.index == 1
                and not gate.entered
            ):
                await gate.wait()

    harness = ExecutionHarness.create(
        tmp_path,
        source=SOURCE.replace("hands = agic:child", "handoffs = agic:child"),
        responses=[
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        tool_call_id="transfer",
                        call_id="provider-transfer",
                        name="_too__execute",
                        input={"runnable": "agic:child"},
                    ),
                )
            ),
            ModelCallResult(message=Message.assistant("done")),
        ],
    )
    tracer = DeliveryTracer()

    async def scenario() -> None:
        async with harness:
            handle = harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="parent",
                ),
                tracer=tracer,
            )
            await asyncio.wait_for(gate.wait_until_entered(), timeout=1)
            steer = handle.steer(Message.user("extra requirement"), timing="immediate")
            root = await asyncio.wait_for(handle, timeout=2)
            assert root.status == "succeeded", root.error
            followup = harness.adapter.invocations[1].call.messages
            assert Message.user("Child task.") in followup
            assert followup[-1] == Message.user("extra requirement")
            steps = harness.store.list_steps(run_id=root.id)
            assert steps[1].output is not None
            (part,) = parts_from_local(steps[1].output)
            assert isinstance(part, ToolResultPart)
            assert part.output == {"executed": "agent$agic:child"}
            assert steps[1].aborted_by == steer.ref
            assert_run_event_integrity(tracer.events)
            projector = ProgressProjector()
            rows = [
                row
                for event in tracer.events
                for block in projector.handle(event).committed
                for row in block.rows
            ]
            assert not projector._broken
            assert any(row.text == "---  handoff to agic:child" for row in rows)
            assert not any("Failed to execute" in row.text for row in rows)

    asyncio.run(scenario())


@pytest.mark.parametrize("tool_name", ["math__double", "_too__run", "_too__unknown"])
@pytest.mark.parametrize(
    "boundary", ["step_begin", "queued_begin", "cancel_queued_begin"]
)
def test_steer_at_tool_begin_closes_the_started_step(
    tmp_path: Path, tool_name: str, boundary: str
) -> None:
    gate = AsyncGate()
    blockers: list[asyncio.Task[None]] = []

    async def hold_event_lock(run_id: str) -> None:
        async with harness.executor._active[run_id].event_lock:
            await gate.wait()

    class BeginTracer(RecordingRunTracer):
        async def on_event(self, event: RunEvent) -> None:
            await super().on_event(event)
            if (
                boundary.endswith("queued_begin")
                and isinstance(event, StepEnd)
                and event.step.index == 0
            ):
                blockers.append(asyncio.create_task(hold_event_lock(event.step.run_id)))
                await asyncio.sleep(0)
            if (
                boundary == "step_begin"
                and isinstance(event, StepBegin)
                and event.kind == "tool"
                and not gate.entered
            ):
                await gate.wait()

    tool = RecordingTool("math__double", output={"value": 6})
    harness = ExecutionHarness.create(
        tmp_path,
        source=SOURCE,
        tools={tool.name: tool},
        responses=[
            ModelCallResult(tool_calls=(call(tool_name),)),
            ModelCallResult(message=Message.assistant("revised")),
        ],
    )
    tracer = BeginTracer()

    async def scenario() -> None:
        async with harness:
            handle = harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="parent",
                ),
                tracer=tracer,
            )
            await asyncio.wait_for(gate.wait_until_entered(), timeout=1)
            steer = handle.steer(Message.user("skip"), timing="immediate")
            if boundary.endswith("queued_begin"):
                await asyncio.sleep(0)
                if boundary == "cancel_queued_begin":
                    handle.cancel(reason="stop instead")
                    await asyncio.sleep(0)
                gate.release()
            root = await asyncio.wait_for(handle, timeout=2)
            await asyncio.gather(*blockers)
            if boundary == "cancel_queued_begin":
                assert root.status == "canceled", root.error
                assert len(harness.adapter.invocations) == 1
                assert tool.calls == []
                assert_run_event_integrity(tracer.events)
                return
            assert root.status == "succeeded", root.error
            step = harness.store.list_steps(run_id=root.id)[1]
            assert step.status == "canceled" and step.aborted_by == steer.ref
            assert step.output is not None
            assert (
                parts_from_local(step.output)
                == harness.adapter.invocations[-1].call.messages[-2].parts
            )
            assert tool.calls == []
            assert harness.store.list_run_tree(root_run_id=root.id) == [root]
            assert_run_event_integrity(tracer.events)

    asyncio.run(scenario())


@pytest.mark.parametrize("boundary", ["step_begin", "step_end"])
def test_immediate_steer_during_skipped_batch_preserves_all_results(
    tmp_path: Path, boundary: str
) -> None:
    model_gate, skip_gate = AsyncGate(), AsyncGate()

    class SkipTracer(RecordingRunTracer):
        async def on_event(self, event: RunEvent) -> None:
            await super().on_event(event)
            if (
                isinstance(event, (StepBegin, StepEnd))
                and event.type == boundary
                and event.step.index == 2
                and not skip_gate.entered
            ):
                await skip_gate.wait()

    requests = tuple(call("math__double", id=str(index)) for index in range(3))
    tool = RecordingTool("math__double", output={"value": 6})
    harness = ExecutionHarness.create(
        tmp_path,
        source=SOURCE,
        tools={tool.name: tool},
        responses=[
            ScriptedModelTurn(
                result=ModelCallResult(tool_calls=requests), gate=model_gate
            ),
            ModelCallResult(message=Message.assistant("revised")),
        ],
    )
    tracer = SkipTracer()

    async def scenario() -> None:
        async with harness:
            handle = harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="parent",
                ),
                tracer=tracer,
            )
            await asyncio.wait_for(model_gate.wait_until_entered(), timeout=1)
            first = handle.steer(Message.user("skip tools"), timing="next_step")
            model_gate.release()
            await asyncio.wait_for(skip_gate.wait_until_entered(), timeout=1)
            second = handle.steer(
                Message.user("another requirement"), timing="immediate"
            )
            root = await asyncio.wait_for(handle, timeout=2)
            assert root.status == "succeeded", root.error
            assert tool.calls == []
            steps = harness.store.list_steps(run_id=root.id)
            assert [step.status for step in steps] == [
                "succeeded",
                "canceled",
                "canceled",
                "canceled",
                "succeeded",
            ]
            parts = tuple(
                part
                for message in harness.adapter.invocations[-1].call.messages
                for part in message.parts
                if isinstance(part, ToolResultPart)
            )
            assert [part.tool_call_id for part in parts] == [
                call.tool_call_id for call in requests
            ]
            assert (
                tuple(
                    part
                    for step in steps[1:4]
                    if step.output is not None
                    for part in parts_from_local(step.output)
                )
                == parts
            )
            assert steps[-1].preceded_by == (first.ref, second.ref)
            assert_run_event_integrity(tracer.events)

    asyncio.run(scenario())
