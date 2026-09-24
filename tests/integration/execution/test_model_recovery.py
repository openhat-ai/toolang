"""Recover unusable model turns without executing or replaying their tools."""

import asyncio
from pathlib import Path

import pytest

from tests.support.execution_assertions import (
    without_route_snapshots,
    assert_replayed,
    assert_run_event_integrity,
)
from tests.support.execution_harness import (
    AsyncGate,
    ExecutionHarness,
    RecordingRunTracer,
    RecordingTool,
    ScriptedModelTurn,
)
from toolang.base.errors import ModelResponseError
from toolang.base.types.message import Message, TextDelta, TextPart, ToolCallPart
from toolang.base.types.policy import RunLimits
from toolang.base.types.run import (
    ModelCallResult,
    ModelPartDelta,
    ModelPartEnd,
    ModelUsage,
    ToolCall,
)
from toolang.execution.executor.runs import agic
from toolang.execution.types import ModelStepNoted, ThreadPrefix
from toolang.lang.types import Array

SOURCE = """
agic chat() -> Text:
  recall = none
  context = none
  user: Start.
"""


def test_recovery_retains_accounting_and_replay_without_executing_failed_batch(
    tmp_path: Path,
) -> None:
    tool = RecordingTool("lookup__item", output={"value": 1})
    harness = ExecutionHarness.create(
        tmp_path,
        source=SOURCE,
        tools={tool.name: tool},
        responses=[
            ModelCallResult(
                tool_calls=(ToolCall("before", "before", tool.name, {}),),
                continuation={"opaque": "keep"},
            ),
            ScriptedModelTurn(
                result=ModelCallResult(),
                updates=(
                    ModelPartDelta(0, TextDelta("unfinished")),
                    ModelPartEnd(
                        1,
                        ToolCallPart(
                            tool_call_id="discard",
                            tool_name=tool.name,
                            tool_family="lookup",
                            input={},
                        ),
                    ),
                ),
                error=ModelResponseError(
                    "bad sibling", kind="invalid_json", usage=ModelUsage(10, 20)
                ),
            ),
            ModelCallResult(
                message=Message.assistant("done"), usage=ModelUsage(30, 40)
            ),
        ],
    )
    tracer = RecordingRunTracer()

    async def scenario() -> None:
        async with harness:
            run = await harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="chat",
                ),
                tracer=tracer,
            )
            assert run.status == "succeeded", run.error
            assert len(tool.calls) == 1
            steps = harness.store.list_steps(run_id=run.id)
            assert [step.status for step in steps] == [
                "succeeded",
                "succeeded",
                "failed",
                "succeeded",
            ]
            failed = steps[2]
            assert isinstance(failed.noted, ModelStepNoted)
            assert failed.noted.accounting is not None
            assert failed.noted.accounting.input_tokens == 10
            assert failed.noted.accounting.output_tokens == 20
            retry = harness.adapter.invocations[-1].call
            assert retry.continuation == {"opaque": "keep"}
            assert not any(
                isinstance(part, ToolCallPart) and part.tool_call_id == "discard"
                for message in retry.messages
                for part in message.parts
            )
            assert Message.assistant("unfinished") not in retry.messages
            assert "invalid_json" in str(retry.messages)
            assert_run_event_integrity(tracer.events)

    asyncio.run(scenario())
    assert_replayed(harness.store.db_path, tracer.events)


