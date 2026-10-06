"""Runtime calls return child results inside their synchronous Tool Steps."""

from __future__ import annotations

import asyncio
from pathlib import Path
from collections.abc import Iterable

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
    ReasoningPart,
    TextPart,
    ToolCallPart,
    ToolResultPart,
    message_text,
)
from toolang.base.types.policy import RunLimits
from toolang.base.types.run import ModelCallResult, ToolCall
from toolang.execution.events import (
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
  context = none
  instruct = none
  user: Parent task.

agic child() -> Text:
  recall = none
  context = none
  instruct = none
  user: Private child task.
"""


def run_call(id: str) -> ToolCall:
    return ToolCall(id, f"provider-{id}", "_toolang__run", {"runnable": "agic:child"})


def tool_results(messages: Iterable[Message]) -> list[ToolResultPart]:
    return [
        part
        for message in messages
        for part in message.parts
        if isinstance(part, ToolResultPart)
    ]


def test_batch_runs_finish_before_replies_and_next_tools(tmp_path: Path) -> None:
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
    beginnings = []

    class Tracer(RecordingRunTracer):
        async def on_event(self, event: RunEvent) -> None:
            await super().on_event(event)
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
            assert len(beginnings) == 2
            for step, control in beginnings:
                assert step is not None and step.status == "running"
                assert step.output is None
                assert control is not None and control.status == "applied"
                assert control.finished_at == control.created_at
            children = [
                r
                for r in harness.store.list_run_tree(root_run_id=root.id)
                if r.parent is not None
            ]
            assert len(children) == 2
            for child in children:
                assert child.parent is not None
                tool_end = next(
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
                assert child_begin < child_end < tool_end < next_tool_or_model
            followup = harness.adapter.invocations[3].call
            assert followup.continuation == {"id": "parent-continuation"}
            replies = tool_results(followup.messages)
            assert [part.output for part in replies] == [
                {"type": "Text", "value": "first output"},
                {"value": 6},
                {"type": "Text", "value": "second output"},
            ]
            assert not any(m.tag == "run-result" for m in followup.messages)
            assert all(
                "Private child task" not in message_text(m.parts)
                for m in followup.messages
            )
            next_root = await harness.executor.run(
                harness.run_spec(thread=thread, runnable="parent")
            )
            selected = harness.store.message_history(next_root.id).select(None)
            assert tool_results(selected.near) == replies
            conversation = harness.store.recent_conversation_messages(
                thread_id=thread, limit=100
            )
            assert tool_results(conversation) == replies
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
            assert not projector._steps
        assert_replayed(harness.store.db_path, tracer.events)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("output_type", "value", "expected"),
    [
        ("Text", "<result>&", "<result>&"),
        ("Number[]", "[1, 2]", [1, 2]),
        ("Number[]", "[]", []),
        ("Boolean", "true", True),
        ("Part[]", "parts result", [TextPart("parts result").to_data()]),
    ],
)
def test_typed_result_survives_no_followup_and_reopen(
    tmp_path: Path, output_type: str, value: str, expected: object
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
            assert not any(m.tag == "run-result" for m in history.near)
            assert all(
                "Private child task" not in message_text(m.parts) for m in history.near
            )
            (reply,) = tool_results(history.near)
            assert reply.error is None
            assert reply.output == {"type": output_type, "value": expected}
            conversation = reopened.recent_conversation_messages(
                thread_id=thread, limit=100
            )
            assert tool_results(conversation) == [reply]
        finally:
            reopened.close()

    asyncio.run(scenario())


def test_repeated_steer_during_children_resumes_caller(tmp_path: Path) -> None:
    gates = [AsyncGate(), AsyncGate()]
    harness = ExecutionHarness.create(
        tmp_path,
        source=SOURCE,
        responses=[
            ModelCallResult(tool_calls=(run_call("first"), run_call("skipped"))),
            ScriptedModelTurn(
                ModelCallResult(message=Message.assistant("unused")), gate=gates[0]
            ),
            ModelCallResult(tool_calls=(run_call("second"),)),
            ScriptedModelTurn(
                ModelCallResult(message=Message.assistant("unused")), gate=gates[1]
            ),
            ModelCallResult(message=Message.assistant("revised")),
        ],
    )
    tracer = RecordingRunTracer()

    async def scenario() -> None:
        async with harness:
            handle = harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="parent",
                ),
                tracer=tracer,
            )
            controls = []
            for index, gate in enumerate(gates):
                await asyncio.wait_for(gate.wait_until_entered(), 2)
                controls.append(
                    handle.steer(
                        Message.user(f"correction {index}"), timing="immediate"
                    )
                )
            root = await asyncio.wait_for(handle, 2)
            assert root.status == "succeeded", root.error
            assert len(harness.adapter.invocations) == 5
            messages = harness.adapter.invocations[-1].call.messages
            text = "".join(message_text(m.parts) for m in messages)
            assert "correction 0" in text and "correction 1" in text
            assert not any(m.tag == "run-result" for m in messages)
            replies = tool_results(messages)
            assert len(replies) == 3 and all(p.error for p in replies)
            for control in controls:
                saved = harness.store.get_run_control(
                    run_id=root.id, index=control.index
                )
                assert saved is not None and saved.status == "applied"
            assert_run_event_integrity(tracer.events)
        assert_replayed(harness.store.db_path, tracer.events)

    asyncio.run(scenario())


@pytest.mark.parametrize("part_kind", ["call", "result"])
@pytest.mark.parametrize("followup", [False, True])
def test_result_keeps_returned_tool_parts_as_data(
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
  context = none
  instruct = none
  user: Parent.

flow child(_: Part[]) -> Part[]:
  let note =
    unchanged
"""
    media = ImagePart(image_url="https://example.test/image.png")
    origin: dict[str, object] = {"adapter": "generate_content", "model": "model"}
    primary = [
        part.to_data()
        for part in (
            ReasoningPart("child reasoning", "reasoning-signature", "provider", origin),
            TextPart(
                "<note>&",
                signature="text-signature",
                provider="provider",
                provider_metadata=origin,
            ),
            media,
            returned,
        )
    ]
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
                tool_results(harness.adapter.invocations[-1].call.messages)
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
            (reply,) = tool_results(history.near)
            if followup:
                assert live == [reply]
            assert reply.output == {
                "type": "Part[]",
                "value": [
                    TextPart("<note>&").to_data(),
                    media.to_data(),
                    returned.to_data(),
                ],
            }
            assert not any(m.tag == "run-result" for m in history.near)
            assert all(
                p.tool_call_id != "other-call"
                for m in history.near
                for p in m.parts
                if isinstance(p, (ToolCallPart, ToolResultPart))
            )
            conversation = reopened.recent_conversation_messages(
                thread_id=thread, limit=100
            )
            assert tool_results(conversation) == [reply]
        finally:
            reopened.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("boundary", ["run_begin", "child"])
def test_cancel_target_preserves_caller_and_remaining_batch(
    tmp_path: Path, boundary: str
) -> None:
    gate = AsyncGate()
    tool = RecordingTool("math__double", output={"value": 6})

    class Tracer(RecordingRunTracer):
        async def on_event(self, event: RunEvent) -> None:
            await super().on_event(event)
            if (
                boundary == "run_begin"
                and isinstance(event, RunBegin)
                and event.parent is not None
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
            replies = tool_results(messages)
            assert len(replies) == 2
            assert (
                replies[0].error is not None
                and "stop just this target" in replies[0].error
            )
            assert replies[1].error is None
            assert not any(m.tag == "run-result" for m in messages)
            assert_run_event_integrity(tracer.events)
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
