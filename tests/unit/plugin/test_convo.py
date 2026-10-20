"""Conversation tools exercise the real Hub contract on an offline backend."""

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import patch

from fakeredis import FakeServer
import pytest

from tests.unit.messaging.test_agent_api import CONNECTION, agent
from tests.unit.messaging.test_protocol import client
from tests.support.execution_fixtures import project_run_start
from toolang.base.errors import ToolangError
from toolang.base.types.message import Message as RunMessage, ToolResultPart
from toolang.base.types.run import ToolCall
from toolang.base.types.tool import ConvoToolContext
from toolang.execution.store import RunStore
from toolang.execution.types import Output, StepRef, ToolStepSpec, ToolStepReport
from toolang.plugin.toolsets.convo import ConvoToolset
from toolang.teaming.agent_client import AgentClient
from toolang.teaming.api import create_app
from toolang.teaming.errors import MessagingError, SendUnconfirmed
from toolang.teaming.schemas import Message


@asynccontextmanager
async def environment(tmp_path, peer="bob"):
    human = client(FakeServer(server_type="valkey"), CONNECTION.human)
    app = create_app(human, version="test")
    context = ConvoToolContext(
        tmp_path / "alice", tmp_path, run_id="run_1", thread_id="chat_1"
    )
    async with (
        app.router.lifespan_context(app),
        agent(app, tmp_path) as alice,
        agent(app, tmp_path, f"agent:{peer}", "peer-lease") as bob,
    ):
        await alice.register(CONNECTION.human)
        await bob.register(CONNECTION.human)
        toolset = ConvoToolset({"root": str(tmp_path)})
        with patch.object(toolset, "connection", lambda _: agent(app, tmp_path)):
            yield toolset.tools(), context, alice, bob


def test_team_tool_projects_member_colors_out_of_its_existing_shape(tmp_path):
    async def scenario():
        async with environment(tmp_path) as (tools, context, alice, _):
            assert all(row.get("color") for row in await alice.team())
            result = await tools["team"].invoke({}, context)
            assert result.error is None
            assert {row["id"] for row in result.output["value"]} == {
                CONNECTION.human,
                "agent:alice",
                "agent:bob",
            }
            assert all(
                set(row) == {"id", "name", "online"} for row in result.output["value"]
            )

    asyncio.run(scenario())


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


@pytest.mark.parametrize(
    "operation,limit", [("peek", 0), ("peek", 50), ("create", 0), ("rename", 0)]
)
def test_conversation_metadata_results_persist_as_completed_tool_steps(
    tmp_path, operation, limit
):
    async def scenario():
        async with environment(tmp_path) as (tools, context, alice, _):
            group = await alice.create_conversation("Original")
            await alice.send(group.id, body="History")
            arguments = (
                {"target": group.id, "limit": limit}
                if operation == "peek"
                else {"name": "Created"}
                if operation == "create"
                else {
                    "conversation": group.id,
                    "name": "Renamed",
                    "revision": group.revision,
                }
            )
            result = await tools[operation].invoke(arguments, context)
            assert result.error is None
            name = f"convo__{operation}"
            call = ToolCall("call_metadata", "call_metadata", name, arguments)
            part = ToolResultPart(
                "call_metadata",
                name,
                name,
                dict(result.output),
                call_id="call_metadata",
            )
            store = RunStore(tmp_path / "runs.db")
            try:
                run = project_run_start(
                    store,
                    run_id="run_metadata",
                    thread_id="chat_metadata",
                    input=RunMessage.user("Read metadata"),
                )
                ref = StepRef.from_local(run.id, (0,))
                store.begin_step(
                    ref=ref,
                    kind="tool",
                    input=(),
                    spec=ToolStepSpec("convo", call),
                    started_at="2026-10-13T00:00:00Z",
                )
                finished = store.finish_step(
                    ref=ref,
                    kind="tool",
                    status="succeeded",
                    output=Output(part),
                    report=ToolStepReport("Completed"),
                    error=None,
                    finished_at="2026-10-13T00:00:01Z",
                )
                assert finished.status == "succeeded"
                assert finished.output == Output(part)
                reopened = RunStore(store.db_path, read_only=True)
                try:
                    assert reopened.get_step(ref=ref) == finished
                finally:
                    reopened.close()
            finally:
                store.close()

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


@pytest.mark.parametrize("name", ["gc_zzzzzzzz", "dm_zzzzzzzz"])
@pytest.mark.parametrize("operation", ["write", "peek"])
def test_conversation_ids_take_precedence_over_registered_agent_names(
    tmp_path, name, operation
):
    async def scenario():
        async with environment(tmp_path, peer=name) as (tools, context, alice, _):
            dm = await alice.create_conversation(
                participants=["agent:alice", f"agent:{name}"]
            )
            await tools["write"].invoke(
                {"target": dm.id, "message": "explicit ID works"}, context
            )
            args = {"target": name}
            if operation == "write":
                args["message"] = "must not reach the agent"
            with pytest.raises(MessagingError):
                await tools[operation].invoke(args, context)
            assert len(await alice.history(dm.id)) == 1

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


def test_rename_preserves_a_concurrent_change_without_retrying(tmp_path):
    async def scenario():
        async with environment(tmp_path) as (tools, context, alice, bob):
            group = await alice.create_conversation("before")
            await bob.join_conversation(group.id)
            original = AgentClient.conversation

            async def read_then_race(client, conversation):
                info = await original(client, conversation)
                await bob.rename_conversation(
                    conversation, "concurrent", revision=info.revision
                )
                return info

            with patch.object(AgentClient, "conversation", read_then_race):
                with pytest.raises(MessagingError, match="revision"):
                    await tools["rename"].invoke(
                        {
                            "conversation": group.id,
                            "name": "must not overwrite",
                            "revision": (await original(alice, group.id)).revision,
                        },
                        context,
                    )
            current = await alice.conversation(group.id)
            assert current.name == "concurrent"

    asyncio.run(scenario())


def test_rename_read_failure_never_attempts_a_mutation(tmp_path):
    async def scenario():
        async with environment(tmp_path) as (tools, context, alice, _):
            group = await alice.create_conversation("before")
            with (
                patch.object(
                    AgentClient,
                    "conversation",
                    side_effect=MessagingError("unavailable"),
                ),
                patch.object(AgentClient, "rename_conversation") as rename,
            ):
                with pytest.raises(MessagingError, match="unavailable"):
                    await tools["rename"].invoke(
                        {
                            "conversation": group.id,
                            "name": "after",
                            "revision": group.revision,
                        },
                        context,
                    )
                rename.assert_not_awaited()
            assert (await alice.conversation(group.id)).name == "before"

    asyncio.run(scenario())


@pytest.mark.parametrize("limit", [0, 50])
def test_peek_cannot_read_another_agents_dm(tmp_path, limit):
    async def scenario():
        async with environment(tmp_path) as (tools, context, _, bob):
            private = await bob.create_conversation(
                participants=["agent:bob", "human:owner"]
            )
            await bob.send(private.id, body="private message")
            with pytest.raises(MessagingError):
                await tools["peek"].invoke(
                    {"target": private.id, "limit": limit}, context
                )

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
