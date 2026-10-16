"""Stable codecs and backend reservation boundaries without wall-clock timing."""

import asyncio
import json
from unittest.mock import AsyncMock

from fakeredis import FakeAsyncValkey, FakeServer
from fakeredis.commands_mixins.scripting_mixin import ScriptingCommandsMixin
import pytest

from tests.unit.messaging.test_protocol import CONFIG, client, snapshot
from toolang.common.id_codec import decode_short_id, derive_id_key, scramble_id
from toolang.teaming.backend.valkey import ValkeyBackend
from toolang.teaming.errors import MessagingError, StorageIntegrityError
from toolang.teaming.ids import (
    dm_id,
    gc_id,
    talk_thread_id,
    GC_EPOCH_SECONDS,
    GC_SEQUENCE_LIMIT,
    GC_TICK_LIMIT,
)
from toolang.teaming.backend.valkey.keys import (
    CONVOS,
    GC_ALLOCATOR,
    STATS,
    SYSTEM,
    SCHEMA,
    convo_key,
)
from toolang.teaming.backend.valkey import scripts


@pytest.mark.parametrize(
    "sequence,expected", [(0, "gc_ky36cnjw"), (1, "gc_kdzfwqq7"), (2, "gc_trf9bwrf")]
)
def test_gc_golden_vectors(sequence, expected):
    assert gc_id(6770, sequence) == expected


def test_conversation_kind_domains_and_gc_reservations_are_disjoint():
    key = derive_id_key("toolang:conversation:v3")
    pairs = {(0, 0), (GC_TICK_LIMIT - 1, GC_SEQUENCE_LIMIT - 1)}
    pairs.update(
        (i * 7919 % GC_TICK_LIMIT, i * 104729 % GC_SEQUENCE_LIMIT) for i in range(2000)
    )
    suffixes = set()
    for tick, seq in pairs:
        suffix = gc_id(tick, seq)[3:]
        decoded = scramble_id(decode_short_id(suffix), width=8, key=key, reverse=True)
        assert decoded >> 39 == 1
        assert (decoded & ((1 << 39) - 1)) == (tick << 19) | seq
        suffixes.add(suffix)
    assert len(suffixes) == len(pairs)
    for i in range(2000):
        suffix = dm_id("agent:alice", f"human:member{i}")[3:]
        payload = scramble_id(decode_short_id(suffix), width=8, key=key, reverse=True)
        assert payload >> 39 == 0
        # Even the exact same 39-bit payload belongs to a different GC suffix.
        assert suffix != gc_id(payload >> 19, payload & (GC_SEQUENCE_LIMIT - 1))[3:]
        assert suffix not in suffixes


@pytest.mark.parametrize(
    "tick,seq", [(-1, 0), (GC_TICK_LIMIT, 0), (0, GC_SEQUENCE_LIMIT), (0, -1)]
)
def test_gc_rejects_out_of_range_reservations(tick, seq):
    with pytest.raises(ValueError, match="exhausted"):
        gc_id(tick, seq)


@pytest.mark.parametrize(
    "a,b,expected",
    [
        ("agent:alice", "agent:bob", "dm_vngc4qcp"),
        ("agent:alice", "human:alice", "dm_x0wq49jj"),
        ("agent:é", "human:中", "dm_n7a926nq"),
        ("agent:e\u0301", "human:中", "dm_3r8sqfby"),
    ],
)
def test_dm_namespace_unicode_and_reversal_vectors(a, b, expected):
    assert dm_id(a, b) == dm_id(b, a) == expected


@pytest.mark.parametrize(
    "agent,expected",
    [
        ("alice", "talk_29typzhw"),
        ("Alice", "talk_8efta07f"),
        ("bob", "talk_kx885310"),
        ("é", "talk_zc5f78xp"),
        ("e\u0301", "talk_05whmaze"),
    ],
)
def test_talk_thread_id_golden_vectors(agent, expected):
    assert talk_thread_id("gc_ky36cnjw", agent_name=agent) == expected
    assert talk_thread_id("gc_ky36cnjw", agent_name=agent) == expected


def test_talk_threads_preserve_per_agent_conversation_uniqueness():
    conversations = [gc_id(6770, i) for i in range(1000)]
    conversations.extend(dm_id("agent:alice", f"human:member{i}") for i in range(1000))
    alice = [talk_thread_id(ref, agent_name="alice") for ref in conversations]
    bob = [talk_thread_id(ref, agent_name="bob") for ref in conversations]
    assert len(set(alice)) == len(conversations)
    assert len(set(bob)) == len(conversations)
    assert alice != bob
    assert all(len(value) == 13 and value.startswith("talk_") for value in alice + bob)


@pytest.mark.parametrize("agent", ["", " alice", "alice ", "agent:alice", "a/b"])
def test_talk_thread_id_rejects_invalid_agent_identity(agent):
    with pytest.raises(MessagingError):
        talk_thread_id("gc_ky36cnjw", agent_name=agent)


@pytest.mark.parametrize(
    "conversation", ["alice", "gc_123", "gc_ABCDEFGH", "gc_abcdefgh "]
)
def test_talk_thread_id_requires_a_canonical_conversation(conversation):
    with pytest.raises(MessagingError):
        talk_thread_id(conversation, agent_name="alice")


@pytest.mark.parametrize("schema", ["1", "2", "4"])
def test_other_conversation_schemas_fail_without_mutation(schema):
    async def scenario():
        raw = FakeAsyncValkey(decode_responses=True)
        backend = ValkeyBackend(CONFIG, client=raw)
        await backend.initialize()
        await raw.set(SCHEMA, schema)
        before = await snapshot(raw)
        with pytest.raises(
            StorageIntegrityError, match="unsupported conversation schema"
        ):
            await backend.initialize()
        assert await snapshot(raw) == before
        await backend.close()

    asyncio.run(scenario())


