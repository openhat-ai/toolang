"""Protocol validation must precede every atomic storage mutation."""

import asyncio
import json

from fakeredis import FakeServer
import pytest

from tests.unit.messaging.test_protocol import client, snapshot
from toolang.teaming.errors import StorageIntegrityError
from toolang.teaming.backend.valkey.keys import CONVOS


@pytest.mark.parametrize(
    "field,value", [("name", " invalid "), ("created_by", "agent:")]
)
def test_invalid_conversation_record_rejects_membership_without_writes(field, value):
    async def scenario():
        server = FakeServer(server_type="valkey")
        async with (
            client(server, "human:owner") as human,
            client(server, "agent:alice") as alice,
        ):
            await alice.register(human.actor)
            conversation = await human.create_conversation("Original")
            raw = human.backend._client
            record = json.loads(await raw.hget(CONVOS, conversation.id))
            record[field] = value
            await raw.hset(CONVOS, conversation.id, json.dumps(record))
            before = await snapshot(raw)
            with pytest.raises(StorageIntegrityError):
                await alice.join_conversation(conversation.id)
            assert await snapshot(raw) == before

    asyncio.run(scenario())


def test_changed_record_is_revalidated_before_commit(monkeypatch):
    from toolang.teaming.backend.valkey import scripts
    from toolang.teaming.backend.valkey.keys import TEAM_EVENTS, convo_key

    async def scenario():
        server = FakeServer(server_type="valkey")
        async with (
            client(server, "human:owner") as human,
            client(server, "agent:alice") as alice,
        ):
            await alice.register(human.actor)
            convo = await human.create_conversation("Original")
            raw = human.backend._client
            events = await raw.xlen(TEAM_EVENTS)
            evaluate = alice.backend._eval
            changed = False

            async def corrupt_after_validation(script, keys, args):
                nonlocal changed
                if script == scripts.EDIT and not changed:
                    changed = True
                    record = json.loads(await raw.hget(CONVOS, convo.id))
                    record["name"] = " invalid "
                    await raw.hset(CONVOS, convo.id, json.dumps(record))
                return await evaluate(script, keys, args)

            monkeypatch.setattr(alice.backend, "_eval", corrupt_after_validation)
            with pytest.raises(StorageIntegrityError):
                await alice.join_conversation(convo.id)
            assert changed
            assert not await raw.sismember(convo_key(convo.id, "members"), alice.actor)
            assert await raw.xlen(TEAM_EVENTS) == events

    asyncio.run(scenario())


def test_team_read_revalidates_a_concurrently_added_member(monkeypatch):
    from toolang.teaming.backend.valkey import scripts
    from toolang.teaming.backend.valkey.keys import TEAM

    async def scenario():
        async with client(FakeServer(server_type="valkey"), "human:owner") as human:
            evaluate = human.backend._eval
            changed = False

            async def add_after_validation(script, keys, args):
                nonlocal changed
                if script == scripts.READ and not changed:
                    changed = True
                    await human.backend._client.hset(TEAM, "agent:bad", "{}")
                return await evaluate(script, keys, args)

            monkeypatch.setattr(human.backend, "_eval", add_after_validation)
            with pytest.raises(StorageIntegrityError):
                await human.team()

    asyncio.run(scenario())


def test_roster_is_a_team_subset_and_cas_preserves_json_semantics():
    from toolang.teaming.backend.valkey.keys import ROSTER, TEAM
    from toolang.teaming.roster import Roster

    async def scenario():
        async with client(FakeServer(server_type="valkey"), "human:owner") as human:
            found = {"agent:alice"}
            roster = Roster(
                human.backend,
                root="root-中文",
                owner=human.actor,
                discover=lambda: found,
            )
            await roster.scan()
            raw = human.backend._client
            # Whitespace, key ordering and Unicode escaping do not change a record.
            await raw.hset(
                ROSTER,
                "agent:alice",
                json.dumps({"missing": 0, "managed": True, "root": "root-中文"}),
            )
            found.clear()
            await roster.scan()
            assert (await roster.agents())["agent:alice"]["missing"] == 1
            await raw.hdel(TEAM, "agent:alice")
            before = await snapshot(raw)
            with pytest.raises(StorageIntegrityError):
                await roster.scan()
            assert await snapshot(raw) == before

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "changes",
    [{"id": "group:old"}, {"revision": 0}, {"revision": True}, {"created_at": None}],
)
def test_persisted_conversation_cannot_represent_a_pending_dm(changes):
    from toolang.teaming.errors import MessagingError
    from toolang.teaming.schemas import Conversation, PendingDM

    values = dict(
        id="dm_00000001",
        kind="dm",
        participants=("agent:alice", "human:owner"),
        name=None,
        created_by="human:owner",
        created_at="2026-10-10T00:00:00Z",
        updated_at="2026-10-10T00:00:00Z",
        revision=1,
    )
    with pytest.raises((ValueError, MessagingError)):
        Conversation(**(values | changes))
    pending = PendingDM("dm_00000001", ("agent:alice", "human:owner"))
    assert not hasattr(pending, "revision") and not hasattr(pending, "created_at")


@pytest.mark.parametrize("fields", [{"unexpected": "missing data"}, {"data": "{}"}])
def test_event_without_a_valid_envelope_rejects_creation_without_writes(fields):
    from toolang.teaming.backend.valkey.keys import TEAM_EVENTS

    async def scenario():
        async with client(FakeServer(server_type="valkey"), "human:owner") as human:
            raw = human.backend._client
            await raw.xadd(TEAM_EVENTS, fields)
            before = await snapshot(raw)
            with pytest.raises(StorageIntegrityError):
                await human.create_conversation("Rejected")
            assert await snapshot(raw) == before

    asyncio.run(scenario())
