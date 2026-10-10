"""Offline tests for typed targets, authoritative membership, and delivery."""

import asyncio
import json
from uuid import UUID

from fakeredis import FakeAsyncValkey, FakeServer
import pytest
from valkey.exceptions import ConnectionError

from toolang.teaming.backend import Backend
from toolang.teaming.keys import (
    TEAM,
    PRESENCE,
    TEAM_EVENTS,
    CONVOS,
    STATS,
    GC_ALLOCATOR,
    SCHEMA,
    convo_key,
    name_key,
)
from toolang.teaming.ids import dm_id
from toolang.teaming.errors import (
    BackendUnavailable,
    ConversationAccessDenied,
    StorageIntegrityError,
)
from toolang.teaming.team_events import TeamEvents
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
    for kind in ("agent", "human"):
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


async def snapshot(raw):
    return {key: await raw.dump(key) for key in await raw.keys("*")}


def test_lookup_is_read_only_and_first_send_creates_atomically():
    async def scenario():
        server = FakeServer(server_type="valkey")
        raw = FakeAsyncValkey(server=server, decode_responses=True)
        async with (
            client(server, "human:owner") as human,
            client(server, "agent:alice") as alice,
            client(server, "agent:bob") as bob,
        ):
            await alice.register("human:owner")
            await bob.register("human:owner")
            before = await snapshot(raw)
            missing = await human.resolve("alice")
            assert not missing.exists
            assert missing.conversation == dm_id("human:owner", "agent:alice")
            with pytest.raises(MessagingError, match="No conversation exists"):
                await human.resolve("alice,bob")
            with pytest.raises(MessagingError, match="Unknown conversation"):
                await human.resolve(missing.conversation)
            with pytest.raises(MessagingError, match="nonparticipants"):
                await human.create_conversation(
                    participants=["agent:alice", "agent:bob"]
                )
            with pytest.raises(MessagingError, match="nonblank"):
                await human.send("alice", body=" ")
            assert await snapshot(raw) == before
            receipts = await asyncio.gather(
                *(human.send("alice", body=str(i)) for i in range(20))
            )
            ref = missing.conversation
            assert {r["conversation"] for r in receipts} == {ref}
            assert (await human.resolve("alice")).exists
            assert len(await alice.history(ref)) == 20
            assert await human.statistics() == dict(
                conversations_total=2, dm_count=1, gc_count=1, messages_total=20
            )
            assert (await human.statistics(ref))["messages_total"] == 20
            rows = await raw.xrange(TEAM_EVENTS)
            additions = [
                json.loads(fields["data"])
                for _, fields in rows
                if json.loads(fields["data"]).get("conversation") == ref
            ]
            assert len(additions) == 2
            assert {r["type"] for r in additions} == {"conversation.member_added"}
            for action in (human.join_conversation, human.leave_conversation):
                with pytest.raises(MessagingError, match="DM/system"):
                    await action(ref)
            with pytest.raises(ConversationAccessDenied):
                await bob.history(ref)
            private = (await alice.send("bob", body="private"))["conversation"]
            assert (await human.resolve("alice,bob")).conversation == private
            assert (await human.resolve("bob,alice")).conversation == private
            with pytest.raises(MessagingError, match="read-only"):
                await human.send(private, body="observer")
            assert len(await human.history(private)) == 1

    asyncio.run(scenario())


def test_contacts_survive_membership_loss_while_loading_previews(monkeypatch):
    async def scenario():
        server = FakeServer(server_type="valkey")
        async with (
            client(server, "human:owner") as human,
            client(server, "agent:alice") as alice,
        ):
            await alice.register(human.actor)
            leaving = await alice.create_conversation("Leaving")
            remaining = await alice.create_conversation("Remaining")
            evaluate = alice._backend._eval
            changed = False

            async def leave_before_read(script, keys, args):
                nonlocal changed
                if (
                    not changed
                    and '"checked"' in args[0]
                    and '"action":"contacts"' in args[0]
                ):
                    changed = True
                    await alice.leave_conversation(leaving.id)
                return await evaluate(script, keys, args)

            monkeypatch.setattr(alice._backend, "_eval", leave_before_read)
            contacts = await alice.contacts(include_preview=True)
            ids = {row["conversation"] for row in contacts}
            assert leaving.id not in ids
            assert remaining.id in ids

    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["contacts", "name"])
