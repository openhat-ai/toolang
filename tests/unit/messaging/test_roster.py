"""Local discovery, presence and membership cleanup preserve history."""

import asyncio
import json

from fakeredis import FakeServer
import pytest

from tests.unit.messaging.keys import PRESENCE, convo_key
from toolang.teaming.ids import dm_id
from toolang.teaming.roster import Roster
from tests.unit.messaging.test_protocol import client


def test_roster_tracks_discovery_without_inventing_online():
    async def scenario():
        server = FakeServer(server_type="valkey")
        found = {"agent:alice"}
        async with client(server, "human:owner") as human:
            roster = Roster(
                human.backend,
                root="root-one",
                owner=human.actor,
                discover=lambda: set(found),
            )
            await roster.scan()
            assert "agent:alice" in await roster.agents()
            assert not await human.backend.online("agent:alice")
            await roster.register("agent:alice", "lease", "", True)
            assert await human.backend.online("agent:alice")
            assert await human.backend._call("ZSCORE", PRESENCE, "agent:alice") > 0
            assert await human.backend._call("TTL", PRESENCE) == -1
            found.clear()
            await roster.scan()
            await roster.scan()
            assert (await roster.agents())["agent:alice"]["missing"] == 2

    asyncio.run(scenario())


def test_removal_clears_ordinary_membership_and_preserves_history_and_dms():
    async def scenario():
        server = FakeServer(server_type="valkey")
        found = {"agent:alice"}
        async with client(server, "human:owner") as human:
            backend = human.backend
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
            await backend.release_lease("agent:alice", "lease-one")
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
            assert dm_id(human.actor, "agent:alice") == old_direct
            assert "agent:alice" in (await human.conversation(old_direct)).participants
            found.add("agent:alice")
            await roster.scan()
            await roster.register("agent:alice", "lease-two", "", True)
            await human.send("agent:alice", body="Continue the direct conversation")
            assert dm_id(human.actor, "agent:alice") == old_direct
            assert await backend._call("XLEN", convo_key(old_direct, "messages")) == 2
            assert not await backend.renew_lease("agent:alice", "lease-one")

    asyncio.run(scenario())


def test_failed_scans_restart_and_foreign_roots_do_not_delete_agents():
    async def scenario():
        server = FakeServer(server_type="valkey")
        found = {"agent:alice"}
        async with client(server, "human:owner") as human:
            roster = Roster(
                human.backend,
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
                human.backend, root="root-two", owner=human.actor, discover=set
            )
            await other.scan()
            await other.scan()
            assert json.dumps(await roster.agents()) == before
            restarted = Roster(
                human.backend, root="root-one", owner=human.actor, discover=set
            )
            await restarted.scan()
            assert "agent:alice" in await restarted.agents()
            await restarted.scan()
            assert not await restarted.agents()

    asyncio.run(scenario())


pytestmark = pytest.mark.usefixtures("fixed_conversation_ids")
