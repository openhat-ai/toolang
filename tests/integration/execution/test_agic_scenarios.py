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
    TEST_MODEL_REF,
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
from toolang.base.types.model import (
    ModelOverride,
    ModelParameters,
    ModelRequest,
    ReasoningParameters,
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
from toolang.execution.events import (
    PartBegin,
    PartDelta,
    PartEnd,
    RunBegin,
    RunEnd,
    RunEvent,
)
from toolang.execution.executor import RunLimits
from toolang.execution.records import (
    RetryControlPayload,
    RunControlPayload,
)
from toolang.execution.schemas import RerunRequest
from toolang.execution.store import RunStore
from toolang.execution.values import parts_from_local
from toolang.execution.types import (
    ControlRef,
    Pointer,
    ErrorMessage,
    ErrorRef,
    FieldRef,
    ModelStepNoted,
    ModelTokenCount,
    ModelTokenPrice,
    StepRef,
    ThreadPrefix,
    ToolStepNoted,
)
from toolang.lang.input import resolve_input_parts
from toolang.setup import ModelCollection


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
            record = await harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="reply",
                    primary=resolve_input_parts("hello"),
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
                FieldRef.from_path(
                    ControlRef.for_run(record.id, 0), "payload", "input", 0, "value"
                ),
                FieldRef.from_path(
                    ControlRef.for_run(record.id, 0), "payload", "input", 1, "value"
                ),
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


def test_agic_repairs_one_invalid_structured_output(tmp_path: Path) -> None:
    tool = RecordingTool("lookup__value", output={"value": True})
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic decide(_: Text) -> Boolean:
  recall = none
  tools = lookup/*
  context: none
  instruct: none
  user: {{_}}
""",
        responses=[
            ModelCallResult(usage=ModelUsage(input_tokens=8, output_tokens=1)),
            ModelCallResult(
                message=Message.assistant("true"),
                usage=ModelUsage(input_tokens=12, output_tokens=1),
            ),
        ],
        tools={tool.name: tool},
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            record = await harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="decide",
                    primary=resolve_input_parts("Is the evidence relevant?"),
                )
            )

            assert record.status == "succeeded"
            assert harness.store.run_output_text(run_id=record.id) == "true"
            steps = harness.store.list_steps(run_id=record.id)
            assert [(step.kind, step.status) for step in steps] == [
                ("model", "succeeded"),
                ("model", "succeeded"),
            ]
            assert {
                tool.name for tool in harness.adapter.invocations[0].call.tools
            } == {
                "_too__execute",
                "_too__reload",
                "_too__run",
                "lookup__value",
            }
            initial = harness.adapter.invocations[0].call
            assert "<output-contract>" not in initial.instructions
            assert "type: Boolean" not in initial.instructions
            assert initial.output_schema == {"type": "boolean"}
            repair = harness.adapter.invocations[1].call
            assert repair.tools == ()
            assert repair.output_schema == initial.output_schema
            assert repair.messages[-1].role == "user"
            repair_part = repair.messages[-1].parts[0]
            assert isinstance(repair_part, TextPart)
            assert "Return only a corrected Boolean value" in repair_part.text
            assert harness.adapter.pending_responses == 0

    asyncio.run(scenario())


@pytest.mark.parametrize("retries", [1, 2])
def test_retry_restarts_an_agic_cycle_with_a_fresh_step_index(
    tmp_path: Path,
    retries: int,
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
            *[RuntimeError("temporary failure") for _ in range(retries)],
            ModelCallResult(message=Message.assistant("recovered")),
        ],
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            run = await harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="reply",
                    primary=(TextPart("hello"),),
                )
            )
            assert run.status == "failed"

            for _attempt in range(retries):
                run = await harness.executor.retry(
                    run.id,
                    setup=harness.setup,
                    state=harness.state,
                )

            assert run.status == "succeeded"
            active = harness.store.list_steps(run_id=run.id)
            assert [(step.ref.index, step.status) for step in active] == [
                (0, "succeeded")
            ]
            assert active[0].input == (
                FieldRef.from_path(
                    ControlRef.for_run(run.id, 0), "payload", "input", 0, "value"
                ),
            )
            assert [call.call.messages for call in harness.adapter.invocations] == [
                [Message.user("hello")]
            ] * (retries + 1)
            assert active[0].preceded_by == (ControlRef.for_run(run.id, retries),)
            assert (
                harness.store.select_pointer(Pointer(active[0].input[0])).runtime
                is not None
            )

    asyncio.run(scenario())


def test_reasoning_effort_reaches_accounting_and_restart_persistence(
    tmp_path: Path,
) -> None:
    responses = [
        ModelCallResult(
            message=Message.assistant(label),
            usage=ModelUsage(input_tokens=1, output_tokens=1),
        )
        for label in (
            "source",
            "retry",
            "rerun",
            "sparse",
            "replacement",
            "automatic",
        )
    ]
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic reply(_: Part[]) -> Part[]:
  recall = none
  context: none
  instruct: none
  user: {{_}}
""",
        responses=responses,
    )
    harness.setup = replace(
        harness.setup,
        models=ModelCollection(
            tuple(
                replace(
                    entry,
                    target=replace(
                        entry.target,
                        reasoning={"enabled": True, "effort": "medium"},
                    ),
                    info=replace(
                        entry.info,
                        metadata={
                            **entry.info.metadata,
                            "reasoning_options": [
                                {"type": "toggle"},
                                {
                                    "type": "effort",
                                    "values": ["medium", "high", "low"],
                                },
                            ],
                        },
                    ),
                )
                for entry in harness.setup.models.entries
            )
        ),
    )
    harness.executor._setup = lambda: harness.setup
    high = ModelRequest(
        TEST_MODEL_REF,
        ModelParameters(ReasoningParameters("high")),
    )
    low = ModelRequest(
        TEST_MODEL_REF,
        ModelParameters(ReasoningParameters("low")),
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            source_spec = replace(
                harness.run_spec(
                    thread=thread,
                    runnable="reply",
                    primary=resolve_input_parts("hello"),
                    model=TEST_MODEL_REF,
                ),
                model_request=high,
            )
            source = await harness.executor.run(source_spec)
            source_control = harness.store.get_run_control(
                run_id=source.id,
                index=0,
            )
            assert source_control is not None
            assert isinstance(source_control.payload, RunControlPayload)
            assert source_control.payload.model_request == high

            retried = await harness.executor.retry(
                source.id,
                setup=harness.setup,
                state=harness.state,
            )
            retry_control = harness.store.list_run_controls(run_id=retried.id)[-1]
            assert isinstance(retry_control.payload, RetryControlPayload)
            assert retry_control.payload.model_request == high

            preserved = await harness.executor.rerun(
                source.id,
                setup=harness.setup,
                state=harness.state,
            )
            preserved_control = harness.store.get_run_control(
                run_id=preserved.id,
                index=0,
            )
            assert preserved_control is not None
            assert isinstance(preserved_control.payload, RunControlPayload)
            assert preserved_control.payload.model_request == high

            sparse = await harness.executor.rerun(
                RerunRequest(
                    source.id,
                    (),
                    "rerun_sparse_model_parameters",
                    model_override=ModelOverride(effort="low"),
                )
            )
            sparse_control = harness.store.get_run_control(
                run_id=sparse.id,
                index=0,
            )
            assert sparse_control is not None
            assert isinstance(sparse_control.payload, RunControlPayload)
            assert sparse_control.payload.model_request == low

            replacement = await harness.executor.rerun(
                source.id,
                setup=harness.setup,
                state=harness.state,
                model_request=low,
            )
            replacement_control = harness.store.get_run_control(
                run_id=replacement.id,
                index=0,
            )
            assert replacement_control is not None
            assert isinstance(replacement_control.payload, RunControlPayload)
            assert replacement_control.payload.model_request == low

            automatic_spec = replace(
                harness.run_spec(
                    thread=thread,
                    runnable="reply",
                    primary=resolve_input_parts("automatic"),
                    model=TEST_MODEL_REF,
                ),
                model_request=ModelRequest(TEST_MODEL_REF),
            )
            automatic = await harness.executor.run(automatic_spec)

            assert source.status == retried.status == "succeeded"
            assert (
                preserved.status
                == sparse.status
                == replacement.status
                == automatic.status
                == ("succeeded")
            )
            assert [
                invocation.target.reasoning
                for invocation in harness.adapter.invocations
            ] == [
                {"effort": "high"},
                {"effort": "high"},
                {"effort": "high"},
                {"effort": "low"},
                {"effort": "low"},
                {"enabled": True, "effort": "medium"},
            ]
            for run, expected in (
                (preserved, {"effort": "high"}),
                (sparse, {"effort": "low"}),
                (replacement, {"effort": "low"}),
                (automatic, {"enabled": True, "effort": "medium"}),
            ):
                step = harness.store.list_steps(run_id=run.id)[0]
                assert isinstance(step.noted, ModelStepNoted)
                assert step.noted.accounting is not None
                assert step.noted.accounting.reasoning.requested == expected

            before = harness.store.list_runs(thread_id=thread, limit=None)
            unsupported = replace(
                source_spec,
                model_request=ModelRequest(
                    TEST_MODEL_REF,
                    ModelParameters(ReasoningParameters("max")),
                ),
            )
            with pytest.raises(ToolangError, match="allowed: medium, high, low"):
                harness.executor.run(unsupported)
            assert harness.store.list_runs(thread_id=thread, limit=None) == before

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
            record = await harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="reply",
                    named={"topic": "provenance"},
                )
            )

            steps = harness.store.list_steps(run_id=record.id)
            assert len(steps) == 1
            assert steps[0].input == (
                FieldRef.from_path(
                    ControlRef.for_run(record.id, 0), "payload", "input", 0, "value"
                ),
            )

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
            input = resolve_input_parts((TextPart("Inspect this: "), image))
            record = await harness.executor.run(
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
            record = await harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="stream",
                    primary=resolve_input_parts("say hello"),
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


def test_streaming_agic_rejects_a_final_result_that_rewrites_deltas(
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
                result=ModelCallResult(message=Message.assistant("different")),
                updates=(
                    ModelPartStart(kind="text"),
                    ModelPartDelta(delta=TextDelta("prefix")),
                ),
            )
        ],
        streaming=True,
    )
    tracer = RecordingRunTracer()

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.WEB)
            record = await harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="stream",
                    primary=resolve_input_parts("start"),
                ),
                tracer=tracer,
            )

            assert record.status == "failed"
            assert record.error == ErrorRef(
                FieldRef.from_path(StepRef.from_local(record.id, (0,)), "error")
            )
            assert [
                (step.status, step.error)
                for step in harness.store.list_steps(run_id=record.id)
            ] == [
                (
                    "failed",
                    ErrorMessage(
                        "ModelCallResult text does not extend streamed TextDelta content"
                    ),
                )
            ]
            assert event_labels(tracer.events) == [
                f"run_begin:{record.id}",
                f"step_begin:{record.id}.0:model",
                f"part_begin:{record.id}.0:0:text",
                f"part_delta:{record.id}.0:0",
                f"part_end:{record.id}.0:0:text",
                f"step_end:{record.id}.0:model:failed",
                f"run_end:{record.id}:failed",
            ]
            assert_run_event_integrity(tracer.events)

    asyncio.run(scenario())


def test_streaming_agic_rejects_a_result_that_rewrites_the_part_end(
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
                result=ModelCallResult(message=Message.assistant("prefix result")),
                updates=(
                    ModelPartStart(kind="text"),
                    ModelPartDelta(delta=TextDelta("prefix")),
                    ModelPartEnd(data=TextPart("prefix closure")),
                ),
            )
        ],
        streaming=True,
    )
    tracer = RecordingRunTracer()

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.WEB)
            record = await harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="stream",
                    primary=resolve_input_parts("start"),
                ),
                tracer=tracer,
            )

            assert record.status == "failed"
            assert [
                (step.status, step.error)
                for step in harness.store.list_steps(run_id=record.id)
            ] == [
                (
                    "failed",
                    ErrorMessage(
                        "ModelCallResult text does not match authoritative ModelPartEnd"
                    ),
                )
            ]
            assert event_labels(tracer.events) == [
                f"run_begin:{record.id}",
                f"step_begin:{record.id}.0:model",
                f"part_begin:{record.id}.0:0:text",
                f"part_delta:{record.id}.0:0",
                f"part_end:{record.id}.0:0:text",
                f"step_end:{record.id}.0:model:failed",
                f"run_end:{record.id}:failed",
            ]
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
            record = await harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="calculate",
                    primary=resolve_input_parts("double three"),
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
                    f"part_begin:{record.id}.0:0:tool_call"
                )
                == 1
            )
            assert (
                event_labels(tracer.events).count(f"part_end:{record.id}.0:0:tool_call")
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
            record = await harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="illustrate",
                    primary=resolve_input_parts("draw"),
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
                f"step_begin:{record.id}.0:model",
                f"part_begin:{record.id}.0:0:text",
                f"part_delta:{record.id}.0:0",
                f"part_end:{record.id}.0:0:text",
                f"part_begin:{record.id}.0:1:image",
                f"part_end:{record.id}.0:1:image",
                f"step_end:{record.id}.0:model:succeeded",
                f"run_end:{record.id}:succeeded",
            ]
            assert_run_event_integrity(tracer.events)

    asyncio.run(scenario())


@pytest.mark.parametrize("interruption", ["cancel", "steer", "error"])
@pytest.mark.parametrize("completed_text", [False, True])
def test_interrupted_model_persists_partial_output(
    tmp_path: Path, interruption: str, completed_text: bool
) -> None:
    gate = AsyncGate()
    image = ImagePart(file_id="image-1", filename="result.png")
    call = ToolCallPart(
        tool_call_id="complete", tool_name="unused", tool_family="unused", input={}
    )
    updates = [
        ModelPartEnd(data=image),
        ModelPartDelta(delta=TextDelta("partial")),
    ]
    if completed_text:
        updates.append(ModelPartEnd(data=TextPart("partial complete")))
    updates.extend(
        [
            ModelPartEnd(data=call),
            ModelPartDelta(delta=ToolCallDelta('{"unfinished":', "incomplete")),
        ]
    )
    expected = (
        image,
        TextPart("partial complete" if completed_text else "partial"),
        call,
    )
    harness = ExecutionHarness.create(
        tmp_path,
        source="agic reply(_: Part[]) -> Part[]:\n  recall = none\n  user: {{_}}\n",
        responses=[
            ScriptedModelTurn(
                result=ModelCallResult(),
                updates=tuple(updates),
                after_updates_gate=gate,
                error=RuntimeError("stream disconnected")
                if interruption == "error"
                else None,
            ),
            ModelCallResult(message=Message.assistant("revised")),
        ],
        streaming=True,
    )
    tracer = RecordingRunTracer()

    async def scenario() -> None:
        async with harness:
            handle = harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="reply",
                    primary=resolve_input_parts("write"),
                ),
                tracer=tracer,
            )
            await asyncio.wait_for(gate.wait_until_entered(), timeout=1)
            control = None
            if interruption == "cancel":
                control = handle.cancel(timing="immediate")
            elif interruption == "steer":
                control = handle.steer(
                    Message.user("change direction"), timing="immediate"
                )
            else:
                gate.release()
            run = await asyncio.wait_for(handle, timeout=2)
            assert (
                run.status
                == {"cancel": "canceled", "steer": "succeeded", "error": "failed"}[
                    interruption
                ]
            )
            steps = harness.store.list_steps(run_id=run.id)
            first = steps[0]
            assert first.status == ("failed" if interruption == "error" else "canceled")
            assert first.aborted_by == (control.ref if control is not None else None)
            assert first.output is not None
            assert parts_from_local(first.output) == expected
            assert all(step.kind == "model" for step in steps)
            assert len(harness.adapter.invocations) == (
                2 if interruption == "steer" else 1
            )
            assert_run_event_integrity(tracer.events)
        reopened = RunStore(tmp_path / "agents/alice/.runtime/runs.db")
        try:
            assert reopened.get_step(ref=first.ref) == first
        finally:
            reopened.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("boundary", ["part_begin", "part_end", "step_end"])
@pytest.mark.parametrize("interruption", ["cancel", "steer"])
def test_interrupting_model_result_delivery_preserves_step_output(
    tmp_path: Path, boundary: str, interruption: str
) -> None:
    gate = AsyncGate()

    class DeliveryTracer(RecordingRunTracer):
        waiting = False

        async def on_event(self, event: RunEvent) -> None:
            await super().on_event(event)
            if event.type == boundary and not self.waiting:
                self.waiting = True
                await gate.wait()

    parts = (TextPart("complete"), ImagePart(file_id="image-1"))
    harness = ExecutionHarness.create(
        tmp_path,
        source="agic reply() -> Part[]:\n  recall = none\n  user: Draw.\n",
        responses=[
            ModelCallResult(message=Message(role="assistant", parts=parts)),
            ModelCallResult(message=Message.assistant("revised")),
        ],
    )
    tracer = DeliveryTracer()

    async def scenario() -> None:
        async with harness:
            handle = harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="reply",
                ),
                tracer=tracer,
            )
            await asyncio.wait_for(gate.wait_until_entered(), timeout=1)
            if interruption == "cancel":
                control = handle.cancel(timing="immediate")
            else:
                control = handle.steer(
                    Message.user("change direction"), timing="immediate"
                )
            run = await asyncio.wait_for(handle, timeout=2)
            assert run.status == (
                "canceled" if interruption == "cancel" else "succeeded"
            )
            first = harness.store.list_steps(run_id=run.id)[0]
            assert first.status == (
                "succeeded" if boundary == "step_end" else "canceled"
            )
            assert first.aborted_by == (None if boundary == "step_end" else control.ref)
            assert first.output is not None
            assert parts_from_local(first.output) == parts
            assert_run_event_integrity(tracer.events)

    asyncio.run(scenario())


def test_streaming_completed_images_preserve_order_without_duplicates(
    tmp_path: Path,
) -> None:
    image = ImagePart(file_id="image-1", filename="result.png")
    parts = (image, TextPart("caption"), image)
    harness = ExecutionHarness.create(
        tmp_path,
        source="agic reply() -> Part[]:\n  user: Draw.\n",
        responses=[
            ScriptedModelTurn(
                result=ModelCallResult(message=Message(role="assistant", parts=parts)),
                updates=tuple(ModelPartEnd(data=part) for part in parts),
            )
        ],
        streaming=True,
    )
    tracer = RecordingRunTracer()

    async def scenario() -> None:
        async with harness:
            run = await harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="reply",
                ),
                tracer=tracer,
            )
            assert run.status == "succeeded"
            assert harness.store.run_output(run_id=run.id) == parts
            assert_run_event_integrity(tracer.events)

    asyncio.run(scenario())


@pytest.mark.parametrize("boundary", ["part_begin", "part_end"])
@pytest.mark.parametrize("tool_name", ["math__double", "_too__reload", "_too__execute"])
def test_cancel_during_tool_result_delivery_preserves_output(
    tmp_path: Path, boundary: str, tool_name: str
) -> None:
    gate = AsyncGate()

    class DeliveryTracer(RecordingRunTracer):
        waiting = False

        async def on_event(self, event: RunEvent) -> None:
            await super().on_event(event)
            if (
                isinstance(event, (PartBegin, PartEnd))
                and event.type == boundary
                and event.step.index == 1
                and not self.waiting
            ):
                self.waiting = True
                await gate.wait()

    tool = RecordingTool("math__double", output={"value": 6})
    harness = ExecutionHarness.create(
        tmp_path,
        source="agic caller() -> Text:\n  handoffs = agic:target\n  user: Call.\n\nagic target() -> Text:\n  user: Done.\n",
        tools={tool.name: tool},
        responses=[
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        tool_call_id="call-1",
                        call_id="provider-1",
                        name=tool_name,
                        input={"runnable": "agic:target"}
                        if tool_name == "_too__execute"
                        else {},
                    ),
                )
            )
        ],
    )
    tracer = DeliveryTracer()

    async def scenario() -> None:
        async with harness:
            handle = harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="caller",
                ),
                tracer=tracer,
            )
            await asyncio.wait_for(gate.wait_until_entered(), timeout=1)
            control = handle.cancel(timing="immediate")
            run = await asyncio.wait_for(handle, timeout=2)
            assert run.status == "canceled"
            step = harness.store.list_steps(run_id=run.id)[1]
            assert step.status == "canceled"
            assert step.aborted_by == control.ref
            assert step.output is not None
            (part,) = parts_from_local(step.output)
            assert isinstance(part, ToolResultPart)
            assert part.tool_call_id == "call-1" and part.tool_name == tool_name
            if tool_name == "math__double":
                assert part.output == {"value": 6}
            elif tool_name == "_too__execute":
                assert part.output == {"executed": "agent$agic:target"}
            else:
                assert (
                    part.error == "Agent State refresh is unavailable in this executor"
                )
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
            record = await harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="stream",
                    primary=resolve_input_parts("start"),
                ),
                tracer=tracer,
            )

            assert record.status == "failed"
            assert record.error == ErrorRef(
                FieldRef.from_path(StepRef.from_local(record.id, (0,)), "error")
            )
            assert [
                (step.kind, step.status, step.error)
                for step in harness.store.list_steps(run_id=record.id)
            ] == [("model", "failed", ErrorMessage("stream disconnected"))]
            assert event_labels(tracer.events) == [
                f"run_begin:{record.id}",
                f"step_begin:{record.id}.0:model",
                f"part_begin:{record.id}.0:0:text",
                f"part_delta:{record.id}.0:0",
                f"part_end:{record.id}.0:0:text",
                f"step_end:{record.id}.0:model:failed",
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
            handle = harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="stream",
                    primary=resolve_input_parts("start"),
                ),
                tracer=tracer,
            )
            await asyncio.wait_for(gate.wait_until_entered(), timeout=1)
            control = handle.cancel(reason="cancel partial stream")
            record = await asyncio.wait_for(handle, timeout=2)

            assert record.status == "canceled"
            stored = harness.store.get_run_control(
                run_id=record.id,
                index=control.index,
            )
            assert stored is not None and stored.status == "applied"
            assert event_labels(tracer.events) == [
                f"run_begin:{record.id}",
                f"step_begin:{record.id}.0:model",
                f"part_begin:{record.id}.0:0:text",
                f"part_delta:{record.id}.0:0",
                f"part_end:{record.id}.0:0:text",
                f"step_end:{record.id}.0:model:canceled",
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
            record = await harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="calculate",
                    primary=resolve_input_parts("double three"),
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
        parameters={
            "type": "object",
            "properties": {"value": {"type": "integer"}},
        },
        error=RuntimeError("calculator unavailable"),
    )
    silent = RecordingTool(
        "math__silent",
        output={},
        error=RuntimeError(),
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
            name=silent.name,
            input={},
        ),
        ToolCall(
            tool_call_id="tool-3",
            call_id="call-3",
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
        tools={broken.name: broken, silent.name: silent},
    )
    tracer = RecordingRunTracer()

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            record = await harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="calculate",
                    primary=resolve_input_parts("calculate"),
                ),
                tracer=tracer,
            )

            assert record.status == "succeeded"
            steps = harness.store.list_steps(run_id=record.id)
            assert [(step.kind, step.status) for step in steps] == [
                ("model", "succeeded"),
                ("tool", "failed"),
                ("tool", "failed"),
                ("tool", "failed"),
                ("model", "succeeded"),
            ]
            assert [step.error for step in steps[1:4]] == [
                ErrorMessage("calculator unavailable"),
                ErrorMessage("RuntimeError"),
                ErrorMessage("unknown tool call: missing__tool"),
            ]
            assert [step.noted for step in steps[1:4]] == [
                ToolStepNoted(summary="Failed broken 3"),
                ToolStepNoted(summary="Failed silent"),
                ToolStepNoted(summary="Failed tool"),
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
                "tool-3",
            ]
            assert [result.error for result in results] == [
                "calculator unavailable",
                "RuntimeError",
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
            ModelCallResult(message=Message.assistant("still not a number")),
        ],
        tools={broken.name: broken},
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            record = await harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="calculate",
                    primary=resolve_input_parts("calculate"),
                )
            )

            assert record.status == "failed"
            assert isinstance(record.error, ErrorMessage)
            assert record.error.message.startswith("output is not valid Number")
            steps = harness.store.list_steps(run_id=record.id)
            assert [(step.kind, step.status) for step in steps] == [
                ("model", "succeeded"),
                ("tool", "failed"),
                ("model", "succeeded"),
                ("model", "succeeded"),
            ]
            assert steps[1].error == ErrorMessage("calculator unavailable")
            assert len(harness.adapter.invocations) == 3
            assert harness.adapter.pending_responses == 0

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
            record = await harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="loop",
                    primary=resolve_input_parts("continue"),
                    limits=RunLimits(agic_model_calls=8),
                ),
                tracer=tracer,
            )

            assert record.status == "failed"
            assert record.error == ErrorMessage("Agic model call limit exceeded: 8")
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


def test_agent_setup_limits_are_used_and_run_can_override_them(
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
            rejected = await harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="reply",
                    primary=resolve_input_parts("first"),
                )
            )
            accepted = await harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="reply",
                    primary=resolve_input_parts("second"),
                    limits=RunLimits(agic_model_calls=1),
                ),
            )

            assert rejected.status == "failed"
            assert rejected.error == ErrorMessage("Agic model call limit exceeded: 0")
            assert accepted.status == "succeeded"
            assert len(harness.adapter.invocations) == 1
            rejected_run_control = harness.store.get_run_control(
                run_id=rejected.id,
                index=0,
            )
            accepted_run_control = harness.store.get_run_control(
                run_id=accepted.id,
                index=0,
            )
            assert rejected_run_control is not None
            assert accepted_run_control is not None
            assert isinstance(rejected_run_control.payload, RunControlPayload)
            assert isinstance(accepted_run_control.payload, RunControlPayload)
            assert rejected_run_control.payload.limits == RunLimits(agic_model_calls=0)
            assert accepted_run_control.payload.limits == RunLimits(agic_model_calls=1)

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
            record = await harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="loop",
                    primary=resolve_input_parts("continue"),
                    limits=RunLimits(agic_tool_calls=1),
                ),
            )

            assert record.status == "failed"
            assert record.error == ErrorMessage("Agic tool call limit exceeded: 1")
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
            record = await harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="reply",
                    primary=resolve_input_parts("hello"),
                    limits=RunLimits(tokens=10),
                ),
            )

            assert record.status == "failed"
            assert record.error == ErrorMessage("Run token limit exceeded: 11 > 10")
            assert [
                (step.kind, step.status)
                for step in harness.store.list_steps(run_id=record.id)
            ] == [("model", "succeeded")]
            model_step = harness.store.list_steps(run_id=record.id)[0]
            assert isinstance(model_step.noted, ModelStepNoted)
            assert model_step.noted.tokens == ModelTokenCount(input=6, output=5)
            assert model_step.noted.accounting is not None
            assert model_step.noted.accounting.input_tokens == 6
            assert model_step.noted.accounting.output_tokens == 5

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
        models=ModelCollection(
            tuple(
                replace(
                    entry,
                    info=replace(
                        entry.info,
                        input_price=0.01,
                        output_price=0.02,
                    ),
                )
                for entry in harness.setup.models.entries
            )
        ),
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            record = await harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="reply",
                    primary=resolve_input_parts("hello"),
                    limits=RunLimits(cost=cost_limit),
                ),
            )

            assert record.status == status
            assert record.error == (ErrorMessage(error) if error is not None else None)
            model_step = harness.store.list_steps(run_id=record.id)[0]
            assert isinstance(model_step.noted, ModelStepNoted)
            assert model_step.noted.tokens == ModelTokenCount(input=1, output=1)
            assert model_step.noted.price == ModelTokenPrice(
                input="0.01", output="0.02"
            )
            assert model_step.noted.cost == "0.03"
            assert model_step.noted.accounting is not None

    asyncio.run(scenario())


def test_run_cost_limit_allows_unknown_pricing_as_partial_coverage(
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
            record = await harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="reply",
                    primary=resolve_input_parts("hello"),
                    limits=RunLimits(cost=Decimal("1")),
                ),
            )

            assert record.status == "succeeded"
            assert record.error is None
            assert len(harness.adapter.invocations) == 1

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
            record = await harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="reply",
                    primary=resolve_input_parts("hello"),
                    limits=RunLimits(tokens=100),
                ),
            )

            assert record.status == "failed"
            assert record.error == ErrorMessage(
                "Model usage is required by run token or cost limits: test/scripted"
            )
            assert len(harness.adapter.invocations) == 1
            model_step = harness.store.list_steps(run_id=record.id)[0]
            assert model_step.noted == ModelStepNoted()

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
            record = await harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="reply",
                    primary=resolve_input_parts("hello"),
                    limits=RunLimits(time=0),
                ),
            )

            assert gate.entered
            assert record.status == "failed"
            assert record.error == ErrorMessage("Run time limit exceeded: 0s")
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
                harness.executor.run(
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
            record = await harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="fail",
                    primary=resolve_input_parts("hello"),
                ),
                tracer=tracer,
            )

            assert record.status == "failed"
            assert record.error == ErrorRef(
                FieldRef.from_path(StepRef.from_local(record.id, (0,)), "error")
            )
            steps = harness.store.list_steps(run_id=record.id)
            assert [(step.kind, step.status) for step in steps] == [("model", "failed")]
            assert steps[0].error == ErrorMessage("provider unavailable")
            assert harness.store.run_output(run_id=record.id) == ()
            assert_run_event_integrity(tracer.events)
            assert event_labels(tracer.events) == [
                f"run_begin:{record.id}",
                f"step_begin:{record.id}.0:model",
                f"step_end:{record.id}.0:model:failed",
                f"run_end:{record.id}:failed",
            ]

    asyncio.run(scenario())
