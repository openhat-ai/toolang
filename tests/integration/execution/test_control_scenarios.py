"""Run-control correctness scenarios using real execution objects."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from tests.support.execution_assertions import (
    assert_run_event_integrity,
    event_labels,
)
from tests.support.execution_harness import (
    AsyncGate,
    ExecutionHarness,
    RecordingRunTracer,
    RecordingTool,
    ScriptedModelTurn,
)
from toolang.base.types.message import Message, TextPart, ToolResultPart
from toolang.base.types.run import ModelCallResult, ToolCall
from toolang.execution.types import (
    ControlTiming,
    Pointer,
    StepPath,
    ThreadPrefix,
    ToolStepNoted,
)
from toolang.lang.input import resolve_input_parts


def test_stop_cancels_an_active_model_step_and_finishes_its_control(
    tmp_path: Path,
) -> None:
    gate = AsyncGate()
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic wait(_: Part[]) -> Part[]:
  recall = none
  context: none
  instruct: none
  user: {{_}}
""",
        responses=[
            ScriptedModelTurn(
                result=ModelCallResult(message=Message.assistant("unused")),
                gate=gate,
            )
        ],
    )
    tracer = RecordingRunTracer()

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            handle = harness.executor.start(
                harness.run_spec(
                    thread=thread,
                    runnable="wait",
                    primary=resolve_input_parts("wait"),
                ),
                tracer=tracer,
            )
            await asyncio.wait_for(gate.wait_until_entered(), timeout=1)
            control = handle.stop(reason="user canceled")
            record = await asyncio.wait_for(handle, timeout=2)

            assert record.status == "canceled"
            assert record.error == "user canceled"
            stored_control = harness.store.get_run_control(
                run_id=record.id,
                index=control.index,
            )
            assert stored_control is not None
            assert stored_control.status == "applied"
            steps = harness.store.list_steps(run_id=record.id)
            assert [(step.kind, step.status) for step in steps] == [
                ("model", "canceled")
            ]
            assert_run_event_integrity(tracer.events)
            assert event_labels(tracer.events) == [
                f"run_begin:{record.id}",
                f"step_begin:{record.id}.0:model",
                f"step_end:{record.id}.0:model:canceled",
                f"run_end:{record.id}:canceled",
            ]

    asyncio.run(scenario())


def test_steer_during_a_model_call_is_consumed_by_the_next_call(
    tmp_path: Path,
) -> None:
    gate = AsyncGate()
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic revise(_: Part[]) -> Part[]:
  recall = none
  context: none
  instruct: none
  user: {{_}}
""",
        responses=[
            ScriptedModelTurn(
                result=ModelCallResult(message=Message.assistant("draft")),
                gate=gate,
            ),
            ModelCallResult(message=Message.assistant("revised")),
        ],
    )
    tracer = RecordingRunTracer()

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            handle = harness.executor.start(
                harness.run_spec(
                    thread=thread,
                    runnable="revise",
                    primary=resolve_input_parts("write"),
                ),
                tracer=tracer,
            )
            await asyncio.wait_for(gate.wait_until_entered(), timeout=1)
            control = handle.steer(
                Message.user("make it shorter"),
                timing="next_call",
            )
            gate.release()
            record = await asyncio.wait_for(handle, timeout=2)

            assert record.status == "succeeded"
            assert harness.adapter.invocations[1].call.messages == [
                Message.user("write"),
                Message.assistant("draft"),
                Message.user("make it shorter"),
            ]
            stored_control = harness.store.get_run_control(
                run_id=record.id,
                index=control.index,
            )
            assert stored_control is not None
            assert stored_control.status == "applied"
            steps = harness.store.list_steps(run_id=record.id)
            assert steps[1].input == (
                Pointer.step(StepPath.parse(f"{record.id}.0")),
                Pointer.control(record.id, control.index, "_"),
            )
            assert harness.store.run_output(run_id=record.id) == (TextPart("revised"),)
            assert_run_event_integrity(tracer.events)
            assert [event.type for event in tracer.events].count("run_begin") == 1
            assert [event.type for event in tracer.events].count("run_end") == 1

    asyncio.run(scenario())


def test_next_step_steer_replaces_a_pending_tool_batch(tmp_path: Path) -> None:
    gate = AsyncGate()
    tool = RecordingTool("math__slow", output={"value": 6})
    tool_call = ToolCall(
        tool_call_id="tool-1",
        call_id="call-1",
        name=tool.name,
        input={"value": 3},
    )
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic calculate(_: Part[]) -> Part[]:
  recall = none
  context: none
  instruct: none
  user: {{_}}
""",
        responses=[
            ScriptedModelTurn(
                result=ModelCallResult(tool_calls=(tool_call,)),
                gate=gate,
            ),
            ModelCallResult(message=Message.assistant("revised")),
        ],
        tools={tool.name: tool},
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            handle = harness.executor.start(
                harness.run_spec(
                    thread=thread,
                    runnable="calculate",
                    primary=resolve_input_parts("calculate"),
                )
            )
            await asyncio.wait_for(gate.wait_until_entered(), timeout=1)
            control = handle.steer(Message.user("skip tools"), timing="next_step")
            gate.release()
            record = await asyncio.wait_for(handle, timeout=2)

            assert record.status == "succeeded"
            assert tool.calls == []
            assert [
                (step.kind, step.status)
                for step in harness.store.list_steps(run_id=record.id)
            ] == [("model", "succeeded"), ("model", "succeeded")]
            messages = harness.adapter.invocations[1].call.messages
            assert [message.role for message in messages] == [
                "user",
                "assistant",
                "tool",
                "user",
            ]
            canceled = messages[2].parts[0]
            assert isinstance(canceled, ToolResultPart)
            assert canceled.tool_call_id == tool_call.tool_call_id
            assert canceled.error == "canceled by steer"
            assert messages[3] == Message.user("skip tools")
            second = harness.store.list_steps(run_id=record.id)[1]
            assert second.input == (
                Pointer.step(StepPath.parse(f"{record.id}.0"), 0),
                Pointer.control(record.id, control.index, "_"),
            )

    asyncio.run(scenario())


