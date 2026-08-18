"""Agic correctness scenarios composed from public execution objects."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from decimal import Decimal
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
from toolang.base.types.message import (
    AudioPart,
    ImagePart,
    Message,
    TextDelta,
    TextPart,
    ToolCallDelta,
    ToolCallPart,
    ToolResultPart,
)
from toolang.base.types.run import (
    ModelCallResult,
    ModelPartDelta,
    ModelPartEnd,
    ModelPartStart,
    ModelUsage,
    ToolCall,
)
from toolang.common.errors import ToolangError
from toolang.execution.events import PartDelta, RunBegin, RunEnd
from toolang.execution.executor import RunLimits
from toolang.execution.records import RunControlRef, StartControlPayload
from toolang.execution.types import StepPath, ThreadPrefix, Pointer
from toolang.lang.input import perceive_input


def test_agic_executes_perceived_text_and_typed_arguments(
    tmp_path: Path,
) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic reply(_: Part[], tone: Text) -> Part[]:
  recall = none
  context: none
  instruct: none
  user: Reply to {{_}} in {{tone}}.
""",
        responses=[ModelCallResult(message=Message.assistant("done"))],
    )
    tracer = RecordingRunTracer()

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            record = await harness.executor.start(
                harness.run_spec(
                    thread=thread,
                    runnable="reply",
                    primary=perceive_input("hello"),
                    named={"tone": "brief"},
                ),
                tracer=tracer,
            )

            assert record.status == "succeeded"
            assert harness.store.run_output(run_id=record.id) == (TextPart("done"),)
            assert harness.adapter.invocations[0].call.messages == [
                Message.user("Reply to hello in brief.")
            ]
            steps = harness.store.list_steps(run_id=record.id)
            assert [step.kind for step in steps] == ["model"]
            assert steps[0].input == (
                Pointer.control(record.id, 0, "_"),
                Pointer.control(record.id, 0, "tone"),
            )
            assert [event.type for event in tracer.events] == [
                "run_begin",
                "step_begin",
                "part_begin",
                "part_end",
                "step_end",
                "run_end",
            ]
            assert isinstance(tracer.events[0], RunBegin)
            assert isinstance(tracer.events[-1], RunEnd)

    asyncio.run(scenario())


def test_retry_restarts_an_agic_cycle_with_a_fresh_step_index(
    tmp_path: Path,
) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic reply(_: Part[]) -> Part[]:
  recall = none
  context: none
  instruct: none
  user: {{_}}
""",
        responses=[
            RuntimeError("temporary failure"),
            ModelCallResult(message=Message.assistant("recovered")),
        ],
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            run = await harness.executor.start(
                harness.run_spec(
                    thread=thread,
                    runnable="reply",
                    primary=(TextPart("hello"),),
                )
            )
            assert run.status == "failed"

            run = await harness.executor.retry(
                run.id,
                setup=harness.setup,
                state=harness.state,
            )

            assert run.status == "succeeded"
            active = harness.store.list_steps(run_id=run.id)
            assert [(step.path.index, step.status) for step in active] == [
                (1, "succeeded")
            ]
            historical = harness.store.list_steps(
                run_id=run.id,
                include_ejected=True,
            )
            assert [step.path.index for step in historical] == [0, 1]
            assert historical[0].ejected_by == RunControlRef(run.id, 1)
            assert historical[1].ejected_by is None
            assert historical[1].input == (Pointer.control(run.id, 1, "_"),)

    asyncio.run(scenario())


def test_named_only_agic_records_only_the_local_it_reads(tmp_path: Path) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic reply(topic: Text) -> Part[]:
  recall = none
  context: none
  instruct: none
  user: Discuss {{topic}}.
""",
        responses=[ModelCallResult(message=Message.assistant("done"))],
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            record = await harness.executor.start(
                harness.run_spec(
                    thread=thread,
                    runnable="reply",
                    named={"topic": "provenance"},
                )
            )

            steps = harness.store.list_steps(run_id=record.id)
            assert len(steps) == 1
            assert steps[0].input == (Pointer.control(record.id, 0, "topic"),)

    asyncio.run(scenario())


