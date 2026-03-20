from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import cast

from toolang.agent_refs import ResolvedAgentRef
from toolang.analyze import analyze_program
from toolang.ast import Program
from toolang.errors import ToolangError
from toolang.files.program import SyncedProgram
from toolang.files.sync_state import InputFingerprint, LockEntry, LockedAgentRefs, SyncState
from toolang.layout import (
    agent_source_path,
    agent_sync_path,
    agent_synced_caps_root,
    ensure_agent_home_layout,
    global_caps_dir,
    global_source_path,
    global_synced_caps_root,
    shared_caps_dir,
    shared_source_path,
    synced_caps_root,
)
from toolang.parser import parse_program
from toolang_caps.files import (
    inline_cap_meta_path,
    inline_cap_path,
    remove_stale_text_cap_materializations,
    remove_stale_skill_materializations,
    sync_file_cap_materialization,
    skill_cap_dir,
    skill_cap_meta_path,
    sync_local_skill_materialization,
    sync_skill_materialization,
    sync_text_cap_materialization,
)
from toolang_caps.github import fetch_github_artifact, resolve_github_cap_ref
from toolang_caps.models import (
    CAP_KINDS,
    CapParam,
    CapKind,
    InlineCap,
    InlineCapKind,
    InlineCapMeta,
    ResolvedCapRef,
    SkillMeta,
    TEXT_CAP_KINDS,
    refs_attr_name,
    section_name,
)

SOURCE_DECL_TO_CAP_KIND: dict[str, InlineCapKind] = {
    "service": "service",
    "prompt": "prompt",
    "psyche": "psyche",
}


def sync_agent(agent: ResolvedAgentRef) -> SyncedProgram:
    _existing_source_path(agent)

    source_paths = _home_source_paths(agent.agent_home)
    programs = _parse_home_programs(source_paths)
    if agent.agent_name not in programs:
        raise ToolangError(f"Agent source not found in agent home: {agent.agent_name}.too")

    for agent_name in programs:
        ensure_agent_home_layout(agent.agent_home, agent_name)

    global_ref_entries = _load_scope_refs(
        global_source_path(agent.toolang_root),
        scope_label="global agents.too",
    )
    shared_ref_entries = _load_scope_refs(
        shared_source_path(agent.agent_home),
        scope_label="shared agents.too",
    )
    agent_ref_entries = _resolve_home_refs(programs)

    global_local_entries = _load_local_entries_for_scope(
        root=agent.toolang_root,
        scope_root=agent.toolang_root,
        scope="global",
    )
    shared_local_entries = _load_local_entries_for_scope(
        root=agent.agent_home,
        scope_root=agent.agent_home / ".toolang",
        scope="shared",
    )

    global_effective_entries = _overlay_ref_entries(global_ref_entries, global_local_entries)
    shared_effective_entries = _overlay_ref_entries(shared_ref_entries, shared_local_entries)

    _sync_scope_caps(
        global_synced_caps_root(agent.toolang_root),
        global_effective_entries,
        scope_source_root=agent.toolang_root,
    )
    _sync_scope_caps(
        synced_caps_root(agent.agent_home),
        shared_effective_entries,
        scope_source_root=agent.agent_home / ".toolang",
    )
    _sync_agent_caps(agent.agent_home, programs, agent_ref_entries)

    inputs = _current_inputs(
        source_paths=source_paths,
        shared_source=shared_source_path(agent.agent_home),
        global_source=global_source_path(agent.toolang_root),
        shared_local_root=agent.agent_home / ".toolang",
        global_local_root=agent.toolang_root,
    )
    _sync_agent_states(
        agent=agent,
        programs=programs,
        agent_ref_entries=agent_ref_entries,
        shared_entries=shared_effective_entries,
        global_entries=global_effective_entries,
        inputs=inputs,
    )
    _remove_stale_sync_root_entries(synced_caps_root(agent.agent_home))
    _remove_legacy_lock_files(agent.agent_home)
    _remove_legacy_agent_programs(agent.agent_home)
    return SyncState.load(agent_sync_path(agent.agent_home, agent.agent_name)).program


def ensure_agent_synced(agent: ResolvedAgentRef) -> SyncedProgram:
    if _is_sync_fresh(agent):
        return SyncState.load(agent_sync_path(agent.agent_home, agent.agent_name)).program
    return sync_agent(agent)


def _existing_source_path(agent: ResolvedAgentRef) -> Path:
    source_path = agent.source_path
    if source_path.exists():
        return source_path
    if agent.agent_kind == "visiting":
        raise FileNotFoundError(
            f"Visiting agent is not materialized locally: {agent.agent_uri} -> {source_path}"
        )
    raise FileNotFoundError(f"Agent source not found: {source_path}")