def test_stop_cancels_an_active_tool_step(tmp_path: Path) -> None:
    gate = AsyncGate()
    tool = RecordingTool(
        "math__slow",
        output={"value": 6},
        parameters={
            "type": "object",
            "properties": {"value": {"type": "integer"}},
        },
        gate=gate,
    )
    tool_call = ToolCall(
        tool_call_id="tool-1",
        call_id="call-1",
        name=tool.name,
        input={"value": 3},
    )
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic calculate(_: Part[]) -> Part[]:
  recall = none
  context: none
  instruct: none
  user: {{_}}
""",
        responses=[
            ModelCallResult(tool_calls=(tool_call,)),
            ModelCallResult(message=Message.assistant("unused")),
        ],
        tools={tool.name: tool},
    )
    tracer = RecordingRunTracer()

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            handle = harness.executor.start(
                harness.run_spec(
                    thread=thread,
                    runnable="calculate",
                    primary=resolve_input_parts("slow calculation"),
                ),
                tracer=tracer,
            )
            await asyncio.wait_for(gate.wait_until_entered(), timeout=1)
            control = handle.stop(reason="stop tool")
            record = await asyncio.wait_for(handle, timeout=2)

            assert record.status == "canceled"
            steps = harness.store.list_steps(run_id=record.id)
            assert [(step.kind, step.status) for step in steps] == [
                ("model", "succeeded"),
                ("tool", "canceled"),
            ]
            assert steps[1].noted == ToolStepNoted(summary="Canceled slow 3")
            stored_control = harness.store.get_run_control(
                run_id=record.id,
                index=control.index,
            )
            assert stored_control is not None
            assert stored_control.status == "applied"
            assert harness.adapter.pending_responses == 1
            assert_run_event_integrity(tracer.events)
            assert event_labels(tracer.events) == [
                f"run_begin:{record.id}",
                f"step_begin:{record.id}.0:model",
                f"part_begin:{record.id}.0:0:tool_call",
                f"part_end:{record.id}.0:0:tool_call",
                f"step_end:{record.id}.0:model:succeeded",
                f"step_begin:{record.id}.1:tool",
                f"step_end:{record.id}.1:tool:canceled",
                f"run_end:{record.id}:canceled",
            ]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("timing", "expected_steps"),
    [
        ("immediate", [("run", "canceled")]),
        ("next_step", [("run", "succeeded")]),
        (
            "next_call",
            [("run", "succeeded"), ("value", "succeeded")],
        ),
    ],
)
def test_stop_timing_selects_the_next_matching_flow_boundary(
    tmp_path: Path,
    timing: ControlTiming,
    expected_steps: list[tuple[str, str]],
) -> None:
    gate = AsyncGate()
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic pause(_: Text) -> Text:
  recall = none
  context: none
  instruct: none
  user: {{_}}

flow sequence(_: Text) -> Text:
  run pause
  let note:
    checkpoint
  run pause
""",
        responses=[
            ScriptedModelTurn(
                result=ModelCallResult(message=Message.assistant("draft")),
                gate=gate,
            ),
            ModelCallResult(message=Message.assistant("unused")),
        ],
    )
    tracer = RecordingRunTracer()

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            handle = harness.executor.start(
                harness.run_spec(
                    thread=thread,
                    runnable="sequence",
                    primary=resolve_input_parts("start"),
                ),
                tracer=tracer,
            )
            await asyncio.wait_for(gate.wait_until_entered(), timeout=1)
            control = handle.stop(
                timing=timing,
                reason=f"stop at {timing}",
            )
            if timing != "immediate":
                gate.release()
            record = await asyncio.wait_for(handle, timeout=2)

            assert record.status == "canceled"
            assert record.error == f"stop at {timing}"
            stored = harness.store.get_run_control(
                run_id=record.id,
                index=control.index,
            )
            assert stored is not None and stored.status == "applied"
            root_steps = [
                (step.kind, step.status)
                for step in harness.store.list_steps(run_id=record.id)
                if step.parent is None
            ]
            assert root_steps == expected_steps
            assert harness.adapter.pending_responses == 1
            assert_run_event_integrity(tracer.events)

    asyncio.run(scenario())


