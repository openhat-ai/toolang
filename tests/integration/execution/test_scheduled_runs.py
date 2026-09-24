"""Runtime calls acknowledge scheduling before executing serial child Runs."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from tests.support.execution_assertions import (
    assert_replayed,
    assert_run_event_integrity,
)
from tests.support.execution_harness import (
    ExecutionHarness,
    RecordingRunTracer,
    RecordingTool,
)
from toolang.cli.common.execution_progress import ProgressProjector
from toolang.base.types.message import Message, ToolResultPart, message_text
from toolang.base.types.policy import RunLimits
from toolang.base.types.run import ModelCallResult, ToolCall
from toolang.execution.events import (
    PartBegin,
    RunBegin,
    RunEnd,
    RunEvent,
    StepBegin,
    StepEnd,
)
from toolang.execution.store import RunStore
from toolang.execution.types import ThreadPrefix

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
  user: Private child task.
"""


def run_call(id: str) -> ToolCall:
    return ToolCall(id, f"provider-{id}", "_toolang__run", {"runnable": "agic:child"})


def test_batch_runs_after_receipts_and_before_next_tools(tmp_path: Path) -> None:
    tool = RecordingTool("math__double", output={"value": 6})
    harness = ExecutionHarness.create(
        tmp_path,
        source=SOURCE,
        tools={tool.name: tool},
        responses=[
            ModelCallResult(
                tool_calls=(
                    run_call("first"),
                    ToolCall("middle", "middle", tool.name, {}),
                    run_call("last"),
                ),
                continuation={"id": "parent-continuation"},
            ),
            ModelCallResult(
                message=Message.assistant("first output"),
                continuation={"id": "child-one"},
            ),
            ModelCallResult(
                message=Message.assistant("second output"),
                continuation={"id": "child-two"},
            ),
            ModelCallResult(message=Message.assistant("parent finished")),
            ModelCallResult(message=Message.assistant("next root")),
        ],
    )
    receipts = []
    beginnings = []

    class Tracer(RecordingRunTracer):
        async def on_event(self, event: RunEvent) -> None:
            await super().on_event(event)
            if isinstance(event, PartBegin) and event.step.index in {1, 3}:
                step = harness.store.get_step(ref=event.step)
                if (
                    step is not None
                    and step.output is not None
                    and isinstance(step.output.local.value, ToolResultPart)
                ):
                    run_id = step.output.local.value.output.get("run_id")
                    if isinstance(run_id, str):
                        receipts.append(
                            (
                                step,
                                harness.store.get_run(run_id=run_id),
                                harness.store.get_run_control(run_id=run_id, index=0),
                            )
                        )
            if isinstance(event, RunBegin) and event.parent is not None:
                beginnings.append(
                    (
                        harness.store.get_step(ref=event.parent),
                        harness.store.get_run_control(run_id=event.run, index=0),
                    )
                )

    tracer = Tracer()

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            root = await harness.executor.run(
                harness.run_spec(thread=thread, runnable="parent"), tracer=tracer
            )
            assert root.status == "succeeded", root.error
            assert len(receipts) == len(beginnings) == 2
            for step, child, control in receipts:
                assert step.status == "running" and step.output is not None
                assert child is not None and child.status == "pending"
                assert control is not None and control.status == "pending"
            for step, control in beginnings:
                assert step is not None and step.status == "succeeded"
                assert control is not None and control.status == "applied"
            children = [
                r
                for r in harness.store.list_run_tree(root_run_id=root.id)
                if r.parent is not None
            ]
            assert len(children) == 2
            for child in children:
                assert child.parent is not None
                receipt_end = next(
                    i
                    for i, e in enumerate(tracer.events)
                    if isinstance(e, StepEnd) and e.step == child.parent
                )
                child_begin = next(
                    i
                    for i, e in enumerate(tracer.events)
                    if isinstance(e, RunBegin) and e.run == child.id
                )
                child_end = next(
                    i
                    for i, e in enumerate(tracer.events)
                    if isinstance(e, RunEnd) and e.run == child.id
                )
                next_tool_or_model = next(
                    i
                    for i, e in enumerate(tracer.events)
                    if isinstance(e, StepBegin)
                    and e.step.run_id == root.id
                    and e.step.index == child.parent.index + 1
                )
                assert receipt_end < child_begin < child_end < next_tool_or_model
            followup = harness.adapter.invocations[3].call
            assert followup.continuation == {"id": "parent-continuation"}
            replies = [
                i
                for i, m in enumerate(followup.messages)
                if any(isinstance(p, ToolResultPart) for p in m.parts)
            ]
            outcomes = [
                (i, m) for i, m in enumerate(followup.messages) if m.tag == "run-result"
            ]
            assert len(replies) == 3 and len(outcomes) == 2
            assert max(replies) < min(i for i, _ in outcomes)
            assert "first output" in message_text(outcomes[0][1].parts)
            assert "second output" in message_text(outcomes[1][1].parts)
            assert all(
                "Private child task" not in message_text(m.parts)
                for m in followup.messages
            )
            next_root = await harness.executor.run(
                harness.run_spec(thread=thread, runnable="parent")
            )
            selected = harness.store.message_history(next_root.id).select(None)
            assert [m for m in selected.near if m.tag == "run-result"] == [
                m for _, m in outcomes
            ]
            conversation = harness.store.recent_conversation_messages(
                thread_id=thread, limit=100
            )
            assert [m for m in conversation if m.tag == "run-result"] == [
                m for _, m in outcomes
            ]
            assert_run_event_integrity(tracer.events)
            projector = ProgressProjector()
            rows = [
                row
                for event in tracer.events
                for block in projector.handle(event).committed
                for row in block.rows
            ]
            assert not projector._broken, [row.text for row in rows]
            assert projector.root_metrics.runs == 3
            assert projector.root_metrics.model_calls == 4
            assert projector.root_metrics.tool_calls == 3
            assert not projector._scheduled_runs
        assert_replayed(harness.store.db_path, tracer.events)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("output_type", "value", "expected"),
    [
        ("Text", "<result>&", "&lt;result&gt;&amp;"),
        ("Number[]", "[1, 2]", "[1,2]"),
        ("Boolean", "true", "true"),
        ("Part[]", "parts result", "parts result"),
    ],
)
def test_typed_completion_survives_no_followup_and_reopen(
    tmp_path: Path, output_type: str, value: str, expected: str
) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source=SOURCE.replace(
            "agic child() -> Text:", f"agic child() -> {output_type}:"
        ),
        responses=[
            ModelCallResult(tool_calls=(run_call("typed"),)),
            ModelCallResult(message=Message.assistant(value)),
            ModelCallResult(message=Message.assistant("next root")),
        ],
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            root = await harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="parent",
                    limits=RunLimits(agic_model_calls=1),
                )
            )
            assert root.status == "failed"
            child = next(
                r
                for r in harness.store.list_run_tree(root_run_id=root.id)
                if r.parent is not None
            )
            assert child.status == "succeeded", child.error
            next_root = await harness.executor.run(
                harness.run_spec(thread=thread, runnable="parent")
            )
        reopened = RunStore(harness.store.db_path, read_only=True)
        try:
            history = reopened.message_history(next_root.id).select(None)
            (completion,) = [m for m in history.near if m.tag == "run-result"]
            text = message_text(completion.parts)
            assert expected in text
            assert f'output-type="{output_type}"' in text
            assert child.id in text
            assert all(
                "Private child task" not in message_text(m.parts) for m in history.near
            )
            replies = [
                p
                for m in history.near
                for p in m.parts
                if isinstance(p, ToolResultPart)
            ]
            assert len(replies) == 1 and set(replies[0].output) == {
                "run_id",
                "controls",
            }
            conversation = reopened.recent_conversation_messages(
                thread_id=thread, limit=100
            )
            assert [m for m in conversation if m.tag == "run-result"] == [completion]
        finally:
            reopened.close()

    asyncio.run(scenario())
