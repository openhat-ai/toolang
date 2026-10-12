"""Conversation tools exercise the real Hub contract on an offline backend."""

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import patch

from fakeredis import FakeServer
import pytest

from tests.unit.messaging.test_agent_api import CONNECTION, agent
from tests.unit.messaging.test_protocol import client
from toolang.base.errors import ToolangError
from toolang.base.types.tool import ConvoToolContext
from toolang.plugin.toolsets.convo import ConvoToolset
from toolang.teaming.api import create_app
from toolang.teaming.errors import MessagingError, SendUnconfirmed
from toolang.teaming.schemas import Message


@asynccontextmanager
async def environment(tmp_path):
    human = client(FakeServer(server_type="valkey"), CONNECTION.human)
    app = create_app(human, version="test")
    context = ConvoToolContext(
        tmp_path / "alice", tmp_path, run_id="run_1", thread_id="chat_1"
    )
    async with (
        app.router.lifespan_context(app),
        agent(app, tmp_path) as alice,
        agent(app, tmp_path, "agent:bob", "bob-lease") as bob,
    ):
        await alice.register(CONNECTION.human)
        await bob.register(CONNECTION.human)
        toolset = ConvoToolset({"root": str(tmp_path)})
        with patch.object(toolset, "connection", lambda _: agent(app, tmp_path)):
            yield toolset.tools(), context, alice, bob


def test_write_peek_and_reply_preserve_application_ids_and_origin(tmp_path):
    async def scenario():
        async with environment(tmp_path) as (tools, context, alice, bob):
            before = await alice.resolve("agent:bob")
            assert not before.exists
            with pytest.raises(MessagingError):
                await tools["peek"].invoke({"target": "bob"}, context)
            assert not (await alice.resolve("agent:bob")).exists
            first = (
                await tools["write"].invoke(
                    {"target": "bob", "message": "hello"}, context
                )
            ).output
            assert set(first) == {"conversation", "message"}
            assert set(first["message"]) == {"id", "sender", "message", "reply_to"}
            assert first["conversation"] == before.conversation
            original = Message.decode(
                (await bob.history(before.conversation))[0][1]["data"]
            )
            assert original.id == first["message"]["id"]
            assert original.origin == {"run": "run_1", "thread": "chat_1"}
            second = (
                await tools["write"].invoke(
                    {
                        "target": before.conversation,
                        "message": "follow up",
                        "reply_to": original.id,
                    },
                    context,
                )
            ).output
            assert second["message"]["reply_to"] == original.id
            peeked = (await tools["peek"].invoke({"target": "bob"}, context)).output
            assert peeked["messages"] == [first["message"], second["message"]]
            assert (
                await tools["peek"].invoke({"target": "bob", "limit": 1}, context)
            ).output["messages"] == [second["message"]]
            metadata = (
                await tools["peek"].invoke({"target": "bob", "limit": 0}, context)
            ).output
            assert (
                metadata["messages"] == []
                and metadata["conversation"]["id"] == before.conversation
            )
            # Peeking is independent of the handler's read cursor.
            assert await alice.read(
                before.conversation, after="0-0"
            ) == await alice.history(before.conversation)

    asyncio.run(scenario())


