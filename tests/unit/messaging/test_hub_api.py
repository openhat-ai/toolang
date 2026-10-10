"""Hub HTTP parity, local access, lifecycle, and lost-ack behavior, offline."""

import asyncio
from dataclasses import replace
import json
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

from fakeredis import FakeServer
import httpx
import pytest

from tests.unit.messaging.test_protocol import CONFIG, client
from toolang.teaming.api import create_app
from toolang.teaming.agent_client import AgentClient
from toolang.teaming.backend.valkey.keys import convo_key
from toolang.teaming.client import HubClient
from toolang.teaming.errors import BackendUnavailable, MessagingError, SendUnconfirmed
from toolang.teaming.schemas import HubConnection, Message

CONNECTION = HubConnection("http://hub", "human:owner", CONFIG.identity)


@pytest.mark.parametrize(
    "after",
    [
        "",
        "bad",
        "t1." + "0" * 32 + ".00-0",
        pytest.param("t1." + "0" * 32 + "." + "1" * 5000 + "-0", id="oversized"),
    ],
)
def test_team_subscription_rejects_invalid_cursor_before_sending_headers(after):
    async def scenario():
        human = client(FakeServer(server_type="valkey"), CONNECTION.human)
        app = create_app(human, version="0.4.0-test")
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://hub"
            ) as http,
        ):
            response = await http.get("/team/events", params={"after": after})
            assert response.status_code == 400
            assert response.json()["code"] == "messaging_error"

    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["health", "create", "send", "directory"])
def test_hub_rejects_missing_initialized_data_without_recreating_it(operation):
    async def scenario():
        human = client(FakeServer(server_type="valkey"), CONNECTION.human)
        app = create_app(human, version="0.4.0-test")
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://hub"
            ) as http,
        ):
            raw = human.backend._client
            await raw.flushdb()
            if operation == "health":
                response = await http.get("/healthz")
            elif operation == "create":
                response = await http.post("/msg/conversations", json={"name": "new"})
            elif operation == "send":
                response = await http.post(
                    "/msg/messages",
                    json={
                        "id": str(uuid4()),
                        "target": "gc_00000000",
                        "body": "no recreation",
                    },
                )
            else:
                response = await http.get("/msg/conversations")
            assert response.status_code == 503
            assert await raw.dbsize() == 0

    asyncio.run(scenario())


def test_http_messaging_matches_service_and_isolates_agent_conversations():
    async def scenario():
        server = FakeServer(server_type="valkey")
        human = client(server, CONNECTION.human)
        app = create_app(human, version="0.4.0-test")
        async with (
            client(server, "agent:alice") as alice,
            client(server, "agent:bob") as bob,
            app.router.lifespan_context(app),
            HubClient(CONNECTION, transport=httpx.ASGITransport(app)) as hub,
        ):
            await alice.register("human:owner")
            await bob.register("human:owner")
            assert await hub.agents() == await human.agents()
            created = await hub.create_conversation("后端开发")
            ref = created.id
            assert created.participants == ("human:owner",)
            assert (await hub.resolve("后端开发", kind="name")).conversation == ref
            assert await hub.conversation(ref) == await human.conversation(ref)
            receipt = await hub.send(
                ref, body="literal $x\n你好", in_reply_to=str(uuid4())
            )
            assert receipt["message"]["sender"] == "human:owner"
            assert receipt["message"]["origin"] is None
            assert await hub.read(ref) == await human.read(ref)
            assert await hub.history(ref) == await human.history(ref)
            assert await hub.check_cursor(ref, receipt["stream_id"]) is None
            assert await hub.read(ref, after=receipt["stream_id"]) == []
            assert await hub.contacts(include_preview=True) == await human.contacts(
                include_preview=True
            )
            assert await hub.statistics(ref) == await human.statistics(ref)
            assert await hub.leave_conversation(ref) == {"conversation": ref}
            with pytest.raises(MessagingError, match="read-only"):
                await hub.send(ref, body="outside membership")
            await hub.join_conversation(ref)
            direct = (await alice.send("bob", body="existing exchange"))["conversation"]
            assert not (await hub.conversation(direct)).allows_sender("human:owner")
            for action in (hub.join_conversation, hub.leave_conversation):
                with pytest.raises(MessagingError, match="DM/system"):
                    await action(direct)
            with pytest.raises(MessagingError, match="read-only"):
                await hub.send(direct, body="observer cannot write")
            dm = await hub.resolve("alice")
            assert dm.conversation == (await alice.resolve("human:owner")).conversation
            assert not dm.exists
            await hub.send(
                dm.conversation, body="first", participants=list(dm.participants)
            )
            assert (await hub.resolve("alice")).exists
            assert all(
                "lease" not in row and "deadline" in row for row in await hub.team()
            )

    asyncio.run(scenario())


