"""Control semantics survive execution, history selection, and call replay."""

from __future__ import annotations

import asyncio
import os
import signal
from pathlib import Path

import pytest

from tests.support.execution_harness import (
    AsyncGate,
    ExecutionHarness,
    RecordingRunTracer,
    RecordingTool,
    ScriptedModelTurn,
)
from tests.support.execution_assertions import assert_replayed, steer_message
from toolang.base.types.message import Message, TextPart, ToolResultPart, message_text
from toolang.base.types.run import ModelCallResult, ToolCall
from toolang.execution.types import ThreadPrefix
from toolang.execution.types import (
    RulesRecallTarget,
    SkillRecallTarget,
    ServiceRecallTarget,
    ModelStepGiven,
    TypedRef,
)
from toolang.execution.records import RecallControlPayload
from toolang.execution.events import StepEnd, StepBegin
from toolang.plugin.toolsets.loading import LoadedTool
from toolang.plugin.toolsets.registry import ToolRef
from toolang.plugin.toolsets.shell import create_toolset


@pytest.mark.parametrize("action", ["cancel", "steer"])
def test_immediate_control_stops_shell_and_preserves_history(
    tmp_path: Path, action: str
) -> None:
    shell = LoadedTool(
        "shell",
        "built-in",
        ToolRef("shell", "shell", "execute"),
        create_toolset({}).tools()["execute"],
    )
    call = ToolCall(
        "sleep", "sleep", shell.name, {"command": "echo $$ > started; exec sleep 10"}
    )
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic chat(_: Part[]) -> Part[]:
  context: none
  instruct: none
  user: {{_}}
""",
        tools={shell.name: shell},
        responses=[
            ModelCallResult(tool_calls=(call,)),
            ModelCallResult(message=Message.assistant("new request")),
        ],
    )
    tracer = RecordingRunTracer()

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            handle = harness.executor.run(
                harness.run_spec(
                    thread=thread, runnable="chat", primary=(TextPart("sleep 10s"),)
                ),
                tracer=tracer,
            )
            pid_file = harness.setup.layout.home / "started"
            pid = None
            try:
                async with asyncio.timeout(2):
                    while not pid_file.exists() or not pid_file.read_text().strip():
                        await asyncio.sleep(0.01)
                pid = int(pid_file.read_text())
                control = (
                    handle.cancel()
                    if action == "cancel"
                    else handle.steer(Message.user("new direction"), timing="immediate")
                )
                record = await asyncio.wait_for(handle, timeout=2)
                with pytest.raises(ProcessLookupError):
                    os.kill(pid, 0)
                assert record.status == (
                    "canceled" if action == "cancel" else "succeeded"
                ), record.error
                tool = harness.store.list_steps(run_id=record.id)[1]
                assert tool.aborted_by == control.ref
                assert tool.output is not None
                assert isinstance(tool.output.local.value, ToolResultPart)
                assert tool.output.local.value.error is not None
                if action == "cancel":
                    await harness.executor.run(
                        harness.run_spec(
                            thread=thread, runnable="chat", primary=(TextPart("1"),)
                        ),
                        tracer=tracer,
                    )
                first, following = (item.call for item in harness.adapter.invocations)
                assert first.instructions.startswith("<toolang:protocol>")
                assert "<toolang:messages>" in first.instructions
                assert "<toolang:instruct>" not in first.instructions
                assert following.instructions == first.instructions
                messages = following.messages
                assert [item.role for item in messages] == [
                    "user",
                    "assistant",
                    "tool",
                    "user",
                    *(["user"] if action == "cancel" else []),
                ]
                marker = messages[3]
                assert message_text(marker.parts).startswith(
                    f'<toolang:{action} description="'
                )
                assert record.id not in message_text(marker.parts)
                if action == "cancel":
                    assert message_text(marker.parts).endswith("/>")
                    assert messages[-1] == Message.user("1")
                assert_replayed(harness.store.db_path, tracer.events)
            finally:
                if pid is not None:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    asyncio.run(scenario())


def test_cancel_during_steer_skipped_batch_closes_all_calls(tmp_path: Path) -> None:
    model_gate, tool_gate = AsyncGate(), AsyncGate()
    tool = RecordingTool("lookup__item", output={})
    calls = tuple(ToolCall(str(index), str(index), tool.name, {}) for index in range(3))
    harness = ExecutionHarness.create(
        tmp_path,
        source="agic chat():\n  user: Start.\n",
        tools={tool.name: tool},
        responses=[
            ScriptedModelTurn(result=ModelCallResult(tool_calls=calls), gate=model_gate)
        ],
    )

    class Tracer(RecordingRunTracer):
        async def on_event(self, event):
            await super().on_event(event)
            if (
                isinstance(event, StepBegin)
                and event.kind == "tool"
                and event.step.index == 1
            ):
                await tool_gate.wait()

    async def scenario() -> None:
        async with harness:
            handle = harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="chat",
                ),
                tracer=Tracer(),
            )
            await asyncio.wait_for(model_gate.wait_until_entered(), 1)
            handle.steer(Message.user("skip"), timing="next_step")
            model_gate.release()
            await asyncio.wait_for(tool_gate.wait_until_entered(), 1)
            handle.cancel()
            run = await asyncio.wait_for(handle, 2)
            assert run.status == "canceled"
            results = [
                step
                for step in harness.store.list_steps(run_id=run.id)
                if step.kind == "tool"
            ]
            assert len(results) == 3
            assert all(step.output is not None for step in results)
            assert not tool.calls
            history = harness.store.recent_conversation_messages(
                thread_id=str(run.thread)
            )
            assert [message.role for message in history] == [
                "assistant",
                "tool",
                "tool",
                "tool",
                "user",
            ]
            assert message_text(history[-1].parts).startswith("<toolang:cancel ")

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "target",
    [
        RulesRecallTarget('repo&"', "/src"),
        SkillRecallTarget('skill&"'),
        ServiceRecallTarget('service&"'),
    ],
)
def test_recall_is_adopted_once_and_replays(tmp_path: Path, target) -> None:
    tool = RecordingTool("lookup__item", output={"ok": True})
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic chat(_: Part[]) -> Part[]:
  user: {{_}}
""",
        tools={tool.name: tool},
        responses=[
            ModelCallResult(tool_calls=(ToolCall("one", "one", tool.name, {}),)),
            ModelCallResult(tool_calls=(ToolCall("two", "two", tool.name, {}),)),
            ModelCallResult(message=Message.assistant("done")),
        ],
    )
    recalls = []

    class Tracer(RecordingRunTracer):
        async def on_event(self, event):
            await super().on_event(event)
            if isinstance(event, StepEnd) and event.kind == "tool" and not recalls:
                for revision in ("old", "new"):
                    recalls.append(
                        harness.store.accept_recall_control(
                            run_id=event.step.run_id,
                            triggered_by=event.step,
                            payload=RecallControlPayload(
                                target, revision, f"{revision} <content>"
                            ),
                            created_at=event.finished_at,
                        )
                    )

    tracer = Tracer()

    async def scenario() -> None:
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
            calls = [item.call for item in harness.adapter.invocations]
            for call in calls[1:]:
                recalled = [
                    message
                    for message in call.messages
                    if message_text(message.parts).startswith(
                        f"<toolang:{target.kind + '-guidance' if target.kind in {'skill', 'service'} else target.kind} "
                    )
                ]
                assert len(recalled) == 3
                assert 'removed="true"' in message_text(recalled[-1].parts)
                assert "&amp;&quot;" in message_text(recalled[0].parts)
                assert recalled[1].parts[1] == TextPart("new &lt;content&gt;")
            model_events = [
                event
                for event in tracer.events
                if isinstance(event, StepBegin)
                and isinstance(event.given, ModelStepGiven)
            ]
            assert model_events[1].preceded_by == (
                *(item.ref for item in recalls),
                harness.store.list_run_controls(run_id=run.id)[-1].ref,
            )
            assert model_events[2].preceded_by == ()
            recall_refs = [item.ref for item in recalls]
            for event, expected in zip(model_events, ([], recall_refs, [])):
                assert event.given.delta is not None
                refs = [
                    segment.ref.record
                    for message in event.given.delta.messages
                    for segment in message.segments
                    if isinstance(segment, TypedRef)
                    and segment.ref.record in recall_refs
                ]
                assert refs == expected
            assert all(
                ref.record not in {item.ref for item in recalls}
                for ref in model_events[1].input
            )
            assert calls[0].messages == calls[2].messages[: len(calls[0].messages)]
            assert (
                sum(
                    message_text(message.parts).count("<toolang:context>")
                    for message in calls[2].messages
                )
                == 3
            )
            assert str(run.thread) not in calls[0].instructions
            assert run.id not in calls[0].instructions
        assert_replayed(harness.store.db_path, tracer.events)

    asyncio.run(scenario())


