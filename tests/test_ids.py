from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from toolang.common.ids import (
    AllocatorSnapshot,
    AllocatorState,
    LOCAL_ID_FAMILY,
    RUN_ID_FAMILY,
    allocate_id,
    archive_prefix,
    decode_id,
    encode_id,
    family_by_name,
    reserve_next_id,
    tick_mask,
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


def test_tick_mask_is_stable_for_same_tick() -> None:
    assert tick_mask(family=LOCAL_ID_FAMILY, tick=12_345) == tick_mask(
        family=LOCAL_ID_FAMILY,
        tick=12_345,
    )


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


def test_family_by_name_returns_registered_family() -> None:
    assert family_by_name("local") is LOCAL_ID_FAMILY
    assert family_by_name("run") is RUN_ID_FAMILY


def test_decode_id_rejects_invalid_width() -> None:
    with pytest.raises(ValueError, match="invalid local id width"):
        decode_id("abcd", family=LOCAL_ID_FAMILY)


def test_encode_id_rejects_out_of_range_seq() -> None:
    with pytest.raises(ValueError, match="seq out of range"):
        encode_id(
            family=LOCAL_ID_FAMILY,
            tick=1,
            seq=LOCAL_ID_FAMILY.seq_modulus,
        )