@pytest.mark.parametrize("timing", ["immediate", "next_step", "next_call"])
def test_flow_runs_reject_steer_controls(
    tmp_path: Path,
    timing: ControlTiming,
) -> None:
    gate = AsyncGate()
    guidance = Message.user(f"guidance for {timing}")
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic pause(_: Text) -> Text:
  recall = none
  context: none
  instruct: none
  user: {{_}}

flow sequence(_: Text) -> Text:
  run pause
  let note:
    checkpoint
  run pause
""",
        responses=[
            ScriptedModelTurn(
                result=ModelCallResult(message=Message.assistant("draft")),
                gate=gate,
            ),
            ModelCallResult(message=Message.assistant("final")),
        ],
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            handle = harness.executor.start(
                harness.run_spec(
                    thread=thread,
                    runnable="sequence",
                    primary=resolve_input_parts("start"),
                )
            )
            await asyncio.wait_for(gate.wait_until_entered(), timeout=1)
            with pytest.raises(ValueError, match="steer controls require an agic run"):
                handle.steer(guidance, timing=timing)
            gate.release()
            record = await asyncio.wait_for(handle, timeout=2)

            assert record.status == "succeeded"
            assert harness.store.run_output_text(run_id=record.id) == "final"

    asyncio.run(scenario())


def test_multiple_steers_are_consumed_in_durable_index_order(
    tmp_path: Path,
) -> None:
    gate = AsyncGate()
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic revise(_: Text) -> Text:
  recall = none
  context: none
  instruct: none
  user: {{_}}
""",
        responses=[
            ScriptedModelTurn(
                result=ModelCallResult(message=Message.assistant("draft")),
                gate=gate,
            ),
            ModelCallResult(message=Message.assistant("final")),
        ],
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            handle = harness.executor.start(
                harness.run_spec(
                    thread=thread,
                    runnable="revise",
                    primary=resolve_input_parts("start"),
                )
            )
            await asyncio.wait_for(gate.wait_until_entered(), timeout=1)
            timings: tuple[ControlTiming, ...] = (
                "immediate",
                "next_step",
                "next_call",
            )
            controls = [
                handle.steer(
                    Message.user(f"guidance {index}"),
                    timing=timing,
                )
                for index, timing in enumerate(timings, start=1)
            ]
            gate.release()
            record = await asyncio.wait_for(handle, timeout=2)

            assert record.status == "succeeded"
            assert harness.adapter.invocations[1].call.messages == [
                Message.user("start"),
                Message.user("guidance 1"),
                Message.user("guidance 2"),
                Message.user("guidance 3"),
            ]
            assert [control.index for control in controls] == [1, 2, 3]
            second_step = harness.store.list_steps(run_id=record.id)[1]
            assert second_step.input == (
                Pointer.control(record.id, 0, "_"),
                Pointer.control(record.id, 1, "_"),
                Pointer.control(record.id, 2, "_"),
                Pointer.control(record.id, 3, "_"),
            )
            stored_controls = [
                harness.store.get_run_control(
                    run_id=record.id,
                    index=control.index,
                )
                for control in controls
            ]
            assert all(control is not None for control in stored_controls)
            assert [
                control.status for control in stored_controls if control is not None
            ] == ["applied", "applied", "applied"]

    asyncio.run(scenario())
