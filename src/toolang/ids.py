"""Stable local id families and file-backed allocators.

This module defines fixed-width short-id families for Toolang-owned local
objects and execution records, plus one file-backed allocator that can be
shared across CLI and runtime processes.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
import importlib
import json
from pathlib import Path
from types import ModuleType
from typing import TextIO, cast

try:  # pragma: no cover - exercised on POSIX in tests
    _fcntl_module = importlib.import_module("fcntl")
except ImportError:  # pragma: no cover - platform dependent
    _fcntl_module = None


ID_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"
ID_BASE = len(ID_ALPHABET)
ID_EPOCH = datetime(2026, 1, 1, tzinfo=UTC)

_ID_LOOKUP = {char: index for index, char in enumerate(ID_ALPHABET)}
_FCNTL: ModuleType | None = _fcntl_module
_ID_FEISTEL_ROUNDS = 4


@dataclass(frozen=True, slots=True)
class IdFamily:
    """One fixed-width Toolang-owned id family."""

    name: str
    tick_chars: int
    seq_chars: int
    tick_seconds: int
    tick_multiplier: int
    tick_offset: int
    seq_multiplier: int
    seq_offset: int
    mask_seed: int = 0x15A5
    epoch: datetime = ID_EPOCH

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("id family name cannot be empty")
        if self.tick_chars <= 0:
            raise ValueError("tick_chars must be positive")
        if self.seq_chars <= 0:
            raise ValueError("seq_chars must be positive")
        if self.tick_seconds <= 0:
            raise ValueError("tick_seconds must be positive")
        if self.epoch.tzinfo is None:
            raise ValueError("epoch must be timezone-aware")
        if self.tick_multiplier <= 0 or self.tick_multiplier >= self.tick_modulus:
            raise ValueError("tick_multiplier must be inside the tick domain")
        if self.seq_multiplier <= 0 or self.seq_multiplier >= self.seq_modulus:
            raise ValueError("seq_multiplier must be inside the seq domain")
        if self.tick_offset < 0 or self.tick_offset >= self.tick_modulus:
            raise ValueError("tick_offset must be inside the tick domain")
        if self.seq_offset < 0 or self.seq_offset >= self.seq_modulus:
            raise ValueError("seq_offset must be inside the seq domain")
        if self.tick_multiplier % 2 == 0:
            raise ValueError("tick_multiplier must be odd")
        if self.seq_multiplier % 2 == 0:
            raise ValueError("seq_multiplier must be odd")

    @property
    def width(self) -> int:
        """Return the full encoded width."""

        return self.tick_chars + self.seq_chars

    @property
    def tick_bits(self) -> int:
        """Return the tick-domain width in bits."""

        return self.tick_chars * 5

    @property
    def seq_bits(self) -> int:
        """Return the seq-domain width in bits."""

        return self.seq_chars * 5

    @property
    def tick_modulus(self) -> int:
        """Return the tick-domain modulus."""

        return 1 << self.tick_bits

    @property
    def seq_modulus(self) -> int:
        """Return the seq-domain modulus."""

        return 1 << self.seq_bits

    def tick_for(self, moment: datetime) -> int:
        """Return the bucket index for one timestamp."""

        current = _as_utc(moment)
        if current < self.epoch:
            raise ValueError("moment precedes the id epoch")
        delta = current - self.epoch
        return int(delta.total_seconds()) // self.tick_seconds

    def bucket_started_at(self, tick: int) -> datetime:
        """Return the UTC start time for one tick bucket."""

        _check_range("tick", tick, self.tick_modulus)
        return self.epoch + timedelta(seconds=tick * self.tick_seconds)


@dataclass(frozen=True, slots=True)
class AllocatorState:
    """One monotonic allocator state for one id family."""

    last_tick: int = -1
    last_seq: int = -1

    @classmethod
    def from_data(cls, payload: Mapping[str, object]) -> AllocatorState:
        """Parse one serialized allocator state."""

        return cls(
            last_tick=_int_value(payload.get("last_tick"), default=-1),
            last_seq=_int_value(payload.get("last_seq"), default=-1),
        )

    def to_data(self) -> dict[str, int]:
        """Return one JSON-friendly snapshot."""

        return {
            "last_tick": self.last_tick,
            "last_seq": self.last_seq,
        }


@dataclass(frozen=True, slots=True)
class AllocatorSnapshot:
    """One persisted allocator snapshot keyed by family name."""

    families: dict[str, AllocatorState] = field(default_factory=dict)

    @classmethod
    def from_data(cls, payload: Mapping[str, object]) -> AllocatorSnapshot:
        """Parse one serialized allocator snapshot."""

        raw_families = payload.get("families")
        if not isinstance(raw_families, Mapping):
            return cls()
        items: dict[str, AllocatorState] = {}
        for raw_name, raw_state in raw_families.items():
            name = str(raw_name).strip()
            if not name or not isinstance(raw_state, Mapping):
                continue
            items[name] = AllocatorState.from_data(cast(Mapping[str, object], raw_state))
        return cls(families=items)

    @classmethod
    def load(cls, path: Path) -> AllocatorSnapshot:
        """Load one persisted allocator snapshot from disk."""

        if not path.is_file():
            return cls()
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            return cls()
        data = json.loads(text)
        if not isinstance(data, Mapping):
            raise ValueError(f"invalid allocator snapshot: {path}")
        return cls.from_data(data)

    def save(self, path: Path) -> None:
        """Write this allocator snapshot to disk."""

        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "families": {
                name: state.to_data()
                for name, state in sorted(self.families.items())
            }
        }
        path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def state_for(self, family: IdFamily) -> AllocatorState:
        """Return the current state for one family."""

        return self.families.get(family.name, AllocatorState())

    def with_state(self, family: IdFamily, state: AllocatorState) -> AllocatorSnapshot:
        """Return a copy with one family state replaced."""

        items = dict(self.families)
        items[family.name] = state
        return AllocatorSnapshot(families=items)


@dataclass(frozen=True, slots=True)
class DecodedId:
    """One decoded Toolang-owned id."""

    value: str
    family: IdFamily
    tick: int
    seq: int
    tick_code: int
    seq_code: int

    @property
    def prefix(self) -> str:
        """Return the visible leading chars of the encoded id."""

        return self.value[: self.family.tick_chars]

    @property
    def suffix(self) -> str:
        """Return the visible trailing chars of the encoded id."""

        return self.value[self.family.tick_chars :]

    @property
    def archive_prefix(self) -> str:
        """Return the stable bucket prefix derived from the decoded tick."""

        return _encode_fixed_width(self.tick_code, width=self.family.tick_chars)

    @property
    def bucket_started_at(self) -> datetime:
        """Return the UTC start time for this id's tick bucket."""

        return self.family.bucket_started_at(self.tick)


