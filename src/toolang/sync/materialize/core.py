from __future__ import annotations

from pathlib import Path
from typing import Literal

from toolang.layout import agent_synced_caps_root
from toolang.syntax import Program
from toolang.concepts.caps import CapKind
from toolang.concepts.persisted.sync_state import LockedAgentRefs, SyncState

from ..refs import agent_declared_caps, entries_for_kind
from .skills import has_expected_scope_skills, sync_scope_skills
from .text import (
    has_expected_agent_declared_caps,
    has_expected_scope_declared_caps,
    sync_agent_declared_caps,
    sync_scope_declared_caps,
)

DECLARED_CAP_KINDS: tuple[Literal["service", "prompt", "psyche"], ...] = (
    "service",
    "prompt",
    "psyche",
)


def sync_scope_caps(
    sync_root: Path,
    entries: LockedAgentRefs,
    *,
    scope_source_root: Path,
) -> None:
    sync_scope_skills(
        sync_root,
        entries.skills,
        scope_source_root=scope_source_root,
    )
    for kind in DECLARED_CAP_KINDS:
        sync_scope_declared_caps(
            sync_root,
            kind,
            entries_for_kind(entries, kind),
            scope_source_root=scope_source_root,
        )


def sync_agent_caps(
    agent_home: Path,
    programs: dict[str, Program],
    refs_by_agent: dict[str, LockedAgentRefs],
) -> None:
    for agent_name, program in sorted(programs.items()):
        sync_root = agent_synced_caps_root(agent_home, agent_name)
        sync_scope_skills(
            sync_root,
            refs_by_agent[agent_name].skills,
            scope_source_root=agent_home,
        )
        declared_caps = agent_declared_caps(program)
        for kind in DECLARED_CAP_KINDS:
            sync_agent_declared_caps(
                sync_root,
                kind,
                entries_for_kind(refs_by_agent[agent_name], kind),
                _declared_caps_for_kind(declared_caps, kind),
                scope_source_root=agent_home,
            )


def has_expected_scope_caps(
    sync_root: Path,
    entries: LockedAgentRefs,
) -> bool:
    if not has_expected_scope_skills(sync_root, entries.skills):
        return False
    for kind in DECLARED_CAP_KINDS:
        if not has_expected_scope_declared_caps(sync_root, kind, entries_for_kind(entries, kind)):
            return False
    return True


def has_expected_agent_scope_caps(
    agent_home: Path,
    programs: dict[str, Program],
    states: dict[str, SyncState],
) -> bool:
    for agent_name, state in states.items():
        sync_root = agent_synced_caps_root(agent_home, agent_name)
        if not has_expected_scope_skills(sync_root, state.agent_refs.skills):
            return False
        declared_caps = agent_declared_caps(programs[agent_name])
        for kind in DECLARED_CAP_KINDS:
            if not has_expected_agent_declared_caps(
                sync_root,
                kind,
                entries_for_kind(state.agent_refs, kind),
                _declared_caps_for_kind(declared_caps, kind),
            ):
                return False
    return True


def _declared_caps_for_kind(declared_caps, kind: CapKind):
    return [cap for cap in declared_caps if cap.kind == kind]
