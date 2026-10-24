"""Stable local id families and SQLite-backed allocation.

This module defines fixed-width short-id families for Toolang-owned local
objects and execution records, plus one SQLite-backed allocator that can be
shared across CLI and runtime processes.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
import sqlite3

from .id_codec import decode_short_id, derive_id_key, encode_short_id, scramble_id

ID_EPOCH = datetime(2026, 1, 1, tzinfo=UTC)


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
        object.__setattr__(self, "epoch", self.epoch.astimezone(UTC))
        if (self.width * 5) % 2:
            raise ValueError("id family width must contain an even number of bits")
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

    def __post_init__(self) -> None:
        if self.last_tick < -1:
            raise ValueError("last_tick must be at least -1")
        if self.last_seq < -1:
            raise ValueError("last_seq must be at least -1")
        if self.last_tick == -1 and self.last_seq != -1:
            raise ValueError("last_seq requires an allocated tick")

    @classmethod
    def from_data(cls, payload: Mapping[str, object]) -> AllocatorState:
        """Parse one serialized allocator state."""

        return cls(
            last_tick=_int_value(payload["last_tick"])
            if "last_tick" in payload
            else -1,
            last_seq=_int_value(payload["last_seq"]) if "last_seq" in payload else -1,
        )

    def to_data(self) -> dict[str, int]:
        """Return one JSON-friendly snapshot."""

        return {
            "last_tick": self.last_tick,
            "last_seq": self.last_seq,
        }


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

        return encode_short_id(self.tick_code, width=self.family.tick_chars)

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

        return encode_short_id(
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
    seq_chars=4,
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


def encode_id(*, family: IdFamily, agent_name: str, tick: int, seq: int) -> str:
    """Encode one raw tick/seq pair using its canonical agent name."""

    _check_range("tick", tick, family.tick_modulus)
    _check_range("seq", seq, family.seq_modulus)
    tick_code = _affine_encode(
        tick,
        modulus=family.tick_modulus,
        multiplier=family.tick_multiplier,
        offset=family.tick_offset,
    )
    mixed_seq = seq ^ _tick_mask(family=family, tick=tick)
    seq_code = _affine_encode(
        mixed_seq,
        modulus=family.seq_modulus,
        multiplier=family.seq_multiplier,
        offset=family.seq_offset,
    )
    raw_code = (tick_code << family.seq_bits) | seq_code
    wire_code = scramble_id(
        raw_code,
        width=family.width,
        key=_agent_id_key(family=family, agent_name=agent_name),
    )
    return encode_short_id(wire_code, width=family.width)


def decode_id(value: str, *, family: IdFamily, agent_name: str) -> DecodedId:
    """Decode an id with the canonical agent name used to encode it."""

    text = value.strip().lower()
    if len(text) != family.width:
        raise ValueError(f"invalid {family.name} id width: {value!r}")
    wire_code = decode_short_id(text, width=family.width)
    raw_code = scramble_id(
        wire_code,
        width=family.width,
        key=_agent_id_key(family=family, agent_name=agent_name),
        reverse=True,
    )
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
    seq = mixed_seq ^ _tick_mask(family=family, tick=tick)
    return DecodedId(
        value=text,
        family=family,
        tick=tick,
        seq=seq,
        tick_code=tick_code,
        seq_code=seq_code,
    )


def _tick_mask(*, family: IdFamily, tick: int) -> int:
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
    agent_name: str,
    now: datetime | None = None,
    exists: Callable[[str], bool] | None = None,
    max_attempts: int = 128,
) -> tuple[AllocatedId, AllocatorState]:
    """Reserve the next id from one in-memory allocator state."""

    issued_at = _as_utc(now or datetime.now(UTC))
    if max_attempts <= 0:
        raise ValueError("max_attempts must be positive")
    current_tick = family.tick_for(issued_at)
    tick = max(current_tick, state.last_tick)
    if tick >= family.tick_modulus:
        raise OverflowError(f"{family.name} id allocator exhausted its tick domain")
    next_seq = 0 if tick > state.last_tick else state.last_seq + 1
    for _attempt in range(max_attempts):
        if next_seq >= family.seq_modulus:
            raise OverflowError(f"{family.name} id allocator exhausted for tick {tick}")
        value = encode_id(family=family, agent_name=agent_name, tick=tick, seq=next_seq)
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
    raise RuntimeError(
        f"unable to reserve {family.name} id after {max_attempts} attempts"
    )


def allocate_id(
    db_path: Path,
    *,
    family: IdFamily,
    agent_name: str,
    now: datetime | None = None,
    exists: Callable[[str], bool] | None = None,
    max_attempts: int = 128,
) -> AllocatedId:
    """Commit one family reservation independently of execution writes."""

    target = f"{db_path.expanduser().resolve().as_uri()}?mode=rw"
    with closing(sqlite3.connect(target, uri=True, timeout=30)) as connection:
        connection.execute("PRAGMA synchronous=FULL")
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT last_tick, last_seq FROM seqs WHERE family = ?",
                (family.name,),
            ).fetchone()
            if row is not None and any(type(value) is not int for value in row):
                raise ValueError(f"invalid allocator state for {family.name!r}")
            state = AllocatorState(*row) if row is not None else AllocatorState()
            if state.last_tick != -1:
                _check_range("last_tick", state.last_tick, family.tick_modulus)
            if state.last_seq != -1:
                _check_range("last_seq", state.last_seq, family.seq_modulus)
            allocation, next_state = reserve_next_id(
                state,
                family=family,
                agent_name=agent_name,
                now=now,
                exists=exists,
                max_attempts=max_attempts,
            )
            connection.execute(
                """
                INSERT INTO seqs (family, last_tick, last_seq) VALUES (?, ?, ?)
                ON CONFLICT (family) DO UPDATE
                SET last_tick = excluded.last_tick, last_seq = excluded.last_seq
                """,
                (family.name, next_state.last_tick, next_state.last_seq),
            )
        return allocation


@dataclass(frozen=True, slots=True)
class IdIssuer:
    """Issue process-safe Toolang ids from one durable allocator state."""

    db_path: Path
    agent_name: str

    def issue_run(self) -> str:
        """Issue one run id."""

        value = allocate_id(
            self.db_path, family=RUN_ID_FAMILY, agent_name=self.agent_name
        ).value
        return f"run_{value}"

    def issue_thread(self, prefix: str) -> str:
        """Issue one local thread id with an explicit canonical prefix."""

        normalized = prefix.strip().lower()
        if normalized != prefix or not normalized or not normalized.isalnum():
            raise ValueError(f"invalid thread prefix: {prefix}")
        value = allocate_id(
            self.db_path, family=LOCAL_ID_FAMILY, agent_name=self.agent_name
        ).value
        return f"{prefix}_{value}"


def archive_prefix(value: str, *, family: IdFamily, agent_name: str) -> str:
    """Return the stable archive prefix for one id."""

    return decode_id(value, family=family, agent_name=agent_name).archive_prefix


def _agent_id_key(*, family: IdFamily, agent_name: str) -> bytes:
    """Derive a stable key without changing the canonical name's case."""

    if not agent_name or agent_name != agent_name.strip():
        raise ValueError("agent name must be nonempty and canonical")
    return derive_id_key(family.name, agent_name)


def _affine_encode(value: int, *, modulus: int, multiplier: int, offset: int) -> int:
    return (multiplier * value + offset) % modulus


def _affine_decode(value: int, *, modulus: int, multiplier: int, offset: int) -> int:
    inverse = pow(multiplier, -1, modulus)
    return (inverse * (value - offset)) % modulus


def _check_range(label: str, value: int, modulus: int) -> None:
    if value < 0 or value >= modulus:
        raise ValueError(f"{label} out of range: {value}")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _int_value(value: object) -> int:
    if value is None:
        raise TypeError("null values are not valid integers")
    if isinstance(value, bool):
        raise TypeError("boolean values are not valid integers")
    if isinstance(value, int):
        return value
    return int(str(value).strip())
