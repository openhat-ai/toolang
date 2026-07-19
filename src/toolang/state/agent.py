"""One immutable agent-state snapshot composed from prepared generations."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import cast

from ..common.immutable import freeze_mapping
from ..lang.ast import Program, to_data
from .generation import HomePrepared, RootPrepared, agent_state_version
from .prepared import PreparedEntry


@dataclass(frozen=True, slots=True)
class AgentState:
    """One exact root/home prepared pair used by a top-level run."""

    version: bytes
    root: RootPrepared
    home: HomePrepared
    config: Mapping[str, object]
    caps: tuple[PreparedEntry, ...]
    loaded_at: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "config", freeze_mapping(self.config))

    @property
    def fingerprint(self) -> str:
        """Return the hexadecimal state version for existing runtime records."""

        return self.version.hex()

    @property
    def root_version(self) -> bytes:
        return self.root.version

    @property
    def home_version(self) -> bytes:
        return self.home.version

    @property
    def toolang_version(self) -> str:
        return self.root.toolang_version

    @property
    def program(self) -> Program:
        return self.home.program

    @property
    def updated_at(self) -> str:
        return self.loaded_at

    def to_snapshot(self) -> dict[str, object]:
        return {
            "fingerprint": self.fingerprint,
            "version": self.version.hex(),
            "root_version": self.root.version.hex(),
            "home_version": self.home.version.hex(),
            "toolang_version": self.toolang_version,
            "updated_at": self.updated_at,
            "loaded_at": self.loaded_at,
            "program": to_data(self.program),
            "caps": [entry.path for entry in self.caps],
        }


def compose_agent_state(root: RootPrepared, home: HomePrepared) -> AgentState:
    """Compose one immutable runtime state from prepared generations."""

    return AgentState(
        version=agent_state_version(root.version, home.version),
        root=root,
        home=home,
        config=_merge(root.config, home.config),
        caps=_effective_prepared_caps(root.caps, home.caps),
        loaded_at=datetime.now(timezone.utc).isoformat(),
    )


def _effective_prepared_caps(
    root: tuple[PreparedEntry, ...],
    home: tuple[PreparedEntry, ...],
) -> tuple[PreparedEntry, ...]:
    effective: dict[tuple[str, str], PreparedEntry] = {}
    for entry in (*root, *home):
        effective[(entry.kind, entry.name)] = entry
    return tuple(
        sorted(
            effective.values(),
            key=lambda entry: (entry.kind, entry.name, entry.ref),
        )
    )


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
