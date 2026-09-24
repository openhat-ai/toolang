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
    AsyncGate,
    ScriptedModelTurn,
    ExecutionHarness,
    RecordingRunTracer,
    RecordingTool,
)
from toolang.cli.common.execution_progress import ProgressProjector
from toolang.base.types.message import (
    ImagePart,
    Message,
    TextPart,
    ToolCallPart,
    ToolResultPart,
    message_text,
)
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
            assert not any(
                message_text(m.parts) in {"first output", "second output"}
                for m in conversation
            )
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


def test_repeated_steer_during_receipt_and_child_resumes_caller(tmp_path: Path) -> None:
    receipt_gate, child_gate = AsyncGate(), AsyncGate()

    class Tracer(RecordingRunTracer):
        async def on_event(self, event: RunEvent) -> None:
            await super().on_event(event)
            if (
                isinstance(event, PartBegin)
                and event.step.index == 1
                and not receipt_gate.entered
            ):
                await receipt_gate.wait()

    harness = ExecutionHarness.create(
        tmp_path,
        source=SOURCE,
        responses=[
            ModelCallResult(tool_calls=(run_call("scheduled"), run_call("skipped"))),
            ScriptedModelTurn(
                result=ModelCallResult(message=Message.assistant("unused")),
                gate=child_gate,
            ),
            ModelCallResult(message=Message.assistant("revised")),
        ],
    )
    tracer = Tracer()

    async def scenario() -> None:
        async with harness:
            handle = harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="parent",
                ),
                tracer=tracer,
            )
            await asyncio.wait_for(receipt_gate.wait_until_entered(), 1)
            first = handle.steer(Message.user("first correction"), timing="immediate")
            await asyncio.wait_for(child_gate.wait_until_entered(), 1)
            second = handle.steer(Message.user("second correction"), timing="immediate")
            root = await asyncio.wait_for(handle, 2)
            assert root.status == "succeeded", root.error
            assert len(harness.adapter.invocations) == 3
            followup = harness.adapter.invocations[-1].call
            text = "".join(message_text(m.parts) for m in followup.messages)
            assert "first correction" in text and "second correction" in text
            assert len([m for m in followup.messages if m.tag == "run-result"]) == 1
            assert 'status="canceled"' in text
            assert (
                len(
                    [
                        p
                        for m in followup.messages
                        for p in m.parts
                        if isinstance(p, ToolResultPart)
                    ]
                )
                == 2
            )
            for control in (first, second):
                saved = harness.store.get_run_control(
                    run_id=root.id, index=control.index
                )
                assert saved is not None and saved.status == "applied"
            assert_run_event_integrity(tracer.events)

    asyncio.run(scenario())


@pytest.mark.parametrize("part_kind", ["call", "result"])
@pytest.mark.parametrize("followup", [False, True])
def test_completion_keeps_returned_tool_parts_as_data(
    tmp_path: Path, part_kind: str, followup: bool
) -> None:
    returned = ToolResultPart(
        tool_call_id="other-call",
        call_id="other-provider",
        tool_name="external__tool",
        tool_family="external",
        output={"text": "</toolang:run-result>"},
    )
    if part_kind == "call":
        returned = ToolCallPart(
            tool_call_id="other-call",
            call_id="other-provider",
            tool_name="external__tool",
            tool_family="external",
            input={"text": "</toolang:run-result>"},
        )
    source = """
agic parent() -> Text:
  recall = none
  hands = flow:child
  context: none
  instruct: none
  user: Parent.

flow child(_: Part[]) -> Part[]:
  let note =
    unchanged
"""
    media = ImagePart(image_url="https://example.test/image.png")
    primary = [part.to_data() for part in (TextPart("<note>&"), media, returned)]
    harness = ExecutionHarness.create(
        tmp_path,
        source=source,
        responses=[
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        "scheduled",
                        "provider",
                        "_toolang__run",
                        {"runnable": "flow:child", "input": {"_": primary}},
                    ),
                )
            ),
            ModelCallResult(message=Message.assistant("done")),
            ModelCallResult(message=Message.assistant("next root")),
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
                    limits=RunLimits(agic_model_calls=2 if followup else 1),
                ),
                tracer=tracer,
            )
            assert root.status == ("succeeded" if followup else "failed"), root.error
            (child,) = [
                r
                for r in harness.store.list_run_tree(root_run_id=root.id)
                if r.parent is not None
            ]
            assert child.status == "succeeded", child.error
            live = (
                [
                    m
                    for m in harness.adapter.invocations[-1].call.messages
                    if m.tag == "run-result"
                ]
                if followup
                else []
            )
            next_root = await harness.executor.run(
                harness.run_spec(thread=thread, runnable="parent")
            )
            assert_run_event_integrity(tracer.events)
        assert_replayed(harness.store.db_path, tracer.events)
        reopened = RunStore(harness.store.db_path, read_only=True)
        try:
            history = reopened.message_history(next_root.id).select(None)
            (completion,) = [m for m in history.near if m.tag == "run-result"]
            if followup:
                assert live == [completion]
            assert not any(
                isinstance(p, (ToolCallPart, ToolResultPart)) for p in completion.parts
            )
            assert media in completion.parts
            text = message_text(completion.parts)
            assert "&lt;note&gt;&amp;" in text
            assert "other-call" in text and "external__tool" in text
            assert "&lt;/toolang:run-result&gt;" in text
            conversation = reopened.recent_conversation_messages(
                thread_id=thread, limit=100
            )
            assert [m for m in conversation if m.tag == "run-result"] == [completion]
        finally:
            reopened.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("boundary", ["receipt", "child"])