@pytest.mark.parametrize("error", [BackendUnavailable, StorageIntegrityError])
def test_conversation_discovery_propagates_backend_errors(
    monkeypatch, operation, error
):
    async def scenario():
        server = FakeServer(server_type="valkey")
        async with (
            client(server, "human:owner") as human,
            client(server, "agent:alice") as alice,
        ):
            await alice.register(human.actor)
            await alice.create_conversation("Visible")

            async def unavailable(_):
                raise error("Permission lookup failed")

            if operation == "contacts":
                monkeypatch.setattr(alice._backend, "contacts", unavailable)
            else:
                monkeypatch.setattr(alice, "conversation", unavailable)
            with pytest.raises(error):
                if operation == "contacts":
                    await alice.contacts()
                else:
                    await alice.resolve("Visible", kind="name")

    asyncio.run(scenario())


def test_gc_membership_duplicate_names_and_revision_checked_rename():
    async def scenario():
        server = FakeServer(server_type="valkey")
        async with (
            client(server, "human:owner") as human,
            client(server, "agent:alice") as alice,
        ):
            await alice.register("human:owner")
            first = await human.create_conversation("Development")
            second = await human.create_conversation("Development")
            assert first.id != second.id and first.name == second.name
            with pytest.raises(MessagingError, match="Ambiguous"):
                await human.resolve("Development", kind="name")
            with pytest.raises(MessagingError, match="not a member"):
                await alice.send(first.id, body="not joined")
            await alice.join_conversation(first.id)
            await alice.send(first.id, body="hello")
            renamed = await alice.rename_conversation(first.id, "Review", revision=1)
            assert renamed.revision == 2
            assert renamed.created_at == first.created_at
            with pytest.raises(MessagingError, match="revision conflict"):
                await human.rename_conversation(first.id, "stale", revision=1)
            same = await human.rename_conversation(first.id, "Review", revision=2)
            assert same == renamed
            assert (
                await human.resolve("Development", kind="name")
            ).conversation == second.id
            assert (await human.resolve("Review", kind="name")).conversation == first.id
            cleared = await human.rename_conversation(first.id, None, revision=2)
            assert cleared.name is None and cleared.revision == 3
            await alice.leave_conversation(first.id)
            await human.leave_conversation(first.id)
            assert (await human.conversation(first.id)).participants == ()
            await human.join_conversation(first.id)
            assert (await human.conversation(first.id)).participants == ("human:owner",)
            assert (await human.statistics())["gc_count"] == 3

    asyncio.run(scenario())


