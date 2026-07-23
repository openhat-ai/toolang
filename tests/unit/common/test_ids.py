from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import multiprocessing
import operator
from pathlib import Path
from typing import Any, cast

import pytest

from toolang.common.ids import (
    AllocatorSnapshot,
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


def _allocate_local_ids(state_path: str, count: int, now: datetime) -> tuple[str, ...]:
    return tuple(
        allocate_id(Path(state_path), family=LOCAL_ID_FAMILY, now=now).value
        for _ in range(count)
    )


def test_encode_decode_round_trip_for_local_ids() -> None:
    value = encode_id(family=LOCAL_ID_FAMILY, tick=12_345, seq=17)
    decoded = decode_id(value, family=LOCAL_ID_FAMILY)

    assert len(value) == 8
    assert decoded.tick == 12_345
    assert decoded.seq == 17
    assert decoded.prefix == value[:4]
    assert decoded.suffix == value[4:]


def test_encode_decode_round_trip_for_run_ids() -> None:
    value = encode_id(family=RUN_ID_FAMILY, tick=7_654, seq=12_345)
    decoded = decode_id(value, family=RUN_ID_FAMILY)

    assert len(value) == 8
    assert decoded.tick == 7_654
    assert decoded.seq == 12_345


def test_archive_prefix_matches_decoded_prefix() -> None:
    value = encode_id(family=LOCAL_ID_FAMILY, tick=88, seq=4)

    assert archive_prefix(value, family=LOCAL_ID_FAMILY) == decode_id(
        value,
        family=LOCAL_ID_FAMILY,
    ).archive_prefix


def test_reserve_next_id_advances_seq_within_one_tick() -> None:
    now = datetime(2026, 1, 2, 3, 0, tzinfo=UTC)

    first, state = reserve_next_id(AllocatorState(), family=LOCAL_ID_FAMILY, now=now)
    second, next_state = reserve_next_id(state, family=LOCAL_ID_FAMILY, now=now)

    assert first.tick == second.tick
    assert first.seq == 0
    assert second.seq == 1
    assert first.prefix != second.prefix
    assert first.archive_prefix == second.archive_prefix
    assert next_state == AllocatorState(last_tick=first.tick, last_seq=1)


def test_reserve_next_id_resets_seq_for_next_tick() -> None:
    first_now = datetime(2026, 1, 2, 3, 0, tzinfo=UTC)
    second_now = first_now + timedelta(hours=1)

    _first, state = reserve_next_id(AllocatorState(), family=LOCAL_ID_FAMILY, now=first_now)
    second, next_state = reserve_next_id(state, family=LOCAL_ID_FAMILY, now=second_now)

    assert second.seq == 0
    assert next_state.last_tick == LOCAL_ID_FAMILY.tick_for(second_now)


def test_reserve_next_id_uses_exists_callback_as_safety_belt() -> None:
    seen = set()
    original = encode_id(family=LOCAL_ID_FAMILY, tick=999, seq=0)
    seen.add(original)

    allocated, state = reserve_next_id(
        AllocatorState(last_tick=999, last_seq=-1),
        family=LOCAL_ID_FAMILY,
        now=LOCAL_ID_FAMILY.bucket_started_at(999),
        exists=seen.__contains__,
    )

    assert allocated.value != original
    assert allocated.seq == 1
    assert state == AllocatorState(last_tick=999, last_seq=1)


def test_allocate_id_persists_state_by_family(tmp_path: Path) -> None:
    state_path = tmp_path / ".meta" / "ids.json"
    now = datetime(2026, 1, 2, 4, 0, tzinfo=UTC)

    first = allocate_id(state_path, family=LOCAL_ID_FAMILY, now=now)
    second = allocate_id(state_path, family=LOCAL_ID_FAMILY, now=now)
    run = allocate_id(state_path, family=RUN_ID_FAMILY, now=now)
    snapshot = AllocatorSnapshot.load(state_path)

    assert first.seq == 0
    assert second.seq == 1
    assert run.seq == 0
    assert snapshot.state_for(LOCAL_ID_FAMILY) == AllocatorState(last_tick=first.tick, last_seq=1)
    assert snapshot.state_for(RUN_ID_FAMILY) == AllocatorState(last_tick=run.tick, last_seq=0)


def test_id_issuer_uses_explicit_canonical_prefixes(tmp_path: Path) -> None:
    ids = IdIssuer(tmp_path / ".meta" / "ids.json")

    assert ids.issue_run().startswith("run_")
    assert ids.issue_thread("term").startswith("term_")
    with pytest.raises(ValueError, match="invalid thread prefix"):
        ids.issue_thread("TUI")


def test_allocate_id_is_process_safe(tmp_path: Path) -> None:
    state_path = tmp_path / ".meta" / "ids.json"
    now = datetime(2026, 1, 2, 4, 0, tzinfo=UTC)
    process_context = multiprocessing.get_context("spawn")

    with ProcessPoolExecutor(max_workers=4, mp_context=process_context) as pool:
        batches = tuple(
            pool.submit(_allocate_local_ids, str(state_path), 8, now)
            for _ in range(4)
        )
        values = [value for batch in batches for value in batch.result()]

    assert len(values) == 32
    assert len(set(values)) == 32
    assert sorted(decode_id(value, family=LOCAL_ID_FAMILY).seq for value in values) == list(
        range(32)
    )


def test_allocator_snapshot_rejects_invalid_persisted_state() -> None:
    with pytest.raises(ValueError, match="families must be a mapping"):
        AllocatorSnapshot.from_data({"families": []})

    with pytest.raises(ValueError, match="families must be a mapping"):
        AllocatorSnapshot.from_data({"families": None})

    with pytest.raises(ValueError, match="family name cannot be empty"):
        AllocatorSnapshot.from_data({"families": {"": {}}})

    with pytest.raises(ValueError, match="must be a mapping"):
        AllocatorSnapshot.from_data({"families": {"run": "broken"}})

    with pytest.raises(ValueError, match="last_seq requires"):
        AllocatorSnapshot.from_data({"families": {"run": {"last_seq": 4}}})

    with pytest.raises(TypeError, match="boolean values"):
        AllocatorSnapshot.from_data({"families": {"run": {"last_tick": True}}})

    with pytest.raises(TypeError, match="null values"):
        AllocatorSnapshot.from_data({"families": {"run": {"last_tick": None}}})


def test_allocator_snapshot_save_load_round_trip(tmp_path: Path) -> None:
    path = tmp_path / ".meta" / "ids.json"
    snapshot = AllocatorSnapshot(
        families={"local": AllocatorState(last_tick=12, last_seq=3)}
    )

    snapshot.save(path)

    assert AllocatorSnapshot.load(path) == snapshot
    assert AllocatorSnapshot.load(tmp_path / "missing.json") == AllocatorSnapshot()


def test_allocator_snapshot_copies_and_freezes_family_state() -> None:
    families = {"local": AllocatorState(last_tick=1, last_seq=2)}
    snapshot = AllocatorSnapshot(families=families)
    families.clear()

    assert snapshot.state_for(LOCAL_ID_FAMILY) == AllocatorState(last_tick=1, last_seq=2)
    with pytest.raises(TypeError):
        operator.setitem(cast(Any, snapshot.families), "run", AllocatorState())


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
        reserve_next_id(AllocatorState(), family=LOCAL_ID_FAMILY, max_attempts=0)


def test_reserve_next_id_keeps_tick_monotonic_when_clock_moves_back() -> None:
    state = AllocatorState(last_tick=100, last_seq=7)

    allocated, next_state = reserve_next_id(
        state,
        family=LOCAL_ID_FAMILY,
        now=LOCAL_ID_FAMILY.bucket_started_at(99),
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
        )

    with pytest.raises(RuntimeError, match="after 2 attempts"):
        reserve_next_id(
            AllocatorState(last_tick=tick, last_seq=-1),
            family=LOCAL_ID_FAMILY,
            now=now,
            exists=lambda _value: True,
            max_attempts=2,
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
        )


def test_decode_id_rejects_invalid_width() -> None:
    with pytest.raises(ValueError, match="invalid local id width"):
        decode_id("abcd", family=LOCAL_ID_FAMILY)


def test_decode_id_rejects_invalid_character() -> None:
    with pytest.raises(ValueError, match="unsupported id character"):
        decode_id("0000000i", family=LOCAL_ID_FAMILY)


def test_encode_id_rejects_out_of_range_seq() -> None:
    with pytest.raises(ValueError, match="seq out of range"):
        encode_id(
            family=LOCAL_ID_FAMILY,
            tick=1,
            seq=LOCAL_ID_FAMILY.seq_modulus,
        )