def test_agic_preserves_multimodal_input_and_output(tmp_path: Path) -> None:
    image = ImagePart(file_id="image-1", filename="diagram.png")
    audio = AudioPart(
        data="ZGF0YQ==",
        format="wav",
        transcript="diagram accepted",
    )
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic inspect(_: Part[]) -> Part[]:
  recall = none
  context: none
  instruct: none
  user: {{_}}
""",
        responses=[ModelCallResult(message=Message(role="assistant", parts=(audio,)))],
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.WEB)
            input = perceive_input((TextPart("Inspect this: "), image))
            record = await harness.executor.start(
                harness.run_spec(
                    thread=thread,
                    runnable="inspect",
                    primary=input,
                )
            )

            assert record.status == "succeeded"
            assert harness.adapter.invocations[0].call.messages == [
                Message(role="user", parts=input)
            ]
            assert harness.store.run_output(run_id=record.id) == (audio,)

    asyncio.run(scenario())


def test_streaming_agic_traces_deltas_and_persists_final_output(
    tmp_path: Path,
) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic stream(_: Part[]) -> Part[]:
  recall = none
  context: none
  instruct: none
  user: {{_}}
""",
        responses=[
            ScriptedModelTurn(
                result=ModelCallResult(message=Message.assistant("hello")),
                updates=(
                    ModelPartStart(kind="text"),
                    ModelPartDelta(delta=TextDelta("hel")),
                    ModelPartDelta(delta=TextDelta("lo")),
                    ModelPartEnd(data=TextPart("hello")),
                ),
            )
        ],
        streaming=True,
    )
    tracer = RecordingRunTracer()

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.WEB)
            record = await harness.executor.start(
                harness.run_spec(
                    thread=thread,
                    runnable="stream",
                    primary=perceive_input("say hello"),
                ),
                tracer=tracer,
            )

            assert record.status == "succeeded"
            assert harness.store.run_output(run_id=record.id) == (TextPart("hello"),)
            assert [event.type for event in tracer.events] == [
                "run_begin",
                "step_begin",
                "part_begin",
                "part_delta",
                "part_delta",
                "part_end",
                "step_end",
                "run_end",
            ]
            assert [
                event.delta.text
                for event in tracer.events
                if isinstance(event, PartDelta)
            ] == ["hel", "lo"]
            assert_run_event_integrity(tracer.events)

    asyncio.run(scenario())


def test_streaming_tool_call_deltas_share_one_terminal_part(
    tmp_path: Path,
) -> None:
    tool = RecordingTool("math__double", output={"value": 6})
    call = ToolCall(
        tool_call_id="tool-1",
        call_id="call-1",
        name=tool.name,
        input={"value": 3},
    )
    call_part = ToolCallPart(
        tool_call_id=call.tool_call_id,
        call_id=call.call_id,
        tool_name=call.name,
        tool_family=call.name,
        input=call.input,
    )
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic calculate(_: Text) -> Text:
  recall = none
  context: none
  instruct: none
  user: {{_}}
