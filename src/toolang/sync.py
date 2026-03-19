from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path

from toolang.agent_refs import ResolvedAgentRef
from toolang.analyze import analyze_program
from toolang.ast import Program
from toolang.errors import ToolangError
from toolang.files import (
    InputFingerprint,
    LockEntry,
    LockedAgentRefs,
    SyncState,
    SyncedProgram,
    ToolangConfig,
)
from toolang.layout import (
    agent_source_path,
    agent_sync_path,
    ensure_agent_home_layout,
    shared_caps_dir,
    synced_caps_root,
    toolang_config_path,
)
from toolang.parser import parse_program
from toolang_caps import (
    CAP_KINDS,
    CapParam,
    InlineCap,
    ResolvedCapRef,
    SkillMeta,
    fetch_github_tree,
    inline_cap_meta_path,
    inline_cap_path,
    remove_stale_skill_materializations,
    resolve_github_skill_ref,
    section_name,
    skill_cap_dir,
    skill_cap_meta_path,
    sync_inline_caps,
    sync_skill_materialization,
)

SOURCE_DECL_TO_CAP_KIND = {
    "service": "service",
    "prompt": "prompt",
    "psyche": "psyche",
}


def sync_agent(agent: ResolvedAgentRef) -> SyncedProgram:
    _existing_source_path(agent)
    _validate_supported_cap_inputs(agent.agent_home)

    source_paths = _home_source_paths(agent.agent_home)
    programs = _parse_home_programs(source_paths)
    if agent.agent_name not in programs:
        raise ToolangError(f"Agent source not found in agent home: {agent.agent_name}.too")

    for agent_name in programs:
        ensure_agent_home_layout(agent.agent_home, agent_name)

    sync_root = synced_caps_root(agent.agent_home)
    sync_inline_caps(sync_root, _home_inline_caps(programs))

    refs_by_agent = _resolve_home_skill_refs(programs)
    _sync_skill_caps(sync_root, _materialized_skill_refs(refs_by_agent))
    _sync_agent_states(agent.agent_home, programs, refs_by_agent, _current_inputs(source_paths))
    _remove_stale_sync_root_entries(sync_root)
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


def _current_inputs(source_paths: list[Path]) -> dict[str, InputFingerprint]:
    return {path.name: _fingerprint(path) for path in source_paths}


def _is_sync_fresh(agent: ResolvedAgentRef) -> bool:
    source_path = agent.source_path
    state_path = agent_sync_path(agent.agent_home, agent.agent_name)
    sync_root = synced_caps_root(agent.agent_home)

    if not source_path.exists() or not state_path.exists() or not sync_root.exists():
        return False

    current_inputs = _current_inputs(_home_source_paths(agent.agent_home))
    state = SyncState.load(state_path)
    if state.inputs != current_inputs:
        return False

    try:
        states = _load_agent_states(agent.agent_home, current_inputs)
        return _has_expected_synced_caps(sync_root, states)
    except (FileNotFoundError, ToolangError):
        return False


def _load_agent_states(
    agent_home: Path,
    inputs: dict[str, InputFingerprint],
) -> dict[str, SyncState]:
    states: dict[str, SyncState] = {}
    for source_name in sorted(inputs):
        agent_name = Path(source_name).stem
        state_path = agent_sync_path(agent_home, agent_name)
        if not state_path.exists():
            raise FileNotFoundError(f"Synced state is missing: {state_path}")
        states[agent_name] = SyncState.load(state_path)
    return states


def _validate_supported_cap_inputs(agent_home: Path) -> None:
    config_path = toolang_config_path(agent_home)
    if config_path.exists():
        config = ToolangConfig.load(config_path)
        for kind in CAP_KINDS:
            if getattr(config, section_name(kind)):
                raise ToolangError(
                    "Managed caps from toolang.toml are not supported yet by the current sync implementation."
                )

    for kind in CAP_KINDS:
        directory = shared_caps_dir(agent_home, kind)
        if directory.exists() and any(directory.iterdir()):
            raise ToolangError(
                "Local shared caps are not supported yet by the current sync implementation."
            )


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
) -> dict[str, dict[str, ResolvedCapRef]]:
    refs_by_agent: dict[str, dict[str, ResolvedCapRef]] = {}
    for agent_name, program in sorted(programs.items()):
        resolved_by_name: dict[str, ResolvedCapRef] = {}
        for use in program.uses:
            if use.kind != "skill":
                raise ToolangError(
                    f"Only 'use skill ...' refs are supported by the current sync implementation, got use {use.kind}."
                )
            resolved = resolve_github_skill_ref(use.reference)
            existing = resolved_by_name.get(resolved.name)
            if existing is None:
                resolved_by_name[resolved.name] = resolved
                continue
            if (
                existing.ref != resolved.ref
                or existing.repo != resolved.repo
                or existing.path != resolved.path
            ):
                raise ToolangError(
                    f"Conflicting skill refs resolve to the same name in {agent_name}.too: {resolved.name}"
                )
        refs_by_agent[agent_name] = {
            name: resolved_by_name[name]
            for name in sorted(resolved_by_name)
        }
    return refs_by_agent


