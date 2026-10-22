"""Atomic shared rules, observer access, and revision-guarded delivery."""

import asyncio
from hashlib import sha256
import json

from fakeredis import FakeServer
import httpx
import pytest

from tests.unit.messaging.test_protocol import CONFIG, client, snapshot
from toolang.teaming.api import create_app
from tests.unit.messaging.keys import convo_key
from toolang.teaming.client import HubClient
from toolang.teaming.errors import (
    ConversationAccessDenied,
    ConversationRulesConflict,
    LeaseLost,
    MessagingError,
    StorageIntegrityError,
)
from toolang.teaming.schemas import ConversationRules, HubConnection


@pytest.mark.parametrize("operation", ["preflight", "tool"])
def test_rules_reads_pin_backend_across_target_resolution(
    tmp_path, monkeypatch, operation
):
    from toolang.base.types.tool import ToolContext
    from toolang.common.layout import AgentLayout
    from toolang.execution.executor import conversations
    from toolang.plugin.toolsets.convo import ConvoToolset
    from toolang.teaming.agent_client import AgentClient

    connections = [
        HubConnection(f"http://hub{index}", "human:owner", f"backend-{index}")
        for index in range(2)
    ]
    current = 0
    requests = []

    async def dispatch(request):
        nonlocal current
        requests.append(request)
        if request.url.path.endswith("/resolve"):
            current = 1
            return httpx.Response(
                200,
                json={
                    "conversation": "gc_00000001",
                    "participants": [],
                    "exists": True,
                },
            )
        return httpx.Response(
            200,
            json=ConversationRules.create(
                "gc_00000001", "Backend zero policy."
            ).model_dump(),
        )

    def connection(root, *, actor):
        return AgentClient(
            root,
            actor=actor,
            token="lease",
            connection=lambda: connections[current],
            transport=httpx.MockTransport(dispatch),
        )

    async def scenario():
        if operation == "preflight":
            monkeypatch.setattr(conversations, "AgentClient", connection)
            access = await conversations.load_target(
                AgentLayout.resident(tmp_path, "alice"), "gc_00000001"
            )
            assert access.scope.backend == connections[0].identity
        else:
            toolset = ConvoToolset({"root": str(tmp_path)})
            monkeypatch.setattr(
                toolset,
                "connection",
                lambda context: connection(tmp_path, actor="agent:alice"),
            )
            result = await toolset.tools()["get_rules"].invoke(
                {"target": "gc_00000001"}, ToolContext(tmp_path / "alice", tmp_path)
            )
            assert result.error is None
        assert len(requests) == 2
        assert all(
            request.url.host == "hub0"
            and request.headers["X-Toolang-Backend"] == connections[0].identity
            for request in requests
        )

    asyncio.run(scenario())


def test_rules_exact_content_observer_access_clear_and_stale_noop():
    async def scenario():
        server = FakeServer(server_type="valkey")
        async with (
            client(server, "human:owner") as human,
            client(server, "agent:alice") as alice,
        ):
            await alice.register("human:owner")
            conversation = (await alice.create_conversation("rules")).id
            assert (await human.get_rules(conversation)).revision == "0"
            content = "# Members\r\n\nKeep literal {{input}} and café.\n"
            document = await human.set_rules(conversation, content, revision="0")
            assert document == await alice.get_rules(conversation)
            assert document.content.encode() == content.encode()
            assert (
                "human:owner"
                not in (await human.conversation(conversation)).participants
            )
            before = await snapshot(human.backend._client)
            assert (
                await alice.set_rules(conversation, content, revision=document.revision)
                == document
            )
            assert before == await snapshot(human.backend._client)
            with pytest.raises(ConversationRulesConflict):
                await alice.set_rules(conversation, content, revision="0")
            assert before == await snapshot(human.backend._client)
            cleared = await human.set_rules(
                conversation, "", revision=document.revision
            )
            assert cleared == ConversationRules.create(conversation)
            assert await alice.get_rules(conversation) == cleared

    asyncio.run(scenario())


def test_rules_cas_has_one_winner_and_access_and_lease_are_checked():
    async def scenario():
        server = FakeServer(server_type="valkey")
        async with (
            client(server, "human:owner") as human,
            client(server, "agent:alice") as alice,
            client(server, "agent:bob") as bob,
        ):
            await alice.register("human:owner")
            await bob.register("human:owner")
            conversation = (await alice.create_conversation()).id
            for operation in (
                bob.get_rules(conversation),
                bob.set_rules(conversation, "no", revision="0"),
            ):
                with pytest.raises(ConversationAccessDenied):
                    await operation
            results = await asyncio.gather(
                human.set_rules(conversation, "first", revision="0"),
                alice.set_rules(conversation, "second", revision="0"),
                return_exceptions=True,
            )
            assert sum(isinstance(result, ConversationRules) for result in results) == 1
            assert (
                sum(isinstance(result, ConversationRulesConflict) for result in results)
                == 1
            )
            current = await human.get_rules(conversation)
            await alice.unregister()
            with pytest.raises(LeaseLost):
                await alice.set_rules(
                    conversation, "stale owner", revision=current.revision
                )

    asyncio.run(scenario())