@dataclass(frozen=True, slots=True)
class AllocatedId:
    """One newly allocated Toolang-owned id."""

    value: str
    family: IdFamily
    tick: int
    seq: int
    issued_at: datetime

    @property
    def prefix(self) -> str:
        """Return the visible leading chars of the encoded id."""

        return self.value[: self.family.tick_chars]

    @property
    def archive_prefix(self) -> str:
        """Return the stable bucket prefix derived from the decoded tick."""

        return _encode_fixed_width(
            _affine_encode(
                self.tick,
                modulus=self.family.tick_modulus,
                multiplier=self.family.tick_multiplier,
                offset=self.family.tick_offset,
            ),
            width=self.family.tick_chars,
        )

    @property
    def bucket_started_at(self) -> datetime:
        """Return the UTC start time for this id's tick bucket."""

        return self.family.bucket_started_at(self.tick)


LOCAL_ID_FAMILY = IdFamily(
    name="local",
    tick_chars=4,
    seq_chars=2,
    tick_seconds=3_600,
    tick_multiplier=699_051,
    tick_offset=123_457,
    seq_multiplier=717,
    seq_offset=233,
)

RUN_ID_FAMILY = IdFamily(
    name="run",
    tick_chars=4,
    seq_chars=4,
    tick_seconds=3_600,
    tick_multiplier=699_051,
    tick_offset=123_457,
    seq_multiplier=641_861,
    seq_offset=77_531,
    mask_seed=0x6D35,
)

ID_FAMILIES = {
    family.name: family
    for family in (LOCAL_ID_FAMILY, RUN_ID_FAMILY)
}


def encode_id(*, family: IdFamily, tick: int, seq: int) -> str:
    """Encode one raw tick/seq pair into one fixed-width id."""

    _check_range("tick", tick, family.tick_modulus)
    _check_range("seq", seq, family.seq_modulus)
    tick_code = _affine_encode(
        tick,
        modulus=family.tick_modulus,
        multiplier=family.tick_multiplier,
        offset=family.tick_offset,
    )
    mixed_seq = seq ^ tick_mask(family=family, tick=tick)
    seq_code = _affine_encode(
        mixed_seq,
        modulus=family.seq_modulus,
        multiplier=family.seq_multiplier,
        offset=family.seq_offset,
    )
    raw_code = (tick_code << family.seq_bits) | seq_code
    wire_code = _permute_wire_code(
        raw_code,
        family=family,
    )
    return _encode_fixed_width(wire_code, width=family.width)