def test_presence_deadline_is_authority_and_reconciliation_is_atomic():
    async def scenario():
        server = FakeServer(server_type="valkey")
        raw = FakeAsyncValkey(server=server, decode_responses=True)
        async with (
            client(server, "human:owner") as human,
            client(server, "agent:alice", "old") as alice,
            client(server, "agent:alice", "new") as replacement,
        ):
            await alice.register("human:owner", endpoint="http://localhost:7001")
            backend = human._backend
            info = await backend.lease_info("agent:alice")
            assert info["lease"]["token"] == "old"
            assert await raw.ttl(PRESENCE) == -1
            before = await raw.execute_command("HGET", TEAM, "agent:alice")
            events = await raw.execute_command("XLEN", TEAM_EVENTS)
            deadline = await raw.zscore(PRESENCE, "agent:alice")
            await alice.renew()
            assert await raw.execute_command("HGET", TEAM, "agent:alice") == before
            assert await raw.execute_command("XLEN", TEAM_EVENTS) == events
            assert await raw.zscore(PRESENCE, "agent:alice") >= deadline
            with pytest.raises(MessagingError, match="already online"):
                await replacement.register("human:owner")
            await raw.zadd(PRESENCE, {"agent:alice": 1})
            assert not await backend.online("agent:alice")
            for action in (
                alice.renew(),
                alice.send("human:owner", body="expired"),
                alice.create_conversation("expired"),
            ):
                with pytest.raises(MessagingError, match="lease lost"):
                    await action
            await replacement.register("human:owner")
            kinds = [
                json.loads(row["data"])["type"]
                for _, row in (await raw.xrevrange(TEAM_EVENTS, count=2))
            ][::-1]
            assert kinds == ["presence.offline", "presence.online"]
            await alice.unregister()
            assert (await backend.lease_info("agent:alice"))["lease"]["token"] == "new"
            await raw.zadd(PRESENCE, {"agent:alice": 1})
            start = await raw.execute_command("XLEN", TEAM_EVENTS)
            changed = await asyncio.gather(
                *(backend.expire_presence("agent:alice") for _ in range(8))
            )
            assert sum(changed) == 1
            assert await raw.execute_command("XLEN", TEAM_EVENTS) == start + 1
            assert (
                json.loads(await raw.execute_command("HGET", TEAM, "agent:alice"))[
                    "lease"
                ]
                is None
            )
            assert "lease" not in json.dumps(await human.team())

    asyncio.run(scenario())


def test_events_replay_retention_epoch_and_noop_operations():
    async def scenario():
        server = FakeServer(server_type="valkey")
        raw = FakeAsyncValkey(server=server, decode_responses=True)
        async with client(server, "human:owner") as human:
            feed = TeamEvents(human._backend)
            checkpoint = await feed.checkpoint()
            convo = await human.create_conversation("Review")
            rows = await feed.replay(checkpoint)
            assert rows and len(rows) == 1
            assert rows[0][1]["type"] == "conversation.member_added"
            assert await feed.replay(checkpoint) == rows
            tail = await feed.checkpoint()
            await human.rename_conversation(convo.id, "Renamed", revision=1)
            await human.send(convo.id, body="message")
            await human.join_conversation(convo.id)
            assert await feed.checkpoint() == tail
            assert await feed.replay(tail) == []
            wrong = "t1." + "0" * 32 + "." + tail.split(".")[-1]
            assert await feed.replay(wrong) is None
            await raw.xtrim(TEAM_EVENTS, maxlen=1, approximate=False)
            assert await feed.replay(checkpoint) is None
            assert await feed.replay(tail) == []
            await raw.delete(TEAM_EVENTS)
            with pytest.raises(StorageIntegrityError):
                await human.check_backend()

    asyncio.run(scenario())


def test_collision_and_corrupt_keys_fail_before_any_mutation(monkeypatch):
    async def scenario():
        server = FakeServer(server_type="valkey")
        raw = FakeAsyncValkey(server=server, decode_responses=True)
        async with (
            client(server, "human:owner") as human,
            client(server, "agent:alice") as alice,
        ):
            await alice.register("human:owner")
            ref = (await human.send("alice", body="hello"))["conversation"]
            await raw.execute_command("SREM", convo_key(ref, "members"), "agent:alice")
            before = await snapshot(raw)
            with pytest.raises(MessagingError, match="collision|membership"):
                await human.resolve("alice")
            assert await snapshot(raw) == before
            await raw.execute_command("SADD", convo_key(ref, "members"), "agent:alice")
            await raw.set(name_key("bad"), "wrong type")
            before = await snapshot(raw)
            with pytest.raises(StorageIntegrityError):
                await human.rename_conversation(ref, "bad", revision=1)
            assert await snapshot(raw) == before
            await raw.execute_command("HDEL", STATS, "messages_total")
            with pytest.raises(StorageIntegrityError):
                await human.send(ref, body="no partial append")
            assert await raw.execute_command("XLEN", convo_key(ref, "messages")) == 1

    asyncio.run(scenario())