def test_backend_allocation_rollover_rollback_exhaustion_and_restart(monkeypatch):
    clock = [GC_EPOCH_SECONDS + 42 * 3600]
    original = ScriptingCommandsMixin._lua_redis_call

    def call(self, runtime, globals_, command, *args):
        if command.lower() == b"time":
            return runtime.table_from([str(clock[0]).encode(), b"0"])
        return original(self, runtime, globals_, command, *args)

    monkeypatch.setattr(ScriptingCommandsMixin, "_lua_redis_call", call)

    async def scenario():
        raw = FakeAsyncValkey(decode_responses=True)
        backend = ValkeyBackend(CONFIG, client=raw)
        await backend.initialize()

        async def reserve():
            return await backend._operation(scripts.RESERVE, {})

        reservations = await asyncio.gather(*(reserve() for _ in range(100)))
        assert sorted(reservations) == [[42, seq] for seq in range(100)]
        clock[0] -= 3600
        assert await reserve() == [42, 100]
        await raw.execute_command(
            "HSET", GC_ALLOCATOR, "last_seq", GC_SEQUENCE_LIMIT - 2
        )
        assert await reserve() == [42, GC_SEQUENCE_LIMIT - 1]
        before = await snapshot(raw)
        with pytest.raises(MessagingError, match="exhausted"):
            await reserve()
        assert await snapshot(raw) == before
        clock[0] += 7200
        assert await reserve() == [43, 0]
        restarted = ValkeyBackend(CONFIG, client=raw)
        await restarted.initialize()
        assert await restarted._operation(scripts.RESERVE, {}) == [43, 1]
        clock[0] = GC_EPOCH_SECONDS + GC_TICK_LIMIT * 3600
        with pytest.raises(MessagingError, match="exhausted"):
            await reserve()
        await raw.execute_command("HSET", GC_ALLOCATOR, "last_seq", "broken")
        with pytest.raises(StorageIntegrityError):
            await restarted.initialize()
        await backend.close()

    asyncio.run(scenario())


def test_gc_collision_budget_consumes_reservations_without_reusing_them(monkeypatch):
    async def scenario():
        server = FakeServer(server_type="valkey")
        async with client(server, "human:owner") as human:
            system = await human.backend.system_conversation()
            raw = human.backend._client
            previous = int(await raw.execute_command("HGET", GC_ALLOCATOR, "last_seq"))
            monkeypatch.setattr(
                "toolang.teaming.backend.valkey.backend.gc_id", lambda *_: system
            )
            with pytest.raises(MessagingError, match="128 conflicts"):
                await human.create_conversation("conflict")
            assert (
                int(await raw.execute_command("HGET", GC_ALLOCATOR, "last_seq"))
                == previous + 128
            )
            assert (await human.statistics())["gc_count"] == 1

    asyncio.run(scenario())


def test_lookup_command_count_does_not_scale_with_directory_size(monkeypatch):
    async def scenario():
        server = FakeServer(server_type="valkey")
        async with (
            client(server, "human:owner") as human,
            client(server, "agent:alice") as alice,
        ):
            await alice.register("human:owner")
            ref = (await human.send("alice", body="first"))["conversation"]
            raw = human.backend._client
            system = await raw.execute_command("HGET", SYSTEM, "all")
            template = json.loads(await raw.execute_command("HGET", CONVOS, system))
            observed = AsyncMock(wraps=human.backend._call)
            monkeypatch.setattr(human.backend, "_call", observed)
            counts = []
            for total in (10, 10000):
                async with raw.pipeline() as pipe:
                    for seq in range(total):
                        key = gc_id(0, seq)
                        pipe.hset(
                            CONVOS,
                            key,
                            json.dumps({**template, "id": key, "name": None}),
                        )
                    pipe.hset(STATS, "gc_count", total + 1)
                    await pipe.execute()
                observed.reset_mock()
                assert (await human.resolve("alice")).conversation == ref
                assert (await human.resolve(ref)).conversation == ref
                counts.append(observed.await_count)
                assert all(
                    call.args[0] in {"EVAL", "HEXISTS"}
                    for call in observed.await_args_list
                )
            assert counts[0] == counts[1] == 6
            assert await raw.scard(convo_key(ref, "members")) == 2

    asyncio.run(scenario())


def test_directory_reads_use_bounded_batches_and_keep_visibility(monkeypatch):
    async def scenario():
        server = FakeServer(server_type="valkey")
        async with (
            client(server, "human:owner") as human,
            client(server, "agent:alice") as alice,
        ):
            await alice.register(human.actor)
            raw = human.backend._client
            system = await raw.hget(SYSTEM, "all")
            template = json.loads(await raw.hget(CONVOS, system))
            async with raw.pipeline() as pipe:
                for seq in range(129):
                    ref = gc_id(0, seq)
                    pipe.hset(
                        CONVOS,
                        ref,
                        json.dumps({**template, "id": ref, "created_by": human.actor}),
                    )
                    pipe.sadd(convo_key(ref, "members"), human.actor)
                pipe.hincrby(STATS, "gc_count", 129)
                await pipe.execute()
            for reader, expected in ((human, 130), (alice, 1)):
                observed = AsyncMock(wraps=reader.backend._call)
                monkeypatch.setattr(reader.backend, "_call", observed)
                assert len(await reader.contacts()) == expected
                # One ID read and two bounded batches; independent of visible count.
                assert observed.await_count == 6
                batches = [
                    json.loads(call.args[-1]).get("conversations", [])
                    for call in observed.await_args_list
                ]
                assert max(map(len, batches)) == 128

    asyncio.run(scenario())
