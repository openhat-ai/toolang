"""Federation yields atomic per-agent suffixes without slow-source barriers."""

import asyncio
from contextlib import aclosing

from fakeredis import FakeServer

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