def test_statistics_count_appends_despite_retention_and_filter_access():
    async def scenario():
        server = FakeServer(server_type="valkey")
        raw = FakeAsyncValkey(server=server, decode_responses=True)
        async with (
            client(server, "human:owner") as human,
            client(server, "agent:alice") as alice,
        ):
            await alice.register("human:owner")
            ref = (await human.send("alice", body="one"))["conversation"]
            await human.send(ref, body="two")
            await raw.xtrim(convo_key(ref, "messages"), maxlen=1, approximate=False)
            stats = await alice.statistics(ref)
            assert stats["messages_total"] == 2 and stats["messages_retained"] == 1
            assert (await human.statistics())["messages_total"] == 2
            with pytest.raises(MessagingError, match="human observer"):
                await alice.statistics()

    asyncio.run(scenario())


def test_uncertain_send_is_not_retried_and_returns_recoverable_id(monkeypatch):
    async def scenario():
        server = FakeServer(server_type="valkey")
        raw = FakeAsyncValkey(server=server, decode_responses=True)
        async with client(server, "human:owner") as human:
            ref = (await human.create_conversation("Review")).id
            original = human._backend._client.execute_command
            writes = []

            async def execute(*args, **kwargs):
                if args[0] == "EVAL" and '"action":"send"' in str(args[-1]):
                    writes.append(args)
                    await original(*args, **kwargs)
                    raise ConnectionError("reply lost after acceptance")
                return await original(*args, **kwargs)

            monkeypatch.setattr(human._backend._client, "execute_command", execute)
            with pytest.raises(SendUnconfirmed) as error:
                await human.send(ref, body="only once")
            assert len(writes) == 1
            entries = await human.history(ref)
            assert len(entries) == 1
            assert json.loads(entries[0][1]["data"])["id"] in str(error.value)
            assert await raw.execute_command("XLEN", convo_key(ref, "messages")) == 1

    asyncio.run(scenario())


def test_legacy_and_partial_datasets_are_never_initialized():
    async def scenario():
        for key in ("too:teaming:v1:participants", "too:teaming:v1:msg:groups", CONVOS):
            raw = FakeAsyncValkey(decode_responses=True)
            await raw.execute_command("HSET", key, "old", "record")
            before = await snapshot(raw)
            backend = Backend(CONFIG, client=raw)
            with pytest.raises(StorageIntegrityError):
                await backend.initialize()
            assert await snapshot(raw) == before
            await backend.close()
        raw = FakeAsyncValkey(decode_responses=True)
        backend = Backend(CONFIG, client=raw)
        await backend.initialize()
        await raw.delete(GC_ALLOCATOR)
        with pytest.raises(StorageIntegrityError):
            await backend.initialize()
        assert await raw.get(SCHEMA) == "2" and not await raw.exists(GC_ALLOCATOR)
        await backend.close()

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


@pytest.mark.parametrize("field", ["dm_count", "messages_total"])
@pytest.mark.parametrize("value", ["0.0", "0e0", "00", "+0", "-0"])
def test_noncanonical_counters_cannot_partially_commit_first_send(field, value):
    async def scenario():
        server = FakeServer(server_type="valkey")
        async with (
            client(server, "human:owner") as human,
            client(server, "agent:alice") as alice,
        ):
            await alice.register(human.actor)
            raw = human._backend._client
            await raw.hset(STATS, field, value)
            before = await snapshot(raw)
            with pytest.raises(MessagingError) as error:
                await human.send("alice", body="must not partially commit")
            assert await snapshot(raw) == before
            assert isinstance(error.value, StorageIntegrityError)

    asyncio.run(scenario())


def test_agent_cannot_use_an_empty_token_to_bypass_lease_checks():
    with pytest.raises(MessagingError, match="lease token"):
        MessagingClient(CONFIG, actor="agent:alice", token="")


