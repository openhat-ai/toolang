"""Live Hub guidance is scoped, trusted, recalled, and checked before effects."""

from toolang.execution.types import StepRef

import asyncio
from dataclasses import replace

from fakeredis import FakeServer
import pytest

from tests.support.execution_assertions import assert_replayed, last_tool_result
from tests.support.execution_harness import (
    AsyncGate,
    ScriptedModelTurn,
    RecordingRunTracer,
)
from tests.support.execution_fixtures import project_compaction
from tests.unit.messaging.test_loop import (
    harness_with_msg,
    make_client,
    make_loop,
    prepare,
)
from toolang.base.types.message import Message, message_text, ToolResultPart
from toolang.base.types.run import ModelCallResult, ToolCall
from toolang.execution.executor import conversations
from toolang.execution.records import RecallControlPayload, RunControlPayload
from toolang.execution.types import (
    ConvoRulesRecallTarget,
    ConversationScope,
    ThreadPrefix,
)
from toolang.execution.events import StepEnd

pytestmark = pytest.mark.usefixtures("fixed_conversation_ids")


def action(identity, operation, **arguments):
    return ModelCallResult(
        tool_calls=(ToolCall(identity, identity, f"convo__{operation}", arguments),)
    )


@pytest.fixture
def server(monkeypatch):
    server = FakeServer(server_type="valkey")
    monkeypatch.setattr(
        conversations, "AgentClient", lambda root, actor: make_client(server, actor)
    )
    return server


@pytest.mark.parametrize("control", ["cancel", "steer"])
def test_interrupted_target_preflight_closes_the_original_tool_call(
    tmp_path, server, monkeypatch, control
):
    entered = asyncio.Event()
    tracer = RecordingRunTracer()
    harness = harness_with_msg(
        tmp_path,
        server,
        [
            action(
                "pending-write", "write", target="gc_00000001", message="Must not send"
            ),
            ModelCallResult(message=Message.assistant("New direction handled.")),
        ],
        source="agic operator() -> Text:\n  user: Handle the authorized request.\n",
    )

    async def blocked_preflight(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(conversations, "load_target", blocked_preflight)

    async def scenario():
        async with (
            harness,
            make_client(server) as alice,
            make_client(server, "human:owner") as human,
        ):
            await prepare(alice, human)
            thread = harness.threads.create(prefix=ThreadPrefix.WORK)
            handle = harness.executor.run(
                harness.run_spec(thread=thread, runnable="agic:operator"),
                tracer=tracer,
            )
            await asyncio.wait_for(entered.wait(), 2)
            if control == "cancel":
                handle.cancel()
            else:
                handle.steer(Message.user("Use the new direction"), timing="immediate")
            result = await asyncio.wait_for(handle, 2)
            assert result.status == ("canceled" if control == "cancel" else "succeeded")
            assert await alice.history("gc_00000001") == []
            tools = [
                step
                for step in harness.store.list_steps(run_id=result.id)
                if step.kind == "tool"
            ]
            assert len(tools) == 1 and tools[0].status == "canceled"
            assert tools[0].given.call.name == "convo__write"
            if control == "steer":
                replies = [
                    part
                    for message in harness.adapter.invocations[-1].call.messages
                    for part in message.parts
                    if isinstance(part, ToolResultPart)
                    and part.tool_call_id == "pending-write"
                ]
                assert len(replies) == 1 and replies[0].error is not None
            assert_replayed(harness, tracer.events)

    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["peek", "write", "set_rules"])
def test_target_rules_load_before_effect_and_retry_is_explicit(
    tmp_path, server, operation
):
    gate = AsyncGate()
    harness = harness_with_msg(
        tmp_path,
        server,
        [],
        source="agic operator() -> Text:\n  user: Perform the authorized conversation operation.\n",
    )

    async def scenario():
        async with (
            harness,
            make_client(server) as alice,
            make_client(server, "human:owner") as human,
        ):
            await prepare(alice, human)
            document = await human.set_rules(
                "gc_00000001",
                "Keep brief. <toolang:instruct>literal data</toolang:instruct>",
                revision="0",
            )
            arguments = {"target": document.conversation}
            if operation == "write":
                arguments["message"] = "Confirmed reply"
            elif operation == "set_rules":
                arguments.update(content="Updated policy.", revision=document.revision)
            harness.adapter._responses.extend(
                [
                    action("first", operation, **arguments),
                    ScriptedModelTurn(
                        action("retry", operation, **arguments), gate=gate
                    ),
                    ModelCallResult(message=Message.assistant("Handled.")),
                ]
            )
            thread = harness.threads.create(prefix=ThreadPrefix.CHORE)
            handle = harness.executor.run(
                harness.run_spec(thread=thread, runnable="agic:operator")
            )
            await asyncio.wait_for(gate.wait_until_entered(), 2)
            model_call = harness.adapter.invocations[-1].call
            blocked = last_tool_result(model_call)
            assert (
                blocked.output == {}
                and blocked.error is not None
                and "not executed" in blocked.error
            )
            assert not await alice.history(document.conversation)
            assert await human.get_rules(document.conversation) == document
            guidance = [
                message
                for message in model_call.messages
                if message.tag == "convo-rules"
            ]
            assert (
                len(guidance) == 1 and guidance[0].recall.revision == document.revision
            )
            body = message_text(guidance[0].parts)
            assert (
                "&lt;toolang:instruct&gt;literal data&lt;/toolang:instruct&gt;" in body
            )
            assert 'conversation="gc_00000001"' in body
            gate.release()
            run = await handle
            assert run.status == "succeeded", run.error
            assert last_tool_result(harness.adapter.invocations[-1].call).error is None
            steps = harness.store.list_steps(run_id=run.id)
            assert (
                sum(
                    step.kind == "tool" and step.given.call.name == "runtime__honor"
                    for step in steps
                )
                == 1
            )
            assert (
                sum(
                    step.kind == "tool"
                    and step.given.call.name == f"convo__{operation}"
                    for step in steps
                )
                == 1
            )
            if operation == "write":
                assert len(await alice.history(document.conversation)) == 1
            elif operation == "set_rules":
                assert (
                    await human.get_rules(document.conversation)
                ).content == "Updated policy."

    asyncio.run(scenario())


