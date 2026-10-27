"""One Hub serves independent client roots without a startup-root dataset."""

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock
from urllib.parse import quote

from fakeredis import FakeAsyncValkey, FakeServer
import httpx
import pytest

from toolang.teaming.api import create_app
from toolang.teaming.agent_client import AgentClient
from toolang.teaming.backend.valkey import ValkeyBackend
from toolang.teaming.client import HubClient
from toolang.teaming.config import BackendConfig
from toolang.teaming.messaging import MessagingClient
from toolang.teaming.presence import PresenceWorker
from toolang.teaming.schemas import HubConnection, Message


@asynccontextmanager
async def hosted(server=None):
    config = BackendConfig("redis://shared", root="/server")
    raw = FakeAsyncValkey(
        server=server or FakeServer(server_type="valkey"), decode_responses=True
    )
    backend = ValkeyBackend(config, client=raw)
    app = create_app(config, backend=backend, version="test")
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://hub"
        ) as http,
    ):
        yield app, http, backend, raw


def test_probes_do_not_initialize_a_partition():
    async def scenario():
        config = BackendConfig("redis://shared", root="/server")
        raw = FakeAsyncValkey(
            server=FakeServer(server_type="valkey"), decode_responses=True
        )
        backend = ValkeyBackend(config, client=raw)
        app = create_app(config, backend=backend, version="test")
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://hub"
            ) as http,
        ):
            assert (await http.get("/info")).json() == {
                "version": "test",
                "api_version": 0,
            }
            selected = BackendConfig(config.url, root="/client")
            info = await http.get(
                "/info", headers={"X-Toolang-Root": quote(selected.root, safe="")}
            )
            assert info.json() == {
                "version": "test",
                "api_version": 0,
                "root": selected.root,
                "identity": selected.identity,
            }
            assert (
                await http.get(
                    "/healthz",
                    headers={"X-Toolang-Root": "invalid", "X-Toolang-Backend": "other"},
                )
            ).json() == {"ok": True}
            assert await raw.dbsize() == 0

    asyncio.run(scenario())


def test_all_business_routes_require_a_root_before_storage_access():
    async def scenario():
        async with hosted() as (app, http, _, raw):
            count = 0
            for path, operations in app.openapi()["paths"].items():
                if path in {"/info", "/healthz"}:
                    continue
                path = (
                    path.replace("{agent}", "agent:alice")
                    .replace("{conversation}", "gc_00000000")
                    .replace("{convo_id}", "gc_00000000")
                    .replace("{generation}", "g")
                )
                for method, operation in operations.items():
                    assert any(
                        parameter["name"] == "X-Toolang-Root" and parameter["required"]
                        for parameter in operation["parameters"]
                    )
                    response = await http.request(
                        method,
                        path,
                        headers={
                            "X-Toolang-Human": "human:owner",
                            "X-Toolang-Agent-Lease": "lease",
                        },
                    )
                    assert response.status_code == 400, (method, path, response.text)
                    count += 1
            assert count == 46
            assert await raw.dbsize() == 0

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "root",
    [
        "",
        "relative",
        "%2Fone%2F..%2Ftwo",
        "%2Fone%2F",
        "%FF",
        "%2Fone%00",
        "%ZZ",
        "%2F%2Fone",
    ],
)
def test_invalid_root_never_initializes_or_falls_back(root):
    async def scenario():
        async with hosted() as (_, http, _, raw):
            headers = {"X-Toolang-Root": root, "X-Toolang-Human": "human:owner"}
            assert (await http.get("/msg/agents", headers=headers)).status_code == 400
            assert (await http.get("/info", headers=headers)).status_code == 400
            assert await raw.dbsize() == 0

    asyncio.run(scenario())


def test_identity_rejection_precedes_initialization_and_deleted_partitions_stay_deleted():
    async def scenario():
        async with hosted() as (_, http, backend, raw):
            headers = {"X-Toolang-Root": "%2Fone", "X-Toolang-Human": "human:owner"}
            rejected = await http.get(
                "/team", headers=headers | {"X-Toolang-Backend": "other"}
            )
            assert rejected.status_code == 409
            assert await raw.dbsize() == 0
            assert (await http.get("/team", headers=headers)).status_code == 200
            two = headers | {"X-Toolang-Root": "%2Ftwo"}
            assert (await http.get("/team", headers=two)).status_code == 200
            keys = await raw.keys(backend.for_root("/one").keys.prefix + ":*")
            await raw.delete(*keys)
            assert (await http.get("/team", headers=headers)).status_code == 503
            events = await http.get("/events/stream", headers=headers)
            assert events.status_code == 503, events.text
            assert events.json()["code"] == "storage_integrity"
            assert not await raw.keys(backend.for_root("/one").keys.prefix + ":*")
            assert (await http.get("/team", headers=two)).status_code == 200
            assert (await http.get("/healthz")).status_code == 200

    asyncio.run(scenario())


