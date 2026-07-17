"""Immutable root, home, and effective agent source state."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import tomllib
from typing import cast

from toolang.catalog.cap import effective_cap_entries
from ..common.immutable import freeze_mapping
from ..lang.ast import Program, to_data
from toolang.state.prepared import PreparedEntry, PreparedLocks


@dataclass(frozen=True, slots=True)
class RootState:
    """Authored state loaded from the Toolang root."""

    fingerprint: str
    updated_at: str
    config: Mapping[str, object]
    caps: tuple[PreparedEntry, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "config", freeze_mapping(self.config))


@dataclass(frozen=True, slots=True)
class HomeState:
    """Authored state loaded from one agent home."""

    fingerprint: str
    updated_at: str
    config: Mapping[str, object]
    program: Program
    caps: tuple[PreparedEntry, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "config", freeze_mapping(self.config))


@dataclass(frozen=True, slots=True)
class AgentState:
    """Effective immutable source state captured by a run."""

    fingerprint: str
    updated_at: str
    loaded_at: str
    root: RootState
    home: HomeState
    config: Mapping[str, object]
    program: Program
    caps: tuple[PreparedEntry, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "config", freeze_mapping(self.config))

    def to_snapshot(self) -> dict[str, object]:
        return {
            "fingerprint": self.fingerprint,
            "root_fingerprint": self.root.fingerprint,
            "home_fingerprint": self.home.fingerprint,
            "updated_at": self.updated_at,
            "loaded_at": self.loaded_at,
            "program": to_data(self.program),
            "caps": [entry.path for entry in self.caps],
        }


def load_agent_state(
    locks: PreparedLocks,
    *,
    program: Program | None = None,
) -> AgentState:
    """Build effective agent state from one pair of prepared locks."""

    root_config = _load_config(locks.toolang_root / "config.toml")
    home_config = _load_config(
        locks.toolang_root / "agents" / locks.agent_name / "config.toml"
    )
    root = RootState(
        fingerprint=_state_fingerprint(
            _entries_fingerprint(locks.shared_lock.entries), root_config
        ),
        updated_at=locks.shared_lock.updated_at,
        config=root_config,
        caps=_cap_entries(locks.shared_lock.entries),
    )
    home = HomeState(
        fingerprint=_state_fingerprint(
            _state_fingerprint(
                _entries_fingerprint(locks.private_lock.entries),
                locks.program_source.fingerprint(),
            ),
            home_config,
        ),
        updated_at=locks.private_lock.updated_at,
        config=home_config,
        program=program if program is not None else locks.program_source.parse(),
        caps=_cap_entries(locks.private_lock.entries),
    )
    return AgentState(
        fingerprint=_state_fingerprint(root.fingerprint, home.fingerprint),
        updated_at=max(root.updated_at, home.updated_at),
        loaded_at=datetime.now(timezone.utc).isoformat(),
        root=root,
        home=home,
        config=_merge(root.config, home.config),
        program=home.program,
        caps=effective_cap_entries(locks.shared_lock, locks.private_lock),
    )


def _load_config(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {}
    return cast(dict[str, object], tomllib.loads(path.read_text(encoding="utf-8")))


def _cap_entries(entries: tuple[PreparedEntry, ...]) -> tuple[PreparedEntry, ...]:
    return entries


def _entries_fingerprint(entries: tuple[PreparedEntry, ...]) -> str:
    data = [entry.to_data() for entry in entries]
    return sha256(
        json.dumps(
            data, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


def _merge(
    base: Mapping[str, object], override: Mapping[str, object]
) -> dict[str, object]:
    merged = dict(base)
    for key, value in override.items():
        current = merged.get(key)
        if isinstance(current, Mapping) and isinstance(value, Mapping):
            merged[key] = _merge(
                cast(Mapping[str, object], current),
                cast(Mapping[str, object], value),
            )
        else:
            merged[key] = value
    return merged


def _state_fingerprint(source: str, config: str | dict[str, object]) -> str:
    payload = (
        config
        if isinstance(config, str)
        else json.dumps(
            config, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
    )
    return sha256(f"{source}\0{payload}".encode()).hexdigest()
