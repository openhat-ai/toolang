from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path

from toolang.agent_refs import ResolvedAgentRef
from toolang.analyze import analyze_program
from toolang.ast import Program
from toolang.errors import ToolangError
from toolang.files import (
    AgentLock,
    AgentLockEntry,
    InputFingerprint,
    SyncState,
    SyncedProgram,
    ToolangConfig,
)
from toolang.layout import (
    agent_lock_path,
    agent_program_path,
    agent_source_path,
    agent_sync_state_path,
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
    source_path = _existing_source_path(agent)
    ensure_agent_home_layout(agent.agent_home, agent.agent_name)
    _validate_supported_cap_inputs(agent.agent_home)

    program = parse_program(source_path.read_text(encoding="utf-8"))
    analyze_program(program)

    synced_program = SyncedProgram.from_program(program)
    sync_root = synced_caps_root(agent.agent_home)
    inline_caps = _source_inline_caps(program)
    sync_inline_caps(sync_root, inline_caps)

    resolved_skills = _resolve_skill_refs(program)
    _sync_skill_caps(sync_root, resolved_skills)
    _remove_stale_sync_root_entries(sync_root)

    synced_program.save(agent_program_path(agent.agent_home, agent.agent_name))
    _build_agent_lock(resolved_skills).save(agent_lock_path(agent.agent_home))
    _remove_legacy_toolang_lock(agent.agent_home)
    _write_sync_state(agent, source_path)
    return synced_program


def ensure_agent_synced(agent: ResolvedAgentRef) -> SyncedProgram:
    if _is_sync_fresh(agent):
        return SyncedProgram.load(agent_program_path(agent.agent_home, agent.agent_name))
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


def _is_sync_fresh(agent: ResolvedAgentRef) -> bool:
    source_path = agent.source_path
    program_path = agent_program_path(agent.agent_home, agent.agent_name)
    state_path = agent_sync_state_path(agent.agent_home, agent.agent_name)
    lock_path = agent_lock_path(agent.agent_home)
    sync_root = synced_caps_root(agent.agent_home)

    if (
        not source_path.exists()
        or not program_path.exists()
        or not state_path.exists()
        or not lock_path.exists()
        or not sync_root.exists()
    ):
        return False

    state = SyncState.load(state_path)
    fingerprint = _fingerprint(source_path)
    recorded = state.inputs.get(source_path.name)
    if recorded != fingerprint:
        return False

    synced_program = SyncedProgram.load(program_path)
    agent_lock = AgentLock.load(lock_path)
    return _has_expected_synced_caps(sync_root, synced_program.to_program(), agent_lock)


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


def _source_inline_caps(program: Program) -> list[InlineCap]:
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


def _resolve_skill_refs(program: Program):
    resolved_by_name = {}
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
        if existing.ref != resolved.ref or existing.repo != resolved.repo or existing.path != resolved.path:
            raise ToolangError(f"Conflicting skill refs resolve to the same name: {resolved.name}")
    return [resolved_by_name[name] for name in sorted(resolved_by_name)]


def _sync_skill_caps(sync_root: Path, resolved_skills) -> None:
    expected_names: set[str] = set()
    for resolved in resolved_skills:
        source_dir, files = fetch_github_tree(resolved)
        try:
            sync_skill_materialization(sync_root, resolved.name, source_dir, resolved, files)
        finally:
            shutil.rmtree(source_dir.parent, ignore_errors=True)
        expected_names.add(resolved.name)
    remove_stale_skill_materializations(sync_root, expected_names)


def _build_agent_lock(resolved_skills) -> AgentLock:
    return AgentLock(
        skills={
            resolved.name: AgentLockEntry(
                ref=resolved.ref,
                repo=resolved.repo,
                path=resolved.path,
                rev=resolved.rev,
            )
            for resolved in resolved_skills
        }
    )


def _has_expected_synced_caps(sync_root: Path, program: Program, agent_lock: AgentLock) -> bool:
    if not _has_expected_inline_caps(sync_root, _source_inline_caps(program)):
        return False
    return _has_expected_skills(sync_root, agent_lock)


def _has_expected_inline_caps(sync_root: Path, inline_caps: list[InlineCap]) -> bool:
    expected = {
        inline_cap_path(sync_root, cap.kind, cap.name, cap.language)
        for cap in inline_caps
    } | {
        inline_cap_meta_path(sync_root, cap.kind, cap.name)
        for cap in inline_caps
    }

    for kind in ("service", "prompt", "psyche"):
        kind_dir = sync_root / kind
        if not kind_dir.exists():
            return False
        actual = set(kind_dir.iterdir())
        expected_for_kind = {path for path in expected if path.parent == kind_dir}
        if actual != expected_for_kind:
            return False
    return True


def _has_expected_skills(sync_root: Path, agent_lock: AgentLock) -> bool:
    kind_dir = sync_root / "skill"
    if not kind_dir.exists():
        return False

    expected_top_level = {
        skill_cap_dir(sync_root, name) for name in agent_lock.skills
    } | {
        skill_cap_meta_path(sync_root, name) for name in agent_lock.skills
    }
    if set(kind_dir.iterdir()) != expected_top_level:
        return False

    for name, entry in agent_lock.skills.items():
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


def _write_sync_state(agent: ResolvedAgentRef, source_path: Path) -> None:
    state = SyncState(
        synced_at=datetime.now(timezone.utc),
        source_file=agent_source_path(agent.agent_home, agent.agent_name).name,
        agent_room=str(agent_program_path(agent.agent_home, agent.agent_name).parent.relative_to(agent.agent_home)) + "/",
        synced_caps=str(synced_caps_root(agent.agent_home).relative_to(agent.agent_home)) + "/",
        inputs={
            source_path.name: _fingerprint(source_path),
        },
    )
    state.save(agent_sync_state_path(agent.agent_home, agent.agent_name))


def _fingerprint(path: Path) -> InputFingerprint:
    stat = path.stat()
    return InputFingerprint(mtime_ns=stat.st_mtime_ns, size=stat.st_size)


def _remove_stale_sync_root_entries(sync_root: Path) -> None:
    expected = set(CAP_KINDS)
    for path in sync_root.iterdir():
        if path.name not in expected:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()


def _remove_legacy_toolang_lock(agent_home: Path) -> None:
    legacy_lock = agent_home / "toolang.lock"
    if legacy_lock.exists():
        legacy_lock.unlink()