@pytest.mark.parametrize(
    ("streamed", "received", "expected"),
    [
        ("", "received tail", "received tail"),
        ("received ", "received tail", "received tail"),
        ("received tail", "received ", "received tail"),
        ("received tail", "unrelated", "received tail"),
    ],
)
def test_failed_step_retains_available_text(
    tmp_path: Path, streamed, received, expected
):
    harness = ExecutionHarness.create(
        tmp_path,
        source=SOURCE,
        responses=[
            ScriptedModelTurn(
                result=ModelCallResult(),
                updates=(ModelPartDelta(0, TextDelta(streamed)),) if streamed else (),
                error=ModelResponseError(
                    "truncated", kind="output_limit", partial_text=received
                ),
            ),
            ModelCallResult(message=Message.assistant("done")),
        ],
    )
    tracer = RecordingRunTracer()

    async def scenario() -> None:
        async with harness:
            run = await harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="chat",
                ),
                tracer=tracer,
            )
            assert run.status == "succeeded", run.error
            failed, recovered = harness.store.list_steps(run_id=run.id)
            assert failed.status == "failed"
            assert failed.output is not None
            assert isinstance(failed.output.local.value, Array)
            assert tuple(failed.output.local.value) == (TextPart(expected),)
            assert recovered.status == "succeeded"
            assert Message.assistant(expected) not in (
                harness.adapter.invocations[-1].call.messages
            )
            assert_run_event_integrity(tracer.events)

    asyncio.run(scenario())
    assert_replayed(harness.store.db_path, tracer.events)


@pytest.mark.parametrize(
    ("kind", "limit", "attempts"),
    [
        ("invalid_json", None, 3),
        ("output_limit", 1, 1),
        ("provider_rejection", None, 1),
    ],
)
def test_recovery_is_bounded(tmp_path: Path, kind, limit, attempts: int) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source=SOURCE,
        responses=[
            ModelResponseError("cannot use response", kind=kind) for _ in range(4)
        ],
    )
    tracer = RecordingRunTracer()

    async def scenario() -> None:
        async with harness:
            run = await harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="chat",
                    limits=RunLimits(agic_model_calls=limit),
                ),
                tracer=tracer,
            )
            assert run.status == "failed"
            assert len(harness.adapter.invocations) == attempts
            assert all(
                step.status == "failed"
                for step in harness.store.list_steps(run_id=run.id)
            )
            assert_run_event_integrity(tracer.events)

    asyncio.run(scenario())
    assert_replayed(harness.store.db_path, tracer.events)


@pytest.mark.parametrize("action", ["retry", "cancel", "steer"])
@pytest.mark.parametrize("retry_after", [3, 60])
def test_network_backoff_is_cancelable_and_accepts_steering(
    tmp_path: Path, monkeypatch, action: str, retry_after: int
) -> None:
    gate = AsyncGate()
    resumed = AsyncGate()
    delays = []

    async def wait(delay):
        delays.append(delay)
        await (gate if len(delays) == 1 else resumed).wait()

    monkeypatch.setattr(agic, "sleep", wait)
    harness = ExecutionHarness.create(
        tmp_path,
        source=SOURCE,
        responses=[
            ModelResponseError(
                "disconnected", kind="transport_error", retry_after=retry_after
            ),
            ModelCallResult(message=Message.assistant("done")),
        ],
    )
    tracer = RecordingRunTracer()

    async def scenario() -> None:
        async with harness:
            handle = harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="chat",
                ),
                tracer=tracer,
            )
            await asyncio.wait_for(gate.wait_until_entered(), 2)
            if action == "cancel":
                handle.cancel(reason="stop")
            elif action == "steer":
                harness.executor.steer(
                    run_id=handle.run_id,
                    message=Message.user("change"),
                    timing="immediate",
                )
                await asyncio.wait_for(resumed.wait_until_entered(), 2)
                assert len(harness.adapter.invocations) == 1
                resumed.release()
            else:
                gate.release()
            run = await asyncio.wait_for(handle, 2)
            assert run.status == ("canceled" if action == "cancel" else "succeeded"), (
                run.error
            )
            assert len(harness.adapter.invocations) == (1 if action == "cancel" else 2)
            if action == "retry":
                assert without_route_snapshots(
                    harness.adapter.invocations[0].call.messages
                ) == without_route_snapshots(
                    harness.adapter.invocations[1].call.messages
                )
            if action == "steer":
                assert "change" in str(harness.adapter.invocations[-1].call.messages)
            assert len(delays) == (2 if action == "steer" else 1)
            assert all(0 < delay <= retry_after for delay in delays)
            assert_run_event_integrity(tracer.events)

    asyncio.run(scenario())
    assert_replayed(harness.store.db_path, tracer.events)


