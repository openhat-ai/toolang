"""Spawn captures follow ordinary inline inputs and nearest iteration scopes."""

import asyncio
from pathlib import Path

import pytest

from tests.support.execution_assertions import assert_replayed
from tests.support.execution_harness import ExecutionHarness, RecordingRunTracer
from toolang.base.types.message import Message, TextPart, message_text
from toolang.base.types.run import ModelCallResult
from toolang.execution.executor.common import Local
from toolang.execution.executor.iteration import (
    IterationFrame,
    IterationScope,
    iteration_scope,
    snapshot,
)
from toolang.execution.types import RunHandle, ThreadPrefix


@pytest.mark.parametrize("operation", ["run", "spawn", "exec", "let generate 1"])
@pytest.mark.parametrize(
    "template",
    [
        "{{#_}}Job {{job.id}}, label {{label}}{{/_}}",
        "{{#_}}{{#job}}Job {{id}}{{/job}}, label {{label}}{{/_}}",
    ],
)
def test_inline_sections_capture_handle_fields(
    tmp_path: Path, operation: str, template: str
) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source=f"""
flow parent(_: Text) -> Text:
  let label = outer-label
  let job = spawn worker
  {operation}: {template}
flow worker(_: Text) -> Text:
  let unused = Work
""",
        responses=[ModelCallResult(message=Message.assistant("done"))],
    )
    tracer = RecordingRunTracer()
    harness.executor.root_tracer = lambda _thread: tracer

    async def scenario():
        async with harness:
            parent = await harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="flow:parent",
                    primary=(TextPart("input"),),
                ),
                tracer=tracer,
            )
            while harness.executor._tasks:
                await asyncio.gather(*tuple(harness.executor._tasks))
                await asyncio.sleep(0)
            assert parent.status == "succeeded", (
                harness.store.resolve_error(parent.error) if parent.error else None
            )
            launch = harness.store.list_steps(run_id=parent.id)[1]
            assert launch.output is not None
            handle = launch.output.value
            assert isinstance(handle, RunHandle)
            (invocation,) = harness.adapter.invocations
            assert any(
                f"Job {handle.id}, label outer-label" in message_text(message.parts)
                for message in invocation.call.messages
                if message.role == "user"
            )
        assert_replayed(harness.store.db_path, tracer.events)

    asyncio.run(scenario())


@pytest.mark.parametrize("reader", ["flow", "agic", "spawn"])
def test_spawned_root_inner_scope_hides_captured_outer_frames(
    tmp_path: Path, reader: str
) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source=f"""
flow parent(_: Text) -> Text:
  repeat 3 times:
    spawn child
flow child(_: Text) -> Text:
  repeat 1 time windowing 1:
    run bridge
flow bridge(_: Text) -> Text:
  {"spawn" if reader == "spawn" else "run"} reader
{"agic" if reader == "agic" else "flow"} reader(_: Text) -> Text:
  {"user:" if reader == "agic" else "let observed ="} {{{{_2._}}}}
""",
        responses=[],
    )

    async def scenario():
        async with harness:
            parent = await harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="flow:parent",
                    primary=(TextPart("outer-input"),),
                )
            )
            while harness.executor._tasks:
                await asyncio.gather(*tuple(harness.executor._tasks))
                await asyncio.sleep(0)
            assert parent.status == "succeeded"
            roots = [
                run
                for run in harness.store.list_runs()
                if run.parent is None and run.id != parent.id
            ]
            assert len(roots) == 3
            for root in roots:
                assert root.status == "failed" and root.error is not None
                assert (
                    "unavailable iteration input: _2"
                    if reader == "spawn"
                    else "outside the active window: _2"
                ) in harness.store.resolve_error(root.error)
            assert not harness.adapter.invocations

    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["run", "spawn"])
def test_spawned_root_restores_captured_history_after_local_loop(
    tmp_path: Path, operation: str
) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source=f"""
flow parent(_: Text) -> Text:
  spawn child
flow child(_: Text) -> Text:
  repeat 1 time windowing 1:
    let local_frame = {{{{^_1}}}}warming{{{{/_1}}}}
  {operation} reader
agic reader(_: Text) -> Text:
  user: Earlier {{{{_2._}}}}
""",
        responses=[ModelCallResult(message=Message.assistant("done"))],
    )
    tracer = RecordingRunTracer()
    harness.executor.root_tracer = lambda _thread: tracer

    async def scenario():
        async with harness:
            # Start with two completed caller frames; independent roots must
            # read their captured data after the task-local scope is gone.
            frames = tuple(
                IterationFrame(snapshot({}), snapshot({"_": Local(value)}))
                for value in ("recent", "older")
            )
            with iteration_scope(IterationScope(2, frames)):
                parent = await harness.executor.run(
                    harness.run_spec(
                        thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                        runnable="flow:parent",
                        primary=(TextPart("input"),),
                    ),
                    tracer=tracer,
                )
            while harness.executor._tasks:
                await asyncio.gather(*tuple(harness.executor._tasks))
                await asyncio.sleep(0)
            assert parent.status == "succeeded"
            assert all(run.status == "succeeded" for run in harness.store.list_runs())
            (invocation,) = harness.adapter.invocations
            assert any(
                "Earlier older" in message_text(message.parts)
                for message in invocation.call.messages
                if message.role == "user"
            )
        assert_replayed(harness.store.db_path, tracer.events)

    asyncio.run(scenario())
