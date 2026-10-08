"""Offline protocol acceptance tests against an in-memory Valkey implementation."""

import asyncio
import json
from uuid import UUID

from fakeredis import FakeAsyncValkey
import pytest
from valkey.exceptions import ConnectionError, ResponseError

from toolang.messaging.client import MessagingClient, group_key, online_key
from toolang.messaging.config import MessagingConfig
from toolang.messaging.errors import MessagingError, SendUnconfirmed
from toolang.messaging.schemas import (
    Message,
    agent_dm,
    component,
    conversation,
    owner_dm,
    stream_id,
)


@pytest.mark.parametrize(
    "name", ["alice", "a_b", "a%b", "a:b", "a.b~", "中文", "with space"]
)
def test_names_round_trip_without_separator_collisions(name):
    encoded = component(name)
    assert "_" not in encoded and ":" not in encoded
    assert conversation(owner_dm(name)).names == (name,)
    assert conversation("gc_" + encoded).names == (name,)
    assert conversation(agent_dm(name, "ZZ")).names == tuple(
        sorted((name, "ZZ"), key=str.encode)
    )


@pytest.mark.parametrize(
    "group",
    [
        "g_dev",
        "gc_",
        "dm_b_a",
        "dm_a_a",
        "dm_a_b_c",
        "gc_a:b",
        "gc_%61",
        "gc_%ff",
        "dm_a%5fb",
        "gc_%00",
    ],
)
def test_noncanonical_names_fail(group):
    with pytest.raises(MessagingError):
        conversation(group)


def test_message_validation():
    message = Message.create("owner", 'literal $x {{file}}\n"quote"')
    UUID(message.id)
    assert Message.decode(message.encode()) == message
    for raw in ("[]", "null", "{}", '{"id":"x","sender":"a","body":1}', "broken"):
        with pytest.raises(MessagingError):
            Message.decode(raw)
    assert stream_id("100-10") > stream_id("100-9")


def test_registration_offline_dms_membership_and_leases():
    async def scenario():
        redis = FakeAsyncValkey(decode_responses=True)
        async with MessagingClient(
            MessagingConfig("redis://test", ("gc_dev",)), client=redis
        ) as a:
            await a.register("alice", "human", "token-a")
            await a.register("bob", "human", "token-b")
            with pytest.raises(MessagingError, match="already online"):
                await a.register("alice", "human", "duplicate")
            for name, owner in (
                ("human", "alice"),
                ("third", "alice"),
                ("alice", "other"),
            ):
                with pytest.raises(MessagingError):
                    await a.register(name, owner, "bad")
            await a.unregister("alice", "wrong-token")
            assert await redis.get(online_key("alice")) == "token-a"
            await redis.set(online_key("alice"), "replacement")
            with pytest.raises(MessagingError, match="lease lost"):
                await a.renew("alice", "token-a")
            await a.unregister("alice", "token-a")
            assert await redis.get(online_key("alice")) == "replacement"
            await a.unregister("alice", "replacement")
            assert await a.resolve("alice") == "dm_alice"
            await a.send("dm_alice", sender="human", body="available when you return")
            with pytest.raises(ResponseError, match="offline"):
                await a.send("all", sender="alice", agent=True, body="no")
            await a.register("alice", "human", "new-token")
            assert 0 < await redis.ttl(online_key("alice")) <= 30
            receipt = await a.send(
                "dm_alice_bob",
                sender="alice",
                agent=True,
                run="run_test",
                body="hi",
                in_reply_to="original",
            )
            assert receipt["message"]["origin"] == {"agent": "alice", "run": "run_test"}
            assert receipt["message"]["in_reply_to"] == "original"
            assert set(
                await redis.execute_command(
                    "SMEMBERS", group_key("dm_alice_bob", "members")
                )
            ) == {
                "alice",
                "bob",
            }
            with pytest.raises(ResponseError, match="not a member"):
                await a.send("dm_bob", sender="alice", agent=True, body="private")
            assert {g["group"] for g in await a.contacts(agent="alice")} == {
                "all",
                "dm_alice",
                "dm_alice_bob",
                "gc_dev",
            }
            await a.unregister("bob", "token-b")
            groups = await a.contacts(include_dms=False)
            assert groups[0]["members"] == ["alice", "bob", "human"]
            assert groups[0]["online"] == ["alice"]

    asyncio.run(scenario())


