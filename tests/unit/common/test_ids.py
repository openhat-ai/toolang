from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from functools import partial
import multiprocessing
from pathlib import Path
import random
import sqlite3

import pytest

from toolang.common.ids import (
    AllocatorState,
    IdFamily,
    IdIssuer,
    LOCAL_ID_FAMILY,
    RUN_ID_FAMILY,
    allocate_id,
    archive_prefix,
    decode_id,
    encode_id,
    reserve_next_id,
)
from toolang.execution.store import RunStore


def _database(path: Path) -> Path:
    RunStore(path).close()
    return path


def _allocate_ids(
    state_path: str, count: int, now: datetime, family: IdFamily
) -> tuple[str, ...]:
    return tuple(
        allocate_id(Path(state_path), family=family, now=now, agent_name="alice").value
        for _ in range(count)
    )


@pytest.mark.parametrize(
    ("family", "agent_name", "expected"),
    [
        (LOCAL_ID_FAMILY, "alice", "0tcexrxr"),
        (LOCAL_ID_FAMILY, "Alice", "4cce2rcn"),
        (LOCAL_ID_FAMILY, "bob", "8e2j1ct2"),
        (RUN_ID_FAMILY, "alice", "jhprmr0a"),
        (RUN_ID_FAMILY, "Alice", "6m1ee8z5"),
        (RUN_ID_FAMILY, "bob", "ff6hde2y"),
    ],
)
def test_agent_codec_vectors(family: IdFamily, agent_name: str, expected: str) -> None:
    assert (
        encode_id(family=family, agent_name=agent_name, tick=12_345, seq=17) == expected
    )
    decoded = decode_id(expected, family=family, agent_name=agent_name)
    assert (decoded.tick, decoded.seq) == (12_345, 17)


@pytest.mark.parametrize("family", [LOCAL_ID_FAMILY, RUN_ID_FAMILY])
@pytest.mark.parametrize("agent_name", ["alice", "Alice", "bob"])
def test_agent_codec_is_reversible_across_its_domain(
    family: IdFamily, agent_name: str
) -> None:
    randomizer = random.Random(42)
    pairs = {
        (tick, seq)
        for tick in (0, 1, family.tick_modulus - 1)
        for seq in (0, 1, family.seq_modulus - 1)
    }
    pairs.update(
        (
            randomizer.randrange(family.tick_modulus),
            randomizer.randrange(family.seq_modulus),
        )
        for _ in range(256)
    )
    values = set()
    for tick, seq in pairs:
        value = encode_id(family=family, agent_name=agent_name, tick=tick, seq=seq)
        decoded = decode_id(value, family=family, agent_name=agent_name)
        assert (decoded.tick, decoded.seq) == (tick, seq)
        assert len(value) == 8
        assert set(value) <= set("0123456789abcdefghjkmnpqrstvwxyz")
        values.add(value)
    assert len(values) == len(pairs)


@pytest.mark.parametrize("agent_name", ["", " ", "alice ", " alice"])
def test_codec_rejects_noncanonical_agent_names(agent_name: str) -> None:
    with pytest.raises(ValueError, match="agent name"):
        encode_id(family=LOCAL_ID_FAMILY, agent_name=agent_name, tick=0, seq=0)
    with pytest.raises(ValueError, match="agent name"):
        decode_id("00000000", family=LOCAL_ID_FAMILY, agent_name=agent_name)


@pytest.mark.parametrize("family", [LOCAL_ID_FAMILY, RUN_ID_FAMILY])
def test_agent_identity_is_independent_of_allocator_path(
    tmp_path: Path, family: IdFamily
) -> None:
    now = datetime(2026, 10, 9, 2, tzinfo=UTC)
    values = [
        allocate_id(
            _database(tmp_path / path / "runs.db"),
            family=family,
            agent_name=name,
            now=now,
        )
        for path, name in (("host", "alice"), ("guest", "alice"), ("other", "bob"))
    ]
    assert {value.seq for value in values} == {0}
    assert len({value.tick for value in values}) == 1
    assert values[0].value == values[1].value
    assert values[0].value != values[2].value


