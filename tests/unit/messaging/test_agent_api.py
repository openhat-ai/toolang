"""Agent Hub transport, recovery, and admission checks on an isolated backend."""

import asyncio
from dataclasses import replace
import json
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from fakeredis import FakeServer
import httpx
import pytest

from tests.support.execution_harness import ExecutionHarness
from tests.unit.messaging.test_event_observation import run, publish, prefix
from tests.unit.messaging.test_protocol import client
from toolang.base.types.tool import ToolContext
from toolang.common.layout import AgentLayout
from toolang.execution.errors import SnapshotLimitError
from toolang.plugin.toolsets.msg import MsgToolset
from toolang.teaming.agent_client import AgentClient, AgentEventClient
from toolang.teaming.api import create_app
from toolang.teaming.errors import (
    BackendUnavailable,
    EventRecoveryRequired,
    EventProtocolError,
    SendUnconfirmed,
)
from toolang.teaming.event_backend import EventBackend
from toolang.teaming.events import HubScope
from toolang.teaming.exporter import EventExporter
from toolang.teaming.records import HubRecord, MAX_BYTES
from toolang.teaming.schemas import HubConnection
from toolang.teaming.stream_client import HubStreamState
from toolang.teaming.subscriptions import HubSubscription
from toolang.work.messaging import MessagingLoop

CONNECTION = HubConnection("http://hub", "a" * 32, "human:owner", "dataset-one")


def agent(app, root, actor="agent:alice", token="lease", transport=None):
    return AgentClient(
        root,
        actor=actor,
        token=token,
        connection=lambda: CONNECTION,
        transport=transport or httpx.ASGITransport(app),
    )


def test_agent_messaging_uses_hub_authority_and_context(tmp_path):
    async def scenario():
        human = client(FakeServer(server_type="valkey"), CONNECTION.human)
        app = create_app(human, token=CONNECTION.token)
        async with (
            app.router.lifespan_context(app),
            agent(app, tmp_path) as alice,
            agent(app, tmp_path, "agent:bob", "bob-lease") as bob,
        ):
            await alice.register("human:forged", endpoint="http://agent:7001")
            await bob.register(CONNECTION.human)
            assert await human.agents() == {
                "agent:alice": CONNECTION.human,
                "agent:bob": CONNECTION.human,
            }
            await alice.create_group("dev")
            await bob.join_group("group:dev")
            sent = await alice.send(
                "group:dev", body="hello", thread="thread_1", run="run_1"
            )
            assert sent["message"]["origin"] == {"thread": "thread_1", "run": "run_1"}
            assert sent["message"]["sender"] == "agent:alice"
            assert await bob.read("group:dev") == await alice.history("group:dev")
            assert await bob.check_cursor("group:dev", sent["stream_id"]) is None
            await bob.leave_group("group:dev")
            assert all(row["group"] != "group:dev" for row in await bob.contacts())
            await alice.renew()
            await alice.unregister()
            with pytest.raises(EventRecoveryRequired, match="lease lost"):
                await alice.targets()
            await alice.register(CONNECTION.human)
            assert await alice.targets()

    asyncio.run(scenario())


def test_agent_tools_without_hub_never_construct_backend(tmp_path):
    async def scenario():
        msg = MsgToolset({"root": str(tmp_path)})
        context = ToolContext(tmp_path / "alice", tmp_path / "room")
        with patch(
            "toolang.teaming.backend.Backend.__init__",
            side_effect=AssertionError("agent opened backend"),
        ):
            with pytest.raises(BackendUnavailable, match="hub start"):
                await msg.tools()["targets"].invoke({}, context)

    asyncio.run(scenario())