def test_team_and_conversation_filters_are_small_exact_and_caller_explicit(tmp_path):
    async def scenario():
        async with environment(tmp_path) as (tools, context, alice, bob):
            dm = await alice.create_conversation(
                "dev", participants=["agent:alice", "agent:bob"]
            )
            group = await alice.create_conversation("dev")
            await bob.join_conversation(group.id)
            team = (await tools["team"].invoke({}, context)).output["value"]
            assert all(set(row) == {"id", "name", "online"} for row in team)
            assert {r["name"] for r in team} == {"alice", "bob", "owner"}
            assert next(r for r in team if r["id"] == "human:owner")["online"] is None

            async def query(**args):
                return (await tools["list"].invoke(args, context)).output["value"]

            rows = await query(name="dev", members=["bob"])
            assert {r["id"] for r in rows} == {dm.id, group.id}
            assert all(set(r) == {"id", "name", "members"} for r in rows)
            assert await query(name="dev", members=["bob"], exact=True) == []
            assert await query(name="Dev") == []
            assert await query(members=["unknown"]) == []
            exact = await query(
                name="dev", members=["bob", "agent:alice", "bob"], exact=True
            )
            assert exact == rows
            assert await query()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "operation,args",
    [
        ("list", {"members": []}),
        ("list", {"exact": True}),
        ("list", {"members": ["bad:member"]}),
        ("peek", {"target": "bob", "limit": -1}),
        ("peek", {"target": "bob", "limit": 1001}),
        ("peek", {"target": "bob", "limit": True}),
        ("write", {"target": "bob", "message": "x", "reply_to": "1-0"}),
    ],
)
def test_invalid_filters_limits_and_reply_ids_fail(tmp_path, operation, args):
    async def scenario():
        async with environment(tmp_path) as (tools, context, *_):
            with pytest.raises((MessagingError, ToolangError)):
                await tools[operation].invoke(args, context)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "target",
    [
        "alice",
        "agent:bob",
        "human:owner",
        "agent:alice,agent:bob",
        "unknown",
        "gc_zzzzzzzz",
        "dm_zzzzzzzz",
    ],
)
@pytest.mark.parametrize("operation", ["write", "peek"])
def test_targets_never_fall_back_to_names_or_self(tmp_path, target, operation):
    async def scenario():
        async with environment(tmp_path) as (tools, context, alice, _):
            await alice.create_conversation(target)
            args = {"target": target}
            if operation == "write":
                args["message"] = "not delivered"
            with pytest.raises(MessagingError):
                await tools[operation].invoke(args, context)

    asyncio.run(scenario())


def test_rename_reports_captured_name_and_rejects_stale_revision(tmp_path):
    async def scenario():
        async with environment(tmp_path) as (tools, context, alice, _):
            group = await alice.create_conversation("before")
            result = (
                await tools["rename"].invoke(
                    {
                        "conversation": group.id,
                        "name": "after",
                        "revision": group.revision,
                    },
                    context,
                )
            ).output
            assert result["previous_name"] == "before" and result["name"] == "after"
            with pytest.raises(MessagingError, match="revision"):
                await tools["rename"].invoke(
                    {
                        "conversation": group.id,
                        "name": None,
                        "revision": group.revision,
                    },
                    context,
                )
            assert (await alice.conversation(group.id)).name == "after"

    asyncio.run(scenario())


def test_peek_metadata_skips_history_and_malformed_messages_fail(tmp_path):
    async def scenario():
        async with environment(tmp_path) as (tools, context, alice, _):
            group = await alice.create_conversation()
            with patch(
                "toolang.teaming.agent_client.AgentClient.history",
                side_effect=AssertionError("history called"),
            ):
                assert (
                    await tools["peek"].invoke(
                        {"target": group.id, "limit": 0}, context
                    )
                ).output["messages"] == []
            with patch(
                "toolang.teaming.agent_client.AgentClient.history",
                return_value=[("1-0", {"data": "{}"})],
            ):
                with pytest.raises(MessagingError):
                    await tools["peek"].invoke({"target": group.id}, context)

    asyncio.run(scenario())


def test_uncertain_write_is_not_retried(tmp_path):
    async def scenario():
        async with environment(tmp_path) as (tools, context, *_):
            with patch(
                "toolang.teaming.agent_client.AgentClient.send",
                side_effect=SendUnconfirmed("uncertain"),
            ) as send:
                with pytest.raises(SendUnconfirmed):
                    await tools["write"].invoke(
                        {"target": "bob", "message": "once"}, context
                    )
                assert send.call_count == 1

    asyncio.run(scenario())
