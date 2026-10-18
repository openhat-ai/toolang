"""Agent Hub transport, recovery, and admission checks on an isolated backend."""

import asyncio
import logging
from dataclasses import replace
import json
from unittest.mock import MagicMock, patch
from uuid import uuid4

from fakeredis import FakeServer
import httpx
import pytest

from tests.support.execution_harness import ExecutionHarness
from tests.support.talk import admitted_handler
from tests.unit.messaging.test_event_observation import run, publish, prefix
from tests.unit.messaging.test_protocol import CONFIG, client
from toolang.base.types.tool import ToolContext
from toolang.common.layout import AgentLayout
from toolang.execution.errors import SnapshotLimitError
from toolang.plugin.toolsets.convo import ConvoToolset
from toolang.teaming.agent_client import AgentClient, AgentEventClient
from toolang.teaming.api import create_app
from toolang.teaming.config import BackendConfig
from toolang.teaming.discovery import hub_connection
from toolang.teaming.errors import (
    BackendUnavailable,
    EventRecoveryRequired,
    EventProtocolError,
    SendUnconfirmed,
)
from toolang.teaming.events import HubScope
from toolang.teaming.exporter import EventExporter
from toolang.teaming.records import HubRecord, MAX_BYTES
from toolang.teaming.schemas import HubConnection, Message
from toolang.teaming.stream_client import HubStreamState
from toolang.teaming.subscriptions import HubSubscription
from toolang.driver.talk import MessagingLoop

CONNECTION = HubConnection("http://hub", "human:owner", CONFIG.identity)


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
        app = create_app(human, version="0.4.0-test")
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
            await alice.create_conversation("dev")
            await bob.join_conversation("gc_00000001")
            sent = await alice.send(
                "gc_00000001", body="hello", thread="thread_1", run="run_1"
            )
            assert sent["message"]["origin"] == {"thread": "thread_1", "run": "run_1"}
            assert sent["message"]["sender"] == "agent:alice"
            assert await bob.read("gc_00000001") == await alice.history("gc_00000001")
            assert await bob.check_cursor("gc_00000001", sent["stream_id"]) is None
            await bob.leave_conversation("gc_00000001")
            assert all(
                row["conversation"] != "gc_00000001" for row in await bob.contacts()
            )
            await alice.renew()
            await alice.unregister()
            with pytest.raises(EventRecoveryRequired, match="lease lost"):
                await alice.targets()
            await alice.register(CONNECTION.human)
            assert await alice.targets()

    asyncio.run(scenario())


def test_agent_tools_without_hub_never_construct_backend(tmp_path):
    async def scenario():
        msg = ConvoToolset({"root": str(tmp_path)})
        context = ToolContext(tmp_path / "alice", tmp_path / "room")
        with patch(
            "toolang.teaming.backend.valkey.ValkeyBackend.__init__",
            side_effect=AssertionError("agent opened backend"),
        ):
            with pytest.raises(BackendUnavailable, match="hub start"):
                await msg.tools()["team"].invoke({}, context)

    asyncio.run(scenario())


def test_agent_discovery_ignores_legacy_hub_record(tmp_path):
    legacy_path = tmp_path / ".runtime" / "hub.json"
    legacy_path.parent.mkdir()
    HubRecord(
        pid=1,
        created=1.0,
        port=7000,
        human="human:owner",
        identity=CONFIG.identity,
    ).save(legacy_path)
    with pytest.raises(BackendUnavailable, match="Hub is unavailable"):
        hub_connection(tmp_path)


def test_convo_peek_exposes_current_revision_for_renaming(tmp_path, monkeypatch):
    async def scenario():
        human = client(FakeServer(server_type="valkey"), CONNECTION.human)
        app = create_app(human, version="0.4.0-test")
        async with (
            app.router.lifespan_context(app),
            agent(app, tmp_path) as alice,
        ):
            await alice.register(CONNECTION.human)
            conversation = await human.create_conversation("Original")
            await alice.join_conversation(conversation.id)
            await human.rename_conversation(conversation.id, "Current", revision=1)
            msg = ConvoToolset({"root": str(tmp_path)})
            monkeypatch.setattr(msg, "connection", lambda _: agent(app, tmp_path))
            context = ToolContext(tmp_path / "alice", tmp_path / "room")
            tools = msg.tools()
            result = await tools["peek"].invoke(
                {"target": conversation.id, "limit": 0}, context
            )
            row = result.output["conversation"]
            assert row["revision"] == 2
            renamed = await tools["rename"].invoke(
                {
                    "conversation": row["id"],
                    "name": "From the tool",
                    "revision": row["revision"],
                },
                context,
            )
            assert renamed.output["revision"] == 3
            assert (await human.conversation(conversation.id)).name == "From the tool"

    asyncio.run(scenario())


