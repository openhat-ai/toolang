"""Offline tests for typed targets, authoritative membership, and delivery."""

import asyncio
import json
from uuid import UUID, uuid4

from fakeredis import FakeAsyncValkey, FakeServer
import pytest
from valkey.exceptions import ConnectionError

from toolang.teaming.backend import Backend, DIRECT, PARTICIPANTS, group_key, online_key
from toolang.teaming.messaging import MessagingClient
from toolang.teaming.config import BackendConfig
from toolang.teaming.errors import MessagingError, SendUnconfirmed
from toolang.teaming.schemas import Message, direct_pair, identifier, target, stream_id

CONFIG = BackendConfig("redis://test")


def client(server, actor, token=None):
    raw = FakeAsyncValkey(server=server, decode_responses=True)
    return MessagingClient(
        CONFIG, actor=actor, token=token, backend=Backend(CONFIG, client=raw)
    )


@pytest.mark.parametrize(
    "name", ["alice", "a_b", "a-b", "a.b", "中文", "é", "e\u0301", "42"]
)
def test_readable_identifiers_preserve_exact_spelling(name):
    assert identifier(name) == name
    for kind in ("agent", "human", "group"):
        assert target(f"{kind}:{name}").id == f"{kind}:{name}"
    assert direct_pair("agent:alice", "human:alice") == '["agent:alice","human:alice"]'
    assert direct_pair("human:中文", "agent:alice") == direct_pair(
        "agent:alice", "human:中文"
    )


@pytest.mark.parametrize(
    "name",
    ["", "a:b", "a/b", "a%b", "a b", "a\n", "a\x00", "-abc", "_abc", ".abc", "a~b"],
)
def test_invalid_identifiers_fail(name):
    with pytest.raises(MessagingError):
        identifier(name)


@pytest.mark.parametrize(
    "value", ["alice", "Agent:alice", "group:", "robot:alice", "agent:a:b"]
)
def test_invalid_targets_fail(value):
    with pytest.raises(MessagingError):
        target(value)


def test_message_validation():
    message = Message.create("human:owner", 'literal $x {{file}}\n"quote"')
    UUID(message.id)
    assert Message.decode(message.encode()) == message
    for raw in ("[]", "null", "{}", '{"id":"x","sender":"a","body":1}', "broken"):
        with pytest.raises(MessagingError):
            Message.decode(raw)
    for body in ("", " \n", "x" * (256 * 1024 + 1)):
        with pytest.raises(MessagingError):
            Message.create("human:owner", body)
    with pytest.raises(MessagingError):
        Message.create("group:all", "invalid sender")
    with pytest.raises(MessagingError):
        Message.create("human:owner", "invalid reply", "not-a-uuid")
    with pytest.raises(MessagingError):
        Message.create(
            "human:owner", "invalid origin", origin={"thread": None, "run": "r"}
        )
    assert stream_id("100-10") > stream_id("100-9")


def test_direct_messages_are_unique_immutable_and_only_allow_participants():
    async def scenario():
        server = FakeServer(server_type="valkey")
        async with (
            client(server, "agent:alice") as alice,
            client(server, "agent:bob") as bob,
            client(server, "human:owner") as human,
        ):
            await alice.register("human:owner")
            await bob.register("human:owner")
            groups = await asyncio.gather(
                *(
                    alice.resolve("agent:bob") if i % 2 else bob.resolve("agent:alice")
                    for i in range(20)
                )
            )
            assert len(set(groups)) == 1
            group = groups[0]
            info = await human.conversation(group)
            assert info.kind == "direct" and info.members == (
                "agent:alice",
                "agent:bob",
            )
            with pytest.raises(MessagingError, match="read-only"):
                await human.send(group, body="cannot join")
            for actor in (alice, human):
                with pytest.raises(MessagingError, match="direct/system"):
                    await actor.join_group(group)
                with pytest.raises(MessagingError, match="direct/system"):
                    await actor.leave_group(group)
            first = await alice.send(
                group, body="hello", run="r", in_reply_to=str(uuid4())
            )
            await bob.send(group, body="reply", in_reply_to=first["message"]["id"])
            assert first["message"]["origin"] == {"thread": None, "run": "r"}
            assert len(await human.history(group)) == 2
            own = await human.resolve("agent:alice")
            assert own != group and await alice.resolve("human:owner") == own
            await human.send(own, body="owner message")
            assert len(await alice.history(own)) == 1
            with pytest.raises(MessagingError, match="not a member"):
                await bob.history(own)
            with pytest.raises(MessagingError):
                await alice.resolve("agent:alice")
            raw = FakeAsyncValkey(server=server, decode_responses=True)
            assert await raw.execute_command("HLEN", DIRECT) == 2

    asyncio.run(scenario())