def _materialized_skill_refs(
    refs_by_agent: dict[str, dict[str, ResolvedCapRef]],
) -> list[ResolvedCapRef]:
    materialized: dict[str, ResolvedCapRef] = {}
    for agent_name in sorted(refs_by_agent):
        for name, resolved in refs_by_agent[agent_name].items():
            existing = materialized.get(name)
            if existing is None:
                materialized[name] = resolved
                continue
            if (
                existing.ref != resolved.ref
                or existing.repo != resolved.repo
                or existing.path != resolved.path
            ):
                raise ToolangError(
                    f"Conflicting skill refs resolve to the same materialized name across agent home: {name}"
                )
    return [materialized[name] for name in sorted(materialized)]


def _sync_skill_caps(sync_root: Path, resolved_skills: list[ResolvedCapRef]) -> None:
    expected_names: set[str] = set()
    for resolved in resolved_skills:
        source_dir, files = fetch_github_tree(resolved)
        try:
            sync_skill_materialization(sync_root, resolved.name, source_dir, resolved, files)
        finally:
            shutil.rmtree(source_dir.parent.parent, ignore_errors=True)
        expected_names.add(resolved.name)
    remove_stale_skill_materializations(sync_root, expected_names)


def _sync_agent_states(
    agent_home: Path,
    programs: dict[str, Program],
    refs_by_agent: dict[str, dict[str, ResolvedCapRef]],
    inputs: dict[str, InputFingerprint],
) -> None:
    expected = {
        agent_sync_path(agent_home, agent_name)
        for agent_name in programs
    }
    for agent_name, program in sorted(programs.items()):
        SyncState(
            synced_at=datetime.now(timezone.utc),
            source_file=agent_source_path(agent_home, agent_name).name,
            inputs=inputs,
            program=SyncedProgram.from_program(program),
            refs=LockedAgentRefs(
                skills={
                    name: LockEntry(
                        ref=resolved.ref,
                        repo=resolved.repo,
                        path=resolved.path,
                        rev=resolved.rev,
                    )
                    for name, resolved in refs_by_agent[agent_name].items()
                }
            ),
        ).save(agent_sync_path(agent_home, agent_name))

    for path in synced_caps_root(agent_home).glob("*.state.json"):
        if path not in expected:
            path.unlink()


def _has_expected_synced_caps(
    sync_root: Path,
    states: dict[str, SyncState],
) -> bool:
    programs = {
        agent_name: state.to_program()
        for agent_name, state in states.items()
    }
    if not _has_expected_inline_caps(sync_root, _home_inline_caps(programs)):
        return False
    return _has_expected_skills(sync_root, states)


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


def _has_expected_skills(sync_root: Path, states: dict[str, SyncState]) -> bool:
    materialized = _materialized_skill_entries(states)
    kind_dir = sync_root / section_name("skill")
    if not kind_dir.exists():
        return False

    expected_top_level = {
        skill_cap_dir(sync_root, name) for name in materialized
    } | {
        skill_cap_meta_path(sync_root, name) for name in materialized
    }
    if set(kind_dir.iterdir()) != expected_top_level:
        return False

    for name, entry in materialized.items():
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


def _materialized_skill_entries(states: dict[str, SyncState]) -> dict[str, LockEntry]:
    materialized: dict[str, LockEntry] = {}
    for agent_name in sorted(states):
        for name, entry in states[agent_name].refs.skills.items():
            existing = materialized.get(name)
            if existing is None:
                materialized[name] = entry
                continue
            if existing != entry:
                raise ToolangError(
                    f"Conflicting skill refs resolve to the same materialized name across agent home: {name}"
                )
    return materialized


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
    agent_root = agent_home / ".toolang" / "agent"
    if not agent_root.exists():
        return
    for room in agent_root.iterdir():
        if not room.is_dir():
            continue
        for filename in ("program.json", "sync.json"):
            path = room / filename
            if path.exists():
                path.unlink()