""",
        responses=[
            ScriptedModelTurn(
                result=ModelCallResult(
                    message=Message(role="assistant", parts=(call_part,)),
                    tool_calls=(call,),
                ),
                updates=(
                    ModelPartStart(kind="tool_call"),
                    ModelPartDelta(delta=ToolCallDelta('{"value":', call.tool_call_id)),
                    ModelPartDelta(delta=ToolCallDelta("3}", call.tool_call_id)),
                    ModelPartEnd(data=call_part),
                ),
            ),
            ModelCallResult(message=Message.assistant("six")),
        ],
        tools={tool.name: tool},
        streaming=True,
    )
    tracer = RecordingRunTracer()

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            record = await harness.executor.start(
                harness.run_spec(
                    thread=thread,
                    runnable="calculate",
                    primary=perceive_input("double three"),
                ),
                tracer=tracer,
            )

            assert record.status == "succeeded"
            deltas = [
                event.delta
                for event in tracer.events
                if isinstance(event, PartDelta)
                and isinstance(event.delta, ToolCallDelta)
            ]
            assert [delta.text for delta in deltas] == ['{"value":', "3}"]
            assert {delta.tool_call_id for delta in deltas} == {call.tool_call_id}
            assert (
                event_labels(tracer.events).count(
                    f"part_begin:{record.id}/0:0:tool_call"
                )
                == 1
            )
            assert (
                event_labels(tracer.events).count(f"part_end:{record.id}/0:0:tool_call")
                == 1
            )
            assert harness.store.run_output_text(run_id=record.id) == "six"
            assert_run_event_integrity(tracer.events)

    asyncio.run(scenario())


def test_streaming_model_preserves_text_and_image_part_order(
    tmp_path: Path,
) -> None:
    image = ImagePart(file_id="image-1", filename="result.png")
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic illustrate(_: Text) -> Part[]:
  recall = none
  context: none
  instruct: none
  user: {{_}}
""",
        responses=[
            ScriptedModelTurn(
                result=ModelCallResult(
                    message=Message(
                        role="assistant",
                        parts=(TextPart("caption"), image),
                    )
                ),
                updates=(
                    ModelPartStart(kind="text"),
                    ModelPartDelta(delta=TextDelta("caption")),
                    ModelPartEnd(data=TextPart("caption")),
                ),
            )
        ],
        streaming=True,
    )
    tracer = RecordingRunTracer()

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.WEB)
            record = await harness.executor.start(
                harness.run_spec(
                    thread=thread,
                    runnable="illustrate",
                    primary=perceive_input("draw"),
                ),
                tracer=tracer,
            )

            assert record.status == "succeeded"
            assert harness.store.run_output(run_id=record.id) == (
                TextPart("caption"),
                image,
            )
            assert event_labels(tracer.events) == [
                f"run_begin:{record.id}",
                f"step_begin:{record.id}/0:model",
                f"part_begin:{record.id}/0:0:text",
                f"part_delta:{record.id}/0:0",
                f"part_end:{record.id}/0:0:text",
                f"part_begin:{record.id}/0:1:image",
                f"part_end:{record.id}/0:1:image",
                f"step_end:{record.id}/0:model:succeeded",
                f"run_end:{record.id}:succeeded",
            ]
            assert_run_event_integrity(tracer.events)

    asyncio.run(scenario())


def test_streaming_failure_closes_the_model_step_after_partial_output(
    tmp_path: Path,
) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic stream(_: Text) -> Text:
  recall = none
  context: none
  instruct: none
  user: {{_}}
""",
        responses=[
            ScriptedModelTurn(
                result=ModelCallResult(),
                updates=(
                    ModelPartStart(kind="text"),
                    ModelPartDelta(delta=TextDelta("partial")),
                ),
                error=RuntimeError("stream disconnected"),
            )
        ],
        streaming=True,
    )
    tracer = RecordingRunTracer()

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            record = await harness.executor.start(
                harness.run_spec(
                    thread=thread,
                    runnable="stream",
                    primary=perceive_input("start"),
                ),
                tracer=tracer,
            )

            assert record.status == "failed"
            assert record.error == Pointer.step(StepPath(record.id, (0,)))
            assert [
                (step.kind, step.status, step.error)
                for step in harness.store.list_steps(run_id=record.id)
            ] == [("model", "failed", "stream disconnected")]
            assert event_labels(tracer.events) == [
                f"run_begin:{record.id}",
                f"step_begin:{record.id}/0:model",
                f"part_begin:{record.id}/0:0:text",
                f"part_delta:{record.id}/0:0",
                f"step_end:{record.id}/0:model:failed",
                f"run_end:{record.id}:failed",
            ]
            assert_run_event_integrity(tracer.events)

    asyncio.run(scenario())


def test_streaming_cancellation_closes_the_model_step_after_partial_output(
    tmp_path: Path,
) -> None:
    gate = AsyncGate()
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic stream(_: Text) -> Text:
  recall = none
  context: none
  instruct: none
  user: {{_}}
""",
        responses=[
            ScriptedModelTurn(
                result=ModelCallResult(),
                updates=(
                    ModelPartStart(kind="text"),
                    ModelPartDelta(delta=TextDelta("partial")),
                ),
                after_updates_gate=gate,
            )
        ],
        streaming=True,
    )
    tracer = RecordingRunTracer()

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            handle = harness.executor.start(
                harness.run_spec(
                    thread=thread,
                    runnable="stream",
                    primary=perceive_input("start"),
                ),
                tracer=tracer,
            )
            await asyncio.wait_for(gate.wait_until_entered(), timeout=1)
            control = handle.stop(reason="cancel partial stream")
            record = await asyncio.wait_for(handle, timeout=2)

            assert record.status == "canceled"
            stored = harness.store.get_run_control(
                run_id=record.id,
                index=control.index,
            )
            assert stored is not None and stored.status == "applied"
            assert event_labels(tracer.events) == [
                f"run_begin:{record.id}",
                f"step_begin:{record.id}/0:model",
                f"part_begin:{record.id}/0:0:text",
                f"part_delta:{record.id}/0:0",
                f"step_end:{record.id}/0:model:canceled",
                f"run_end:{record.id}:canceled",
            ]
            assert_run_event_integrity(tracer.events)

    asyncio.run(scenario())