def test_membership_survives_registration_and_is_only_changed_explicitly():
    async def scenario():
        server = FakeServer(server_type="valkey")
        async with (
            client(server, "human:owner") as human,
            client(server, "agent:alice") as alice,
        ):
            await alice.register("human:owner")
            await human.create_group("dev")
            with pytest.raises(MessagingError, match="not a member"):
                await alice.send("group:dev", body="not joined")
            await alice.join_group("group:dev")
            await alice.send("group:dev", body="joined")
            await alice.unregister()
            await alice.register("human:owner")
            assert "agent:alice" in (await human.conversation("group:dev")).members
            await alice.leave_group("group:dev")
            await alice.register("human:owner")
            assert "agent:alice" not in (await human.conversation("group:dev")).members
            assert {g["group"] for g in await alice.contacts()} == {"group:all"}
            with pytest.raises(MessagingError, match="exists"):
                await human.create_group("dev")
            for method in (human.join_group, human.leave_group):
                with pytest.raises(MessagingError, match="direct/system"):
                    await method("group:all")
            with pytest.raises(MessagingError, match="reserved"):
                await human.create_group("all")

    asyncio.run(scenario())


def test_lease_token_protects_writes_renewal_release_and_owner():
    async def scenario():
        server = FakeServer(server_type="valkey")
        raw = FakeAsyncValkey(server=server, decode_responses=True)
        async with (
            client(server, "agent:alice", "old") as old,
            client(server, "agent:alice", "new") as replacement,
            client(server, "human:owner") as human,
        ):
            await old.register("human:owner", endpoint="http://localhost:7001")
            assert 0 < await raw.ttl(online_key("agent:alice")) <= 30
            assert (
                await raw.execute_command("HGET", online_key("agent:alice"), "endpoint")
                == "http://localhost:7001"
            )
            with pytest.raises(MessagingError, match="already online"):
                await replacement.register("human:owner")
            with pytest.raises(MessagingError, match="owner mismatch"):
                await old.register("human:other")
            assert not await raw.execute_command("HEXISTS", PARTICIPANTS, "human:other")
            await human.create_group("dev")
            await raw.delete(online_key("agent:alice"))
            await human.send("agent:alice", body="available offline")
            await replacement.register("human:owner")
            for operation in (
                old.renew(),
                old.send("group:all", body="stale"),
                old.create_group("stale"),
                old.join_group("group:dev"),
                old.leave_group("group:dev"),
            ):
                with pytest.raises(MessagingError, match="lease lost"):
                    await operation
            await old.unregister()
            assert (
                await raw.execute_command("HGET", online_key("agent:alice"), "token")
                == "new"
            )
            await replacement.send("group:all", body="current")
            await replacement.renew()
            await replacement.unregister()
            assert not await raw.exists(online_key("agent:alice"))

    asyncio.run(scenario())


def test_independent_readers_retention_and_ambiguous_targets():
    async def scenario():
        server = FakeServer(server_type="valkey")
        raw = FakeAsyncValkey(server=server, decode_responses=True)
        async with (
            client(server, "human:alice") as human,
            client(server, "agent:alice") as alice,
        ):
            await alice.register("human:alice")
            await human.create_group("alice")
            with pytest.raises(MessagingError, match="Ambiguous"):
                await human.resolve("alice")
            assert await human.resolve("alice", kind="dm") == await human.resolve(
                "agent:alice"
            )
            assert await human.resolve("alice", kind="group") == "group:alice"
            assert await human.resolve("all") == "group:all"
            with pytest.raises(MessagingError, match="Unknown"):
                await human.resolve("missing")
            key = group_key("group:all", "messages")
            for sid in ("100-9", "100-10", "101-0"):
                await raw.xadd(
                    key, {"data": Message.create("human:alice", sid).encode()}, id=sid
                )
            first = await human.read("group:all", after="100-9", count=1)
            second = await alice.read("group:all", after="100-9", count=1)
            assert first == second and first[0][0] == "100-10"
            assert [sid for sid, _ in await human.history("group:all", count=2)] == [
                "100-10",
                "101-0",
            ]
            await raw.xtrim(key, maxlen=1, approximate=False)
            assert "no longer retained" in (
                await human.check_cursor("group:all", "100-10") or ""
            )
            with pytest.raises(MessagingError, match="precedes"):
                await human.check_cursor("group:all", "102-0")
            await raw.delete(key)
            assert "missing" in (await human.check_cursor("group:all", "101-0") or "")

    asyncio.run(scenario())