@pytest.mark.parametrize("timing", ["immediate", "next_call"])
def test_recall_and_steer_preserve_adoption_order(tmp_path: Path, timing) -> None:
    gate = AsyncGate()
    tool = RecordingTool("lookup__item", output={}, gate=gate)
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic chat(_: Part[]) -> Part[]:
  context: none
  instruct: none
  user: {{_}}
""",
        tools={tool.name: tool},
        responses=[
            ModelCallResult(tool_calls=(ToolCall("one", "one", tool.name, {}),)),
            ModelCallResult(message=Message.assistant("done")),
        ],
    )
    tracer = RecordingRunTracer()

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            handle = harness.executor.run(
                harness.run_spec(
                    thread=thread, runnable="chat", primary=(TextPart("start"),)
                ),
                tracer=tracer,
            )
            await asyncio.wait_for(gate.wait_until_entered(), 1)
            step = harness.store.list_steps(run_id=handle.run_id)[-1]

            def recall(revision: str):
                return harness.store.accept_recall_control(
                    run_id=handle.run_id,
                    triggered_by=step.ref,
                    payload=RecallControlPayload(
                        SkillRecallTarget("testing"), revision, revision
                    ),
                    created_at=step.started_at,
                )

            first = recall("old")
            steer = handle.steer(Message.user("new direction"), timing=timing)
            last = recall("new")
            if timing == "next_call":
                gate.release()
            run = await asyncio.wait_for(handle, 2)
            assert run.status == "succeeded", run.error
            call = harness.adapter.invocations[-1].call
            adopted = call.messages[3:]
            assert [message.parts[0] for message in adopted] == [
                TextPart('<toolang:skill-guidance ref="testing" revision="old">'),
                steer_message("new direction").parts[0],
                TextPart('<toolang:skill-guidance ref="testing" revision="new">'),
                TextPart('<toolang:skill-guidance ref="testing" removed="true"/>'),
            ]
            assert harness.store.list_steps(run_id=run.id)[-1].preceded_by == (
                first.ref,
                steer.ref,
                last.ref,
                harness.store.list_run_controls(run_id=run.id)[-1].ref,
            )
            assert harness.store.recent_conversation_messages(thread_id=thread) == [
                *call.messages,
                Message.assistant("done"),
            ]
        assert_replayed(harness.store.db_path, tracer.events)

    asyncio.run(scenario())