@pytest.mark.parametrize("owner", ["human:owner", "human:维护者"])
def test_local_access_validation_and_backend_readiness(owner):
    async def scenario():
        server = FakeServer(server_type="valkey")
        human = client(server, owner)
        app = create_app(human, version="0.4.0-test")
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://hub"
            ) as http,
            HubClient(
                replace(CONNECTION, human=owner), transport=httpx.ASGITransport(app)
            ) as hub,
        ):
            expected = await human.targets()
            expected["participants"] = HubClient._presence(expected["participants"])
            assert await hub.targets() == expected
            assert (await http.get("/healthz")).status_code == 200
            assert (await http.post("/msg/messages", json={})).status_code == 400
            assert (await http.get("/healthz")).json() == {"ok": True}
            assert (await http.get("/threads")).status_code == 404
            body = {"id": str(uuid4()), "target": "gc_00000000", "body": "test"}
            for extra in (
                {"actor": "agent:alice"},
                {"sender": "human:another"},
                {"origin": {"thread": "x"}},
            ):
                assert (
                    await http.post("/msg/messages", json=body | extra)
                ).status_code == 400
            for bad in (
                {"id": "bad"},
                {"in_reply_to": "bad"},
                {"body": " "},
                {"body": "x" * 262145},
            ):
                assert (
                    await http.post("/msg/messages", json=body | bad)
                ).status_code == 400
            for query in ("count=0", "count=1001", "after=bad"):
                assert (
                    await http.get("/msg/conversations/gc_00000000/messages?" + query)
                ).status_code == 400
            assert await human.history("gc_00000000") == []
            human.backend.ping = AsyncMock(side_effect=BackendUnavailable("offline"))
            response = await http.get("/healthz")
            assert response.status_code == 503
            assert response.json()["code"] == "backend_unavailable"

    asyncio.run(scenario())


def test_startup_failure_closes_service_without_publishing_readiness():
    async def scenario():
        human = client(FakeServer(server_type="valkey"), CONNECTION.human)
        human.check_backend = AsyncMock(side_effect=BackendUnavailable("offline"))
        human.close = AsyncMock(wraps=human.close)
        ready = []
        app = create_app(
            human, version="0.4.0-test", on_ready=lambda: ready.append(True)
        )
        with pytest.raises(BackendUnavailable):
            async with app.router.lifespan_context(app):
                pytest.fail("startup must fail")
        assert ready == []
        human.close.assert_awaited_once()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "changes", [{"identity": "another-backend"}, {"human": "human:another"}]
)
def test_hub_config_switch_rejects_stale_talk_before_any_storage_access(changes):
    async def scenario():
        human = client(FakeServer(server_type="valkey"), CONNECTION.human)
        app = create_app(human, version="0.4.0-test")
        async with (
            human,
            HubClient(
                replace(CONNECTION, **changes),
                transport=httpx.ASGITransport(app),
            ) as hub,
        ):
            human.register_human = AsyncMock(
                side_effect=AssertionError("storage touched")
            )
            with pytest.raises(MessagingError, match="identity changed; reopen Talk"):
                await hub.send("gc_00000000", body="must not reach another dataset")
            human.register_human.assert_not_awaited()

    asyncio.run(scenario())


def test_history_preserves_corrupt_records_and_full_cursors():
    async def scenario():
        human = client(FakeServer(server_type="valkey"), CONNECTION.human)
        app = create_app(human, version="0.4.0-test")
        async with (
            app.router.lifespan_context(app),
            HubClient(CONNECTION, transport=httpx.ASGITransport(app)) as hub,
        ):
            raw = human.backend._client
            key = convo_key("gc_00000000", "messages")
            await raw.xadd(key, {"data": "broken"}, id="100-9")
            await raw.xadd(key, {"other": "no data"}, id="100-10")
            await raw.xadd(
                key,
                {"data": Message.create("human:owner", "valid").encode()},
                id="101-0",
            )
            rows = await hub.read("gc_00000000")
            assert [sid for sid, _ in rows] == ["100-9", "100-10", "101-0"]
            assert rows[0][1] == {"data": "broken"} and rows[1][1] == {}
            assert await hub.check_cursor("gc_00000000", "99-0") is not None
            with pytest.raises(MessagingError, match="precedes saved cursor"):
                await hub.check_cursor("gc_00000000", "102-0")

    asyncio.run(scenario())