def test_agic_tool_loop_persists_and_replays_each_call(tmp_path: Path) -> None:
    tool = RecordingTool(
        "math__double",
        output={"value": 6},
    )
    tool_call = ToolCall(
        tool_call_id="tool-1",
        call_id="call-1",
        name=tool.name,
        input={"value": 3},
    )
    tool_part = ToolCallPart(
        tool_call_id=tool_call.tool_call_id,
        call_id=tool_call.call_id,
        tool_name=tool_call.name,
        tool_family=tool_call.name,
        input=tool_call.input,
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
            ModelCallResult(
                message=Message(role="assistant", parts=(tool_part,)),
                tool_calls=(tool_call,),
            ),
            ModelCallResult(message=Message.assistant("six")),
        ],
        tools={tool.name: tool},
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            record = await harness.executor.start(
                harness.run_spec(
                    thread=thread,
                    runnable="calculate",
                    primary=perceive_input("double three"),
                )
            )

            assert record.status == "succeeded"
            assert len(tool.calls) == 1
            arguments, context = tool.calls[0]
            assert arguments == {"value": 3}
            assert context.run_id == record.id
            assert [
                step.kind for step in harness.store.list_steps(run_id=record.id)
            ] == ["model", "tool", "model"]
            followup = harness.adapter.invocations[1].call.messages
            assert [message.role for message in followup] == [
                "user",
                "assistant",
                "tool",
            ]
            assert isinstance(followup[-1].parts[0], ToolResultPart)
            assert followup[-1].parts[0].output == {"value": 6}
            assert harness.store.run_output(run_id=record.id) == (TextPart("six"),)

    asyncio.run(scenario())


def test_multiple_tool_failures_are_reported_in_order_and_can_recover(
    tmp_path: Path,
) -> None:
    broken = RecordingTool(
        "math__broken",
        output={},
        error=RuntimeError("calculator unavailable"),
    )
    calls = (
        ToolCall(
            tool_call_id="tool-1",
            call_id="call-1",
            name=broken.name,
            input={"value": 3},
        ),
        ToolCall(
            tool_call_id="tool-2",
            call_id="call-2",
            name="missing__tool",
            input={"value": 4},
        ),
    )
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic calculate(_: Text) -> Text:
  recall = none
  context: none
  instruct: none
  user: {{_}}
