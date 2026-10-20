"""Batch receipts survive cursor failures and interrupted execution admission."""

import asyncio
from contextlib import closing
import json
from unittest.mock import AsyncMock

from fakeredis import FakeServer
import pytest

from tests.support.execution_harness import AsyncGate, ScriptedModelTurn
from tests.unit.messaging.test_loop import (
    harness_with_msg,
    make_client,
    make_loop,
    prepare,
)
from toolang.base.types.message import Message as ModelMessage
from toolang.base.types.run import ModelCallResult, ToolCall
from toolang.execution.executor import conversations
from toolang.execution.executor.executor import _Execution
from toolang.execution.store import RunStore
from toolang.execution.types import ConversationScope, ThreadKind, ThreadRef
from toolang.lang.input import CallInput
from toolang.lang.types import Array
from toolang.teaming.errors import BackendUnavailable
from toolang.teaming.ids import talk_thread_id
from toolang.teaming.schemas import Message

pytestmark = pytest.mark.usefixtures("fixed_conversation_ids")


@pytest.fixture
def server(monkeypatch):
    server = FakeServer(server_type="valkey")
    monkeypatch.setattr(
        conversations, "AgentClient", lambda root, actor: make_client(server, actor)
    )
    return server


@pytest.mark.parametrize("operation", ["fork", "rewind"])
def test_thread_controls_preserve_talk_binding_receipt_and_cursor(
    tmp_path, server, operation
):
    harness = harness_with_msg(
        tmp_path,
        server,
        [
            ModelCallResult(message=ModelMessage.assistant("First handled.")),
            ModelCallResult(message=ModelMessage.assistant("Second handled.")),
        ],
    )

    async def scenario():
        async with (
            harness,
            make_client(server) as alice,
            make_client(server, "human:owner") as human,
        ):
            await prepare(alice, human)
            first = await human.send("gc_00000001", body="First request.")
            await make_loop(tmp_path, alice, harness).poll()
            scope = ConversationScope(alice.config.identity, "gc_00000001")
            thread = harness.store.talk.binding(scope)
            assert thread == talk_thread_id("gc_00000001", agent_name="alice")
            receipt = harness.store.talk.get(scope, "0-0")
            assert receipt is not None
            assert receipt.batch.through == first["stream_id"]
            assert await alice.talk_cursor("gc_00000001") == first["stream_id"]

            forked = None
            if operation == "fork":
                forked = harness.threads.fork(thread_id=thread, run_id=receipt.run)
                assert forked != thread and ThreadRef(forked).kind == ThreadKind.TALK
            else:
                harness.threads.rewind(thread_id=thread, run_id=receipt.run)

            with closing(RunStore(harness.store.db_path, read_only=True)) as reopened:
                assert reopened.talk.binding(scope) == thread
                assert reopened.talk.get(scope, "0-0") == receipt
                assert reopened.get_run(run_id=receipt.run) is not None
                assert [
                    run.id
                    for run in reopened.list_thread_history_chronological(
                        thread_id=thread
                    )
                ] == ([receipt.run] if operation == "fork" else [])
            assert await alice.talk_cursor("gc_00000001") == first["stream_id"]

            restarted = make_loop(tmp_path, alice, harness)
            await restarted.poll()
            assert len(harness.adapter.invocations) == 1
            assert len(harness.store.list_runs()) == 1
            second = await human.send("gc_00000001", body="Second request.")
            await restarted.poll()
            assert await alice.talk_cursor("gc_00000001") == second["stream_id"]
            assert len(harness.adapter.invocations) == 2
            next_receipt = harness.store.talk.get(scope, first["stream_id"])
            assert next_receipt is not None and next_receipt.run != receipt.run
            next_run = harness.store.get_run(run_id=next_receipt.run)
            assert next_run is not None and next_run.thread.id == thread
            assert harness.store.talk.binding(scope) == thread
            assert harness.store.talk.get(scope, "0-0") == receipt
            if forked is not None:
                assert [
                    run.id
                    for run in harness.store.list_thread_history_chronological(
                        thread_id=forked
                    )
                ] == [receipt.run]
            else:
                assert [
                    run.id
                    for run in harness.store.list_thread_history_chronological(
                        thread_id=thread
                    )
                ] == [next_run.id]

    asyncio.run(scenario())