@pytest.mark.parametrize(
    "field,value",
    [
        ("created_by", None),
        ("revision", "1"),
        ("revision", 0),
        ("created_at", "bad"),
        ("unexpected", True),
    ],
)
def test_corrupt_metadata_cannot_be_renamed_or_sent(field, value):
    async def scenario():
        async with client(FakeServer(server_type="valkey"), "human:owner") as human:
            ref = (await human.create_conversation("Original")).id
            raw = human._backend._client
            record = json.loads(await raw.execute_command("HGET", CONVOS, ref))
            record[field] = value
            await raw.execute_command("HSET", CONVOS, ref, json.dumps(record))
            before = await snapshot(raw)
            for operation in (
                human.send(ref, body="rejected"),
                human.rename_conversation(ref, "rejected", revision=1),
            ):
                with pytest.raises(StorageIntegrityError):
                    await operation
                assert await snapshot(raw) == before

    asyncio.run(scenario())


def test_first_send_failure_and_forced_dm_collision_leave_no_partial_state(monkeypatch):
    async def scenario():
        server = FakeServer(server_type="valkey")
        async with (
            client(server, "human:owner") as human,
            client(server, "agent:alice") as alice,
            client(server, "agent:bob") as bob,
        ):
            await alice.register("human:owner")
            await bob.register("human:owner")
            raw = human._backend._client
            alice_ref = dm_id(human.actor, alice.actor)
            await raw.set(convo_key(alice_ref, "messages"), "bad stream type")
            before = await snapshot(raw)
            with pytest.raises(StorageIntegrityError):
                await human.send("alice", body="cannot create")
            assert await snapshot(raw) == before
            await raw.delete(convo_key(alice_ref, "messages"))
            await human.send("alice", body="accepted")
            monkeypatch.setattr("toolang.teaming.messaging.dm_id", lambda *_: alice_ref)
            before = await snapshot(raw)
            with pytest.raises(MessagingError, match="collision"):
                await human.resolve("bob")
            assert await snapshot(raw) == before

    asyncio.run(scenario())


def test_gc_retry_after_lost_response_creates_another_distinct_conversation(
    monkeypatch,
):
    from toolang.teaming.errors import BackendUnavailable

    async def scenario():
        async with client(FakeServer(server_type="valkey"), "human:owner") as human:
            original = human._backend._operation
            accepted = []

            async def operation(script, op, extra=None):
                result = await original(script, op, extra)
                if op.get("action") == "create":
                    accepted.append(result)
                    if len(accepted) == 1:
                        raise BackendUnavailable("response lost after creation")
                return result

            monkeypatch.setattr(human._backend, "_operation", operation)
            with pytest.raises(BackendUnavailable):
                await human.create_conversation("Retry")
            second = await human.create_conversation("Retry")
            assert second.id != accepted[0]
            assert (await human.statistics())["gc_count"] == 3
            with pytest.raises(MessagingError, match="Ambiguous"):
                await human.resolve("Retry", kind="name")

    asyncio.run(scenario())


def test_concurrent_initialization_preserves_one_system_gc_after_rename():
    async def scenario():
        server = FakeServer(server_type="valkey")
        clients = [client(server, f"human:h{i}") for i in range(10)]
        try:
            await asyncio.gather(*(human.__aenter__() for human in clients))
            systems = await asyncio.gather(
                *(human._backend.system_conversation() for human in clients)
            )
            assert len(set(systems)) == 1
            human = clients[0]
            ref = systems[0]
            await human.rename_conversation(ref, "Renamed system", revision=1)
            await asyncio.gather(*(human.register_human() for human in clients))
            assert await human._backend.system_conversation() == ref
            assert (await human.conversation(ref)).name == "Renamed system"
            assert (await human.statistics())["gc_count"] == 1
        finally:
            await asyncio.gather(*(human.close() for human in clients))

    asyncio.run(scenario())