def _home_source_paths(agent_home: Path) -> list[Path]:
    paths = sorted(
        path
        for path in agent_home.glob("*.too")
        if path.name != "agents.too" and path.is_file()
    )
    if not paths:
        raise ToolangError(f"No .too source files found in agent home: {agent_home}")
    return paths


def _parse_home_programs(source_paths: list[Path]) -> dict[str, Program]:
    programs: dict[str, Program] = {}
    for source_path in source_paths:
        program = parse_program(source_path.read_text(encoding="utf-8"))
        analyze_program(program)
        programs[source_path.stem] = program
    return programs


def _load_scope_refs(path: Path, *, scope_label: str) -> LockedAgentRefs:
    if not path.exists():
        return LockedAgentRefs()

    program = parse_program(path.read_text(encoding="utf-8"))
    if program.declarations or program.thunks:
        raise ToolangError(f"{scope_label} may only contain 'use ...' statements.")

    refs = LockedAgentRefs()
    for use in program.uses:
        if use.kind not in CAP_KINDS:
            raise ToolangError(f"Unsupported cap kind in {scope_label}: {use.kind}")
        kind = cast(CapKind, use.kind)
        resolved = resolve_github_cap_ref(kind, use.reference)
        entries = _entries_for_kind(refs, kind)
        entry = LockEntry(
            ref=resolved.ref,
            repo=resolved.repo,
            path=resolved.path,
            rev=resolved.rev,
        )
        existing = entries.get(resolved.name)
        if existing is not None and existing != entry:
            raise ToolangError(
                f"Conflicting {use.kind} refs resolve to the same name in {scope_label}: {resolved.name}"
            )
        entries[resolved.name] = entry
    return _sorted_entries(refs)


def _current_inputs(
    *,
    source_paths: list[Path],
    shared_source: Path,
    global_source: Path,
    shared_local_root: Path,
    global_local_root: Path,
) -> dict[str, InputFingerprint]:
    inputs = {
        f"agent/{path.name}": _fingerprint(path)
        for path in source_paths
    }
    if shared_source.exists():
        inputs["shared/agents.too"] = _fingerprint(shared_source)
    if global_source.exists():
        inputs["global/agents.too"] = _fingerprint(global_source)
    for kind in CAP_KINDS:
        inputs.update(_tree_fingerprints("shared", shared_local_root / section_name(kind)))
        inputs.update(_tree_fingerprints("global", global_local_root / section_name(kind)))
    return inputs