def test_guarded_append_conflicts_before_first_dm_or_message_mutation():
    async def scenario():
        server = FakeServer(server_type="valkey")
        async with (
            client(server, "human:owner") as human,
            client(server, "agent:alice") as alice,
        ):
            await alice.register("human:owner")
            pending = await alice.resolve("human:owner")
            with pytest.raises(MessagingError, match="Unknown conversation"):
                await alice.get_rules(pending.conversation)
            before = await snapshot(human.backend._client)
            with pytest.raises(ConversationRulesConflict):
                await alice.send(
                    "human:owner", body="must not create", rules_revision="a" * 64
                )
            assert before == await snapshot(human.backend._client)
            assert not (await alice.resolve("human:owner")).exists
            receipt = await alice.send("human:owner", body="first", rules_revision="0")
            conversation = receipt["conversation"]
            document = await human.set_rules(
                conversation, "Reply briefly.", revision="0"
            )
            before = await snapshot(human.backend._client)
            with pytest.raises(ConversationRulesConflict):
                await alice.send(conversation, body="stale", rules_revision="0")
            assert before == await snapshot(human.backend._client)
            await alice.send(
                conversation, body="current", rules_revision=document.revision
            )
            assert len(await alice.history(conversation)) == 2

    asyncio.run(scenario())


@pytest.mark.parametrize("raw", ["broken", json.dumps({"content": "hidden"}), None])
def test_corrupt_rules_are_not_missing(raw):
    async def scenario():
        server = FakeServer(server_type="valkey")
        async with client(server, "human:owner") as human:
            conversation = (await human.create_conversation()).id
            key = convo_key(conversation, "rules")
            if raw is None:
                await human.backend._client.set(key, "wrong key type")
            else:
                await human.backend._client.hset(key, "document", raw)
            before = await snapshot(human.backend._client)
            with pytest.raises(StorageIntegrityError):
                await human.get_rules(conversation)
            with pytest.raises(StorageIntegrityError):
                await human.set_rules(conversation, "replacement", revision="0")
            assert before == await snapshot(human.backend._client)

    asyncio.run(scenario())


def test_hub_rules_api_and_validation():
    async def scenario():
        server = FakeServer(server_type="valkey")
        human = client(server, "human:owner")
        app = create_app(human, version="test")
        connection = HubConnection("http://hub", "human:owner", CONFIG.identity)
        async with (
            app.router.lifespan_context(app),
            HubClient(connection, transport=httpx.ASGITransport(app)) as hub,
        ):
            conversation = (await hub.create_conversation()).id
            document = await hub.set_rules(
                conversation, "é" * (32 * 1024), revision="0"
            )
            assert await hub.get_rules(conversation) == document
            with pytest.raises(ConversationRulesConflict):
                await hub.set_rules(conversation, "old", revision="0")
            with pytest.raises(ValueError):
                await hub.set_rules(
                    conversation, "é" * (32 * 1024 + 1), revision=document.revision
                )
            with pytest.raises(ValueError):
                await hub.set_rules(conversation, "bad", revision="latest")
            with pytest.raises(MessagingError, match="Unknown conversation"):
                await hub.get_rules("gc_00000000")
            assert (
                await hub.set_rules(conversation, "", revision=document.revision)
            ).revision == "0"

    asyncio.run(scenario())


@pytest.mark.parametrize("identity", ["human", "agent"])
def test_rules_http_documents_validation_and_conflict_responses(identity):
    async def scenario():
        server = FakeServer(server_type="valkey")
        app = create_app(client(server, "human:owner"), version="test")
        async with (
            app.router.lifespan_context(app),
            client(server, "agent:alice") as alice,
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://hub"
            ) as http,
        ):
            await alice.register("human:owner")
            conversation = (await alice.create_conversation()).id
            prefix = "/agents/agent:alice" if identity == "agent" else ""
            headers = {"X-Toolang-Agent-Lease": alice.token} if prefix else {}
            path = f"{prefix}/msg/conversations/{conversation}/rules"
            empty = {
                "conversation": conversation,
                "filename": "MEMBERS.md",
                "content": "",
                "revision": "0",
            }
            response = await http.get(path, headers=headers)
            assert response.status_code == 200 and response.json() == empty
            content = "# Members\r\n\nKeep literal {{value}} and café.\n"
            document = {
                **empty,
                "content": content,
                "revision": sha256(content.encode()).hexdigest(),
            }
            response = await http.put(
                path, headers=headers, json={"content": content, "revision": "0"}
            )
            assert response.status_code == 200 and response.json() == document
            for invalid in (
                {"content": "new", "revision": "latest"},
                {"content": "new", "revision": 0},
                {"content": None, "revision": document["revision"]},
                {"content": "é" * 32769, "revision": document["revision"]},
                {"content": "new", "revision": document["revision"], "extra": True},
            ):
                response = await http.put(path, headers=headers, json=invalid)
                assert response.status_code == 400
                assert response.json() == {
                    "code": "messaging_error",
                    "detail": "Invalid Hub request",
                }
                assert (await http.get(path, headers=headers)).json() == document
            stale = await http.put(
                path, headers=headers, json={"content": "stale", "revision": "0"}
            )
            assert stale.status_code == 409
            assert stale.json()["code"] == "conversation_rules_conflict"
            assert (await http.get(path, headers=headers)).json() == document
            response = await http.put(
                path,
                headers=headers,
                json={"content": "", "revision": document["revision"]},
            )
            assert response.status_code == 200 and response.json() == empty
            assert (await http.get(path, headers=headers)).json() == empty

    asyncio.run(scenario())
