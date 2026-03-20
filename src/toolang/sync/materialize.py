from __future__ import annotations

import shutil
from pathlib import Path

from toolang.files.sync_state import LockEntry, LockedAgentRefs, SyncState
from toolang.layout import agent_synced_caps_root
from toolang.syntax import Program
from toolang_caps.files import (
    inline_cap_meta_path,
    inline_cap_path,
    remove_stale_text_cap_materializations,
    remove_stale_skill_materializations,
    skill_cap_dir,
    skill_cap_meta_path,
    sync_file_cap_materialization,
    sync_local_skill_materialization,
    sync_skill_materialization,
    sync_text_cap_materialization,
)
from toolang_caps.models import (
    InlineCap,
    InlineCapKind,
    InlineCapMeta,
    ResolvedCapRef,
    SkillMeta,
    TEXT_CAP_KINDS,
    section_name,
)

from .refs import agent_inline_caps, entries_for_kind
from . import remote


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
    for kind in TEXT_CAP_KINDS:
        sync_scope_text_caps(
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
        inline_caps = agent_inline_caps(program)
        for kind in TEXT_CAP_KINDS:
            sync_agent_text_caps(
                sync_root,
                kind,
                entries_for_kind(refs_by_agent[agent_name], kind),
                [cap for cap in inline_caps if cap.kind == kind],
                scope_source_root=agent_home,
            )


def has_expected_scope_caps(
    sync_root: Path,
    entries: LockedAgentRefs,
) -> bool:
    if not has_expected_scope_skills(sync_root, entries.skills):
        return False
    for kind in TEXT_CAP_KINDS:
        if not has_expected_scope_text_caps(sync_root, kind, entries_for_kind(entries, kind)):
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
        inline_caps = agent_inline_caps(programs[agent_name])
        for kind in TEXT_CAP_KINDS:
            if not has_expected_agent_text_caps(
                sync_root,
                kind,
                entries_for_kind(state.agent_refs, kind),
                [cap for cap in inline_caps if cap.kind == kind],
            ):
                return False
    return True


def remove_stale_sync_root_entries(sync_root: Path) -> None:
    expected_dirs = {section_name(kind) for kind in ("skill", *TEXT_CAP_KINDS)}
    for path in sync_root.iterdir():
        if path.is_dir():
            if path.name not in expected_dirs:
                shutil.rmtree(path)
            continue
        if path.suffixes != [".state", ".json"]:
            path.unlink()


def remove_legacy_lock_files(agent_home: Path) -> None:
    for filename in ("agent.lock", "toolang.lock"):
        path = agent_home / filename
        if path.exists():
            path.unlink()


def remove_legacy_agent_programs(agent_home: Path) -> None:
    agent_root = agent_home / ".toolang" / "agents"
    if not agent_root.exists():
        return
    for room in agent_root.iterdir():
        if not room.is_dir():
            continue
        for filename in ("program.json", "sync.json"):
            path = room / filename
            if path.exists():
                path.unlink()


def sync_scope_skills(
    sync_root: Path,
    entries: dict[str, LockEntry],
    *,
    scope_source_root: Path,
) -> None:
    expected_names: set[str] = set()
    for name, entry in entries.items():
        if entry.ref is None:
            source_dir = scope_source_root / entry.path
            files = skill_files(source_dir)
            sync_local_skill_materialization(
                sync_root,
                name,
                source_dir,
                files=files,
                source_path=entry.path,
            )
        else:
            resolved = ResolvedCapRef(
                kind="skill",
                name=name,
                ref=entry.ref,
                repo=entry.repo or "",
                path=entry.path,
                rev=entry.rev or "",
            )
            source_dir, files = remote.fetch_github_artifact(resolved)
            try:
                sync_skill_materialization(sync_root, name, source_dir, resolved, files)
            finally:
                shutil.rmtree(source_dir.parent.parent, ignore_errors=True)
        expected_names.add(name)
    remove_stale_skill_materializations(sync_root, expected_names)


def sync_scope_text_caps(
    sync_root: Path,
    kind: InlineCapKind,
    entries: dict[str, LockEntry],
    *,
    scope_source_root: Path,
) -> None:
    expected_names: set[str] = set()
    for name, entry in entries.items():
        if entry.ref is None:
            sync_file_cap_materialization(
                sync_root,
                kind,
                name,
                scope_source_root / entry.path,
                source_path=entry.path,
            )
        else:
            resolved = ResolvedCapRef(
                kind=kind,
                name=name,
                ref=entry.ref,
                repo=entry.repo or "",
                path=entry.path,
                rev=entry.rev or "",
            )
            source_path, _ = remote.fetch_github_artifact(resolved)
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
        expected_names.add(name)
    remove_stale_text_cap_materializations(sync_root, kind, expected_names)


def sync_agent_text_caps(
    sync_root: Path,
    kind: InlineCapKind,
    entries: dict[str, LockEntry],
    inline_caps: list[InlineCap],
    *,
    scope_source_root: Path,
) -> None:
    expected_names: set[str] = set()
    for name, entry in entries.items():
        if entry.ref is None:
            sync_file_cap_materialization(
                sync_root,
                kind,
                name,
                scope_source_root / entry.path,
                source_path=entry.path,
            )
        else:
            resolved = ResolvedCapRef(
                kind=kind,
                name=name,
                ref=entry.ref,
                repo=entry.repo or "",
                path=entry.path,
                rev=entry.rev or "",
            )
            source_path, _ = remote.fetch_github_artifact(resolved)
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
        expected_names.add(name)

    for cap in inline_caps:
        sync_text_cap_materialization(
            sync_root,
            cap.kind,
            cap.name,
            cap.raw_text,
            language=cap.language,
            params=cap.params,
        )
        expected_names.add(cap.name)

    remove_stale_text_cap_materializations(sync_root, kind, expected_names)


def skill_files(source_dir: Path) -> list[str]:
    return sorted(
        str(path.relative_to(source_dir))
        for path in source_dir.rglob("*")
        if path.is_file()
    )


def has_expected_scope_skills(
    sync_root: Path,
    entries: dict[str, LockEntry],
) -> bool:
    kind_dir = sync_root / section_name("skill")
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
        meta = SkillMeta.model_validate_json(meta_path.read_text(encoding="utf-8"))
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
        if actual_files != meta.files:
            return False
    return True


def has_expected_scope_text_caps(
    sync_root: Path,
    kind: InlineCapKind,
    entries: dict[str, LockEntry],
) -> bool:
    kind_dir = sync_root / section_name(kind)
    if not kind_dir.exists():
        return False
    expected_paths = {inline_cap_path(sync_root, kind, name, "md") for name in entries} | {
        inline_cap_meta_path(sync_root, kind, name) for name in entries
    }
    actual_paths = set(kind_dir.iterdir())
    if actual_paths != expected_paths:
        return False
    for name, entry in entries.items():
        meta_path = inline_cap_meta_path(sync_root, kind, name)
        if not meta_path.exists():
            return False
        meta = InlineCapMeta.model_validate_json(meta_path.read_text(encoding="utf-8"))
        if (
            meta.ref != entry.ref
            or meta.repo != entry.repo
            or meta.source_path != entry.path
            or meta.rev != entry.rev
        ):
            return False
        raw_path = sync_root / meta.path
        if not raw_path.exists() or raw_path.read_text(encoding="utf-8") != meta.raw_text:
            return False
    return True


def has_expected_agent_text_caps(
    sync_root: Path,
    kind: InlineCapKind,
    entries: dict[str, LockEntry],
    inline_caps: list[InlineCap],
) -> bool:
    kind_dir = sync_root / section_name(kind)
    if not kind_dir.exists():
        return False

    inline_by_name = {cap.name: cap for cap in inline_caps}
    expected_names = set(entries) | set(inline_by_name)
    expected_paths = {inline_cap_meta_path(sync_root, kind, name) for name in expected_names} | {
        inline_cap_path(
            sync_root,
            kind,
            name,
            inline_by_name[name].language if name in inline_by_name else "md",
        )
        for name in expected_names
    }
    if set(kind_dir.iterdir()) != expected_paths:
        return False

    for name in expected_names:
        meta = InlineCapMeta.model_validate_json(
            inline_cap_meta_path(sync_root, kind, name).read_text(encoding="utf-8")
        )
        raw_path = sync_root / meta.path
        if not raw_path.exists() or raw_path.read_text(encoding="utf-8") != meta.raw_text:
            return False
        inline_cap = inline_by_name.get(name)
        if inline_cap is not None:
            if (
                meta.language != inline_cap.language
                or meta.raw_text != inline_cap.raw_text
                or meta.params != inline_cap.params
            ):
                return False
            continue
        entry = entries[name]
        if (
            meta.ref != entry.ref
            or meta.repo != entry.repo
            or meta.source_path != entry.path
            or meta.rev != entry.rev
        ):
            return False
    return True
