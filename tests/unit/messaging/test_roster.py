"""Local discovery, presence and membership cleanup preserve history."""

import asyncio
import json

from fakeredis import FakeServer
import pytest

from toolang.teaming.activity import ActivityBackend, HubActivity
from toolang.execution.activity import ActivityQuery
from toolang.execution.schemas import ActivitySnapshot
from toolang.teaming.keys import PREFIX, PRESENCE, convo_key
from toolang.teaming.ids import dm_id
from toolang.teaming.roster import Roster
from tests.unit.messaging.test_protocol import client


def test_roster_tracks_discovery_without_inventing_online_or_observations():
    async def scenario():
        server = FakeServer(server_type="valkey")
        found = {"agent:alice"}
        async with client(server, "human:owner") as human:
            roster = Roster(
                human._backend,
                root="root-one",
                owner=human.actor,
                discover=lambda: set(found),
            )
            await roster.scan()
            reader = HubActivity(human._backend, roster=roster)
            snapshot = (await reader.read(ActivityQuery()))[0]
            assert snapshot.agent == "agent:alice"
            assert snapshot.presence == "offline"
            assert (
                snapshot.observed is None and "last_seen" not in snapshot.model_dump()
            )
            assert snapshot.stats.cost is None and snapshot.stats.input_tokens is None
            await roster.register("agent:alice", "lease", "", True)
            assert await human._backend.online("agent:alice")
            assert await human._backend._call("ZSCORE", PRESENCE, "agent:alice") > 0
            assert await human._backend._call("TTL", PRESENCE) == -1
            found.clear()
            await roster.scan()
            await roster.scan()
            assert (await roster.agents())["agent:alice"]["missing"] == 2
            snapshot = (
                await HubActivity(human._backend, roster=roster).read(ActivityQuery())
            )[0]
            assert snapshot.presence == "online" and snapshot.home_missing

    asyncio.run(scenario())


def test_removal_clears_ordinary_membership_and_preserves_history_and_dms():
    async def scenario():
        server = FakeServer(server_type="valkey")
        found = {"agent:alice"}
        async with client(server, "human:owner") as human:
            backend = human._backend
            roster = Roster(
                backend,
                root="root-one",
                owner=human.actor,
                discover=lambda: set(found),
            )
            await roster.register("agent:alice", "lease-one", "", True)
            await human.send("gc_00000000", body="Shared history survives deletion")
            await human.send("agent:alice", body="Old direct history")
            old_direct = dm_id(human.actor, "agent:alice")
            assert old_direct is not None
            await ActivityBackend(backend).save(
                "agent:alice",
                "lease-one",
                [
                    ActivitySnapshot(
                        agent="agent:alice",
                        revision=1,
                        observed=10,
                        since="session",
                        recent=1800,
                    )
                ],
            )
            await backend.lease("agent:alice", "lease-one", 0)
            found.clear()
            await roster.scan()
            assert "agent:alice" in await roster.agents()
            await roster.scan()
            assert "agent:alice" not in await roster.agents()
            assert "agent:alice" not in await backend.participants()
            assert (
                "agent:alice"
                not in (await human.conversation("gc_00000000")).participants
            )
            assert (
                await backend._call("XLEN", convo_key("gc_00000000", "messages")) == 1
            )
            assert await backend._call("TTL", f"{PREFIX}:activity:agent:alice") == -1
            assert dm_id(human.actor, "agent:alice") == old_direct
            assert "agent:alice" in (await human.conversation(old_direct)).participants
            found.add("agent:alice")
            await roster.scan()
            await roster.register("agent:alice", "lease-two", "", True)
            await human.send("agent:alice", body="Continue the direct conversation")
            assert dm_id(human.actor, "agent:alice") == old_direct
            assert await backend._call("XLEN", convo_key(old_direct, "messages")) == 2
            assert not await backend.lease("agent:alice", "lease-one", 15)
            page = (
                await ActivityBackend(backend).cached("agent:alice", ActivityQuery())
            )[0]
            assert page.observed == 10

    asyncio.run(scenario())


def test_failed_scans_restart_and_foreign_roots_do_not_delete_agents():
    async def scenario():
        server = FakeServer(server_type="valkey")
        found = {"agent:alice"}
        async with client(server, "human:owner") as human:
            roster = Roster(
                human._backend,
                root="root-one",
                owner=human.actor,
                discover=lambda: set(found),
            )
            await roster.scan()
            before = json.dumps(await roster.agents())

            def fail():
                raise PermissionError("directory unavailable")

            roster.discover = fail
            with pytest.raises(PermissionError):
                await roster.scan()
            assert json.dumps(await roster.agents()) == before
            other = Roster(
                human._backend, root="root-two", owner=human.actor, discover=set
            )
            await other.scan()
            await other.scan()
            assert json.dumps(await roster.agents()) == before
            restarted = Roster(
                human._backend, root="root-one", owner=human.actor, discover=set
            )
            await restarted.scan()
            assert "agent:alice" in await restarted.agents()
            await restarted.scan()
            assert not await restarted.agents()

    asyncio.run(scenario())


pytestmark = pytest.mark.usefixtures("fixed_conversation_ids")