def test_agent_discovery_pins_batch_identity_and_refreshes_connections(tmp_path):
    async def scenario():
        current = CONNECTION
        human = client(FakeServer(server_type="valkey"), CONNECTION.human)
        app = create_app(human, version="0.4.0-test")

        async def handle(request):
            return await httpx.ASGITransport(app).handle_async_request(request)

        def save():
            HubRecord(
                pid=1,
                created=1.0,
                port=7000,
                human=current.human,
                identity=current.identity,
            ).save(tmp_path / "hub" / "hub.json")

        save()
        async with (
            human,
            AgentClient(
                tmp_path, actor="agent:alice", transport=httpx.MockTransport(handle)
            ) as alice,
        ):
            await alice.register(CONNECTION.human)
            with alice.session() as identity:
                human.config = BackendConfig("redis://second")
                current = replace(current, identity=human.config.identity)
                app = create_app(human, version="0.4.0-test")
                save()
                assert identity == CONNECTION.identity
                with pytest.raises(BackendUnavailable, match="identity changed"):
                    await alice.targets()
                async with AgentClient(
                    tmp_path,
                    actor=alice.actor,
                    transport=httpx.MockTransport(handle),
                ) as tool_client:
                    with pytest.raises(BackendUnavailable, match="identity changed"):
                        await tool_client.targets()
                async with AgentClient(
                    tmp_path / "other-root",
                    actor=alice.actor,
                    connection=lambda: current,
                    transport=httpx.MockTransport(handle),
                ) as other:
                    assert await other.targets()
            await alice.register(CONNECTION.human)
            assert await alice.targets()
            with alice.session() as identity:
                assert identity == current.identity
            (tmp_path / "hub" / "hub.json").unlink()
            with pytest.raises(BackendUnavailable):
                await alice.targets()

    asyncio.run(scenario())


def test_talk_cursors_are_separate_after_backend_switch(tmp_path, monkeypatch):
    async def scenario():
        humans = [
            client(FakeServer(server_type="valkey"), CONNECTION.human) for _ in range(2)
        ]
        for index, human in enumerate(humans):
            human.config = BackendConfig(f"redis://dataset-{index}")
        apps = [create_app(human, version="0.4.0-test") for human in humans]
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
                connection=lambda: replace(
                    CONNECTION, identity=humans[current].config.identity
                ),
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
            handler = admitted_handler(loop)
            for current in range(2):
                await alice.register(CONNECTION.human)
                receipt = await humans[current].send(
                    "gc_00000000", body=f"dataset {current}"
                )
                await loop.poll()
                batch = handler.call_args[0][0]["conversations"][0]
                assert batch["messages"][-1]["id"] == receipt["message"]["id"]
                assert await alice.talk_cursor("gc_00000000") == receipt["stream_id"]
            assert not (
                AgentLayout.resident(tmp_path, "alice").runtime / "channels"
            ).exists()
            assert handler.await_count == 2
            current = 0
            await loop.poll()
            assert handler.await_count == 2

    asyncio.run(scenario())


def test_agent_send_lost_http_ack_is_not_retried(tmp_path):
    async def scenario():
        human = client(FakeServer(server_type="valkey"), CONNECTION.human)
        app = create_app(human, version="0.4.0-test")
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
                await alice.send("gc_00000000", body="only once")
            assert writes == 1 and len(await human.history("gc_00000000")) == 1

    asyncio.run(scenario())