@pytest.mark.parametrize("status", ["pending", "running"])
def test_existing_live_batch_is_only_observed(tmp_path, server, monkeypatch, status):
    gate = AsyncGate()
    response = ModelCallResult(message=ModelMessage.assistant("Handled."))
    harness = harness_with_msg(
        tmp_path,
        server,
        [response if status == "pending" else ScriptedModelTurn(response, gate=gate)],
    )
    execute = _Execution.execute

    async def pause_startup(self, *args, **kwargs):
        await gate.wait()
        return await execute(self, *args, **kwargs)

    if status == "pending":
        monkeypatch.setattr(_Execution, "execute", pause_startup)

    async def scenario():
        async with (
            harness,
            make_client(server) as alice,
            make_client(server, "human:owner") as human,
        ):
            await prepare(alice, human)
            source = await human.send("gc_00000001", body="Still being handled.")
            handling = asyncio.create_task(make_loop(tmp_path, alice, harness).poll())
            try:
                await asyncio.wait_for(gate.wait_until_entered(), 2)
                (before,) = harness.store.list_runs()
                assert before.status == status
                with harness.executor.stream.boundary() as (_, _, frames):
                    before_events = frames
                await make_loop(tmp_path, alice, harness).poll()
                assert harness.store.get_run(run_id=before.id) == before
                with harness.executor.stream.boundary() as (_, _, frames):
                    assert frames == before_events
                assert await alice.talk_cursor("gc_00000001") == "0-0"
                messages = [
                    Message.decode(fields["data"])
                    for _, fields in await alice.history("gc_00000001")
                ]
                assert not any(message.handler_error for message in messages)
            finally:
                gate.release()
                await asyncio.wait_for(handling, 2)
            assert await alice.talk_cursor("gc_00000001") == source["stream_id"]
            assert len(harness.adapter.invocations) == 1
            assert len(harness.store.list_runs()) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("saved", [False, True])
def test_cursor_save_failure_and_lost_ack_do_not_repeat_dispatch(
    tmp_path, server, monkeypatch, saved
):
    harness = harness_with_msg(
        tmp_path,
        server,
        [
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        "reply",
                        "reply",
                        "convo__write",
                        {"target": "gc_00000001", "message": "Confirmed."},
                    ),
                )
            ),
            ModelCallResult(message=ModelMessage.assistant("Handled.")),
            ModelCallResult(message=ModelMessage.assistant("Next batch.")),
        ],
    )

    async def scenario():
        async with (
            harness,
            make_client(server) as alice,
            make_client(server, "human:owner") as human,
        ):
            await prepare(alice, human)
            source = await human.send("gc_00000001", body="First request.")
            advance = alice.advance_talk_cursor

            async def interrupted(conversation, cursor):
                if saved:
                    await advance(conversation, cursor)
                raise BackendUnavailable("cursor acknowledgment lost")

            with monkeypatch.context() as patch:
                patch.setattr(alice, "advance_talk_cursor", interrupted)
                await make_loop(tmp_path, alice, harness).poll()
            assert len(harness.adapter.invocations) == 2
            scope = ConversationScope(alice.config.identity, "gc_00000001")
            receipt = harness.store.talk.get(scope, "0-0")
            assert receipt is not None and receipt.batch.through == source["stream_id"]
            await make_loop(tmp_path, alice, harness).poll()
            assert len(harness.adapter.invocations) == 2
            await human.send("gc_00000001", body="Second request.")
            await make_loop(tmp_path, alice, harness).poll()
            assert len(harness.adapter.invocations) == 3
            assert (
                len(harness.store.list_runs()) == 2
                and len(harness.store.list_threads()) == 1
            )
            messages = [
                Message.decode(fields["data"])
                for _, fields in await alice.history("gc_00000001")
            ]
            assert sum(message.body == "Confirmed." for message in messages) == 1
            original = json.loads(receipt.batch.payload)
            assert [
                message["id"] for message in original["conversations"][0]["messages"]
            ] == [source["message"]["id"]]

    asyncio.run(scenario())


def test_rule_read_failure_retains_the_trigger_without_admitting_work(
    tmp_path, server, monkeypatch
):
    harness = harness_with_msg(
        tmp_path, server, [ModelCallResult(message=ModelMessage.assistant("Handled."))]
    )

    async def scenario():
        async with (
            harness,
            make_client(server) as alice,
            make_client(server, "human:owner") as human,
        ):
            await prepare(alice, human)
            source = await human.send("gc_00000001", body="Keep queued.")
            with monkeypatch.context() as patch:
                patch.setattr(
                    alice,
                    "get_rules",
                    AsyncMock(side_effect=BackendUnavailable("offline")),
                )
                await make_loop(tmp_path, alice, harness).poll()
            assert await alice.talk_cursor("gc_00000001") == "0-0"
            assert not harness.store.list_runs() and not harness.store.list_threads()
            assert len(await alice.history("gc_00000001")) == 1
            await make_loop(tmp_path, alice, harness).poll()
            assert await alice.talk_cursor("gc_00000001") == source["stream_id"]
            assert len(harness.adapter.invocations) == 1

    asyncio.run(scenario())


