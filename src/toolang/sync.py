from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path

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
    remove_stale_skill_materializations,
    skill_cap_dir,
    skill_cap_meta_path,
    sync_inline_caps,
    sync_local_skill_materialization,
    sync_skill_materialization,
)
from toolang_caps.github import fetch_github_tree, resolve_github_skill_ref
from toolang_caps.models import (
    CAP_KINDS,
    CapParam,
    InlineCap,
    InlineCapKind,
    ResolvedCapRef,
    SkillMeta,
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

    global_skill_refs = _load_scope_skill_refs(
        global_source_path(agent.toolang_root),
        scope_label="global agents.too",
    )
    shared_skill_refs = _load_scope_skill_refs(
        shared_source_path(agent.agent_home),
        scope_label="shared agents.too",
    )
    agent_skill_refs = _resolve_home_skill_refs(programs)

    global_local_skills = _load_local_skill_entries(
        global_caps_dir(agent.toolang_root, "skill"),
        scope_root=agent.toolang_root,
    )
    shared_local_skills = _load_local_skill_entries(
        shared_caps_dir(agent.agent_home, "skill"),
        scope_root=agent.agent_home / ".toolang",
    )

    shared_sync_root = synced_caps_root(agent.agent_home)
    sync_inline_caps(shared_sync_root, _home_inline_caps(programs))

    global_skill_entries = _overlay_skill_entries(global_skill_refs, global_local_skills)
    shared_skill_entries = _overlay_skill_entries(shared_skill_refs, shared_local_skills)

    _sync_scope_skills(
        global_synced_caps_root(agent.toolang_root),
        global_skill_entries,
        scope_source_root=agent.toolang_root,
    )
    _sync_scope_skills(
        shared_sync_root,
        shared_skill_entries,
        scope_source_root=agent.agent_home / ".toolang",
    )
    _sync_agent_skill_scopes(agent.agent_home, agent_skill_refs)

    inputs = _current_inputs(
        source_paths=source_paths,
        shared_source=shared_source_path(agent.agent_home),
        global_source=global_source_path(agent.toolang_root),
        shared_local_root=shared_caps_dir(agent.agent_home, "skill"),
        global_local_root=global_caps_dir(agent.toolang_root, "skill"),
    )
    _sync_agent_states(
        agent=agent,
        programs=programs,
        agent_skill_refs=agent_skill_refs,
        shared_skill_entries=shared_skill_entries,
        global_skill_entries=global_skill_entries,
        inputs=inputs,
    )
    _remove_stale_sync_root_entries(shared_sync_root)
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


def _load_scope_skill_refs(path: Path, *, scope_label: str) -> dict[str, LockEntry]:
    if not path.exists():
        return {}

    program = parse_program(path.read_text(encoding="utf-8"))
    if program.declarations or program.thunks:
        raise ToolangError(f"{scope_label} may only contain 'use skill ...' statements.")

    refs_by_name: dict[str, LockEntry] = {}
    for use in program.uses:
        if use.kind != "skill":
            raise ToolangError(f"{scope_label} may only contain 'use skill ...' statements.")
        resolved = resolve_github_skill_ref(use.reference)
        entry = LockEntry(
            ref=resolved.ref,
            repo=resolved.repo,
            path=resolved.path,
            rev=resolved.rev,
        )
        existing = refs_by_name.get(resolved.name)
        if existing is not None and existing != entry:
            raise ToolangError(
                f"Conflicting skill refs resolve to the same name in {scope_label}: {resolved.name}"
            )
        refs_by_name[resolved.name] = entry
    return {name: refs_by_name[name] for name in sorted(refs_by_name)}


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
    inputs.update(_tree_fingerprints("shared", shared_local_root))
    inputs.update(_tree_fingerprints("global", global_local_root))
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
        shared_local_root=shared_caps_dir(agent.agent_home, "skill"),
        global_local_root=global_caps_dir(agent.toolang_root, "skill"),
    )
    state = SyncState.load(state_path)
    if state.inputs != current_inputs:
        return False

    try:
        states = _load_agent_states(agent.agent_home, _home_agent_names(agent.agent_home))
        programs = {agent_name: item.to_program() for agent_name, item in states.items()}
        if not _has_expected_inline_caps(shared_sync_root, _home_inline_caps(programs)):
            return False
        if not _has_expected_scope_skills(
            global_synced_caps_root(agent.toolang_root),
            _shared_scope_entries(states, "global_refs"),
        ):
            return False
        if not _has_expected_scope_skills(
            shared_sync_root,
            _shared_scope_entries(states, "shared_refs"),
        ):
            return False
        return _has_expected_agent_scope_skills(agent.agent_home, states)
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