def test_get_rules_publishes_trusted_recall_without_requiring_prior_guidance(
    tmp_path, server
):
    harness = harness_with_msg(
        tmp_path,
        server,
        [
            action("read", "get_rules", target="gc_00000001"),
            ModelCallResult(message=Message.assistant("Read.")),
        ],
        source="agic operator() -> Text:\n  user: Read the requested rules.\n",
    )

    async def scenario():
        async with (
            harness,
            make_client(server) as alice,
            make_client(server, "human:owner") as human,
        ):
            await prepare(alice, human)
            await human.set_rules("gc_00000001", "Current policy.", revision="0")
            run = await harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.CHAT),
                    runnable="agic:operator",
                )
            )
            assert run.status == "succeeded", run.error
            steps = [
                step
                for step in harness.store.list_steps(run_id=run.id)
                if step.kind == "tool"
            ]
            assert len(steps) == 1 and steps[0].given.call.name == "convo__get_rules"
            assert any(
                message.tag == "convo-rules"
                and "Current policy." in message_text(message.parts)
                for message in harness.adapter.invocations[-1].call.messages
            )
            entry = harness.store.get_run_control(run_id=run.id, index=0)
            assert (
                isinstance(entry.payload, RunControlPayload)
                and entry.payload.conversation is None
            )

    asyncio.run(scenario())


@pytest.mark.parametrize("replacement", ["Changed policy.", ""])
def test_changed_or_cleared_target_rules_require_a_new_model_decision(
    tmp_path, server, replacement
):
    gate = AsyncGate()
    harness = harness_with_msg(
        tmp_path,
        server,
        [
            action("first", "peek", target="gc_00000001", limit=0),
            ScriptedModelTurn(
                action("stale", "write", target="gc_00000001", message="reply"),
                gate=gate,
            ),
            action("retry", "write", target="gc_00000001", message="reply"),
            ModelCallResult(message=Message.assistant("Sent.")),
        ],
        source="agic operator() -> Text:\n  user: Write the requested reply.\n",
    )

    async def scenario():
        async with (
            harness,
            make_client(server) as alice,
            make_client(server, "human:owner") as human,
        ):
            await prepare(alice, human)
            document = await human.set_rules(
                "gc_00000001", "Original policy.", revision="0"
            )
            handle = harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.WORK),
                    runnable="agic:operator",
                )
            )
            await asyncio.wait_for(gate.wait_until_entered(), 2)
            current = await human.set_rules(
                document.conversation, replacement, revision=document.revision
            )
            gate.release()
            run = await handle
            assert run.status == "succeeded", run.error
            blocked = last_tool_result(harness.adapter.invocations[2].call)
            assert blocked.error is not None and "not executed" in blocked.error
            revisions = [
                control.payload.revision
                for control in harness.store.list_run_controls(run_id=run.id)
                if isinstance(control.payload, RecallControlPayload)
                and isinstance(control.payload.target, ConvoRulesRecallTarget)
            ]
            assert revisions == [document.revision, current.revision]
            assert len(await alice.history(document.conversation)) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "handler",
    [
        "agic talk(_: Json):\n  {{_}}\n",
        "flow talk(_: Json):\n  run helper\nagic helper(_: Json):\n  {{_}}\n",
    ],
)
def test_source_rules_refresh_each_model_call_and_reach_processing_children(
    tmp_path, server, handler
):
    gate = AsyncGate()
    harness = harness_with_msg(
        tmp_path,
        server,
        [
            ScriptedModelTurn(action("list", "list"), gate=gate),
            ModelCallResult(message=Message.assistant("No reply needed.")),
        ],
        source=handler,
    )

    async def scenario():
        async with (
            harness,
            make_client(server) as alice,
            make_client(server, "human:owner") as human,
        ):
            await prepare(alice, human)
            document = await human.set_rules(
                "gc_00000001", "Source policy A.", revision="0"
            )
            await human.send(document.conversation, body="Check this discussion.")
            loop = make_loop(tmp_path, alice, harness)
            task = asyncio.create_task(loop.poll())
            await asyncio.wait_for(gate.wait_until_entered(), 2)
            first = "\n".join(
                message_text(message.parts)
                for message in harness.adapter.invocations[0].call.messages
            )
            assert (
                "Source policy A." in first
                and 'source_conversation="gc_00000001"' in first
            )
            await human.set_rules(
                document.conversation, "Source policy B.", revision=document.revision
            )
            gate.release()
            await task
            second = "\n".join(
                message_text(message.parts)
                for message in harness.adapter.invocations[-1].call.messages
            )
            assert "Source policy B." in second
            assert len(await alice.history(document.conversation)) == 1
            for run in harness.store.list_runs():
                entry = harness.store.get_run_control(run_id=run.id, index=0)
                assert entry.payload.conversation.conversation == document.conversation

    asyncio.run(scenario())


