"""Capability sync materialization and validation helpers."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Literal

from toolang.layout import agent_synced_caps_root, cap_section_dir_name
from toolang.program import Program
from toolang.concepts.caps import CapContent, CapKind, CapRef, CapSidecar
from toolang.concepts.persisted.sync_state import LockEntry, LockedAgentRefs, SyncState

from .files import (
    declared_cap_meta_path,
    declared_cap_path,
    remove_stale_declared_cap_materializations,
    remove_stale_skill_materializations,
    skill_cap_dir,
    skill_cap_meta_path,
    sync_declared_cap_materialization,
    sync_file_cap_materialization,
    sync_local_skill_materialization,
    sync_skill_materialization,
)
from . import github
from .refs import agent_declared_caps, entries_for_kind

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
    """Materialize one shared scope of synced capabilities."""

    _sync_scope_skills(
        sync_root,
        entries.skills,
        scope_source_root=scope_source_root,
    )
    for kind in DECLARED_CAP_KINDS:
        _sync_scope_declared_caps(
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
    """Materialize per-agent synced caps for one agent home."""

    for agent_name, program in sorted(programs.items()):
        sync_root = agent_synced_caps_root(agent_home, agent_name)
        _sync_scope_skills(
            sync_root,
            refs_by_agent[agent_name].skills,
            scope_source_root=agent_home,
        )
        declared_caps = agent_declared_caps(program)
        for kind in DECLARED_CAP_KINDS:
            _sync_agent_declared_caps(
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
    """Return whether one shared scope already matches expected synced caps."""

    if not _has_expected_scope_skills(sync_root, entries.skills):
        return False
    for kind in DECLARED_CAP_KINDS:
        if not _has_expected_scope_declared_caps(sync_root, kind, entries_for_kind(entries, kind)):
            return False
    return True


def has_expected_agent_scope_caps(
    agent_home: Path,
    programs: dict[str, Program],
    states: dict[str, SyncState],
) -> bool:
    """Return whether all per-agent synced caps already match expected state."""

    for agent_name, state in states.items():
        sync_root = agent_synced_caps_root(agent_home, agent_name)
        if not _has_expected_scope_skills(sync_root, state.agent_refs.skills):
            return False
        declared_caps = agent_declared_caps(programs[agent_name])
        for kind in DECLARED_CAP_KINDS:
            if not _has_expected_agent_declared_caps(
                sync_root,
                kind,
                entries_for_kind(state.agent_refs, kind),
                _declared_caps_for_kind(declared_caps, kind),
            ):
                return False
    return True


def _declared_caps_for_kind(declared_caps: list[CapContent], kind: CapKind) -> list[CapContent]:
    return [cap for cap in declared_caps if cap.kind == kind]


def _sync_scope_skills(
    sync_root: Path,
    entries: dict[str, LockEntry],
    *,
    scope_source_root: Path,
) -> None:
    expected_names: set[str] = set()
    for name, entry in entries.items():
        if entry.ref is None:
            source_dir = scope_source_root / entry.path
            sync_local_skill_materialization(
                sync_root,
                name,
                source_dir,
                files=_skill_files(source_dir),
                source_path=entry.path,
            )
        else:
            resolved = _resolved_skill_ref(name, entry)
            source_dir, files = github.fetch_github_artifact(resolved)
            try:
                sync_skill_materialization(sync_root, name, source_dir, resolved, files)
            finally:
                shutil.rmtree(source_dir.parent.parent, ignore_errors=True)
        expected_names.add(name)
    remove_stale_skill_materializations(sync_root, expected_names)


def _has_expected_scope_skills(
    sync_root: Path,
    entries: dict[str, LockEntry],
) -> bool:
    kind_dir = sync_root / cap_section_dir_name("skill")
    if not kind_dir.exists():
        return False

    expected_top_level = {skill_cap_dir(sync_root, name) for name in entries} | {
        skill_cap_meta_path(sync_root, name) for name in entries
    }
    if set(kind_dir.iterdir()) != expected_top_level:
        return False

    for name, entry in entries.items():
        skill_dir = skill_cap_dir(sync_root, name)
        meta_path = skill_cap_meta_path(sync_root, name)
        if not skill_dir.exists() or not meta_path.exists():
            return False
        meta = CapSidecar.model_validate_json(meta_path.read_text(encoding="utf-8"))
        if (
            meta.ref != entry.ref
            or meta.repo != entry.repo
            or meta.source_path != entry.path
            or meta.rev != entry.rev
        ):
            return False
        actual_files = sorted(
            str(path.relative_to(skill_dir))
            for path in skill_dir.rglob("*")
            if path.is_file()
        )
        if actual_files != meta.asset_files:
            return False
    return True


def _skill_files(source_dir: Path) -> list[str]:
    return sorted(
        str(path.relative_to(source_dir))
        for path in source_dir.rglob("*")
        if path.is_file()
    )


def _resolved_skill_ref(name: str, entry: LockEntry) -> CapRef:
    return CapRef(
        kind="skill",
        name=name,
        ref=entry.ref or "",
        repo=entry.repo or "",
        path=entry.path,
        rev=entry.rev or "",
    )


def _sync_scope_declared_caps(
    sync_root: Path,
    kind: CapKind,
    entries: dict[str, LockEntry],
    *,
    scope_source_root: Path,
) -> None:
    expected_names = _sync_locked_declared_caps(
        sync_root,
        kind,
        entries,
        scope_source_root=scope_source_root,
    )
    remove_stale_declared_cap_materializations(sync_root, kind, expected_names)


def _sync_agent_declared_caps(
    sync_root: Path,
    kind: CapKind,
    entries: dict[str, LockEntry],
    declared_caps: list[CapContent],
    *,
    scope_source_root: Path,
) -> None:
    expected_names = _sync_locked_declared_caps(
        sync_root,
        kind,
        entries,
        scope_source_root=scope_source_root,
    )
    for cap in declared_caps:
        sync_declared_cap_materialization(
            sync_root,
            cap.kind,
            cap.name,
            cap.raw_text,
            language=cap.language,
            params=cap.params,
        )
        expected_names.add(cap.name)
    remove_stale_declared_cap_materializations(sync_root, kind, expected_names)


def _has_expected_scope_declared_caps(
    sync_root: Path,
    kind: CapKind,
    entries: dict[str, LockEntry],
) -> bool:
    kind_dir = sync_root / cap_section_dir_name(kind)
    if not kind_dir.exists():
        return False
    expected_paths = {declared_cap_path(sync_root, kind, name, "md") for name in entries} | {
        declared_cap_meta_path(sync_root, kind, name) for name in entries
    }
    if set(kind_dir.iterdir()) != expected_paths:
        return False
    for name, entry in entries.items():
        meta_path = declared_cap_meta_path(sync_root, kind, name)
        if not meta_path.exists():
            return False
        meta = CapSidecar.model_validate_json(meta_path.read_text(encoding="utf-8"))
        if not _declared_meta_matches_entry(meta, entry):
            return False
        raw_path = sync_root / meta.path
        if not raw_path.exists() or raw_path.read_text(encoding="utf-8") != meta.raw_text:
            return False
    return True


def _has_expected_agent_declared_caps(
    sync_root: Path,
    kind: CapKind,
    entries: dict[str, LockEntry],
    declared_caps: list[CapContent],
) -> bool:
    kind_dir = sync_root / cap_section_dir_name(kind)
    if not kind_dir.exists():
        return False

    declared_by_name = {cap.name: cap for cap in declared_caps}
    expected_names = set(entries) | set(declared_by_name)
    expected_paths = {declared_cap_meta_path(sync_root, kind, name) for name in expected_names} | {
        declared_cap_path(
            sync_root,
            kind,
            name,
            declared_by_name[name].language if name in declared_by_name else "md",
        )
        for name in expected_names
    }
    if set(kind_dir.iterdir()) != expected_paths:
        return False

    for name in expected_names:
        meta = CapSidecar.model_validate_json(
            declared_cap_meta_path(sync_root, kind, name).read_text(encoding="utf-8")
        )
        raw_path = sync_root / meta.path
        if not raw_path.exists() or raw_path.read_text(encoding="utf-8") != meta.raw_text:
            return False
        declared_cap = declared_by_name.get(name)
        if declared_cap is not None:
            if (
                meta.language != declared_cap.language
                or meta.raw_text != declared_cap.raw_text
                or meta.params != declared_cap.params
            ):
                return False
            continue
        if not _declared_meta_matches_entry(meta, entries[name]):
            return False
    return True


def _sync_locked_declared_caps(
    sync_root: Path,
    kind: CapKind,
    entries: dict[str, LockEntry],
    *,
    scope_source_root: Path,
) -> set[str]:
    expected_names: set[str] = set()
    for name, entry in entries.items():
        _sync_locked_declared_cap(
            sync_root,
            kind,
            name,
            entry,
            scope_source_root=scope_source_root,
        )
        expected_names.add(name)
    return expected_names


def _sync_locked_declared_cap(
    sync_root: Path,
    kind: CapKind,
    name: str,
    entry: LockEntry,
    *,
    scope_source_root: Path,
) -> None:
    if entry.ref is None:
        sync_file_cap_materialization(
            sync_root,
            kind,
            name,
            scope_source_root / entry.path,
            source_path=entry.path,
        )
        return
    resolved = _resolved_declared_ref(kind, name, entry)
    source_path, _ = github.fetch_github_artifact(resolved)
    try:
        sync_file_cap_materialization(
            sync_root,
            kind,
            name,
            source_path,
            source_path=resolved.path,
            ref=resolved.ref,
            repo=resolved.repo,
            rev=resolved.rev,
        )
    finally:
        shutil.rmtree(source_path.parent.parent, ignore_errors=True)


def _resolved_declared_ref(
    kind: CapKind,
    name: str,
    entry: LockEntry,
) -> CapRef:
    return CapRef(
        kind=kind,
        name=name,
        ref=entry.ref or "",
        repo=entry.repo or "",
        path=entry.path,
        rev=entry.rev or "",
    )


def _declared_meta_matches_entry(meta: CapSidecar, entry: LockEntry) -> bool:
    return (
        meta.ref == entry.ref
        and meta.repo == entry.repo
        and meta.source_path == entry.path
        and meta.rev == entry.rev
    )