@pytest.mark.parametrize("recoveries_before_repair", [1, 2])
def test_recovery_budget_and_schema_survive_typed_output_repair(
    tmp_path: Path, recoveries_before_repair: int
) -> None:
    tool = RecordingTool("lookup__item", output={})
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic chat() -> Boolean:
  recall = none
  context = none
  tools = lookup/*
  user: Decide.
""",
        tools={tool.name: tool},
        responses=[
            *(
                ModelResponseError("bad arguments", kind="invalid_json")
                for _ in range(recoveries_before_repair)
            ),
            ModelCallResult(message=Message.assistant("not a boolean")),
            ModelResponseError("truncated", kind="output_limit"),
            ModelCallResult(message=Message.assistant("true")),
        ],
    )
    tracer = RecordingRunTracer()

    async def scenario() -> None:
        async with harness:
            run = await harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="chat",
                ),
                tracer=tracer,
            )
            assert run.status == (
                "succeeded" if recoveries_before_repair == 1 else "failed"
            ), run.error
            calls = [item.call for item in harness.adapter.invocations]
            assert calls[0].tools
            assert all(call.output_schema == calls[0].output_schema for call in calls)
            assert all(
                call.tools == () for call in calls[recoveries_before_repair + 1 :]
            )
            assert len(tool.calls) == 0
            assert len(calls) == 4
            assert_run_event_integrity(tracer.events)

    asyncio.run(scenario())
    assert_replayed(harness.store.db_path, tracer.events)


@pytest.mark.parametrize("known_usage", [True, False])
def test_failed_attempt_respects_token_accounting_limits(
    tmp_path: Path, known_usage: bool
) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source=SOURCE,
        responses=[
            ModelResponseError(
                "truncated",
                kind="output_limit",
                usage=ModelUsage(10, 20) if known_usage else None,
            ),
            ModelCallResult(message=Message.assistant("unexpected")),
        ],
    )
    tracer = RecordingRunTracer()

    async def scenario() -> None:
        async with harness:
            run = await harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="chat",
                    limits=RunLimits(tokens=25),
                ),
                tracer=tracer,
            )
            assert run.status == "failed"
            assert len(harness.adapter.invocations) == 1
            assert (
                "token limit" in str(run.error)
                if known_usage
                else "usage is required" in str(run.error)
            )
            (step,) = harness.store.list_steps(run_id=run.id)
            assert step.status == "failed"
            assert isinstance(step.noted, ModelStepNoted)
            assert (step.noted.accounting is not None) == known_usage
            assert_run_event_integrity(tracer.events)

    asyncio.run(scenario())
    assert_replayed(harness.store.db_path, tracer.events)


@pytest.mark.parametrize("steer", [False, True])
def test_run_time_limit_stops_long_backoff(
    tmp_path: Path, monkeypatch, steer: bool
) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source=SOURCE,
        responses=[
            ModelResponseError("rate limited", kind="transport_error", retry_after=60),
            ModelCallResult(message=Message.assistant("unexpected")),
        ],
    )
    tracer = RecordingRunTracer()

    gate = AsyncGate()

    async def wait(_delay):
        await gate.wait()

    monkeypatch.setattr(agic, "sleep", wait)

    async def scenario() -> None:
        async with harness:
            handle = harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="chat",
                    limits=RunLimits(time=1),
                ),
                tracer=tracer,
            )
            task = asyncio.ensure_future(handle)
            try:
                await asyncio.wait_for(gate.wait_until_entered(), 2)
                if steer:
                    harness.executor.steer(
                        run_id=handle.run_id,
                        message=Message.user("change"),
                        timing="immediate",
                    )
                run = await asyncio.wait_for(asyncio.shield(task), 3)
                assert run.status == "failed"
                assert "Run time limit exceeded" in str(run.error)
                assert len(harness.adapter.invocations) == 1
                assert_run_event_integrity(tracer.events)
            finally:
                if not task.done():
                    handle.cancel(reason="test cleanup")
                    await task

    asyncio.run(scenario())