def test_cancel_target_preserves_caller_and_remaining_batch(
    tmp_path: Path, boundary: str
) -> None:
    gate = AsyncGate()
    tool = RecordingTool("math__double", output={"value": 6})

    class Tracer(RecordingRunTracer):
        async def on_event(self, event: RunEvent) -> None:
            await super().on_event(event)
            if (
                boundary == "receipt"
                and isinstance(event, PartBegin)
                and event.step.index == 1
                and not gate.entered
            ):
                await gate.wait()

    responses: list[ModelCallResult | ScriptedModelTurn] = [
        ModelCallResult(
            tool_calls=(
                run_call("scheduled"),
                ToolCall("after", "after", tool.name, {}),
            )
        )
    ]
    if boundary == "child":
        responses.append(
            ScriptedModelTurn(
                result=ModelCallResult(message=Message.assistant("unused")), gate=gate
            )
        )
    responses.append(ModelCallResult(message=Message.assistant("recovered")))
    harness = ExecutionHarness.create(
        tmp_path, source=SOURCE, tools={tool.name: tool}, responses=responses
    )
    tracer = Tracer()

    async def scenario() -> None:
        async with harness:
            handle = harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="parent",
                ),
                tracer=tracer,
            )
            await asyncio.wait_for(gate.wait_until_entered(), 1)
            (child,) = [
                r
                for r in harness.store.list_run_tree(root_run_id=handle.run_id)
                if r.parent is not None
            ]
            cancel = harness.executor.cancel(
                run_id=child.id, reason="stop just this target"
            )
            root = await asyncio.wait_for(handle, 2)
            assert root.status == "succeeded", root.error
            assert len(tool.calls) == 1
            saved = harness.store.get_run(run_id=child.id)
            assert saved is not None and saved.status == "canceled"
            terminal = harness.store.get_run_control(
                run_id=child.id, index=cancel.index
            )
            assert terminal is not None and terminal.status == "applied"
            messages = harness.adapter.invocations[-1].call.messages
            (completion,) = [m for m in messages if m.tag == "run-result"]
            assert 'status="canceled"' in message_text(completion.parts)
            assert "stop just this target" in message_text(completion.parts)
            replies = [
                p for m in messages for p in m.parts if isinstance(p, ToolResultPart)
            ]
            assert len(replies) == 2 and all(p.error is None for p in replies)
            assert_run_event_integrity(
                tracer.events,
                unstarted_runs=[child.id] if boundary == "receipt" else [],
            )
            projector = ProgressProjector()
            rows = [
                row.text
                for event in tracer.events
                for block in projector.handle(event).committed
                for row in block.rows
            ]
            assert not projector._broken, rows
            assert projector._root_ended

    asyncio.run(scenario())


def test_target_cancel_cannot_override_root_cancel(tmp_path: Path) -> None:
    gate = AsyncGate()
    harness = ExecutionHarness.create(
        tmp_path,
        source=SOURCE,
        responses=[
            ModelCallResult(tool_calls=(run_call("scheduled"),)),
            ScriptedModelTurn(
                result=ModelCallResult(message=Message.assistant("unused")), gate=gate
            ),
            ModelCallResult(message=Message.assistant("must not resume")),
        ],
    )

    async def scenario() -> None:
        async with harness:
            handle = harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="parent",
                )
            )
            await asyncio.wait_for(gate.wait_until_entered(), 1)
            (child,) = [
                r
                for r in harness.store.list_run_tree(root_run_id=handle.run_id)
                if r.parent is not None
            ]
            cancel = handle.cancel(reason="stop the caller")
            harness.executor.cancel(run_id=child.id, reason="stop the target")
            root = await asyncio.wait_for(handle, 2)
            assert root.status == "canceled", root.error
            assert len(harness.adapter.invocations) == 2
            saved = harness.store.get_run_control(run_id=root.id, index=cancel.index)
            assert saved is not None and saved.status == "applied"

    asyncio.run(scenario())