@pytest.mark.parametrize("actor", ["human:owner", "agent:alice"])
@pytest.mark.parametrize(
    "failure",
    [
        "timeout",
        "invalid_json",
        "server_error",
        "missing_receipt",
        "wrong_message",
        "wrong_sender",
        "invalid_group",
        "invalid_cursor",
        "empty_cursor",
    ],
)
def test_lost_send_response_reports_preallocated_id_without_retry(
    tmp_path, failure, actor
):
    calls = []

    def transport(request):
        calls.append(json.loads(request.content))
        if failure == "timeout":
            raise httpx.ReadTimeout("lost ack", request=request)
        if failure == "invalid_json":
            return httpx.Response(200, text="bad")
        if failure == "server_error":
            return httpx.Response(500, json={"detail": "failure"})
        if failure == "missing_receipt":
            return httpx.Response(200, json={"ok": True})
        receipt = {
            "conversation": "gc_00000000",
            "stream_id": "1-0",
            "message": Message(calls[-1]["id"], actor, "once").data(),
        }
        if failure == "wrong_message":
            receipt["message"]["id"] = str(uuid4())
        elif failure == "wrong_sender":
            receipt["message"]["sender"] = "human:another"
        elif failure == "invalid_group":
            receipt["conversation"] = "agent:alice"
        else:
            receipt["stream_id"] = "bad" if failure == "invalid_cursor" else "0-0"
        return httpx.Response(201, json=receipt)

    async def scenario():
        http = httpx.MockTransport(transport)
        hub = (
            HubClient(CONNECTION, transport=http)
            if actor == CONNECTION.human
            else AgentClient(
                tmp_path, actor=actor, connection=lambda: CONNECTION, transport=http
            )
        )
        async with hub:
            with pytest.raises(SendUnconfirmed) as error:
                await hub.send("gc_00000000", body="once")
            assert len(calls) == 1
            UUID(calls[0]["id"])
            assert calls[0]["id"] in str(error.value)

    asyncio.run(scenario())


def test_backend_uncertain_send_keeps_uuid_over_http():
    async def scenario():
        human = client(FakeServer(server_type="valkey"), CONNECTION.human)
        original = human.backend.append

        async def lose_ack(*args, **kwargs):
            await original(*args, **kwargs)
            raise SendUnconfirmed("ack lost")

        human.backend.append = lose_ack
        app = create_app(human, version="0.4.0-test")
        async with (
            app.router.lifespan_context(app),
            HubClient(CONNECTION, transport=httpx.ASGITransport(app)) as hub,
        ):
            with pytest.raises(SendUnconfirmed) as error:
                await hub.send("gc_00000000", body="one append")
            rows = await hub.history("gc_00000000")
            assert len(rows) == 1
            assert Message.decode(rows[0][1]["data"]).id in str(error.value)

    asyncio.run(scenario())


pytestmark = pytest.mark.usefixtures("fixed_conversation_ids")


def test_membership_denial_keeps_its_error_type_across_http(tmp_path):
    from toolang.teaming.errors import ConversationAccessDenied

    async def scenario():
        human = client(FakeServer(server_type="valkey"), CONNECTION.human)
        app = create_app(human, version="0.4.0-test")
        async with (
            app.router.lifespan_context(app),
            AgentClient(
                tmp_path,
                actor="agent:alice",
                token="lease",
                connection=lambda: CONNECTION,
                transport=httpx.ASGITransport(app),
            ) as alice,
        ):
            await alice.register(human.actor)
            convo = await human.create_conversation("Private")
            with pytest.raises(ConversationAccessDenied):
                await alice.conversation(convo.id)
            with pytest.raises(ConversationAccessDenied):
                await alice.read(convo.id)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "operation,body",
    [
        ("team", [{"member": "agent:alice", "lease": {"token": "private"}}]),
        ("contacts", [{"conversation": "group:old"}]),
        ("statistics", {"messages_total": True}),
        ("conversation", {"id": "dm_00000001", "kind": "dm", "participants": []}),
    ],
)
def test_client_rejects_malformed_public_records(operation, body):
    async def scenario():
        async with HubClient(
            CONNECTION,
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body)),
        ) as hub:
            with pytest.raises(MessagingError, match="Invalid Hub") as error:
                if operation == "conversation":
                    await hub.conversation("dm_00000001")
                else:
                    await getattr(hub, operation)()
            assert "private" not in str(error.value)

    asyncio.run(scenario())


def test_hub_info_reports_server_version_and_keeps_readiness_contract():
    async def scenario():
        human = client(FakeServer(server_type="valkey"), CONNECTION.human)
        app = create_app(human, version="0.4.0-server*")
        async with (
            app.router.lifespan_context(app),
            HubClient(CONNECTION, transport=httpx.ASGITransport(app)) as hub,
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://hub"
            ) as http,
        ):
            assert (await hub.info()).version == "0.4.0-server*"
            assert (await http.get("/healthz")).json() == {"ok": True}
            response = await http.get(
                "/info", headers={"X-Toolang-Backend": "different"}
            )
            assert response.status_code == 409
            assert response.json()["code"] == "hub_changed"

    asyncio.run(scenario())