def test_agent_discovery_refreshes_tokens_and_pins_batch_identity(tmp_path):
    async def scenario():
        current = CONNECTION
        human = client(FakeServer(server_type="valkey"), CONNECTION.human)
        app = create_app(human, token=current.token)

        async def handle(request):
            return await httpx.ASGITransport(app).handle_async_request(request)

        def save():
            HubRecord(
                pid=1,
                created=1.0,
                port=7000,
                token=current.token,
                human=current.human,
                identity=current.identity,
            ).save(tmp_path / ".runtime" / "hub.json")

        save()
        async with (
            human,
            AgentClient(
                tmp_path, actor="agent:alice", transport=httpx.MockTransport(handle)
            ) as alice,
        ):
            await alice.register(CONNECTION.human)
            with alice.session() as identity:
                current = replace(current, token="b" * 32, identity="dataset-two")
                app = create_app(human, token=current.token)
                save()
                assert identity == CONNECTION.identity
                with pytest.raises(BackendUnavailable, match="identity changed"):
                    await alice.targets()
            await alice.register(CONNECTION.human)
            assert await alice.targets()
            with alice.session() as identity:
                assert identity == "dataset-two"
            (tmp_path / ".runtime" / "hub.json").unlink()
            with pytest.raises(BackendUnavailable):
                await alice.targets()

    asyncio.run(scenario())


def test_message_checkpoints_are_separate_after_backend_switch(tmp_path, monkeypatch):
    async def scenario():
        humans = [
            client(FakeServer(server_type="valkey"), CONNECTION.human) for _ in range(2)
        ]
        apps = [create_app(human, token=CONNECTION.token) for human in humans]
        current = 0

        async def handle(request):
            return await httpx.ASGITransport(apps[current]).handle_async_request(
                request
            )

        async with (
            humans[0],
            humans[1],
            AgentClient(
                tmp_path,
                actor="agent:alice",
                token="lease",
                transport=httpx.MockTransport(handle),
                connection=lambda: replace(CONNECTION, identity=f"dataset-{current}"),
            ) as alice,
        ):
            loop = MessagingLoop(
                layout=AgentLayout.resident(tmp_path, "alice"),
                owner=CONNECTION.human,
                client=alice,
                executor=MagicMock(),
                threads=MagicMock(),
                get_agent_setup=MagicMock(),
                get_agent_state=MagicMock(),
            )
            handler = AsyncMock()
            monkeypatch.setattr(loop, "handle", handler)
            paths = []
            for current in range(2):
                await alice.register(CONNECTION.human)
                receipt = await humans[current].send(
                    "group:all", body=f"dataset {current}"
                )
                await loop.poll()
                assert (
                    loop.saved["group:all"]["messages"][-1]["id"]
                    == receipt["message"]["id"]
                )
                paths.append(loop.path)
            assert paths[0] != paths[1] and all(path.is_file() for path in paths)
            assert handler.await_count == 2
            current = 0
            await loop.poll()
            assert handler.await_count == 2

    asyncio.run(scenario())


def test_agent_send_lost_http_ack_is_not_retried(tmp_path):
    async def scenario():
        human = client(FakeServer(server_type="valkey"), CONNECTION.human)
        app = create_app(human, token=CONNECTION.token)
        writes = 0

        async def handle(request):
            nonlocal writes
            response = await httpx.ASGITransport(app).handle_async_request(request)
            if request.url.path.endswith("/msg/messages"):
                writes += 1
                await response.aread()
                raise httpx.ReadError("response lost", request=request)
            return response

        async with (
            app.router.lifespan_context(app),
            agent(app, tmp_path, transport=httpx.MockTransport(handle)) as alice,
        ):
            await alice.register(CONNECTION.human)
            with pytest.raises(SendUnconfirmed):
                await alice.send("group:all", body="only once")
            assert writes == 1 and len(await human.history("group:all")) == 1

    asyncio.run(scenario())