def test_decoded_records_can_reconstruct_a_persisted_high_water_mark(
    tmp_path: Path,
) -> None:
    pairs = [(42, 3), (43, 7), (42, 20), (43, 2)]
    records = [
        encode_id(family=RUN_ID_FAMILY, agent_name="alice", tick=tick, seq=seq)
        for tick, seq in pairs
    ]
    decoded = [
        decode_id(value, family=RUN_ID_FAMILY, agent_name="alice") for value in records
    ]
    tick, seq = max((value.tick, value.seq) for value in decoded)
    state_path = _database(tmp_path / "runs.db")
    with sqlite3.connect(state_path) as connection:
        connection.execute("INSERT INTO seqs VALUES ('run', ?, ?)", (tick, seq))

    resumed = allocate_id(
        state_path,
        family=RUN_ID_FAMILY,
        agent_name="alice",
        now=RUN_ID_FAMILY.bucket_started_at(43),
    )
    assert (resumed.tick, resumed.seq) == (43, 8)
    assert resumed.value not in records


def test_encode_decode_round_trip_for_local_ids() -> None:
    value = encode_id(family=LOCAL_ID_FAMILY, tick=12_345, seq=17, agent_name="alice")
    decoded = decode_id(value, family=LOCAL_ID_FAMILY, agent_name="alice")

    assert len(value) == 8
    assert decoded.tick == 12_345
    assert decoded.seq == 17
    assert decoded.prefix == value[:4]
    assert decoded.suffix == value[4:]


def test_encode_decode_round_trip_for_run_ids() -> None:
    value = encode_id(family=RUN_ID_FAMILY, tick=7_654, seq=12_345, agent_name="alice")
    decoded = decode_id(value, family=RUN_ID_FAMILY, agent_name="alice")

    assert len(value) == 8
    assert decoded.tick == 7_654
    assert decoded.seq == 12_345


def test_archive_prefix_matches_decoded_prefix() -> None:
    value = encode_id(family=LOCAL_ID_FAMILY, tick=88, seq=4, agent_name="alice")

    assert (
        archive_prefix(value, family=LOCAL_ID_FAMILY, agent_name="alice")
        == decode_id(value, family=LOCAL_ID_FAMILY, agent_name="alice").archive_prefix
    )


def test_reserve_next_id_advances_seq_within_one_tick() -> None:
    now = datetime(2026, 1, 2, 3, 0, tzinfo=UTC)

    first, state = reserve_next_id(
        AllocatorState(), family=LOCAL_ID_FAMILY, now=now, agent_name="alice"
    )
    second, next_state = reserve_next_id(
        state, family=LOCAL_ID_FAMILY, now=now, agent_name="alice"
    )

    assert first.tick == second.tick
    assert first.seq == 0
    assert second.seq == 1
    assert first.prefix != second.prefix
    assert first.archive_prefix == second.archive_prefix
    assert next_state == AllocatorState(last_tick=first.tick, last_seq=1)


def test_reserve_next_id_resets_seq_for_next_tick() -> None:
    first_now = datetime(2026, 1, 2, 3, 0, tzinfo=UTC)
    second_now = first_now + timedelta(hours=1)

    _first, state = reserve_next_id(
        AllocatorState(), family=LOCAL_ID_FAMILY, now=first_now, agent_name="alice"
    )
    second, next_state = reserve_next_id(
        state, family=LOCAL_ID_FAMILY, now=second_now, agent_name="alice"
    )

    assert second.seq == 0
    assert next_state.last_tick == LOCAL_ID_FAMILY.tick_for(second_now)


