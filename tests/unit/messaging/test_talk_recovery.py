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


def test_restart_closes_admitted_orphan_without_rebuilding_its_range(
    tmp_path, server, monkeypatch
):
    first = harness_with_msg(tmp_path, server, [])

    async def scenario():
        async with (
            make_client(server) as alice,
            make_client(server, "human:owner") as human,
        ):
            await prepare(alice, human)
            source = await human.send("gc_00000001", body="Original request.")
            async with first:

                def failed_launch(*args, **kwargs):
                    raise RuntimeError("process interrupted after durable admission")

                with monkeypatch.context() as patch:
                    patch.setattr(first.executor, "_launch", failed_launch)
                    await make_loop(tmp_path, alice, first).poll()
                (orphan,) = first.store.list_runs()
                assert orphan.status == "pending"
                assert await alice.talk_cursor("gc_00000001") == "0-0"
            await human.send("gc_00000001", body="Newer request.")
            restarted = harness_with_msg(
                tmp_path,
                server,
                [
                    ModelCallResult(
                        message=ModelMessage.assistant("Handled new request.")
                    )
                ],
            )
            async with restarted:
                loop = make_loop(tmp_path, alice, restarted)
                await loop.poll()
                recovered = restarted.store.get_run(run_id=orphan.id)
                assert recovered is not None and recovered.status == "failed"
                assert await alice.talk_cursor("gc_00000001") == source["stream_id"]
                assert not restarted.adapter.invocations
                await loop.poll()
                assert len(restarted.adapter.invocations) == 1
                assert (
                    len(restarted.store.list_runs()) == 2
                    and len(restarted.store.list_threads()) == 1
                )
                messages = [
                    Message.decode(fields["data"])
                    for _, fields in await alice.history("gc_00000001")
                ]
                assert sum(message.handler_error for message in messages) == 1

    asyncio.run(scenario())
