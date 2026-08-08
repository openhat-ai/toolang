"""Run-event order, completeness, and non-redundancy scenarios."""

from __future__ import annotations

import asyncio
from pathlib import Path

from tests.support.execution_assertions import (
    assert_run_event_integrity,
    event_labels,
)
from tests.support.execution_harness import (
    ExecutionHarness,
    RecordingRunTracer,
    RecordingTool,
)
from toolang.base.types.message import Message
from toolang.base.types.run import ModelCallResult, ToolCall
from toolang.execution.events import RunBegin, RunEnd, StepBegin, StepEnd
from toolang.execution.types import StepPath, ThreadPrefix
from toolang.lang.input import perceive_input


def test_tool_loop_events_are_complete_ordered_and_non_redundant(
    tmp_path: Path,
) -> None:
    tool = RecordingTool("math__double", output={"value": 6})
    call = ToolCall(
        tool_call_id="tool-1",
        call_id="call-1",
        name=tool.name,
        input={"value": 3},
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
            ModelCallResult(tool_calls=(call,)),
            ModelCallResult(message=Message.assistant("six")),
        ],
        tools={tool.name: tool},
    )
    tracer = RecordingRunTracer()

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            run = await harness.executor.start(
                harness.run_spec(
                    thread=thread,
                    runnable="calculate",
                    primary=perceive_input("double three"),
                ),
                tracer=tracer,
            )

            assert_run_event_integrity(tracer.events)
            assert event_labels(tracer.events) == [
                f"run_begin:{run.id}",
                f"step_begin:{run.id}/0:model",
                f"part_begin:{run.id}/0:0:tool_call",
                f"part_end:{run.id}/0:0:tool_call",
                f"step_end:{run.id}/0:model:finished",
                f"step_begin:{run.id}/1:tool",
                f"part_begin:{run.id}/1:0:tool_result",
                f"part_end:{run.id}/1:0:tool_result",
                f"step_end:{run.id}/1:tool:finished",
                f"step_begin:{run.id}/2:model",
                f"part_begin:{run.id}/2:0:text",
                f"part_end:{run.id}/2:0:text",
                f"step_end:{run.id}/2:model:finished",
                f"run_end:{run.id}:finished",
            ]

    asyncio.run(scenario())


def test_nested_run_events_are_strictly_inside_the_parent_run_step(
    tmp_path: Path,
) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic echo(_: Text) -> Text:
  recall = none
  context: none
  instruct: none
  user: {{_}}

flow relay(_: Text) -> Text:
  run echo
""",
        responses=[ModelCallResult(message=Message.assistant("relayed"))],
    )
    tracer = RecordingRunTracer()

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            root = await harness.executor.start(
                harness.run_spec(
                    thread=thread,
                    runnable="relay",
                    primary=perceive_input("hello"),
                ),
                tracer=tracer,
            )
            child = next(
                run
                for run in harness.store.list_runs(
                    thread_id=thread,
                    limit=None,
                )
                if run.parent is not None
            )

            assert_run_event_integrity(tracer.events)
            assert event_labels(tracer.events) == [
                f"run_begin:{root.id}",
                f"step_begin:{root.id}/0:run",
                f"run_begin:{child.id}",
                f"step_begin:{child.id}/0:model",
                f"part_begin:{child.id}/0:0:text",
                f"part_end:{child.id}/0:0:text",
                f"step_end:{child.id}/0:model:finished",
                f"run_end:{child.id}:finished",
                f"step_end:{root.id}/0:run:finished",
                f"run_end:{root.id}:finished",
            ]

    asyncio.run(scenario())


def test_parallel_events_are_balanced_without_requiring_sibling_order(
    tmp_path: Path,
) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic worker(_: Text) -> Text:
  recall = none
  context: none
  instruct: none
  user: {{_}}

flow parallel(_: Text) -> Text[]:
  ## Run workers in parallel.
  storm 3 worker par 3
""",
        responses=[
            ModelCallResult(message=Message.assistant(f"item {index}"))
            for index in range(3)
        ],
    )
    tracer = RecordingRunTracer()

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            root = await harness.executor.start(
                harness.run_spec(
                    thread=thread,
                    runnable="parallel",
                    primary=perceive_input("work"),
                ),
                tracer=tracer,
            )
            children = [
                run
                for run in harness.store.list_runs(
                    thread_id=thread,
                    limit=None,
                )
                if run.parent is not None
            ]

            assert_run_event_integrity(tracer.events)
            root_step = StepPath.parse(f"{root.id}/0")
            parent_begin = tracer.events.index(
                next(
                    event
                    for event in tracer.events
                    if isinstance(event, StepBegin)
                    and event.step == root_step
                )
            )
            parent_end = tracer.events.index(
                next(
                    event
                    for event in tracer.events
                    if isinstance(event, StepEnd)
                    and event.step == root_step
                )
            )
            parent_event = tracer.events[parent_begin]
            assert isinstance(parent_event, StepBegin)
            assert parent_event.given["doc"] == "Run workers in parallel."
            for child in children:
                begin_event = next(
                    event
                    for event in tracer.events
                    if isinstance(event, RunBegin) and event.run == child.id
                )
                begin = [tracer.events.index(begin_event)]
                end = [
                    index
                    for index, event in enumerate(tracer.events)
                    if isinstance(event, RunEnd) and event.run == child.id
                ]
                assert len(begin) == len(end) == 1
                assert begin_event.parent == child.parent == root_step
                assert parent_begin < begin[0] < end[0] < parent_end
            assert sum(
                isinstance(event, RunBegin) for event in tracer.events
            ) == 4
            assert sum(
                isinstance(event, RunEnd) for event in tracer.events
            ) == 4

    asyncio.run(scenario())
