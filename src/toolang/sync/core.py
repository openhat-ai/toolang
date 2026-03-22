"""Sync orchestration.

This module owns the public sync entry points that parse source files, resolve
refs, materialize sync artifacts, and persist synced program state.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from toolang.agent.refs import AgentRef
from toolang.errors import ToolangError
from toolang.files.program import SyncedProgram
from toolang.files.sync_state import InputFingerprint, LockedAgentRefs, SyncState
from toolang.layout import (
    agent_source_path,
    agent_sync_path,
    ensure_agent_home_layout,
    global_source_path,
    global_synced_caps_root,
    shared_source_path,
    synced_caps_root,
)
from toolang.syntax import Program, analyze_program, parse_program
from toolang_caps.models import CAP_KINDS, section_name

from .materialize import (
    has_expected_agent_scope_caps,
    has_expected_scope_caps,
    remove_legacy_agent_programs,
    remove_legacy_lock_files,
    remove_stale_sync_root_entries,
    sync_agent_caps,
    sync_scope_caps,
)
from .refs import (
    load_local_entries_for_scope,
    load_scope_refs,
    overlay_ref_entries,
    resolve_home_refs,
)


def sync_agent(agent: AgentRef) -> SyncedProgram:
    """Parse, resolve, materialize, and persist synced state for one agent."""
    _existing_source_path(agent)

    source_paths = _home_source_paths(agent.agent_home)
    programs = _parse_home_programs(source_paths)
    if agent.agent_name not in programs:
        raise ToolangError(f"Agent source not found in agent home: {agent.agent_name}.too")

    for agent_name in programs:
        ensure_agent_home_layout(agent.agent_home, agent_name)

    global_ref_entries = load_scope_refs(
        global_source_path(agent.toolang_root),
        scope_label="global agents.too",
    )
    shared_ref_entries = load_scope_refs(
        shared_source_path(agent.agent_home),
        scope_label="shared agents.too",
    )
    agent_ref_entries = resolve_home_refs(programs)

    global_local_entries = load_local_entries_for_scope(
        root=agent.toolang_root,
        scope_root=agent.toolang_root,
        scope="global",
    )
    shared_local_entries = load_local_entries_for_scope(
        root=agent.agent_home,
        scope_root=agent.agent_home / ".toolang",
        scope="shared",
    )

    global_effective_entries = overlay_ref_entries(global_ref_entries, global_local_entries)
    shared_effective_entries = overlay_ref_entries(shared_ref_entries, shared_local_entries)

    sync_scope_caps(
        global_synced_caps_root(agent.toolang_root),
        global_effective_entries,
        scope_source_root=agent.toolang_root,
    )
    sync_scope_caps(
        synced_caps_root(agent.agent_home),
        shared_effective_entries,
        scope_source_root=agent.agent_home / ".toolang",
    )
    sync_agent_caps(agent.agent_home, programs, agent_ref_entries)

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
    remove_stale_sync_root_entries(synced_caps_root(agent.agent_home))
    remove_legacy_lock_files(agent.agent_home)
    remove_legacy_agent_programs(agent.agent_home)
    return SyncState.load(agent_sync_path(agent.agent_home, agent.agent_name)).program


def ensure_agent_synced(agent: AgentRef) -> SyncedProgram:
    """Return synced program state, refreshing it first when inputs changed."""
    if _is_sync_fresh(agent):
        return SyncState.load(agent_sync_path(agent.agent_home, agent.agent_name)).program
    return sync_agent(agent)


def _existing_source_path(agent: AgentRef) -> Path:
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


def _current_inputs(
    *,
    source_paths: list[Path],
    shared_source: Path,
    global_source: Path,
    shared_local_root: Path,
    global_local_root: Path,
) -> dict[str, InputFingerprint]:
    inputs = {f"agent/{path.name}": _fingerprint(path) for path in source_paths}
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


def _is_sync_fresh(agent: AgentRef) -> bool:
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
        if not has_expected_scope_caps(
            global_synced_caps_root(agent.toolang_root),
            _shared_scope_entries(states, "global_refs"),
        ):
            return False
        if not has_expected_scope_caps(
            shared_sync_root,
            _shared_scope_entries(states, "shared_refs"),
        ):
            return False
        return has_expected_agent_scope_caps(agent.agent_home, programs, states)
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


def _sync_agent_states(
    *,
    agent: AgentRef,
    programs: dict[str, Program],
    agent_ref_entries: dict[str, LockedAgentRefs],
    shared_entries: LockedAgentRefs,
    global_entries: LockedAgentRefs,
    inputs: dict[str, InputFingerprint],
) -> None:
    expected = {agent_sync_path(agent.agent_home, agent_name) for agent_name in programs}
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


def _fingerprint(path: Path) -> InputFingerprint:
    stat = path.stat()
    return InputFingerprint(mtime_ns=stat.st_mtime_ns, size=stat.st_size)
