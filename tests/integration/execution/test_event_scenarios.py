"""Run-event order, completeness, and non-redundancy scenarios."""

from __future__ import annotations

from toolang.execution.events import FlowStepInfo


from toolang.execution.events import ToolStepInfo, StepSummary

from toolang.execution.types import StepRef

import asyncio
from pathlib import Path
import pytest
from tests.support.execution_fixtures import project_run_start, project_step

from tests.support.execution_assertions import (
    assert_run_event_integrity,
    event_labels,
)
from tests.support.execution_harness import (
    ExecutionHarness,
    RecordingRunTracer,
    RecordingTool,
)
from toolang.base.types.message import Message, ReasoningPart, TextPart
from toolang.base.types.run import ModelCallResult, ToolCall
from toolang.execution.events import (
    RunBegin,
    RunEnd,
    StepBegin,
    StepEnd,
    run_event_from_data,
    run_event_to_data,
)
from toolang.execution.subscriptions import RecordSnapshot
from toolang.execution.types import (
    ThreadPrefix,
    ToolStepGiven,
    ToolStepNoted,
)
from toolang.lang.input import resolve_input_parts


@pytest.mark.parametrize("with_child", [False, True])
def test_reconcile_orphaned_tree_publishes_child_ends_first(tmp_path, with_child):
    harness = ExecutionHarness.create(
        tmp_path, source="flow root():\n  let value = unused\n", responses=[]
    )

    async def scenario():
        async with harness:
            root = project_run_start(
                harness.store,
                run_id="run_root",
                thread_id="chat_test",
                origin="chat",
                input=Message.user("work"),
                runnable_kind="flow",
            )

            def step(run, parent, kind):
                return project_step(
                    harness.store,
                    run_id=run,
                    parent=parent,
                    index=0,
                    kind=kind,
                    status="running",
                    input=(),
                    output=None,
                    started_at=root.started_at,
                    finished_at=None,
                )

            outer = step(root.id, None, "loop")
            source = step(root.id, outer.ref, "run" if with_child else "value")
            if with_child:
                child = project_run_start(
                    harness.store,
                    run_id="run_child",
                    thread_id="chat_test",
                    origin="chat",
                    input=Message.user("work"),
                    parent=source.ref,
                    runnable_kind="flow",
                )
                nested = step(child.id, None, "loop")
                step(child.id, nested.ref, "value")
            runs = harness.store.list_run_tree(root_run_id=root.id)
            snapshot = RecordSnapshot(
                harness.store,
                runs,
                [s for run in runs for s in harness.store.list_steps(run_id=run.id)],
            )
            prefix = [
                run_event_from_data(frame.data) for frame in snapshot.structural()
            ]

            result = await harness.executor.reconcile_talk(root.id)

            assert result.status == "failed"
            ends: list[StepEnd | RunEnd] = []
            with harness.executor.stream.boundary() as (_tail, _floor, frames):
                for frame in frames:
                    if frame.root_run_id == root.id:
                        assert isinstance(frame.event, StepEnd | RunEnd)
                        ends.append(frame.event)
            assert_run_event_integrity([*prefix, *ends])
            expected = (
                [
                    "step_end:run_child.0.0:value:failed",
                    "step_end:run_child.0:loop:failed",
                    "run_end:run_child:failed",
                ]
                if with_child
                else []
            )
            assert event_labels(ends) == [
                *expected,
                f"step_end:run_root.0.0:{'run' if with_child else 'value'}:failed",
                "step_end:run_root.0:loop:failed",
                "run_end:run_root:failed",
            ]
            assert not harness.adapter.invocations

    asyncio.run(scenario())


def test_tool_loop_events_include_durable_generic_summaries(
    tmp_path: Path,
) -> None:
    tool = RecordingTool(
        "math__double",
        output={"value": 6},
        parameters={
            "type": "object",
            "properties": {"value": {"type": "integer"}},
        },
    )
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
  context = none
  instruct = none
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
            thread = harness.threads.create(prefix=ThreadPrefix.CHAT)
            run = await harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="calculate",
                    primary=resolve_input_parts("double three"),
                ),
                tracer=tracer,
            )

            assert_run_event_integrity(tracer.events)
            assert event_labels(tracer.events) == [
                f"run_begin:{run.id}",
                f"step_begin:{run.id}.0:model",
                f"part_begin:{run.id}.0:0:tool_call",
                f"part_end:{run.id}.0:0:tool_call",
                f"step_end:{run.id}.0:model:succeeded",
                f"step_begin:{run.id}.1:tool",
                f"part_begin:{run.id}.1:0:tool_result",
                f"part_end:{run.id}.1:0:tool_result",
                f"step_end:{run.id}.1:tool:succeeded",
                f"step_begin:{run.id}.2:model",
                f"part_begin:{run.id}.2:0:text",
                f"part_end:{run.id}.2:0:text",
                f"step_end:{run.id}.2:model:succeeded",
                f"run_end:{run.id}:succeeded",
            ]
            tool_begin = next(
                event
                for event in tracer.events
                if isinstance(event, StepBegin) and event.kind == "tool"
            )
            tool_end = next(
                event
                for event in tracer.events
                if isinstance(event, StepEnd) and event.kind == "tool"
            )
            assert isinstance(tool_begin.given, ToolStepInfo)
            assert tool_begin.given.summary == "Executing double: 3"
            assert isinstance(tool_end.noted, StepSummary)
            assert tool_end.noted.summary == "Executed double: 3"

            stored_tool = next(
                step
                for step in harness.store.list_steps(run_id=run.id)
                if str(step.ref) == tool_begin.step
            )
            assert isinstance(stored_tool.given, ToolStepGiven)
            assert stored_tool.given.summary == "Executing double: 3"
            assert stored_tool.noted == ToolStepNoted(summary="Executed double: 3")

    asyncio.run(scenario())


