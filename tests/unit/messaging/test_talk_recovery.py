"""Batch receipts survive cursor failures and interrupted execution admission."""

import asyncio
import json
from unittest.mock import AsyncMock

from fakeredis import FakeServer
import pytest

from tests.unit.messaging.test_loop import (
    harness_with_msg,
    make_client,
    make_loop,
    prepare,
)
from toolang.base.types.message import Message as ModelMessage
from toolang.base.types.run import ModelCallResult, ToolCall
from toolang.execution.executor import conversations
from toolang.execution.types import ConversationScope
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
            harness.store.create_thread(thread_id=thread, origin="work")
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
@pytest.mark.parametrize("stage", ["dispatch", "monitor"])
def test_startup_failure_finishes_before_talk_advances_the_batch(
    tmp_path, server, monkeypatch, control, stage
):
    harness = harness_with_msg(
        tmp_path,
        server,
        [ModelCallResult(message=ModelMessage.assistant("Handled new request."))],
    )
    controls = []

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

    async def scenario():
        async with (
            harness,
            make_client(server) as alice,
            make_client(server, "human:owner") as human,
        ):
            await prepare(alice, human)
            source = await human.send("gc_00000001", body="Original request.")
            with monkeypatch.context() as patch:
                patch.setattr(
                    harness.executor,
                    "_launch" if stage == "dispatch" else "_ensure_monitor",
                    failed_startup,
                )
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