def _tree_fingerprints(scope: str, root: Path) -> dict[str, InputFingerprint]:
    if not root.exists():
        return {}
    return {
        f"{scope}/{path.relative_to(root.parent)}": _fingerprint(path)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _is_sync_fresh(agent: ResolvedAgentRef) -> bool:
    source_path = agent.source_path
    state_path = agent_sync_path(agent.agent_home, agent.agent_name)
    shared_sync_root = synced_caps_root(agent.agent_home)
    if not source_path.exists() or not state_path.exists() or not shared_sync_root.exists():
        return False

    current_inputs = _current_inputs(
        source_paths=_home_source_paths(agent.agent_home),
        shared_source=shared_source_path(agent.agent_home),
        global_source=global_source_path(agent.toolang_root),
        shared_local_root=agent.agent_home / ".toolang",
        global_local_root=agent.toolang_root,
    )
    state = SyncState.load(state_path)
    if state.inputs != current_inputs:
        return False

    try:
        states = _load_agent_states(agent.agent_home, _home_agent_names(agent.agent_home))
        programs = {agent_name: item.to_program() for agent_name, item in states.items()}
        if not _has_expected_scope_caps(
            global_synced_caps_root(agent.toolang_root),
            _shared_scope_entries(states, "global_refs"),
        ):
            return False
        if not _has_expected_scope_caps(
            shared_sync_root,
            _shared_scope_entries(states, "shared_refs"),
        ):
            return False
        return _has_expected_agent_scope_caps(agent.agent_home, programs, states)
    except (FileNotFoundError, ToolangError):
        return False


def _home_agent_names(agent_home: Path) -> list[str]:
    return [path.stem for path in _home_source_paths(agent_home)]


def _load_agent_states(agent_home: Path, agent_names: list[str]) -> dict[str, SyncState]:
    states: dict[str, SyncState] = {}
    for agent_name in sorted(agent_names):
        state_path = agent_sync_path(agent_home, agent_name)
        if not state_path.exists():
            raise FileNotFoundError(f"Synced state is missing: {state_path}")
        states[agent_name] = SyncState.load(state_path)
    return states


def _agent_inline_caps(program: Program) -> list[InlineCap]:
    caps: list[InlineCap] = []
    for declaration in program.declarations:
        kind = SOURCE_DECL_TO_CAP_KIND.get(declaration.kind)
        if kind is None:
            continue
        caps.append(
            InlineCap(
                kind=kind,
                name=declaration.name,
                language=declaration.language,
                raw_text=declaration.body,
                params=[
                    CapParam(name=param.name, optional=param.optional)
                    for param in declaration.params
                ],
            )
        )
    return caps


def _resolve_home_refs(programs: dict[str, Program]) -> dict[str, LockedAgentRefs]:
    refs_by_agent: dict[str, LockedAgentRefs] = {}
    for agent_name, program in sorted(programs.items()):
        refs = LockedAgentRefs()
        for use in program.uses:
            if use.kind not in CAP_KINDS:
                raise ToolangError(
                    f"Unsupported capability ref kind in {agent_name}.too: {use.kind}."
                )
            kind = cast(CapKind, use.kind)
            resolved = resolve_github_cap_ref(kind, use.reference)
            entries = _entries_for_kind(refs, kind)
            entry = LockEntry(
                ref=resolved.ref,
                repo=resolved.repo,
                path=resolved.path,
                rev=resolved.rev,
            )
            existing = entries.get(resolved.name)
            if existing is not None and existing != entry:
                raise ToolangError(
                    f"Conflicting {use.kind} refs resolve to the same name in {agent_name}.too: {resolved.name}"
                )
            entries[resolved.name] = entry
        refs_by_agent[agent_name] = _sorted_entries(refs)
    return refs_by_agent


def _load_local_entries_for_scope(
    *,
    root: Path,
    scope_root: Path,
    scope: str,
) -> LockedAgentRefs:
    refs = LockedAgentRefs()
    for kind in CAP_KINDS:
        kind_dir = (shared_caps_dir(root, kind) if scope == "shared" else global_caps_dir(root, kind))
        entries = _entries_for_kind(refs, kind)
        if not kind_dir.exists():
            continue
        if kind == "skill":
            for item in sorted(kind_dir.iterdir()):
                if not item.is_dir() or not (item / "SKILL.md").exists():
                    continue
                entries[item.name] = LockEntry(path=str(item.relative_to(scope_root)))
            continue
        for item in sorted(kind_dir.glob("*.md")):
            entries[item.stem] = LockEntry(path=str(item.relative_to(scope_root)))
    return _sorted_entries(refs)


def _overlay_ref_entries(refs: LockedAgentRefs, locals_by_name: LockedAgentRefs) -> LockedAgentRefs:
    effective = LockedAgentRefs()
    for kind in CAP_KINDS:
        merged = dict(_entries_for_kind(refs, kind))
        merged.update(_entries_for_kind(locals_by_name, kind))
        setattr(
            effective,
            refs_attr_name(kind),
            {name: merged[name] for name in sorted(merged)},
        )
    return effective


def _sync_scope_caps(
    sync_root: Path,
    entries: LockedAgentRefs,
    *,
    scope_source_root: Path,
) -> None:
    _sync_scope_skills(
        sync_root,
        entries.skills,
        scope_source_root=scope_source_root,
    )
    for kind in TEXT_CAP_KINDS:
        _sync_scope_text_caps(
            sync_root,
            kind,
            _entries_for_kind(entries, kind),
            scope_source_root=scope_source_root,
        )


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
            files = _skill_files(source_dir)
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
            source_dir, files = fetch_github_artifact(resolved)
            try:
                sync_skill_materialization(sync_root, name, source_dir, resolved, files)
            finally:
                shutil.rmtree(source_dir.parent.parent, ignore_errors=True)
        expected_names.add(name)
    remove_stale_skill_materializations(sync_root, expected_names)


def _sync_scope_text_caps(
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
            source_path, _ = fetch_github_artifact(resolved)
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


def _sync_agent_caps(
    agent_home: Path,
    programs: dict[str, Program],
    refs_by_agent: dict[str, LockedAgentRefs],
) -> None:
    for agent_name, program in sorted(programs.items()):
        sync_root = agent_synced_caps_root(agent_home, agent_name)
        _sync_scope_skills(
            sync_root,
            refs_by_agent[agent_name].skills,
            scope_source_root=agent_home,
        )
        inline_caps = _agent_inline_caps(program)
        for kind in TEXT_CAP_KINDS:
            _sync_agent_text_caps(
                sync_root,
                kind,
                _entries_for_kind(refs_by_agent[agent_name], kind),
                [cap for cap in inline_caps if cap.kind == kind],
                scope_source_root=agent_home,
            )


def _entries_for_kind(refs: LockedAgentRefs, kind: CapKind) -> dict[str, LockEntry]:
    return getattr(refs, refs_attr_name(kind))


def _sorted_entries(refs: LockedAgentRefs) -> LockedAgentRefs:
    sorted_refs = LockedAgentRefs()
    for kind in CAP_KINDS:
        entries = _entries_for_kind(refs, kind)
        setattr(
            sorted_refs,
            refs_attr_name(kind),
            {name: entries[name] for name in sorted(entries)},
        )
    return sorted_refs


def _sync_agent_text_caps(
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
            source_path, _ = fetch_github_artifact(resolved)
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


def _skill_files(source_dir: Path) -> list[str]:
    return sorted(
        str(path.relative_to(source_dir))
        for path in source_dir.rglob("*")
        if path.is_file()
    )


def _sync_agent_states(
    *,
    agent: ResolvedAgentRef,
    programs: dict[str, Program],
    agent_ref_entries: dict[str, LockedAgentRefs],
    shared_entries: LockedAgentRefs,
    global_entries: LockedAgentRefs,
    inputs: dict[str, InputFingerprint],
) -> None:
    expected = {
        agent_sync_path(agent.agent_home, agent_name)
        for agent_name in programs
    }
    for agent_name, program in sorted(programs.items()):
        SyncState(
            synced_at=datetime.now(timezone.utc),
            source_file=agent_source_path(agent.agent_home, agent_name).name,
            inputs=inputs,
            program=SyncedProgram.from_program(program),
            agent_refs=agent_ref_entries[agent_name],
            shared_refs=shared_entries,
            global_refs=global_entries,
        ).save(agent_sync_path(agent.agent_home, agent_name))

    for path in synced_caps_root(agent.agent_home).glob("*.state.json"):
        if path not in expected:
            path.unlink()


def _shared_scope_entries(
    states: dict[str, SyncState],
    attr: str,
) -> LockedAgentRefs:
    expected: LockedAgentRefs | None = None
    for agent_name in sorted(states):
        current = getattr(states[agent_name], attr)
        if expected is None:
            expected = current
            continue
        if expected != current:
            raise ToolangError(
                f"Shared scoped cap state drift detected for {attr}: {agent_name}"
            )
    return expected or LockedAgentRefs()


def _has_expected_scope_caps(
    sync_root: Path,
    entries: LockedAgentRefs,
) -> bool:
    if not _has_expected_scope_skills(sync_root, entries.skills):
        return False
    for kind in TEXT_CAP_KINDS:
        if not _has_expected_scope_text_caps(sync_root, kind, _entries_for_kind(entries, kind)):
            return False
    return True


def _has_expected_scope_skills(
    sync_root: Path,
    entries: dict[str, LockEntry],
) -> bool:
    kind_dir = sync_root / section_name("skill")
    if not kind_dir.exists():
        return False

    expected_top_level = {
        skill_cap_dir(sync_root, name) for name in entries
    } | {
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


def _has_expected_scope_text_caps(
    sync_root: Path,
    kind: InlineCapKind,
    entries: dict[str, LockEntry],
) -> bool:
    kind_dir = sync_root / section_name(kind)
    if not kind_dir.exists():
        return False
    expected_paths = {
        inline_cap_path(sync_root, kind, name, "md") for name in entries
    } | {
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


def _has_expected_agent_scope_caps(
    agent_home: Path,
    programs: dict[str, Program],
    states: dict[str, SyncState],
) -> bool:
    for agent_name, state in states.items():
        sync_root = agent_synced_caps_root(agent_home, agent_name)
        if not _has_expected_scope_skills(sync_root, state.agent_refs.skills):
            return False
        inline_caps = _agent_inline_caps(programs[agent_name])
        for kind in TEXT_CAP_KINDS:
            if not _has_expected_agent_text_caps(
                sync_root,
                kind,
                _entries_for_kind(state.agent_refs, kind),
                [cap for cap in inline_caps if cap.kind == kind],
            ):
                return False
    return True


def _has_expected_agent_text_caps(
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
    expected_paths = {
        inline_cap_meta_path(sync_root, kind, name) for name in expected_names
    } | {
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


def _fingerprint(path: Path) -> InputFingerprint:
    stat = path.stat()
    return InputFingerprint(mtime_ns=stat.st_mtime_ns, size=stat.st_size)


def _remove_stale_sync_root_entries(sync_root: Path) -> None:
    expected_dirs = {section_name(kind) for kind in CAP_KINDS}
    for path in sync_root.iterdir():
        if path.is_dir():
            if path.name not in expected_dirs:
                shutil.rmtree(path)
            continue
        if path.suffixes != [".state", ".json"]:
            path.unlink()


def _remove_legacy_lock_files(agent_home: Path) -> None:
    for filename in ("agent.lock", "toolang.lock"):
        path = agent_home / filename
        if path.exists():
            path.unlink()


def _remove_legacy_agent_programs(agent_home: Path) -> None:
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