def test_parallel_part_results_match_live_wire_and_record_snapshots(tmp_path: Path):
    parts = (ReasoningPart("hidden"), TextPart("answer"))
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic worker() -> Part[]:
  user: Answer

flow parent() -> Part[][]:
  generate 2 in 2 lanes using worker
""",
        responses=[ModelCallResult(message=Message(role="assistant", parts=parts))] * 2,
    )
    tracer = RecordingRunTracer()

    async def scenario():
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.CHAT)
            root = await harness.executor.run(
                harness.run_spec(thread=thread, runnable="parent"), tracer=tracer
            )
            assert root.status == "succeeded"
            runs = harness.store.list_runs(thread_id=thread, limit=None)
            snapshot = RecordSnapshot(
                harness.store,
                runs,
                [
                    step
                    for run in runs
                    for step in harness.store.list_steps(run_id=run.id)
                ],
            )
            for identity in (StepRef.parse(f"{root.id}.0"), root.id):
                live = next(
                    event
                    for event in tracer.events
                    if isinstance(event, StepEnd)
                    and event.step == str(identity)
                    or isinstance(event, RunEnd)
                    and event.run == identity
                )
                assert isinstance(live, StepEnd | RunEnd)
                assert live.output is not None and live.output.type == "Part[][]"
                assert live.output.value == (parts, parts)
                wire = run_event_from_data(run_event_to_data(live))
                assert wire == live
                recorded, _cursor = snapshot.ends[identity]
                assert isinstance(recorded, StepEnd | RunEnd)
                assert recorded.output == live.output

    asyncio.run(scenario())


def test_nested_run_events_are_strictly_inside_the_parent_run_step(
    tmp_path: Path,
) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic echo(_: Text) -> Text:
  recall = none
  context = none
  instruct = none
  user: {{_}}

flow relay(_: Text) -> Text:
  run echo
""",
        responses=[ModelCallResult(message=Message.assistant("relayed"))],
    )
    tracer = RecordingRunTracer()

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.CHAT)
            root = await harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="relay",
                    primary=resolve_input_parts("hello"),
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
                f"step_begin:{root.id}.0:run",
                f"run_begin:{child.id}",
                f"step_begin:{child.id}.0:model",
                f"part_begin:{child.id}.0:0:text",
                f"part_end:{child.id}.0:0:text",
                f"step_end:{child.id}.0:model:succeeded",
                f"run_end:{child.id}:succeeded",
                f"step_end:{root.id}.0:run:succeeded",
                f"run_end:{root.id}:succeeded",
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
  context = none
  instruct = none
  user: {{_}}

flow parallel(_: Text) -> Text[]:
  ## Run workers in parallel.
  generate 3 in 3 lanes using worker
""",
        responses=[
            ModelCallResult(message=Message.assistant(f"item {index}"))
            for index in range(3)
        ],
    )
    tracer = RecordingRunTracer()

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.CHAT)
            root = await harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="parallel",
                    primary=resolve_input_parts("work"),
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
            root_step = StepRef.parse(f"{root.id}.0")
            parent_begin = tracer.events.index(
                next(
                    event
                    for event in tracer.events
                    if isinstance(event, StepBegin)
                    and StepRef.parse(event.step) == root_step
                )
            )
            parent_end = tracer.events.index(
                next(
                    event
                    for event in tracer.events
                    if isinstance(event, StepEnd)
                    and StepRef.parse(event.step) == root_step
                )
            )
            parent_event = tracer.events[parent_begin]
            assert isinstance(parent_event, StepBegin)
            assert isinstance(parent_event.given, FlowStepInfo)
            assert parent_event.given.doc == "Run workers in parallel."
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
                assert begin_event.parent == str(child.parent) == str(root_step)
                assert parent_begin < begin[0] < end[0] < parent_end
            assert sum(isinstance(event, RunBegin) for event in tracer.events) == 4
            assert sum(isinstance(event, RunEnd) for event in tracer.events) == 4

    asyncio.run(scenario())