def test_event_publication_retries_exact_http_operation_and_recovers(tmp_path):
    harness = ExecutionHarness.create(
        tmp_path, source="flow example:\n  let result = Done\n", responses=[]
    )

    async def scenario():
        human = client(FakeServer(server_type="valkey"), CONNECTION.human)
        app = create_app(human, token=CONNECTION.token)
        lost = False
        commits = []

        async def handle(request):
            nonlocal lost
            response = await httpx.ASGITransport(app).handle_async_request(request)
            if request.url.path.endswith("/events/commits"):
                op = json.loads(request.content)
                commits.append(op)
                if op["kind"] == "event" and not lost:
                    lost = True
                    await response.aread()
                    raise httpx.ReadError("response lost", request=request)
            return response

        async with (
            harness,
            app.router.lifespan_context(app),
            agent(app, tmp_path, transport=httpx.MockTransport(handle)) as alice,
        ):
            await alice.register(CONNECTION.human)
            exporter = EventExporter(
                harness.executor.stream,
                harness.store.db_path,
                AgentEventClient(alice),
                agent=alice.actor,
                token=alice.token,
            )
            try:
                await exporter.recover("initial")
                record = await run(harness)
                await publish(exporter)
                assert lost
                events = [op for op in commits if op["kind"] == "event"]
                assert events[0] == events[1]
                assert all("old_key" not in op for op in commits)
                service = EventBackend(human._backend)
                before = await service.capture(alice.actor)
                for extra in (
                    {"source": "bad"},
                    {"source_epoch": uuid4().hex},
                    {
                        "updates": {
                            '["root","other"]': json.dumps(
                                {
                                    "v": 1,
                                    "thread": "t",
                                    "root": "other",
                                    "terminal": "wrong",
                                }
                            )
                        }
                    },
                ):
                    with pytest.raises(EventProtocolError):
                        await alice._request(
                            "POST", "/events/commits", json=events[-1] | extra
                        )
                with pytest.raises(SnapshotLimitError):
                    await alice._request(
                        "POST",
                        "/events/commits",
                        json=events[-1] | {"bytes": MAX_BYTES + 1},
                    )
                assert await service.capture(alice.actor) == before
                sub = HubSubscription(
                    service, HubScope(alice.actor, run=record.id), None
                )
                try:
                    await sub.prepare()
                    state = HubStreamState(HubScope(alice.actor, run=record.id))
                    for frame in await prefix(sub):
                        state.feed(frame)
                    assert state.agents[alice.actor].complete(record.id)
                finally:
                    sub.close()
                await exporter.recover("reconnect")
                assert exporter.highwater == harness.executor.stream.tail
            finally:
                exporter.close()

    asyncio.run(scenario())


def test_agent_api_rejects_bad_identity_lease_and_publications(tmp_path):
    async def scenario():
        human = client(FakeServer(server_type="valkey"), CONNECTION.human)
        app = create_app(human, token=CONNECTION.token)
        async with app.router.lifespan_context(app), agent(app, tmp_path) as alice:
            await alice.register(CONNECTION.human)
            publisher = AgentEventClient(alice)
            meta = await publisher.initialize()
            base = dict(
                v=1,
                epoch=meta["epoch"],
                generation=uuid4().hex,
                recovery=uuid4().hex,
                kind="incomplete",
                reason="initial",
            )
            base["id"] = "{}:incomplete".format(base["recovery"])
            for extra in (
                {"agent": "agent:bob"},
                {"token": "forged"},
                {"old_key": "other"},
                {"kind": "bad"},
                {"epoch": "bad"},
                {"id": "bad"},
            ):
                with pytest.raises(EventProtocolError):
                    await alice._request("POST", "/events/commits", json=base | extra)
            assert (await publisher.capture(alice.actor))[1] == {}
            async with agent(app, tmp_path, token="stale") as stale:
                with pytest.raises(EventRecoveryRequired):
                    await stale._request("POST", "/events/commits", json=base)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://hub"
            ) as http:
                assert (
                    await http.put("/agents/agent:alice/lease", json={})
                ).status_code == 401
                assert (
                    await http.put(
                        "/agents/human:owner/lease",
                        json={},
                        headers={
                            "Authorization": "Bearer " + CONNECTION.token,
                            "X-Toolang-Agent-Lease": "lease",
                        },
                    )
                ).status_code == 400

    asyncio.run(scenario())