def test_target_previews_are_optional_bounded_and_membership_filtered():
    async def scenario():
        server = FakeServer(server_type="valkey")
        raw = FakeAsyncValkey(server=server, decode_responses=True)
        async with (
            client(server, "human:owner") as human,
            client(server, "agent:alice") as alice,
            client(server, "agent:bob") as bob,
            client(server, "agent:carol") as carol,
        ):
            for agent in (alice, bob, carol):
                await agent.register("human:owner")
            private = (await bob.send("agent:carol", body="private"))["group"]
            own = (await human.send("agent:alice", body="x" * 300))["group"]
            await raw.xadd(group_key("group:all", "messages"), {"data": "malformed"})
            assert all("preview" not in g for g in await alice.contacts())
            groups = {g["group"]: g for g in await alice.contacts(include_preview=True)}
            assert set(groups) == {"group:all", own}
            assert groups["group:all"]["preview"] is None
            assert groups[own]["preview"] == {
                "sender": "human:owner",
                "body": "x" * 160,
            }
            assert private in {g["group"] for g in await human.contacts()}
            targets = await alice.targets()
            assert {p["target"] for p in targets["participants"]} == {
                "human:owner",
                "agent:bob",
                "agent:carol",
            }

    asyncio.run(scenario())


def test_uncertain_send_is_not_retried_and_returns_recoverable_id(monkeypatch):
    async def scenario():
        raw = FakeAsyncValkey(decode_responses=True)
        async with MessagingClient(
            CONFIG, actor="human:owner", backend=Backend(CONFIG, client=raw)
        ) as human:
            original = raw.execute_command
            writes = []

            async def execute(*args, **kwargs):
                if args[0] == "EVAL" and "XADD" in args[1]:
                    writes.append(args)
                    await original(*args, **kwargs)
                    raise ConnectionError("reply lost after acceptance")
                return await original(*args, **kwargs)

            monkeypatch.setattr(raw, "execute_command", execute)
            with pytest.raises(SendUnconfirmed) as error:
                await human.send("group:all", body="only once")
            assert len(writes) == 1
            entries = await human.history("group:all")
            assert len(entries) == 1
            assert json.loads(entries[0][1]["data"])["id"] in str(error.value)

    asyncio.run(scenario())


def test_creation_rejects_orphan_stream_collision_and_wrong_key_types():
    from toolang.teaming.backend import GROUPS

    async def scenario():
        server = FakeServer(server_type="valkey")
        raw = FakeAsyncValkey(server=server, decode_responses=True)
        async with client(server, "human:owner") as human:
            await raw.xadd(group_key("group:orphan", "messages"), {"data": "old"})
            with pytest.raises(MessagingError, match="already exists"):
                await human.create_group("orphan")
            assert not await raw.execute_command("HEXISTS", GROUPS, "group:orphan")
            await raw.set(group_key("group:bad", "members"), "wrong-type")
            with pytest.raises(MessagingError, match="key type"):
                await human.create_group("bad")
            assert not await raw.execute_command("HEXISTS", GROUPS, "group:bad")
            await human.create_group("empty")
            await human.leave_group("group:empty")
            assert (await human.conversation("group:empty")).members == ()
            await human.join_group("group:empty")
            assert (await human.conversation("group:empty")).members == ("human:owner",)

    asyncio.run(scenario())


def test_driver_never_retries_writes_even_with_url_retry_option():
    from valkey.exceptions import TimeoutError

    async def scenario():
        backend = Backend(BackendConfig("redis://localhost?retry_on_timeout=true"))
        connection = backend._client.connection_pool.make_connection()
        writes = []

        async def write():
            writes.append("accepted")
            raise TimeoutError("reply lost")

        async def fail(_error):
            pass

        try:
            with pytest.raises(TimeoutError):
                await connection.retry.call_with_retry(write, fail)
            assert writes == ["accepted"]
        finally:
            await backend.close()

    asyncio.run(scenario())


def test_agent_cannot_use_an_empty_token_to_bypass_lease_checks():
    with pytest.raises(MessagingError, match="lease token"):
        MessagingClient(CONFIG, actor="agent:alice", token="")
