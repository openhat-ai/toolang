"""Latest snapshots recover independently of missed events and slow sources."""

import asyncio
from contextlib import aclosing
import json

import httpx
import pytest

from toolang.cli.common.activity_view import Activity
from toolang.execution.activity import ActivityQuery
from toolang.execution.schemas import ActivitySnapshot
from toolang.teaming.client import HubClient
from toolang.teaming.hub_observation import HubObservation
from toolang.teaming.schemas import HubConnection
from toolang.teaming.notifications import Changes


@pytest.fixture
def notifications_only(monkeypatch):
    wait = Changes.wait

    async def without_timer(self, timeout=None):
        await wait(self)

    monkeypatch.setattr(Changes, "wait", without_timer)


class Events(httpx.AsyncByteStream):
    def __init__(self, checkpoint):
        self.checkpoint = checkpoint
        self.queue = asyncio.Queue()
        self.closed = False
        self.handled = None

    async def __aiter__(self):
        yield f"event: {self.checkpoint}\ndata: {{}}\n\n".encode()
        while True:
            item, handled = await self.queue.get()
            self.handled = handled
            if item is None:
                handled.set()
                return
            yield item
            handled.set()

    async def send(self, event, data=None):
        handled = asyncio.Event()
        await self.queue.put(
            (
                f"event: {event}\ndata: {json.dumps(data or {})}\n\n".encode()
                if event is not None
                else None,
                handled,
            )
        )
        await asyncio.wait_for(handled.wait(), 2)

    async def aclose(self):
        self.closed = True
        if self.handled:
            self.handled.set()


class Hub:
    def __init__(self):
        self.roster = {"agent:alice": "online", "agent:bob": "online"}
        self.streams = {}
        self.connections = []
        self.requests = []
        self.gate = asyncio.Event()
        self.started = asyncio.Event()
        self.block = "agent:alice"
        self.revision = 1
        self.failure = False

    async def request(self, request):
        self.requests.append(request)
        path = request.url.path
        if path in {"/team/events", "/events/stream"}:
            assert "after" not in request.url.params
            stream = Events(
                "checkpoint" if path == "/team/events" else "stream_checkpoint"
            )
            self.streams[path] = stream
            self.connections.append(stream)
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, stream=stream
            )
        if path == "/activity/roster":
            return httpx.Response(200, json=dict(self.roster))
        assert path == "/activity"
        agent = request.url.params["agent"]
        revision = self.revision
        if agent == self.block:
            self.started.set()
            await self.gate.wait()
        if self.failure and agent == "agent:alice":
            return httpx.Response(500)
        return httpx.Response(
            200,
            json=[
                ActivitySnapshot(
                    agent=agent,
                    revision=revision,
                    observed=revision,
                    since="session",
                    recent=1800,
                    # A delayed activity response must not own presence.
                    presence="online",
                ).model_dump()
            ],
        )


async def until(stream, state, condition):
    async with asyncio.timeout(5):
        while True:
            event, data = await anext(stream)
            state.feed(event, data)
            if event == "observation_status" and condition():
                return


def test_top_presence_and_healthy_agent_do_not_wait_for_slow_or_failed_history(
    notifications_only,
):
    async def scenario():
        hub = Hub()
        state = Activity(None)
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(hub.request), base_url="http://hub"
        ) as http:
            async with aclosing(
                HubObservation(http).updates(ActivityQuery())
            ) as stream:
                await until(
                    stream,
                    state,
                    lambda: (
                        state.snapshots.get("agent:bob") is not None
                        and state.snapshots["agent:bob"].revision == 1
                    ),
                )
                assert hub.started.is_set()
                assert state.snapshots["agent:alice"].observed is None
                hub.roster["agent:alice"] = "offline"
                await hub.streams["/team/events"].send(
                    "change", {"type": "presence.offline"}
                )
                await until(
                    stream,
                    state,
                    lambda: state.snapshots["agent:alice"].presence == "offline",
                )
                hub.failure = True
                hub.gate.set()
                await until(
                    stream,
                    state,
                    lambda: state.snapshots["agent:alice"].stale,
                )
                assert state.snapshots["agent:bob"].presence == "online"
                assert not state.snapshots["agent:bob"].stale
                assert state.source_reconnecting and not state.reconnecting
        assert all(stream.closed for stream in hub.connections)
        assert not any(r.url.path == "/activity/stream" for r in hub.requests)

    asyncio.run(scenario())


def test_changes_during_snapshot_trigger_another_read_and_fresh_reconnect(
    notifications_only,
):
    async def scenario():
        hub = Hub()
        state = Activity(None)
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(hub.request), base_url="http://hub"
        ) as http:
            async with aclosing(
                HubObservation(http).updates(ActivityQuery())
            ) as stream:
                await until(stream, state, hub.started.is_set)
                hub.revision = 2
                for _ in range(8):
                    await hub.streams["/events/stream"].send(
                        "run_end", {"agent": "agent:alice"}
                    )
                hub.gate.set()
                await until(
                    stream,
                    state,
                    lambda: state.snapshots["agent:alice"].revision == 2,
                )
                # All disconnected transitions collapse to the latest snapshot.
                member_stream = hub.streams["/team/events"]
                await member_stream.send("resync_required")
                hub.roster = {"agent:bob": "offline"}
                await until(
                    stream,
                    state,
                    lambda: (
                        set(state.snapshots) == {"agent:bob"}
                        and state.snapshots["agent:bob"].presence == "offline"
                    ),
                )
                assert member_stream.closed
                assert hub.streams["/team/events"] is not member_stream
                old = hub.streams["/events/stream"]
                await old.send(None)
                hub.revision = 3
                await until(
                    stream,
                    state,
                    lambda: state.snapshots["agent:bob"].revision == 3,
                )
                assert old.closed
        assert all(stream.closed for stream in hub.connections)

    asyncio.run(scenario())


def test_idle_activity_refreshes_without_execution_notifications():
    async def scenario():
        hub = Hub()
        hub.gate.set()
        state = Activity(None)
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(hub.request), base_url="http://hub"
        ) as http:
            async with aclosing(
                HubObservation(http).updates(ActivityQuery())
            ) as stream:
                await until(
                    stream,
                    state,
                    lambda: (
                        state.snapshots.get("agent:alice") is not None
                        and state.snapshots["agent:alice"].revision == 1
                    ),
                )
                hub.revision = 2
                await until(
                    stream,
                    state,
                    lambda: state.snapshots["agent:alice"].revision == 2,
                )

    asyncio.run(scenario())


def test_talk_member_subscription_filters_unrelated_changes_and_preserves_headers():
    async def scenario():
        hub = Hub()
        connection = HubConnection("http://hub", "human:owner", "backend")
        async with HubClient(
            connection, transport=httpx.MockTransport(hub.request)
        ) as client:
            async with client.team_changes("gc_00000001") as changes:
                async with asyncio.timeout(2):
                    await changes.wait()
                assert changes.connected
                stream = hub.streams["/team/events"]
                await stream.send(
                    "change",
                    {"type": "conversation.member_added", "conversation": "other"},
                )
                assert not changes.changed.is_set()
                await stream.send(
                    "change",
                    {
                        "type": "conversation.member_removed",
                        "conversation": "gc_00000001",
                    },
                )
                assert changes.changed.is_set()
        assert stream.closed
        assert [r.url.path for r in hub.requests] == ["/team/events"]
        assert hub.requests[0].headers["X-Toolang-Backend"] == "backend"
        assert hub.requests[0].headers["X-Toolang-Human"] == "human%3Aowner"

    asyncio.run(scenario())