def test_independent_readers_retention_and_ambiguous_targets():
    async def scenario():
        redis = FakeAsyncValkey(decode_responses=True)
        async with MessagingClient(
            MessagingConfig("redis://test", ("gc_alice",)), client=redis
        ) as client:
            await client.register("alice", "owner", "token")
            with pytest.raises(MessagingError, match="Ambiguous"):
                await client.resolve("alice")
            assert await client.resolve("alice", kind="dm") == "dm_alice"
            assert await client.resolve("alice", kind="group") == "gc_alice"
            with pytest.raises(MessagingError, match="Unknown"):
                await client.resolve("missing")
            key = group_key("all", "msg")
            for sid in ("100-9", "100-10", "101-0"):
                await redis.xadd(
                    key, {"data": Message.create("owner", sid).encode()}, id=sid
                )
            first = await client.read("all", after="100-9", count=1)
            second = await client.read("all", after="100-9", count=1)
            assert first == second and first[0][0] == "100-10"
            assert [sid for sid, _ in await client.history("all", count=2)] == [
                "100-10",
                "101-0",
            ]
            await redis.xtrim(key, maxlen=1, approximate=False)
            assert "no longer retained" in (
                await client.check_cursor("all", "100-10") or ""
            )
            with pytest.raises(MessagingError, match="precedes"):
                await client.check_cursor("all", "102-0")
            await redis.delete(key)
            assert "missing" in (await client.check_cursor("all", "101-0") or "")

    asyncio.run(scenario())


def test_contact_previews_are_optional_bounded_and_membership_filtered():
    async def scenario():
        redis = FakeAsyncValkey(decode_responses=True)
        async with MessagingClient(
            MessagingConfig("redis://test"), client=redis
        ) as client:
            for name in ("alice", "bob", "carol"):
                await client.register(name, "owner", name)
            await client.send("dm_bob_carol", sender="bob", agent=True, body="private")
            await client.send("dm_alice", sender="owner", body="x" * 300)
            await redis.xadd(group_key("all", "msg"), {"data": "malformed"})
            assert all("preview" not in group for group in await client.contacts())
            groups = {
                group["group"]: group
                for group in await client.contacts(agent="alice", include_preview=True)
            }
            assert set(groups) == {"all", "dm_alice"}
            assert groups["all"]["preview"] is None
            assert groups["dm_alice"]["preview"] == {
                "sender": "owner",
                "body": "x" * 160,
            }
            owner_groups = {
                group["group"]: group
                for group in await client.contacts(include_preview=True)
            }
            assert owner_groups["dm_bob_carol"]["preview"]["body"] == "private"
            assert owner_groups["dm_bob"]["preview"] is None

    asyncio.run(scenario())


def test_uncertain_send_is_not_retried_and_returns_recoverable_id(monkeypatch):
    async def scenario():
        redis = FakeAsyncValkey(decode_responses=True)
        async with MessagingClient(
            MessagingConfig("redis://test"), client=redis
        ) as client:
            await client.register("alice", "owner", "token")
            original = redis.execute_command
            writes = []

            async def execute(*args, **kwargs):
                if args[0] == "EVAL" and "XADD" in args[1]:
                    writes.append(args)
                    await original(*args, **kwargs)
                    raise ConnectionError("reply lost after acceptance")
                return await original(*args, **kwargs)

            monkeypatch.setattr(redis, "execute_command", execute)
            with pytest.raises(SendUnconfirmed) as error:
                await client.send("all", sender="owner", body="only once")
            assert len(writes) == 1
            entries = await client.history("all")
            assert len(entries) == 1
            assert json.loads(entries[0][1]["data"])["id"] in str(error.value)

    asyncio.run(scenario())