def _home_inline_caps(programs: dict[str, Program]) -> list[InlineCap]:
    caps: list[InlineCap] = []
    seen: dict[tuple[str, str], str] = {}
    for agent_name, program in sorted(programs.items()):
        for declaration in program.declarations:
            kind = SOURCE_DECL_TO_CAP_KIND.get(declaration.kind)
            if kind is None:
                continue
            key = (kind, declaration.name)
            previous = seen.get(key)
            if previous is not None:
                raise ToolangError(
                    f"Duplicate {kind} declaration {declaration.name!r} across agent home: "
                    f"{previous}.too and {agent_name}.too"
                )
            seen[key] = agent_name
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


def _resolve_home_skill_refs(
    programs: dict[str, Program],
) -> dict[str, dict[str, LockEntry]]:
    refs_by_agent: dict[str, dict[str, LockEntry]] = {}
    for agent_name, program in sorted(programs.items()):
        resolved_by_name: dict[str, LockEntry] = {}
        for use in program.uses:
            if use.kind != "skill":
                raise ToolangError(
                    f"Only 'use skill ...' refs are supported by the current sync implementation, got use {use.kind}."
                )
            resolved = resolve_github_skill_ref(use.reference)
            entry = LockEntry(
                ref=resolved.ref,
                repo=resolved.repo,
                path=resolved.path,
                rev=resolved.rev,
            )
            existing = resolved_by_name.get(resolved.name)
            if existing is not None and existing != entry:
                raise ToolangError(
                    f"Conflicting skill refs resolve to the same name in {agent_name}.too: {resolved.name}"
                )
            resolved_by_name[resolved.name] = entry
        refs_by_agent[agent_name] = {
            name: resolved_by_name[name]
            for name in sorted(resolved_by_name)
        }
    return refs_by_agent


def _load_local_skill_entries(kind_dir: Path, *, scope_root: Path) -> dict[str, LockEntry]:
    if not kind_dir.exists():
        return {}

    entries: dict[str, LockEntry] = {}
    for item in sorted(kind_dir.iterdir()):
        if not item.is_dir() or not (item / "SKILL.md").exists():
            continue
        entries[item.name] = LockEntry(path=str(item.relative_to(scope_root)))
    return entries


def _overlay_skill_entries(
    refs: dict[str, LockEntry],
    locals_by_name: dict[str, LockEntry],
) -> dict[str, LockEntry]:
    effective = dict(refs)
    effective.update(locals_by_name)
    return {
        name: effective[name]
        for name in sorted(effective)
    }


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
            source_dir, files = fetch_github_tree(resolved)
            try:
                sync_skill_materialization(sync_root, name, source_dir, resolved, files)
            finally:
                shutil.rmtree(source_dir.parent.parent, ignore_errors=True)
        expected_names.add(name)
    remove_stale_skill_materializations(sync_root, expected_names)


def _sync_agent_skill_scopes(
    agent_home: Path,
    refs_by_agent: dict[str, dict[str, LockEntry]],
) -> None:
    for agent_name, entries in sorted(refs_by_agent.items()):
        _sync_scope_skills(
            agent_synced_caps_root(agent_home, agent_name),
            entries,
            scope_source_root=agent_home,
        )


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
    agent_skill_refs: dict[str, dict[str, LockEntry]],
    shared_skill_entries: dict[str, LockEntry],
    global_skill_entries: dict[str, LockEntry],
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
            agent_refs=LockedAgentRefs(skills=agent_skill_refs[agent_name]),
            shared_refs=LockedAgentRefs(skills=shared_skill_entries),
            global_refs=LockedAgentRefs(skills=global_skill_entries),
        ).save(agent_sync_path(agent.agent_home, agent_name))

    for path in synced_caps_root(agent.agent_home).glob("*.state.json"):
        if path not in expected:
            path.unlink()


def _has_expected_inline_caps(sync_root: Path, inline_caps: list[InlineCap]) -> bool:
    expected = {
        inline_cap_path(sync_root, cap.kind, cap.name, cap.language)
        for cap in inline_caps
    } | {
        inline_cap_meta_path(sync_root, cap.kind, cap.name)
        for cap in inline_caps
    }

    for kind in ("service", "prompt", "psyche"):
        kind_dir = sync_root / section_name(kind)
        if not kind_dir.exists():
            return False
        actual = set(kind_dir.iterdir())
        expected_for_kind = {path for path in expected if path.parent == kind_dir}
        if actual != expected_for_kind:
            return False
    return True


def _shared_scope_entries(
    states: dict[str, SyncState],
    attr: str,
) -> dict[str, LockEntry]:
    expected: dict[str, LockEntry] | None = None
    for agent_name in sorted(states):
        current = dict(getattr(states[agent_name], attr).skills)
        if expected is None:
            expected = current
            continue
        if expected != current:
            raise ToolangError(
                f"Shared scoped skill state drift detected for {attr}: {agent_name}"
            )
    return expected or {}


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


def _has_expected_agent_scope_skills(
    agent_home: Path,
    states: dict[str, SyncState],
) -> bool:
    for agent_name, state in states.items():
        if not _has_expected_scope_skills(
            agent_synced_caps_root(agent_home, agent_name),
            state.agent_refs.skills,
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