def test_reserve_next_id_uses_exists_callback_as_safety_belt() -> None:
    seen = set()
    original = encode_id(family=LOCAL_ID_FAMILY, tick=999, seq=0, agent_name="alice")
    seen.add(original)

    allocated, state = reserve_next_id(
        AllocatorState(last_tick=999, last_seq=-1),
        family=LOCAL_ID_FAMILY,
        now=LOCAL_ID_FAMILY.bucket_started_at(999),
        exists=seen.__contains__,
        agent_name="alice",
    )

    assert allocated.value != original
    assert allocated.seq == 1
    assert state == AllocatorState(last_tick=999, last_seq=1)


def test_allocate_id_persists_state_by_family(tmp_path: Path) -> None:
    state_path = _database(tmp_path / ".runtime" / "runs.db")
    now = datetime(2026, 1, 2, 4, 0, tzinfo=UTC)

    first = allocate_id(state_path, family=LOCAL_ID_FAMILY, now=now, agent_name="alice")
    second = allocate_id(
        state_path, family=LOCAL_ID_FAMILY, now=now, agent_name="alice"
    )
    run = allocate_id(state_path, family=RUN_ID_FAMILY, now=now, agent_name="alice")
    with sqlite3.connect(state_path) as connection:
        rows = connection.execute(
            "SELECT family, last_tick, last_seq FROM seqs ORDER BY family"
        ).fetchall()

    assert first.seq == 0
    assert second.seq == 1
    assert run.seq == 0
    assert rows == [("local", first.tick, 1), ("run", run.tick, 0)]
    assert not state_path.with_name("ids.json").exists()


def test_id_issuer_uses_explicit_canonical_prefixes(tmp_path: Path) -> None:
    ids = IdIssuer(_database(tmp_path / ".runtime" / "runs.db"), agent_name="alice")

    run_id = ids.issue_run()
    thread_id = ids.issue_thread("term")
    assert run_id.startswith("run_")
    assert thread_id.startswith("term_")
    assert decode_id(run_id[4:], family=RUN_ID_FAMILY, agent_name="alice").seq == 0
    assert decode_id(thread_id[5:], family=LOCAL_ID_FAMILY, agent_name="alice").seq == 0
    with pytest.raises(ValueError, match="invalid thread prefix"):
        ids.issue_thread("TUI")


@pytest.mark.parametrize("family", [LOCAL_ID_FAMILY, RUN_ID_FAMILY])
def test_allocate_id_is_process_safe(tmp_path: Path, family: IdFamily) -> None:
    state_path = _database(tmp_path / ".runtime" / "runs.db")
    now = datetime(2026, 1, 2, 4, 0, tzinfo=UTC)
    process_context = multiprocessing.get_context("spawn")

    with ProcessPoolExecutor(max_workers=4, mp_context=process_context) as pool:
        batches = tuple(
            pool.submit(_allocate_ids, str(state_path), 8, now, family)
            for _ in range(4)
        )
        values = [value for batch in batches for value in batch.result()]

    assert len(values) == 32
    assert len(set(values)) == 32
    assert sorted(
        decode_id(value, family=family, agent_name="alice").seq for value in values
    ) == list(range(32))
    resumed = allocate_id(state_path, family=family, agent_name="alice", now=now)
    assert resumed.seq == 32


@pytest.mark.parametrize(
    ("tick", "seq"),
    [
        (-2, 0),
        (-1, 4),
        (0, -2),
        (0, "broken"),
        (0, 1.5),
        (LOCAL_ID_FAMILY.tick_modulus, 0),
        (0, LOCAL_ID_FAMILY.seq_modulus),
    ],
)
def test_allocate_id_rejects_invalid_persisted_state(tmp_path, tick, seq) -> None:
    path = _database(tmp_path / "runs.db")
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA ignore_check_constraints=ON")
        connection.execute("INSERT INTO seqs VALUES ('local', ?, ?)", (tick, seq))
    with pytest.raises(ValueError):
        allocate_id(path, family=LOCAL_ID_FAMILY, agent_name="alice")
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT last_tick, last_seq FROM seqs"
        ).fetchone() == (tick, seq)