def decode_id(value: str, *, family: IdFamily) -> DecodedId:
    """Decode one fixed-width id back into raw tick/seq values."""

    text = value.strip().lower()
    if len(text) != family.width:
        raise ValueError(f"invalid {family.name} id width: {value!r}")
    wire_code = _decode_fixed_width(text, width=family.width)
    raw_code = _unpermute_wire_code(wire_code, family=family)
    tick_code = raw_code >> family.seq_bits
    seq_code = raw_code & (family.seq_modulus - 1)
    tick = _affine_decode(
        tick_code,
        modulus=family.tick_modulus,
        multiplier=family.tick_multiplier,
        offset=family.tick_offset,
    )
    mixed_seq = _affine_decode(
        seq_code,
        modulus=family.seq_modulus,
        multiplier=family.seq_multiplier,
        offset=family.seq_offset,
    )
    seq = mixed_seq ^ tick_mask(family=family, tick=tick)
    return DecodedId(
        value=text,
        family=family,
        tick=tick,
        seq=seq,
        tick_code=tick_code,
        seq_code=seq_code,
    )


def tick_mask(*, family: IdFamily, tick: int) -> int:
    """Return the reversible tick-derived seq mask for one family."""

    _check_range("tick", tick, family.tick_modulus)
    value = tick
    value ^= value >> 7
    value ^= value >> 13
    value ^= family.mask_seed
    return value & (family.seq_modulus - 1)


def reserve_next_id(
    state: AllocatorState,
    *,
    family: IdFamily,
    now: datetime | None = None,
    exists: Callable[[str], bool] | None = None,
    max_attempts: int = 128,
) -> tuple[AllocatedId, AllocatorState]:
    """Reserve the next id from one in-memory allocator state."""

    issued_at = _as_utc(now or datetime.now(UTC))
    current_tick = family.tick_for(issued_at)
    tick = max(current_tick, state.last_tick)
    next_seq = 0 if tick > state.last_tick else state.last_seq + 1
    for _attempt in range(max_attempts):
        if next_seq >= family.seq_modulus:
            raise OverflowError(f"{family.name} id allocator exhausted for tick {tick}")
        value = encode_id(family=family, tick=tick, seq=next_seq)
        if exists is None or not exists(value):
            allocation = AllocatedId(
                value=value,
                family=family,
                tick=tick,
                seq=next_seq,
                issued_at=issued_at,
            )
            return allocation, AllocatorState(last_tick=tick, last_seq=next_seq)
        next_seq += 1
    raise RuntimeError(f"unable to reserve {family.name} id after {max_attempts} attempts")


def allocate_id(
    state_path: Path,
    *,
    family: IdFamily,
    now: datetime | None = None,
    exists: Callable[[str], bool] | None = None,
    max_attempts: int = 128,
) -> AllocatedId:
    """Allocate and persist the next id for one family under one shared state file."""

    with _locked_state_file(state_path) as handle:
        snapshot = _snapshot_from_handle(handle)
        allocation, next_state = reserve_next_id(
            snapshot.state_for(family),
            family=family,
            now=now,
            exists=exists,
            max_attempts=max_attempts,
        )
        _write_snapshot(handle, snapshot.with_state(family, next_state))
        return allocation


def archive_prefix(value: str, *, family: IdFamily) -> str:
    """Return the stable archive prefix for one id."""

    return decode_id(value, family=family).archive_prefix


def family_by_name(name: str) -> IdFamily:
    """Return one registered family by name."""

    key = name.strip()
    if not key:
        raise KeyError("id family name cannot be empty")
    return ID_FAMILIES[key]


def _permute_wire_code(value: int, *, family: IdFamily) -> int:
    wire_bits = family.width * 5
    wire_modulus = 1 << wire_bits
    _check_range("wire_code", value, wire_modulus)
    if wire_bits % 2 != 0:
        raise ValueError("wire bits must be even")
    half_bits = wire_bits // 2
    half_mask = (1 << half_bits) - 1
    left = (value >> half_bits) & half_mask
    right = value & half_mask
    for round_index in range(_ID_FEISTEL_ROUNDS):
        left, right = right, left ^ _wire_round_function(
            right,
            family=family,
            round_index=round_index,
            mask=half_mask,
        )
    return ((left & half_mask) << half_bits) | (right & half_mask)


