"""Independent subscriptions, filtering, gaps, and sanitized local presence."""

import asyncio
import json

from fakeredis import FakeServer
import pytest

from tests.unit.messaging.test_protocol import client, snapshot
from toolang.teaming.client import HubClient
from toolang.teaming.errors import StorageIntegrityError
from toolang.teaming.keys import TEAM_EVENTS, PREFIX
from toolang.teaming.team_events import TeamEvents


def test_subscribers_checkpoint_before_snapshot_and_replay_independently():
    async def scenario():
        server = FakeServer(server_type="valkey")
        async with client(server, "human:owner") as human:
            feed = TeamEvents(human._backend)
            stream = feed.frames(human, None)
            checkpoint = await anext(stream)
            assert checkpoint.event == "checkpoint"
            convo = await human.create_conversation("Between checkpoint and snapshot")
            assert any(c["conversation"] == convo.id for c in await human.contacts())
            change = await anext(stream)
            assert change.event == "change" and change.data["conversation"] == convo.id
            replay = feed.frames(human, checkpoint.id)
            assert (await anext(replay)).data == change.data
            assert (await anext(stream)).event == "checkpoint"
            await stream.aclose()
            await replay.aclose()

    asyncio.run(scenario())


def test_filtered_events_advance_checkpoint_and_own_removal_remains_visible():
    async def scenario():
        server = FakeServer(server_type="valkey")
        async with (
            client(server, "human:owner") as human,
            client(server, "agent:alice") as alice,
        ):
            await alice.register("human:owner")
            feed = TeamEvents(human._backend)
            cursor = await feed.checkpoint()
            private = await human.create_conversation("Private")
            stream = feed.frames(alice, cursor)
            frame = await anext(stream)
            assert frame.event == "checkpoint" and frame.id != cursor
            await stream.aclose()
            cursor = frame.id
            await alice.join_conversation(private.id)
            await alice.leave_conversation(private.id)
            stream = feed.frames(alice, cursor)
            frame = await anext(stream)
            assert frame.event == "change"
            assert frame.data["type"] == "conversation.member_removed"
            assert frame.data["payload"] == {"member": alice.actor}
            assert (await anext(stream)).event == "checkpoint"
            await stream.aclose()

    asyncio.run(scenario())


def test_retention_gap_emits_resync_and_closes():
    async def scenario():
        async with client(FakeServer(server_type="valkey"), "human:owner") as human:
            feed = TeamEvents(human._backend)
            cursor = await feed.checkpoint()
            await human.create_conversation("New")
            await human._backend._client.xtrim(TEAM_EVENTS, maxlen=1, approximate=False)
            stream = feed.frames(human, cursor)
            assert (await anext(stream)).event == "resync_required"
            with pytest.raises(StopAsyncIteration):
                await anext(stream)

    asyncio.run(scenario())


def test_malformed_event_and_mixed_legacy_data_fail_without_writes():
    async def scenario():
        async with client(FakeServer(server_type="valkey"), "human:owner") as human:
            raw = human._backend._client
            await raw.hset(PREFIX + ":participants", "legacy", "old")
            before = await snapshot(raw)
            with pytest.raises(StorageIntegrityError):
                await human.check_backend()
            assert await snapshot(raw) == before
            await raw.delete(PREFIX + ":participants")
            feed = TeamEvents(human._backend)
            cursor = await feed.checkpoint()
            epoch = cursor.split(".")[1]
            await raw.xadd(
                TEAM_EVENTS,
                {
                    "data": json.dumps(
                        dict(
                            v=1,
                            epoch=epoch,
                            type="presence.online",
                            actor=None,
                            conversation=None,
                            payload={"member": "agent:alice", "token": "private"},
                        )
                    )
                },
            )
            before = await snapshot(raw)
            with pytest.raises(StorageIntegrityError):
                await human.team()
            with pytest.raises(StorageIntegrityError):
                await feed.replay(cursor)
            assert await snapshot(raw) == before

    asyncio.run(scenario())


def test_presence_snapshot_correction_is_local_and_never_writes(monkeypatch):
    async def scenario():
        server = FakeServer(server_type="valkey")
        async with (
            client(server, "human:owner") as human,
            client(server, "agent:alice") as alice,
        ):
            await alice.register("human:owner", endpoint="http://private")
            raw = human._backend._client
            before = await snapshot(raw)
            rows = await human.team()
            deadline = next(
                row["deadline"] for row in rows if row["member"] == alice.actor
            )
            for millis, online in [
                (deadline - 1, True),
                (deadline, False),
                (deadline + 1000, False),
            ]:
                monkeypatch.setattr(
                    "toolang.teaming.client.time.time", lambda: millis / 1000
                )
                projected = HubClient._presence(rows)
                assert (
                    next(
                        row["online"]
                        for row in projected
                        if row["member"] == alice.actor
                    )
                    is online
                )
                assert (
                    next(
                        row["online"]
                        for row in projected
                        if row["member"] == human.actor
                    )
                    is None
                )
            assert "online" not in rows[0]
            assert "private" not in json.dumps(rows)
            assert await snapshot(raw) == before

    asyncio.run(scenario())