def test_allocate_id_requires_an_initialized_database(tmp_path: Path) -> None:
    path = tmp_path / "runs.db"
    with pytest.raises(sqlite3.OperationalError, match="unable to open"):
        allocate_id(path, family=LOCAL_ID_FAMILY, agent_name="alice")
    assert not path.exists()
    with sqlite3.connect(path):
        pass
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        allocate_id(path, family=LOCAL_ID_FAMILY, agent_name="alice")
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT name FROM sqlite_master").fetchall() == []


def test_reservation_survives_business_rollback_and_history_deletion(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "toolang.common.ids.allocate_id",
        partial(allocate_id, now=LOCAL_ID_FAMILY.bucket_started_at(100)),
    )
    path = tmp_path / "runs.db"
    store = RunStore(path)
    try:
        ids = IdIssuer(path, agent_name="alice")
        first = ids.issue_thread("chat")
        with pytest.raises(RuntimeError, match="creation failed"):
            with store.write_transaction():
                store.create_thread(thread_id=first, origin="chat")
                raise RuntimeError("creation failed")
        assert store.get_thread(thread_id=first) is None
        store.create_thread(thread_id=ids.issue_thread("work"), origin="spawn")
    finally:
        store.close()
    with sqlite3.connect(path) as connection:
        connection.execute("DELETE FROM threads")
    third = IdIssuer(path, agent_name="alice").issue_thread("script")
    decoded = decode_id(
        third.split("_", 1)[1], family=LOCAL_ID_FAMILY, agent_name="alice"
    )
    assert decoded.seq == 2


@pytest.mark.parametrize("failure", ["callback", "collision", "exhaustion", "write"])
def test_failed_reservations_preserve_state_and_release_the_transaction(
    tmp_path, failure
) -> None:
    path = _database(tmp_path / "runs.db")
    now = LOCAL_ID_FAMILY.bucket_started_at(100)
    first = allocate_id(path, family=LOCAL_ID_FAMILY, agent_name="alice", now=now)
    if failure == "write":
        with sqlite3.connect(path) as connection:
            connection.execute(
                "CREATE TRIGGER reject_reservation BEFORE UPDATE ON seqs "
                "BEGIN SELECT RAISE(ABORT, 'write failed'); END"
            )

    def exists(_value: str) -> bool:
        if failure == "callback":
            raise RuntimeError("callback failed")
        return failure == "collision"

    later = (
        LOCAL_ID_FAMILY.epoch + timedelta(hours=LOCAL_ID_FAMILY.tick_modulus)
        if failure == "exhaustion"
        else now
    )
    with pytest.raises((RuntimeError, OverflowError, sqlite3.IntegrityError)):
        allocate_id(
            path,
            family=LOCAL_ID_FAMILY,
            agent_name="alice",
            now=later,
            exists=exists,
            max_attempts=2,
        )
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT last_tick, last_seq FROM seqs"
        ).fetchone() == (first.tick, first.seq)
        if failure == "write":
            connection.execute("DROP TRIGGER reject_reservation")
    assert (
        allocate_id(path, family=LOCAL_ID_FAMILY, agent_name="alice", now=now).seq == 1
    )


@pytest.mark.parametrize(
    "state",
    [
        AllocatorState(last_tick=-1, last_seq=-1),
        AllocatorState(last_tick=0, last_seq=-1),
        AllocatorState(last_tick=0, last_seq=0),
    ],
)
def test_allocator_state_accepts_valid_boundaries(state: AllocatorState) -> None:
    assert state.last_tick >= -1


def test_allocator_state_rejects_sequence_without_tick() -> None:
    with pytest.raises(ValueError, match="last_seq requires"):
        AllocatorState(last_seq=0)