def _unpermute_wire_code(value: int, *, family: IdFamily) -> int:
    wire_bits = family.width * 5
    wire_modulus = 1 << wire_bits
    _check_range("wire_code", value, wire_modulus)
    if wire_bits % 2 != 0:
        raise ValueError("wire bits must be even")
    half_bits = wire_bits // 2
    half_mask = (1 << half_bits) - 1
    left = (value >> half_bits) & half_mask
    right = value & half_mask
    for round_index in range(_ID_FEISTEL_ROUNDS - 1, -1, -1):
        left, right = right ^ _wire_round_function(
            left,
            family=family,
            round_index=round_index,
            mask=half_mask,
        ), left
    return ((left & half_mask) << half_bits) | (right & half_mask)


def _wire_round_function(
    value: int,
    *,
    family: IdFamily,
    round_index: int,
    mask: int,
) -> int:
    key = _wire_round_key(family=family, round_index=round_index, mask=mask)
    multiplier = ((key << 1) | 1) & mask
    if multiplier == 0:
        multiplier = 1
    mixed = (value + key) & mask
    mixed ^= mixed >> 3
    mixed ^= (mixed << 5) & mask
    mixed = (mixed * multiplier) & mask
    mixed ^= mixed >> 4
    return mixed & mask


def _wire_round_key(*, family: IdFamily, round_index: int, mask: int) -> int:
    key = (
        family.tick_multiplier
        ^ family.seq_multiplier
        ^ (family.tick_offset << (round_index + 1))
        ^ (family.seq_offset << (round_index + 2))
        ^ (family.mask_seed * (round_index + 1))
        ^ (0x9E37 * (round_index + 1))
    )
    return key & mask


def _affine_encode(value: int, *, modulus: int, multiplier: int, offset: int) -> int:
    return (multiplier * value + offset) % modulus


def _affine_decode(value: int, *, modulus: int, multiplier: int, offset: int) -> int:
    inverse = pow(multiplier, -1, modulus)
    return (inverse * (value - offset)) % modulus


def _encode_fixed_width(value: int, *, width: int) -> str:
    _check_range("value", value, 1 << (width * 5))
    chars = ["0"] * width
    current = value
    for index in range(width - 1, -1, -1):
        chars[index] = ID_ALPHABET[current & 0x1F]
        current >>= 5
    return "".join(chars)


def _decode_fixed_width(value: str, *, width: int) -> int:
    text = value.strip().lower()
    if len(text) != width:
        raise ValueError(f"expected {width} base32 chars, got {value!r}")
    decoded = 0
    for char in text:
        try:
            digit = _ID_LOOKUP[char]
        except KeyError as exc:
            raise ValueError(f"unsupported id character: {char!r}") from exc
        decoded = (decoded << 5) | digit
    return decoded


def _check_range(label: str, value: int, modulus: int) -> None:
    if value < 0 or value >= modulus:
        raise ValueError(f"{label} out of range: {value}")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _int_value(value: object, *, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        raise TypeError("boolean values are not valid integers")
    if isinstance(value, int):
        return value
    return int(str(value).strip())


@contextmanager
def _locked_state_file(path: Path) -> Iterator[TextIO]:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+", encoding="utf-8")
    try:
        if _FCNTL is None:  # pragma: no cover - platform dependent
            raise RuntimeError("file-backed id allocation requires fcntl")
        _FCNTL.flock(handle.fileno(), _FCNTL.LOCK_EX)
        handle.seek(0)
        yield handle
    finally:
        if _FCNTL is not None:  # pragma: no branch - simple cleanup
            _FCNTL.flock(handle.fileno(), _FCNTL.LOCK_UN)
        handle.close()


def _snapshot_from_handle(handle: TextIO) -> AllocatorSnapshot:
    text = handle.read().strip()
    if not text:
        return AllocatorSnapshot()
    data = json.loads(text)
    if not isinstance(data, Mapping):
        raise ValueError("invalid allocator state file")
    return AllocatorSnapshot.from_data(data)


def _write_snapshot(handle: TextIO, snapshot: AllocatorSnapshot) -> None:
    payload = {
        "families": {
            name: state.to_data()
            for name, state in sorted(snapshot.families.items())
        }
    }
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    handle.seek(0)
    handle.truncate()
    handle.write(text)
    handle.flush()
