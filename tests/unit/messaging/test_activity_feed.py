"""Federation yields atomic per-agent suffixes without slow-source barriers."""

import asyncio
from contextlib import aclosing

from fakeredis import FakeServer
import pytest

from toolang.cli.common.activity_view import Activity
from toolang.execution.activity import ActivityQuery
from toolang.execution.schemas import ActivitySnapshot
from toolang.teaming.activity import HubActivity
from toolang.teaming.activity_feed import HubActivityFeed
from tests.unit.messaging.test_protocol import client


def test_slow_source_independent_updates_roster_and_reconnect(monkeypatch):
    gates = {name: asyncio.Event() for name in ("agent:alice", "agent:bob")}

    async def source(self, http, agent, lease):
        await gates[agent].wait()
        self.replace(
            agent,
            [
                ActivitySnapshot(
                    agent=agent,
                    session=agent,
                    revision=2,
                    observed=10,
                    since="session",
                    recent=1800,
                )
            ],
        )
        await asyncio.Event().wait()

    monkeypatch.setattr(HubActivityFeed, "source", source)

    async def checkpoint(frames, state):
        async with asyncio.timeout(2):
            while True:
                event, data = await anext(frames)
                state.feed(event, data)
                if event == "activity_checkpoint":
                    return data

    async def scenario():
        server = FakeServer(server_type="valkey")
        async with client(server, "human:owner") as human:
            backend = human._backend
            for name in gates:
                await backend.register(
                    human.actor, agent=name, token=name, endpoint="http://source"
                )
            reader = HubActivity(backend)
            query = ActivityQuery()
            state = Activity(None)
            async with aclosing(reader.updates(query)) as frames:
                assert (await checkpoint(frames, state))["agents"] == sorted(gates)
                assert state.snapshots["agent:bob"].observed is None
                gates["agent:alice"].set()
                data = await checkpoint(frames, state)
                assert data == {"agents": ["agent:alice"], "replace": False}
                assert state.snapshots["agent:alice"].revision == 2
                assert "agent:bob" in state.snapshots
                # A second subscriber receives a complete baseline, not just the suffix.
                other = Activity(None)
                async with aclosing(reader.updates(query)) as second:
                    await checkpoint(second, other)
                    assert sorted(other.snapshots) == sorted(gates)
                    assert other.snapshots["agent:alice"].revision == 2
                from toolang.teaming.backend import PARTICIPANTS

                await backend._call("HDEL", PARTICIPANTS, "agent:bob")
                async with asyncio.timeout(2):
                    while "agent:bob" in state.snapshots:
                        event, data = await anext(frames)
                        state.feed(event, data)
                assert sorted(state.snapshots) == ["agent:alice"]
            assert not reader._feeds

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "revision,observed,token,expected",
    [
        (1, 10, "lease", (2, 20)),
        (2, 30, "lease", (2, 30)),
        (1, 10, "replacement", (1, 10)),
    ],
)
def test_live_frames_advance_independently_of_cache_without_regression(
    revision, observed, token, expected
):
    import json
    import httpx
    from toolang.teaming.activity import ActivityBackend

    async def scenario():
        consumed = asyncio.Event()
        page = ActivitySnapshot(
            agent="agent:alice",
            session="session",
            revision=2,
            observed=20,
            since="session",
            recent=1800,
        )

        class Frames(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield (
                    "event: activity_page\ndata: "
                    + page.model_dump_json()
                    + "\n\nevent: activity_checkpoint\ndata: "
                    + json.dumps({"agents": [page.agent]})
                    + "\n\n"
                ).encode()
                consumed.set()
                await asyncio.Event().wait()

        server = FakeServer(server_type="valkey")
        async with client(server, "human:owner") as human:
            backend = ActivityBackend(human._backend)
            await human._backend.register(
                human.actor, agent=page.agent, token="lease", endpoint="http://agent"
            )
            # A REST reader or independent publication observed the same revision later.
            await backend.save(
                page.agent, "lease", [page.model_copy(update={"observed": 21})]
            )
            lease = await backend.lease(page.agent)
            if token != "lease":
                await human._backend.lease(page.agent, "lease", 0)
                await human._backend.register(
                    human.actor, agent=page.agent, token=token, endpoint="http://agent"
                )
            reader = HubActivity(human._backend)
            feed = HubActivityFeed(reader, ActivityQuery())
            feed.replace(
                page.agent,
                [page.model_copy(update={"revision": revision, "observed": observed})],
            )
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda _: httpx.Response(
                        200,
                        headers={"content-type": "text/event-stream"},
                        stream=Frames(),
                    )
                )
            ) as http:
                task = asyncio.create_task(feed.source(http, page.agent, lease))
                drained = asyncio.create_task(consumed.wait())
                try:
                    async with asyncio.timeout(2):
                        await asyncio.wait(
                            {task, drained}, return_when=asyncio.FIRST_COMPLETED
                        )
                    displayed = feed.pages[page.agent][0]
                    assert (displayed.revision, displayed.observed) == expected
                    assert not feed.pages[page.agent][0].stale
                    # The display advances without downgrading the more recent cache.
                    assert (await backend.cached(page.agent, ActivityQuery()))[
                        0
                    ].observed == 21
                finally:
                    task.cancel()
                    drained.cancel()
                    await asyncio.gather(task, drained, return_exceptions=True)

    asyncio.run(scenario())
