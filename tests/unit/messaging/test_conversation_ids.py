"""Stable codecs and backend reservation boundaries without wall-clock timing."""

import asyncio
import hashlib
import json
from unittest.mock import AsyncMock

from fakeredis import FakeAsyncValkey, FakeServer
from fakeredis.commands_mixins.scripting_mixin import ScriptingCommandsMixin
import pytest

from tests.unit.messaging.test_protocol import CONFIG, client, snapshot
from toolang.common.ids import scramble_id
from toolang.teaming.backend.valkey import ValkeyBackend
from toolang.teaming.errors import MessagingError, StorageIntegrityError
from toolang.teaming.ids import dm_id, gc_id, GC_EPOCH_SECONDS, GC_LIMIT
from toolang.teaming.backend.valkey.keys import (
    CONVOS,
    GC_ALLOCATOR,
    STATS,
    SYSTEM,
    convo_key,
)
from toolang.teaming.backend.valkey import scripts


@pytest.mark.parametrize(
    "sequence,expected", [(0, "gc_rcpya1zw"), (1, "gc_apxp9knw"), (2, "gc_zqgdw7cp")]
)
def test_gc_golden_vectors(sequence, expected):
    assert gc_id(6770, sequence) == expected


def test_gc_permutation_round_trips_boundaries_and_samples():
    key = hashlib.blake2s(b"toolang:conversation:gc:v1").digest()
    samples = {0, 1, (1 << 40) - 1, 1 << 20, (1 << 20) - 1}
    samples.update((i * 104729 * 7919) % (1 << 40) for i in range(2000))
    encoded = {scramble_id(value, width=8, key=key) for value in samples}
    assert len(encoded) == len(samples)
    assert {
        scramble_id(value, width=8, key=key, reverse=True) for value in encoded
    } == samples
    for tick, seq in [(-1, 0), (GC_LIMIT, 0), (0, GC_LIMIT), (0, -1)]:
        with pytest.raises(ValueError):
            gc_id(tick, seq)


def test_dm_namespace_unicode_and_reversal_vectors():
    assert dm_id("agent:alice", "agent:bob") == "dm_6x89kwxn"
    pairs = [
        ("agent:alice", "human:alice"),
        ("agent:é", "human:中"),
        ("agent:e\u0301", "human:中"),
    ]
    ids = []
    alphabet = "0123456789abcdefghjkmnpqrstvwxyz"
    for a, b in pairs:
        canonical = json.dumps(
            ["toolang:dm:v1", *sorted((a, b), key=str.encode)],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        number = int.from_bytes(hashlib.sha256(canonical.encode()).digest()[:5], "big")
        expected = "dm_" + "".join(
            alphabet[(number >> shift) & 31] for shift in range(35, -1, -5)
        )
        assert dm_id(a, b) == dm_id(b, a) == expected
        ids.append(expected)
    assert len(set(ids)) == 3


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
        await raw.execute_command("HSET", GC_ALLOCATOR, "last_seq", GC_LIMIT - 2)
        assert await reserve() == [42, GC_LIMIT - 1]
        before = await snapshot(raw)
        with pytest.raises(MessagingError, match="exhausted"):
            await reserve()
        assert await snapshot(raw) == before
        clock[0] += 7200
        assert await reserve() == [43, 0]
        restarted = ValkeyBackend(CONFIG, client=raw)
        await restarted.initialize()
        assert await restarted._operation(scripts.RESERVE, {}) == [43, 1]
        clock[0] = GC_EPOCH_SECONDS + GC_LIMIT * 3600
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