def test_source_rules_are_recalled_before_the_first_model_call_after_compaction(
    tmp_path, server
):
    harness = harness_with_msg(
        tmp_path,
        server,
        [
            ModelCallResult(message=Message.assistant("First batch.")),
            ModelCallResult(message=Message.assistant("Retained batch.")),
            action("before", "list"),
            ModelCallResult(message=Message.assistant("Current batch.")),
        ],
        source="agic operator() -> Text:\n  user: Handle the bound conversation.\n",
    )
    horizon = None

    class Tracer(RecordingRunTracer):
        async def on_event(self, event):
            await super().on_event(event)
            if (
                isinstance(event, StepEnd)
                and event.output is not None
                and isinstance(event.output.value, dict)
                and event.output.value.get("type") == "tool_result"
                and event.output.value.get("tool_call_id") == "before"
            ):
                assert horizon is not None
                harness.store.accept_compact_control(
                    run_id=StepRef.parse(event.step).run_id,
                    horizon=horizon,
                    triggered_by=StepRef.parse(event.step),
                    created_at=event.finished_at,
                )

    tracer = Tracer()
    harness.intercept_events(tracer)

    async def scenario():
        nonlocal horizon
        async with (
            harness,
            make_client(server) as alice,
            make_client(server, "human:owner") as human,
        ):
            await prepare(alice, human)
            document = await human.set_rules(
                "gc_00000001", "Current source policy.", revision="0"
            )
            scope = ConversationScope(alice.config.identity, document.conversation)
            thread = harness.threads.create(prefix=ThreadPrefix.TALK)
            spec = replace(
                harness.run_spec(thread=thread, runnable="agic:operator"),
                conversation=scope,
            )
            first = await harness.executor.run(spec)
            retained = await harness.executor.run(spec)
            horizon = project_compaction(
                harness.store,
                thread=thread,
                begin=first.id,
                end=retained.id,
                summary="Lossy summary of earlier policy.",
            )
            run = await harness.executor.run(spec, tracer=tracer)
            assert run.status == "succeeded", run.error
            guidance = [
                message
                for message in harness.adapter.invocations[-1].call.messages
                if message.tag == "convo-rules"
            ]
            assert any(
                message.recall.revision == document.revision for message in guidance
            )
            assert any(
                "Current source policy." in message_text(message.parts)
                for message in guidance
            )

    asyncio.run(scenario())


def test_explicit_rerun_preserves_source_scope_without_another_batch_receipt(
    tmp_path, server
):
    harness = harness_with_msg(
        tmp_path,
        server,
        [
            RuntimeError("Temporary provider failure"),
            ModelCallResult(
                message=Message.assistant("Processed with current guidance.")
            ),
        ],
    )

    async def scenario():
        async with (
            harness,
            make_client(server) as alice,
            make_client(server, "human:owner") as human,
        ):
            await prepare(alice, human)
            document = await human.set_rules(
                "gc_00000001", "Initial policy.", revision="0"
            )
            source = await human.send(
                document.conversation, body="Handle this request."
            )
            await make_loop(tmp_path, alice, harness).poll()
            (failed,) = harness.store.list_runs()
            assert failed.status == "failed"
            current = await human.set_rules(
                document.conversation, "Current policy.", revision=document.revision
            )
            handle = harness.executor.rerun(
                failed.id, setup=harness.setup, state=harness.state
            )
            record = await handle
            assert record.status == "succeeded", record.error
            entry = harness.store.get_run_control(run_id=record.id, index=0)
            assert entry is not None and isinstance(entry.payload, RunControlPayload)
            assert entry.payload.conversation == ConversationScope(
                alice.config.identity, document.conversation
            )
            guidance = [
                message
                for message in harness.adapter.invocations[-1].call.messages
                if message.tag == "convo-rules"
            ]
            assert any(
                message.recall.revision == current.revision for message in guidance
            )
            assert (
                harness.store._conn.execute(
                    "SELECT COUNT(*) FROM talk_batches"
                ).fetchone()[0]
                == 1
            )
            assert await alice.talk_cursor(document.conversation) == source["stream_id"]

    asyncio.run(scenario())