""",
        responses=[
            ModelCallResult(tool_calls=calls),
            ModelCallResult(message=Message.assistant("recovered")),
        ],
        tools={broken.name: broken},
    )
    tracer = RecordingRunTracer()

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            record = await harness.executor.start(
                harness.run_spec(
                    thread=thread,
                    runnable="calculate",
                    primary=perceive_input("calculate"),
                ),
                tracer=tracer,
            )

            assert record.status == "succeeded"
            steps = harness.store.list_steps(run_id=record.id)
            assert [(step.kind, step.status) for step in steps] == [
                ("model", "succeeded"),
                ("tool", "failed"),
                ("tool", "failed"),
                ("model", "succeeded"),
            ]
            assert [step.error for step in steps[1:3]] == [
                "calculator unavailable",
                "unknown tool call: missing__tool",
            ]
            followup = harness.adapter.invocations[1].call.messages
            results = [
                part
                for message in followup
                for part in message.parts
                if isinstance(part, ToolResultPart)
            ]
            assert [result.tool_call_id for result in results] == [
                "tool-1",
                "tool-2",
            ]
            assert [result.error for result in results] == [
                "calculator unavailable",
                "unknown tool call: missing__tool",
            ]
            assert harness.store.run_output_text(run_id=record.id) == ("recovered")
            assert_run_event_integrity(tracer.events)

    asyncio.run(scenario())


def test_recovered_tool_failure_does_not_hide_a_later_runtime_failure(
    tmp_path: Path,
) -> None:
    broken = RecordingTool(
        "math__broken",
        output={},
        error=RuntimeError("calculator unavailable"),
    )
    call = ToolCall(
        tool_call_id="tool-1",
        call_id="call-1",
        name=broken.name,
        input={"value": 3},
    )
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic calculate(_: Text) -> Number:
  recall = none
  context: none
  instruct: none
  user: {{_}}
""",
        responses=[
            ModelCallResult(tool_calls=(call,)),
            ModelCallResult(message=Message.assistant("not a number")),
        ],
        tools={broken.name: broken},
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            record = await harness.executor.start(
                harness.run_spec(
                    thread=thread,
                    runnable="calculate",
                    primary=perceive_input("calculate"),
                )
            )

            assert record.status == "failed"
            assert isinstance(record.error, str)
            assert record.error.startswith("output is not valid Number")
            steps = harness.store.list_steps(run_id=record.id)
            assert [(step.kind, step.status) for step in steps] == [
                ("model", "succeeded"),
                ("tool", "failed"),
                ("model", "succeeded"),
            ]
            assert steps[1].error == "calculator unavailable"

    asyncio.run(scenario())


def test_agic_model_call_limit_records_a_direct_run_error(
    tmp_path: Path,
) -> None:
    tool = RecordingTool("loop__again", output={"continue": True})
    calls = tuple(
        ToolCall(
            tool_call_id=f"tool-{index}",
            call_id=f"call-{index}",
            name=tool.name,
            input={"round": index},
        )
        for index in range(8)
    )
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic loop(_: Text) -> Text:
  recall = none
  context: none
  instruct: none
  user: {{_}}