def test_event_publication_retries_exact_http_operation_and_recovers(tmp_path):
    harness = ExecutionHarness.create(
        tmp_path, source="flow example:\n  let result = Done\n", responses=[]
    )

    async def scenario():
        human = client(FakeServer(server_type="valkey"), CONNECTION.human)
        app = create_app(human, version="0.4.0-test")
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
                service = human.backend.events
                before = await service.capture(alice.actor)
                for extra in (
                    {"source": "bad"},
                    {"source_epoch": uuid4().hex},
                    {
                        "updates": {
                            '["root","other"]': json.dumps(
                                {
                                    "v": 2,
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
        app = create_app(human, version="0.4.0-test")
        async with app.router.lifespan_context(app), agent(app, tmp_path) as alice:
            await alice.register(CONNECTION.human)
            publisher = AgentEventClient(alice)
            meta = await publisher.initialize()
            base = dict(
                v=2,
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
                ).status_code == 400
                assert (
                    await http.put(
                        "/agents/human:owner/lease",
                        json={},
                        headers={
                            "X-Toolang-Agent-Lease": "lease",
                        },
                    )
                ).status_code == 400

    asyncio.run(scenario())


pytestmark = pytest.mark.usefixtures("fixed_conversation_ids")


def test_handler_errors_do_not_trigger_other_agents(tmp_path, monkeypatch):
    async def scenario():
        human = client(FakeServer(server_type="valkey"), CONNECTION.human)
        app = create_app(human, version="0.4.0-test")
        async with (
            app.router.lifespan_context(app),
            agent(app, tmp_path, "agent:alice", "alice-lease") as alice,
            agent(app, tmp_path, "agent:bob", "bob-lease") as bob,
        ):
            loops = []
            handlers = []
            for name, receiver in (("alice", alice), ("bob", bob)):
                await receiver.register(CONNECTION.human)
                loop = MessagingLoop(
                    layout=AgentLayout.resident(tmp_path, name),
                    owner=CONNECTION.human,
                    client=receiver,
                    executor=MagicMock(),
                    threads=MagicMock(),
                    get_agent_setup=MagicMock(),
                    get_agent_state=MagicMock(),
                )
                handler = admitted_handler(loop, RuntimeError("handler unavailable"))
                monkeypatch.setattr(loop, "handle", handler)
                handlers.append(handler)
                loops.append(loop)
            await human.send("gc_00000000", body="please handle")
            for _ in range(3):
                for loop in loops:
                    await loop.poll()
            history = await human.history("gc_00000000")
            assert len(history) == 3  # Original message and one error per agent.
            replies = [Message.decode(fields["data"]) for _, fields in history[1:]]
            assert all(message.handler_error for message in replies)
            source = Message.decode(history[0][1]["data"])
            assert all(message.in_reply_to == source.id for message in replies)
            assert all(handler.await_count == 1 for handler in handlers)
            # Ordinary agent messages with the same wording still require handling.
            ordinary = await bob.send(
                "gc_00000000", body="Message handling failed: ordinary request"
            )
            for loop in loops:
                await loop.poll()
            assert handlers[0].await_count == 2
            assert handlers[1].await_count == 1
            reply = Message.decode((await human.history("gc_00000000"))[-1][1]["data"])
            assert reply.in_reply_to == ordinary["message"]["id"]

    asyncio.run(scenario())


@pytest.mark.parametrize("fault", ["lost-ack", "rejected"])
def test_handler_error_send_failure_is_checkpointed_without_retry(
    tmp_path, monkeypatch, caplog, fault
):
    caplog.set_level(logging.ERROR)

    async def scenario():
        human = client(FakeServer(server_type="valkey"), CONNECTION.human)
        app = create_app(human, version="0.4.0-test")
        writes = []

        async def handle(request):
            if request.url.path.endswith("/msg/messages"):
                writes.append(json.loads(request.content))
                if fault == "rejected":
                    return httpx.Response(
                        403,
                        json={
                            "code": "conversation_access_denied",
                            "detail": "access denied",
                        },
                    )
                response = await httpx.ASGITransport(app).handle_async_request(request)
                await response.aread()
                raise httpx.ReadError("response lost", request=request)
            return await httpx.ASGITransport(app).handle_async_request(request)

        async with (
            app.router.lifespan_context(app),
            agent(app, tmp_path, transport=httpx.MockTransport(handle)) as alice,
        ):
            await alice.register(CONNECTION.human)
            loop = MessagingLoop(
                layout=AgentLayout.resident(tmp_path, "alice"),
                owner=CONNECTION.human,
                client=alice,
                executor=MagicMock(),
                threads=MagicMock(),
                get_agent_setup=MagicMock(),
                get_agent_state=MagicMock(),
            )
            handler = admitted_handler(loop, RuntimeError("handler unavailable"))
            monkeypatch.setattr(loop, "handle", handler)
            source = await human.send("gc_00000000", body="please handle")
            await loop.poll()
            assert await alice.talk_cursor("gc_00000000") == source["stream_id"]
            assert "handler unavailable" in caplog.text
            executor = MagicMock()
            loop = MessagingLoop(
                layout=AgentLayout.resident(tmp_path, "alice"),
                owner=CONNECTION.human,
                client=alice,
                executor=executor,
                threads=MagicMock(),
                get_agent_setup=MagicMock(),
                get_agent_state=MagicMock(),
            )
            executor.store.talk.get.return_value = None
            monkeypatch.setattr(loop, "handle", handler)
            await loop.poll()
            await loop.poll()
            assert handler.await_count == 1
            assert len(writes) == 1
            assert writes[0]["handler_error"] is True
            history = await human.history("gc_00000000")
            assert len(history) == (2 if fault == "lost-ack" else 1)
            assert "Unable to send message-handler failure reply" in caplog.text

    asyncio.run(scenario())
