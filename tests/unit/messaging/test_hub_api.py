"""Hub HTTP parity, authorization, lifecycle, and lost-ack behavior, offline."""

import asyncio
import json
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

from fakeredis import FakeServer
import httpx
import pytest

from tests.unit.messaging.test_protocol import client
from toolang.teaming.api import create_app
from toolang.teaming.backend import group_key
from toolang.teaming.client import HubClient
from toolang.teaming.errors import BackendUnavailable, MessagingError, SendUnconfirmed
from toolang.teaming.schemas import HubConnection, Message

CONNECTION = HubConnection("http://hub", "test-token", "human:owner", "test")


def test_http_messaging_matches_service_and_isolates_agent_conversations():
    async def scenario():
        server = FakeServer(server_type="valkey")
        human = client(server, CONNECTION.human)
        app = create_app(human, token=CONNECTION.token)
        async with (
            client(server, "agent:alice") as alice,
            client(server, "agent:bob") as bob,
            app.router.lifespan_context(app),
            HubClient(CONNECTION, transport=httpx.ASGITransport(app)) as hub,
        ):
            await alice.register("human:owner")
            await bob.register("human:owner")
            assert await hub.agents() == await human.agents()
            assert await hub.targets() == await human.targets()
            assert await hub.create_group("后端开发") == {
                "group": "group:后端开发",
                "members": ["human:owner"],
            }
            group = await hub.resolve("后端开发")
            assert group == "group:后端开发"
            assert await hub.conversation(group) == await human.conversation(group)
            receipt = await hub.send(
                group, body="literal $x\n你好", in_reply_to=str(uuid4())
            )
            assert receipt["message"]["sender"] == "human:owner"
            assert receipt["message"]["origin"] is None
            assert await hub.read(group) == await human.read(group)
            assert await hub.history(group) == await human.history(group)
            assert await hub.check_cursor(group, receipt["stream_id"]) is None
            assert await hub.read(group, after=receipt["stream_id"]) == []
            assert await hub.contacts(include_preview=True) == await human.contacts(
                include_preview=True
            )
            assert await hub.leave_group(group) == {"group": group, "members": []}
            with pytest.raises(MessagingError, match="read-only"):
                await hub.send(group, body="outside membership")
            await hub.join_group(group)
            await hub.send(group, body="joined again")
            direct = await alice.resolve("agent:bob")
            assert (await hub.conversation(direct)).allows_sender(
                "human:owner"
            ) is False
            assert await hub.history(direct) == []
            for action in (hub.join_group, hub.leave_group):
                with pytest.raises(MessagingError, match="direct/system"):
                    await action(direct)
            with pytest.raises(MessagingError, match="read-only"):
                await hub.send(direct, body="observer cannot write")
            dm = await hub.resolve("alice", kind="dm")
            assert dm == await alice.resolve("human:owner")
            assert await hub.resolve("all") == "group:all"
            # Canonical API send targets are enforced; only resolve accepts shorthand.
            with pytest.raises(MessagingError, match="Invalid target"):
                await hub.send("all", body="not canonical")

    asyncio.run(scenario())


def test_authentication_validation_and_backend_readiness():
    async def scenario():
        server = FakeServer(server_type="valkey")
        human = client(server, CONNECTION.human)
        app = create_app(human, token=CONNECTION.token)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://hub"
            ) as http,
        ):
            assert (await http.get("/healthz")).status_code == 401
            assert (await http.post("/msg/messages", json={})).status_code == 401
            http.headers["Authorization"] = f"Bearer {CONNECTION.token}"
            assert (await http.get("/healthz")).json() == {"ok": True}
            assert (await http.get("/threads")).status_code == 404
            body = {"id": str(uuid4()), "target": "group:all", "body": "test"}
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
                    await http.get("/msg/groups/group:all/messages?" + query)
                ).status_code == 400
            assert await human.history("group:all") == []
            human.check_backend = AsyncMock(side_effect=BackendUnavailable("offline"))
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
            human, token=CONNECTION.token, on_ready=lambda: ready.append(True)
        )
        with pytest.raises(BackendUnavailable):
            async with app.router.lifespan_context(app):
                pytest.fail("startup must fail")
        assert ready == []
        human.close.assert_awaited_once()

    asyncio.run(scenario())


def test_history_preserves_corrupt_records_and_full_cursors():
    async def scenario():
        human = client(FakeServer(server_type="valkey"), CONNECTION.human)
        app = create_app(human, token=CONNECTION.token)
        async with (
            app.router.lifespan_context(app),
            HubClient(CONNECTION, transport=httpx.ASGITransport(app)) as hub,
        ):
            raw = human._backend._client
            key = group_key("group:all", "messages")
            await raw.xadd(key, {"data": "broken"}, id="100-9")
            await raw.xadd(key, {"other": "no data"}, id="100-10")
            await raw.xadd(
                key,
                {"data": Message.create("human:owner", "valid").encode()},
                id="101-0",
            )
            rows = await hub.read("group:all")
            assert [sid for sid, _ in rows] == ["100-9", "100-10", "101-0"]
            assert rows[0][1] == {"data": "broken"} and rows[1][1] == {}
            assert await hub.check_cursor("group:all", "99-0") is not None
            with pytest.raises(MessagingError, match="precedes saved cursor"):
                await hub.check_cursor("group:all", "102-0")

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["timeout", "invalid_json", "server_error"])
def test_lost_send_response_reports_preallocated_id_without_retry(failure):
    calls = []

    def transport(request):
        calls.append(json.loads(request.content))
        if failure == "timeout":
            raise httpx.ReadTimeout("lost ack", request=request)
        if failure == "invalid_json":
            return httpx.Response(200, text="bad")
        return httpx.Response(500, json={"detail": "failure"})

    async def scenario():
        async with HubClient(
            CONNECTION, transport=httpx.MockTransport(transport)
        ) as hub:
            with pytest.raises(SendUnconfirmed) as error:
                await hub.send("group:all", body="once")
            assert len(calls) == 1
            UUID(calls[0]["id"])
            assert calls[0]["id"] in str(error.value)

    asyncio.run(scenario())


def test_backend_uncertain_send_keeps_uuid_over_http():
    async def scenario():
        human = client(FakeServer(server_type="valkey"), CONNECTION.human)
        original = human._backend.append

        async def lose_ack(*args, **kwargs):
            await original(*args, **kwargs)
            raise SendUnconfirmed("ack lost")

        human._backend.append = lose_ack
        app = create_app(human, token=CONNECTION.token)
        async with (
            app.router.lifespan_context(app),
            HubClient(CONNECTION, transport=httpx.ASGITransport(app)) as hub,
        ):
            with pytest.raises(SendUnconfirmed) as error:
                await hub.send("group:all", body="one append")
            rows = await hub.history("group:all")
            assert len(rows) == 1
            assert Message.decode(rows[0][1]["data"]).id in str(error.value)

    asyncio.run(scenario())