""",
        responses=[ModelCallResult(tool_calls=(call,)) for call in calls],
        tools={tool.name: tool},
    )
    tracer = RecordingRunTracer()

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            record = await harness.executor.start(
                harness.run_spec(
                    thread=thread,
                    runnable="loop",
                    primary=perceive_input("continue"),
                    limits=RunLimits(agic_model_calls=8),
                ),
                tracer=tracer,
            )

            assert record.status == "failed"
            assert record.error == "Agic model call limit exceeded: 8"
            steps = harness.store.list_steps(run_id=record.id)
            assert [(step.kind, step.status) for step in steps] == [
                item
                for _ in range(8)
                for item in (
                    ("model", "succeeded"),
                    ("tool", "succeeded"),
                )
            ]
            assert len(tool.calls) == 8
            assert harness.adapter.pending_responses == 0
            assert_run_event_integrity(tracer.events)

    asyncio.run(scenario())


def test_agent_setup_limits_are_used_and_start_can_override_them(
    tmp_path: Path,
) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic reply(_: Text) -> Text:
  recall = none
  context: none
  instruct: none
  user: {{_}}
""",
        responses=[ModelCallResult(message=Message.assistant("done"))],
    )
    harness.setup = replace(
        harness.setup,
        limits=RunLimits(agic_model_calls=0),
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            rejected = await harness.executor.start(
                harness.run_spec(
                    thread=thread,
                    runnable="reply",
                    primary=perceive_input("first"),
                )
            )
            accepted = await harness.executor.start(
                harness.run_spec(
                    thread=thread,
                    runnable="reply",
                    primary=perceive_input("second"),
                    limits=RunLimits(agic_model_calls=1),
                ),
            )

            assert rejected.status == "failed"
            assert rejected.error == "Agic model call limit exceeded: 0"
            assert accepted.status == "succeeded"
            assert len(harness.adapter.invocations) == 1
            rejected_start = harness.store.get_run_control(
                run_id=rejected.id,
                index=0,
            )
            accepted_start = harness.store.get_run_control(
                run_id=accepted.id,
                index=0,
            )
            assert rejected_start is not None
            assert accepted_start is not None
            assert isinstance(rejected_start.payload, StartControlPayload)
            assert isinstance(accepted_start.payload, StartControlPayload)
            assert rejected_start.payload.limits == RunLimits(agic_model_calls=0)
            assert accepted_start.payload.limits == RunLimits(agic_model_calls=1)

    asyncio.run(scenario())


def test_agic_tool_call_limit_counts_each_emitted_call(
    tmp_path: Path,
) -> None:
    tool = RecordingTool("loop__again", output={"continue": True})
    calls = tuple(
        ToolCall(
            tool_call_id=f"tool-{index}",
            call_id=f"call-{index}",
            name=tool.name,
            input={"index": index},
        )
        for index in range(2)
    )
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic loop(_: Text) -> Text:
  recall = none
  context: none
  instruct: none
  user: {{_}}
""",
        responses=[ModelCallResult(tool_calls=calls)],
        tools={tool.name: tool},
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            record = await harness.executor.start(
                harness.run_spec(
                    thread=thread,
                    runnable="loop",
                    primary=perceive_input("continue"),
                    limits=RunLimits(agic_tool_calls=1),
                ),
            )

            assert record.status == "failed"
            assert record.error == "Agic tool call limit exceeded: 1"
            assert len(tool.calls) == 1

    asyncio.run(scenario())


def test_run_token_limit_uses_model_usage(
    tmp_path: Path,
) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic reply(_: Text) -> Text:
  recall = none
  context: none
  instruct: none
  user: {{_}}
""",
        responses=[
            ModelCallResult(
                message=Message.assistant("done"),
                usage=ModelUsage(input_tokens=6, output_tokens=5),
            )
        ],
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            record = await harness.executor.start(
                harness.run_spec(
                    thread=thread,
                    runnable="reply",
                    primary=perceive_input("hello"),
                    limits=RunLimits(tokens=10),
                ),
            )

            assert record.status == "failed"
            assert record.error == "Run token limit exceeded: 11 > 10"
            assert [
                (step.kind, step.status)
                for step in harness.store.list_steps(run_id=record.id)
            ] == [("model", "succeeded")]
            model_step = harness.store.list_steps(run_id=record.id)[0]
            assert model_step.noted["tokens"] == {"input": 6, "output": 5}
            assert model_step.noted["price"] is None
            assert model_step.noted["cost"] is None
            assert "usage" not in model_step.noted

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("cost_limit", "status", "error"),
    [
        (None, "succeeded", None),
        (
            Decimal("0.02"),
            "failed",
            "Run cost limit exceeded: 0.03 > 0.02 USD",
        ),
    ],
)
def test_model_step_records_cost_and_enforces_run_cost_limit(
    tmp_path: Path,
    cost_limit: Decimal | None,
    status: str,
    error: str | None,
) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic reply(_: Text) -> Text:
  recall = none
  context: none
  instruct: none
  user: {{_}}
""",
        responses=[
            ModelCallResult(
                message=Message.assistant("done"),
                usage=ModelUsage(input_tokens=1, output_tokens=1),
            )
        ],
    )
    harness.setup = replace(
        harness.setup,
        models=tuple(
            replace(model, input_price=0.01, output_price=0.02)
            for model in harness.setup.models
        ),
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            record = await harness.executor.start(
                harness.run_spec(
                    thread=thread,
                    runnable="reply",
                    primary=perceive_input("hello"),
                    limits=RunLimits(cost=cost_limit),
                ),
            )

            assert record.status == status
            assert record.error == error
            model_step = harness.store.list_steps(run_id=record.id)[0]
            assert model_step.noted["tokens"] == {"input": 1, "output": 1}
            assert model_step.noted["price"] == {
                "input": "0.01",
                "output": "0.02",
            }
            assert model_step.noted["cost"] == "0.03"

    asyncio.run(scenario())


def test_run_cost_limit_rejects_unknown_pricing_before_model_call(
    tmp_path: Path,
) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic reply(_: Text) -> Text:
  recall = none
  context: none
  instruct: none
  user: {{_}}
""",
        responses=[ModelCallResult(message=Message.assistant("unused"))],
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            record = await harness.executor.start(
                harness.run_spec(
                    thread=thread,
                    runnable="reply",
                    primary=perceive_input("hello"),
                    limits=RunLimits(cost=Decimal("1")),
                ),
            )

            assert record.status == "failed"
            assert record.error == (
                "Model pricing is required by the run cost limit: test/scripted"
            )
            assert harness.adapter.invocations == []

    asyncio.run(scenario())


