"""Reasoning uses ordinary Step outputs, message history, and indexed events."""

from __future__ import annotations

import asyncio
from pathlib import Path
import json

import pytest

from tests.support.execution_assertions import (
    assert_replayed,
    assert_run_event_integrity,
)
from tests.support.execution_harness import (
    ExecutionHarness,
    RecordingRunTracer,
    ScriptedModelTurn,
)
from toolang.base.types.message import (
    Message,
    ReasoningDelta,
    ReasoningPart,
    TextPart,
    TextDelta,
    ToolCallDelta,
    ToolCallPart,
)
from toolang.base.types.run import (
    ModelCallResult,
    ModelPartDelta,
    ModelPartEnd,
    ModelPartStart,
)
from toolang.execution.events import (
    PartEnd,
    PartDelta,
    run_event_from_data,
    run_event_to_data,
)
from toolang.execution.types import ThreadPrefix
from toolang.lang.types import Array


SOURCE = """agic chat(_: Part[]) -> Part[]:
  context = none
  instruct = none
  user: {{_}}
"""


def signed_reasoning(text="reasoning α"):
    return ReasoningPart(
        text,
        signature="opaque-reasoning",
        provider="openai",
        provider_metadata={
            "adapter": "responses",
            "model": "test-model",
            "item_id": "rs",
            "output_index": 0,
            "summary_count": 1,
            "content_count": 0,
            "field": "summary",
            "index": 0,
            "content_type": "summary_text",
        },
    )


def test_reasoning_survives_reopening_new_runs_and_fork_without_continuation(
    tmp_path: Path,
):
    parts = (
        signed_reasoning(),
        TextPart(
            "answer",
            signature="opaque-answer",
            provider="google",
            provider_metadata={"adapter": "generate_content", "model": "gemini"},
        ),
        TextPart(
            "",
            signature="opaque-empty",
            provider="google",
            provider_metadata={"adapter": "generate_content", "model": "gemini"},
        ),
    )
    first = ExecutionHarness.create(
        tmp_path,
        source=SOURCE,
        responses=[ModelCallResult(message=Message("assistant", parts))],
    )
    tracer = RecordingRunTracer()

    async def scenario():
        async with first:
            thread = first.threads.create(prefix=ThreadPrefix.TERM)
            run = await first.executor.run(
                first.run_spec(
                    thread=thread, runnable="chat", primary=(TextPart("start"),)
                ),
                tracer=tracer,
            )
            assert run.status == "succeeded", run.error
            assert first.store.run_output(run_id=run.id) == parts
        reopened = ExecutionHarness.create(
            tmp_path,
            source=SOURCE,
            responses=[
                ModelCallResult(message=Message.assistant("next")),
                ModelCallResult(message=Message.assistant("branch")),
            ],
        )
        async with reopened:
            assert reopened.store.run_output(run_id=run.id) == parts
            fork = reopened.threads.fork(thread_id=thread, run_id=run.id)
            for destination in (thread, fork):
                followup = await reopened.executor.run(
                    reopened.run_spec(
                        thread=destination,
                        runnable="chat",
                        primary=(TextPart("continue"),),
                    ),
                    tracer=tracer,
                )
                assert followup.status == "succeeded", followup.error
                call = reopened.adapter.invocations[-1].call
                assert call.continuation is None
                assert Message("assistant", parts) in call.messages
        assert_replayed(first.store.db_path, tracer.events)

    asyncio.run(scenario())


def test_interleaved_reasoning_events_match_durable_output(tmp_path: Path):
    reason = signed_reasoning("prefix suffix")
    parts = (reason, TextPart("answer"), ReasoningPart("another"))
    updates = (
        ModelPartStart(0, "reasoning"),
        ModelPartDelta(0, ReasoningDelta("prefix")),
        ModelPartStart(1, "text"),
        ModelPartDelta(1, TextDelta("answer")),
        ModelPartEnd(0, reason),
        ModelPartEnd(1, TextPart("answer")),
        ModelPartEnd(2, parts[2]),
    )
    harness = ExecutionHarness.create(
        tmp_path,
        source=SOURCE,
        responses=[
            ScriptedModelTurn(
                ModelCallResult(message=Message("assistant", parts)), updates=updates
            )
        ],
        streaming=True,
    )
    tracer = RecordingRunTracer()

    async def scenario():
        async with harness:
            run = await harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="chat",
                    primary=(TextPart("start"),),
                ),
                tracer=tracer,
            )
            assert run.status == "succeeded", run.error
            assert harness.store.run_output(run_id=run.id) == parts
            assert (
                tuple(
                    event.data for event in tracer.events if isinstance(event, PartEnd)
                )
                == parts
            )
            assert (
                "".join(
                    event.delta.text
                    for event in tracer.events
                    if isinstance(event, PartDelta) and event.part == 0
                )
                == reason.text
            )
            for event in tracer.events:
                assert (
                    run_event_from_data(
                        json.loads(json.dumps(run_event_to_data(event)))
                    )
                    == event
                )
            assert_run_event_integrity(tracer.events)
        assert_replayed(harness.store.db_path, tracer.events)

    asyncio.run(scenario())


def test_failed_stream_persists_reasoning_prefix_without_native_state(tmp_path: Path):
    completed = signed_reasoning("complete")
    harness = ExecutionHarness.create(
        tmp_path,
        source=SOURCE,
        responses=[
            ScriptedModelTurn(
                ModelCallResult(),
                updates=(
                    ModelPartDelta(0, ToolCallDelta('{"unfinished":', "incomplete")),
                    ModelPartEnd(1, completed),
                    ModelPartStart(2, "reasoning"),
                    ModelPartDelta(2, ReasoningDelta("unfinished α")),
                ),
                error=RuntimeError("disconnected"),
            )
        ],
        streaming=True,
    )
    tracer = RecordingRunTracer()

    async def scenario():
        async with harness:
            run = await harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="chat",
                    primary=(TextPart("start"),),
                ),
                tracer=tracer,
            )
            assert run.status == "failed"
            step = harness.store.list_steps(run_id=run.id)[0]
            assert step.output is not None
            assert isinstance(step.output.local.value, Array)
            assert tuple(step.output.local.value) == (
                ToolCallPart("incomplete", "", ""),
                completed,
                ReasoningPart("unfinished α"),
            )
            assert tuple(
                event.data for event in tracer.events if isinstance(event, PartEnd)
            ) == tuple(step.output.local.value)
            assert [
                event.part for event in tracer.events if isinstance(event, PartEnd)
            ] == [0, 1, 2]
            assert_run_event_integrity(tracer.events)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "parts",
    [
        (signed_reasoning(),),
        (
            TextPart(
                "",
                signature="opaque",
                provider="google",
                provider_metadata={"adapter": "generate_content", "model": "gemini"},
            ),
        ),
    ],
)
def test_reasoning_and_empty_signed_text_cannot_satisfy_visible_output(
    tmp_path: Path, parts
):
    harness = ExecutionHarness.create(
        tmp_path,
        source=SOURCE,
        responses=[ModelCallResult(message=Message("assistant", parts))],
    )

    async def scenario():
        async with harness:
            run = await harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="chat",
                    primary=(TextPart("start"),),
                )
            )
            assert run.status == "failed"
            assert "no visible output" in str(run.error)

    asyncio.run(scenario())