@pytest.mark.parametrize("field", ["last_tick", "last_seq"])
def test_allocator_state_rejects_values_below_empty_sentinel(field: str) -> None:
    with pytest.raises(ValueError, match=f"{field} must be at least -1"):
        replace(AllocatorState(), **{field: -2})


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("name", "", "name cannot be empty"),
        ("tick_chars", 0, "tick_chars must be positive"),
        ("seq_chars", 0, "seq_chars must be positive"),
        ("tick_seconds", 0, "tick_seconds must be positive"),
        ("epoch", datetime(2026, 1, 1), "epoch must be timezone-aware"),
        ("tick_multiplier", 0, "tick_multiplier must be inside"),
        ("seq_multiplier", 0, "seq_multiplier must be inside"),
        ("tick_offset", -1, "tick_offset must be inside"),
        ("seq_offset", -1, "seq_offset must be inside"),
        ("tick_multiplier", 2, "tick_multiplier must be odd"),
        ("seq_multiplier", 2, "seq_multiplier must be odd"),
    ],
)
def test_id_family_rejects_invalid_configuration(
    field: str,
    value: object,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        replace(LOCAL_ID_FAMILY, **{field: value})


def test_id_family_rejects_unsupported_odd_wire_width() -> None:
    with pytest.raises(ValueError, match="even number of bits"):
        IdFamily(
            name="odd",
            tick_chars=1,
            seq_chars=2,
            tick_seconds=1,
            tick_multiplier=3,
            tick_offset=0,
            seq_multiplier=3,
            seq_offset=0,
        )


def test_reserve_next_id_rejects_non_positive_attempt_limit() -> None:
    with pytest.raises(ValueError, match="max_attempts must be positive"):
        reserve_next_id(
            AllocatorState(), family=LOCAL_ID_FAMILY, max_attempts=0, agent_name="alice"
        )


def test_reserve_next_id_keeps_tick_monotonic_when_clock_moves_back() -> None:
    state = AllocatorState(last_tick=100, last_seq=7)

    allocated, next_state = reserve_next_id(
        state,
        family=LOCAL_ID_FAMILY,
        now=LOCAL_ID_FAMILY.bucket_started_at(99),
        agent_name="alice",
    )

    assert (allocated.tick, allocated.seq) == (100, 8)
    assert next_state == AllocatorState(last_tick=100, last_seq=8)


def test_reserve_next_id_reports_sequence_and_collision_exhaustion() -> None:
    tick = 100
    now = LOCAL_ID_FAMILY.bucket_started_at(tick)

    with pytest.raises(OverflowError, match="exhausted for tick"):
        reserve_next_id(
            AllocatorState(last_tick=tick, last_seq=LOCAL_ID_FAMILY.seq_modulus - 1),
            family=LOCAL_ID_FAMILY,
            now=now,
            agent_name="alice",
        )

    with pytest.raises(RuntimeError, match="after 2 attempts"):
        reserve_next_id(
            AllocatorState(last_tick=tick, last_seq=-1),
            family=LOCAL_ID_FAMILY,
            now=now,
            exists=lambda _value: True,
            max_attempts=2,
            agent_name="alice",
        )


def test_reserve_next_id_reports_tick_domain_exhaustion() -> None:
    family = IdFamily(
        name="small",
        tick_chars=1,
        seq_chars=1,
        tick_seconds=1,
        tick_multiplier=3,
        tick_offset=1,
        seq_multiplier=3,
        seq_offset=1,
    )

    with pytest.raises(OverflowError, match="tick domain"):
        reserve_next_id(
            AllocatorState(),
            family=family,
            now=family.epoch + timedelta(seconds=family.tick_modulus),
            agent_name="alice",
        )


def test_decode_id_rejects_invalid_width() -> None:
    with pytest.raises(ValueError, match="invalid local id width"):
        decode_id("abcd", family=LOCAL_ID_FAMILY, agent_name="alice")


def test_decode_id_rejects_invalid_character() -> None:
    with pytest.raises(ValueError, match="unsupported id character"):
        decode_id("0000000i", family=LOCAL_ID_FAMILY, agent_name="alice")


def test_encode_id_rejects_out_of_range_seq() -> None:
    with pytest.raises(ValueError, match="seq out of range"):
        encode_id(
            family=LOCAL_ID_FAMILY,
            tick=1,
            seq=LOCAL_ID_FAMILY.seq_modulus,
            agent_name="alice",
        )