def test_run_token_limit_requires_provider_usage(
    tmp_path: Path,
) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic reply(_: Text) -> Text:
  recall = none
  context: none
  instruct: none
  user: {{_}}
""",
        responses=[ModelCallResult(message=Message.assistant("done"))],
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            record = await harness.executor.start(
                harness.run_spec(
                    thread=thread,
                    runnable="reply",
                    primary=perceive_input("hello"),
                    limits=RunLimits(tokens=100),
                ),
            )

            assert record.status == "failed"
            assert record.error == (
                "Model usage is required by run token or cost limits: test/scripted"
            )
            assert len(harness.adapter.invocations) == 1
            model_step = harness.store.list_steps(run_id=record.id)[0]
            assert model_step.noted["tokens"] is None
            assert model_step.noted["price"] is None
            assert model_step.noted["cost"] is None

    asyncio.run(scenario())


def test_run_time_limit_cancels_an_inflight_model_as_failure(
    tmp_path: Path,
) -> None:
    gate = AsyncGate()
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic reply(_: Text) -> Text:
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

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            record = await harness.executor.start(
                harness.run_spec(
                    thread=thread,
                    runnable="reply",
                    primary=perceive_input("hello"),
                    limits=RunLimits(time=0),
                ),
            )

            assert gate.entered
            assert record.status == "failed"
            assert record.error == "Run time limit exceeded: 0s"
            assert [
                (step.kind, step.status)
                for step in harness.store.list_steps(run_id=record.id)
            ] == [("model", "canceled")]

    asyncio.run(scenario())


def test_invalid_input_is_rejected_before_run_acceptance(
    tmp_path: Path,
) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic text_only(_: Text) -> Text:
  recall = none
  context: none
  instruct: none
  user: {{_}}
""",
        responses=[ModelCallResult(message=Message.assistant("unused"))],
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            with pytest.raises(ToolangError, match="non-text parts"):
                harness.executor.start(
                    harness.run_spec(
                        thread=thread,
                        runnable="text_only",
                        primary=(ImagePart(file_id="image-1"),),
                    )
                )

            assert harness.store.list_runs(limit=None) == []
            assert harness.adapter.invocations == []
            assert harness.adapter.pending_responses == 1

    asyncio.run(scenario())


def test_model_failure_records_one_failed_model_step(
    tmp_path: Path,
) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic fail(_: Part[]) -> Part[]:
  recall = none
  context: none
  instruct: none
  user: {{_}}
""",
        responses=[RuntimeError("provider unavailable")],
    )
    tracer = RecordingRunTracer()

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            record = await harness.executor.start(
                harness.run_spec(
                    thread=thread,
                    runnable="fail",
                    primary=perceive_input("hello"),
                ),
                tracer=tracer,
            )

            assert record.status == "failed"
            assert record.error == Pointer.step(StepPath(record.id, (0,)))
            steps = harness.store.list_steps(run_id=record.id)
            assert [(step.kind, step.status) for step in steps] == [("model", "failed")]
            assert steps[0].error == "provider unavailable"
            assert harness.store.run_output(run_id=record.id) == ()
            assert_run_event_integrity(tracer.events)
            assert event_labels(tracer.events) == [
                f"run_begin:{record.id}",
                f"step_begin:{record.id}/0:model",
                f"step_end:{record.id}/0:model:failed",
                f"run_end:{record.id}:failed",
            ]

    asyncio.run(scenario())