def test_cached_connection_metadata_does_not_follow_an_endpoint_into_another_root():
    async def scenario():
        async with hosted() as (app, _, _, _):
            selected = HubConnection("http://hub", root="/one")
            async with HubClient(
                lambda: selected,
                actor="human:owner",
                transport=httpx.ASGITransport(app),
            ) as client:
                one = await client.connect()
                await client.create_conversation("only one")
                selected = HubConnection("http://hub", root="/two")
                two = await client.connect()
                assert one.root == "/one" and two.root == "/two"
                assert one.identity != two.identity
                assert not any(
                    row["name"] == "only one" for row in await client.contacts()
                )

    asyncio.run(scenario())


def test_same_conversation_ids_and_receipts_are_independent_under_concurrent_requests():
    async def scenario():
        async with hosted() as (app, _, _, _):

            async def exchange(root):
                connection = HubConnection("http://hub", root=root)
                transport = httpx.ASGITransport(app)
                async with (
                    AgentClient(
                        Path(root),
                        actor="agent:alice",
                        token="same",
                        connection=connection,
                        transport=transport,
                    ) as agent,
                    HubClient(
                        connection, actor="human:owner", transport=transport
                    ) as human,
                ):
                    await agent.register("human:owner")
                    sent = await human.send("alice", body=root)
                    await agent.send(sent["conversation"], body="reply " + root)
                    messages = [
                        Message.decode(data["data"]).body
                        for _, data in await human.read(sent["conversation"])
                    ]
                    assert messages == [root, "reply " + root]
                    await agent.advance_talk_cursor(
                        sent["conversation"], sent["stream_id"]
                    )
                    assert (
                        await agent.talk_cursor(sent["conversation"])
                        == sent["stream_id"]
                    )
                    return sent["conversation"]

            one, two = await asyncio.gather(exchange("/one"), exchange("/two"))
            assert one == two

    asyncio.run(scenario())


def test_presence_discovery_after_restart_isolates_corrupt_roots_and_borrows_the_pool():
    async def scenario():
        server = FakeServer(server_type="valkey")
        async with hosted(server) as (_, _, backend, raw):
            for root in ("/one", "/two", "/bad", "/bad-utf8"):
                scoped = backend.for_root(root)
                config = BackendConfig("redis://shared", root=root)
                async with MessagingClient(
                    config, actor="agent:alice", backend=scoped
                ) as agent:
                    await agent.register("human:owner")
                await raw.zadd(scoped.keys.presence, {"agent:alice": 1})
            await raw.set(backend.for_root("/bad").keys.schema, "broken")
            await raw.set(backend.for_root("/bad-utf8").keys.root, b"\xff")
            await raw.set("toolang:invalid:v0:root", "/mismatched")
            await raw.set(b"toolang:\xffaaaaaaa:v0:root", "/invalid-key")
        async with hosted(server) as (_, http, backend, raw):
            # Startup reconciliation must discover persisted roots without a registry.
            for root in ("/one", "/two"):
                assert (await backend.for_root(root).lease_info("agent:alice"))[
                    "lease"
                ] is None
            assert (await http.get("/healthz")).status_code == 200
            close = AsyncMock(wraps=raw.aclose)
            raw.aclose = close
            await backend.for_root("/one").close()
            await backend.for_root("/two").close()
            close.assert_not_awaited()
            assert await raw.ping()
            assert await PresenceWorker(backend).reconcile_once() == 0
        close.assert_awaited_once()

    asyncio.run(scenario())


def test_same_name_agents_and_conversations_are_independent_in_one_hub():
    async def scenario():
        config = BackendConfig("redis://shared", root="/server")
        raw = FakeAsyncValkey(
            server=FakeServer(server_type="valkey"), decode_responses=True
        )
        backend = ValkeyBackend(config, client=raw)
        app = create_app(config, backend=backend, version="test")
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://hub"
            ) as http,
        ):
            for root, owner in (("/bryan", "bryan"), ("/brice", "brice")):
                headers = {
                    "X-Toolang-Root": quote(root, safe=""),
                    "X-Toolang-Agent-Lease": "same-token",
                }
                response = await http.put(
                    "/agents/agent:alice/lease",
                    headers=headers,
                    json={"owner": f"human:{owner}", "endpoint": ""},
                )
                assert response.status_code == 200, response.text
                team = await http.get("/agents/agent:alice/team", headers=headers)
                assert team.status_code == 200, team.text
                assert {member["member"] for member in team.json()} == {
                    "agent:alice",
                    f"human:{owner}",
                }
            assert await raw.exists(backend.keys.root) == 0

    asyncio.run(scenario())
