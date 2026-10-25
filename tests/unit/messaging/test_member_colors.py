"""Member colors share the directory lifecycle and stay cosmetic."""

import asyncio
from collections import Counter
import json

from fakeredis import FakeServer
import httpx
import pytest
from valkey.exceptions import ResponseError

from tests.unit.messaging.test_protocol import client, CONFIG
from toolang.teaming.api import create_app
from toolang.teaming.backend.valkey import scripts
from tests.unit.messaging.keys import PREFIX, TEAM_MEMBERS
from toolang.teaming.client import HubClient
from toolang.teaming.schemas import HubConnection
from toolang.teaming.types import MEMBER_COLORS


async def set_color(raw, member, color):
    """Apply the documented atomic edit without replacing stale lease data."""
    await raw.eval(
        """
local raw = redis.call('HGET', KEYS[1], ARGV[1])
if not raw then return redis.error_reply('Unknown team member') end
local member = cjson.decode(raw)
member.color = cjson.decode(ARGV[2])
return redis.call('HSET', KEYS[1], ARGV[1], cjson.encode(member))
""",
        1,
        TEAM_MEMBERS,
        member,
        json.dumps(color),
    )


def test_automatic_colors_prefer_unused_entries_then_balance_reuse():
    async def scenario():
        async with client(FakeServer(server_type="valkey"), "human:owner") as human:
            for name in ("owner", "alice", "bob", "carol", "dave"):
                await human.backend.register(
                    human.actor, agent=f"agent:{name}", token=name
                )
            rows = await human.team()
            assert len(rows) == 6
            assert {row["color"] for row in rows} == set(MEMBER_COLORS)
            assert (
                next(row for row in rows if row["member"] == human.actor)["color"]
                != next(row for row in rows if row["member"] == "agent:owner")["color"]
            )
            for number in range(7):
                await human.backend.register(
                    human.actor, agent=f"agent:extra{number}", token=str(number)
                )
            counts = Counter(row["color"] for row in await human.team())
            assert set(counts) == set(MEMBER_COLORS)
            assert max(counts.values()) - min(counts.values()) == 1

    asyncio.run(scenario())


def test_concurrent_registration_uses_distinct_free_colors():
    async def scenario():
        async with client(FakeServer(server_type="valkey"), "human:owner") as human:
            await asyncio.gather(
                *(
                    human.backend.register(
                        human.actor, agent=f"agent:member{number}", token=str(number)
                    )
                    for number in range(5)
                )
            )
            colors = [row["color"] for row in await human.team()]
            assert len(colors) == len(set(colors)) == 6

    asyncio.run(scenario())


def test_member_created_with_its_new_owner_also_receives_a_distinct_color():
    async def scenario():
        async with client(FakeServer(server_type="valkey"), "agent:alice") as agent:
            await agent.register("human:owner")
            assert {row["color"] for row in await agent.team()} == {"red", "green"}

    asyncio.run(scenario())


def test_manual_edits_preserve_leases_and_survive_reregistration_and_restart():
    async def scenario():
        server = FakeServer(server_type="valkey")
        async with client(server, "human:owner") as human:
            backend, raw = human.backend, human.backend._client
            await backend.register(human.actor, agent="agent:alice", token="first")
            await backend.release_lease("agent:alice", "first")
            await backend.register(
                human.actor, agent="agent:alice", token="replacement", endpoint="new"
            )
            before = json.loads(await raw.hget(TEAM_MEMBERS, "agent:alice"))
            await set_color(raw, "agent:alice", "#7AA2F7")
            assert json.loads(await raw.hget(TEAM_MEMBERS, "agent:alice")) == {
                **before,
                "color": "#7AA2F7",
            }
            await set_color(raw, human.actor, "bright_magenta")
            await backend.renew_lease("agent:alice", "replacement")
            await backend.register(
                human.actor, agent="agent:alice", token="replacement"
            )
            await human.register_human()
            with pytest.raises(ResponseError, match="Unknown team member"):
                await set_color(raw, "agent:unknown", "cyan")
            assert not await raw.hexists(TEAM_MEMBERS, "agent:unknown")
        async with client(server, "human:owner") as restarted:
            colors = {row["member"]: row["color"] for row in await restarted.team()}
            assert colors == {"human:owner": "bright_magenta", "agent:alice": "#7AA2F7"}

    asyncio.run(scenario())


def test_member_colors_are_excluded_from_execution_publication():
    async def scenario():
        async with client(FakeServer(server_type="valkey"), "human:owner") as human:
            backend, raw = human.backend, human.backend._client
            await backend.register(human.actor, agent="agent:alice", token="lease")
            await set_color(raw, "agent:alice", "bright_cyan")
            before = await raw.hget(TEAM_MEMBERS, "agent:alice")
            await backend.renew_lease("agent:alice", "lease")
            await human.send("agent:alice", body="still available")
            await backend.events.initialize()
            _, _, participants = await backend.events.capture()
            assert all(
                set(json.loads(record)) == {"display_name", "owner", "created_at"}
                for record in participants.values()
            )
            assert await raw.hget(TEAM_MEMBERS, "agent:alice") == before

    asyncio.run(scenario())


def test_color_snapshot_is_revalidated_if_an_existing_color_changes(monkeypatch):
    async def scenario():
        async with client(FakeServer(server_type="valkey"), "human:owner") as human:
            evaluate = human.backend._eval
            changed = False

            async def edit_before_creation(script, keys, args):
                nonlocal changed
                if script == scripts.PRESENCE and not changed:
                    changed = True
                    await set_color(human.backend._client, human.actor, "green")
                return await evaluate(script, keys, args)

            monkeypatch.setattr(human.backend, "_eval", edit_before_creation)
            await human.backend.register(
                human.actor, agent="agent:alice", token="lease"
            )
            colors = {row["member"]: row["color"] for row in await human.team()}
            assert changed and colors == {human.actor: "green", "agent:alice": "red"}

    asyncio.run(scenario())


def test_existing_team_route_exposes_colors_without_leases_or_new_routes():
    async def scenario():
        human = client(FakeServer(server_type="valkey"), "human:owner")
        app = create_app(human.config, backend=human.backend, version="test")
        async with (
            app.router.lifespan_context(app),
            HubClient(
                HubConnection("http://hub", CONFIG.identity),
                actor=human.actor,
                transport=httpx.ASGITransport(app),
            ) as hub,
        ):
            await human.register_human()
            await set_color(human.backend._client, human.actor, "#ABCDEF")
            rows = await hub.team()
            assert rows[0]["color"] == "#ABCDEF"
            assert "lease" not in rows[0]
            assert not any("color" in path for path in app.openapi()["paths"])

    asyncio.run(scenario())


def test_all_teaming_keys_use_v0_namespace():
    async def scenario():
        server = FakeServer(server_type="valkey")
        async with client(server, "human:owner") as human:
            raw = human.backend._client
            await human.backend.events.initialize()
            assert PREFIX == f"toolang:{human.config.root_code}:v0"
            keys = await raw.keys("*")
            assert keys and all(key.startswith(PREFIX + ":") for key in keys)

    asyncio.run(scenario())