def test_existing_unbound_talk_id_is_not_adopted(tmp_path, server):
    harness = harness_with_msg(tmp_path, server, [])

    async def scenario():
        async with (
            harness,
            make_client(server) as alice,
            make_client(server, "human:owner") as human,
        ):
            await prepare(alice, human)
            thread = talk_thread_id("gc_00000001", agent_name="alice")
            harness.store.create_thread(thread_id=thread)
            before = harness.store.get_thread(thread_id=thread)
            await human.send("gc_00000001", body="Do not mix histories.")
            await make_loop(tmp_path, alice, harness).poll()
            assert harness.store.get_thread(thread_id=thread) == before
            assert not harness.store.list_runs()
            assert (
                harness.store.talk.binding(
                    ConversationScope(alice.config.identity, "gc_00000001")
                )
                is None
            )
            assert await alice.talk_cursor("gc_00000001") == "0-0"

    asyncio.run(scenario())


@pytest.mark.parametrize("control", ["steer", "cancel"])
@pytest.mark.parametrize("stage", ["task", "monitor"])
def test_startup_failure_finishes_before_talk_advances_the_batch(
    tmp_path, server, monkeypatch, control, stage
):
    harness = harness_with_msg(
        tmp_path,
        server,
        [ModelCallResult(message=ModelMessage.assistant("Handled new request."))],
    )
    controls = []
    create_task = asyncio.create_task

    def failed_startup(*args, **kwargs):
        (run,) = harness.store.list_runs()
        controls.append(
            harness.store.accept_run_control(
                run_id=run.id,
                kind=control,
                timing="next_call",
                input=CallInput(
                    {"_": Array("Part[]", ModelMessage.user("Pending direction").parts)}
                )
                if control == "steer"
                else CallInput(),
                request_id="waiting-control",
                created_at=run.created_at,
            )
        )
        raise RuntimeError("task startup failed")

    def failed_task(coro, **kwargs):
        if coro.cr_code.co_name == "_execute_owned":
            failed_startup()
        return create_task(coro, **kwargs)

    async def scenario():
        async with (
            harness,
            make_client(server) as alice,
            make_client(server, "human:owner") as human,
        ):
            await prepare(alice, human)
            source = await human.send("gc_00000001", body="Original request.")
            with monkeypatch.context() as patch:
                if stage == "task":
                    patch.setattr(asyncio, "create_task", failed_task)
                else:
                    patch.setattr(harness.executor, "_ensure_monitor", failed_startup)
                await make_loop(tmp_path, alice, harness).poll()
            (failed,) = harness.store.list_runs()
            assert failed.status == "failed"
            assert harness.store.resolve_error(failed.error) == "task startup failed"
            assert await alice.talk_cursor("gc_00000001") == source["stream_id"]
            assert (
                not harness.adapter.invocations
                and not harness.executor.has_owned_work()
            )
            (pending,) = controls
            final = harness.store.get_run_control(run_id=failed.id, index=pending.index)
            assert final is not None and final.status == "wontapply"
            assert final.error == "run ended before the control could be applied"
            assert final.finished_at == failed.finished_at
            receipt = harness.store.talk.get(
                ConversationScope(alice.config.identity, "gc_00000001"), "0-0"
            )
            assert receipt is not None and receipt.run == failed.id
            # A new driver consumes the failure notice without dispatching the batch again.
            loop = make_loop(tmp_path, alice, harness)
            await loop.poll()
            assert not harness.adapter.invocations
            assert harness.store.get_run(run_id=failed.id) == failed
            await human.send("gc_00000001", body="Newer request.")
            await loop.poll()
            assert len(harness.adapter.invocations) == 1
            assert (
                len(harness.store.list_runs()) == 2
                and len(harness.store.list_threads()) == 1
            )
            messages = [
                Message.decode(fields["data"])
                for _, fields in await alice.history("gc_00000001")
            ]
            assert sum(message.handler_error for message in messages) == 1

    asyncio.run(scenario())
