from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from toolang.agent_refs import ResolvedAgentRef
from toolang.analyze import analyze_program
from toolang.errors import ToolangError
from toolang.files import InputFingerprint, SyncState, SyncedProgram, ToolangConfig, ToolangLock
from toolang.layout import (
    CAP_KINDS,
    agent_program_path,
    agent_source_path,
    agent_sync_state_path,
    ensure_agent_home_layout,
    shared_caps_dir,
    synced_caps_root,
    toolang_config_path,
    toolang_lock_path,
)
from toolang.parser import parse_program
from toolang_caps import CapParam, SyncedCap, sync_caps_tree

SOURCE_DECL_TO_CAP_KIND = {
    "service": "services",
    "prompt": "prompts",
    "psyche": "psyches",
}


def sync_agent(agent: ResolvedAgentRef) -> SyncedProgram:
    source_path = _existing_source_path(agent)
    ensure_agent_home_layout(agent.agent_home, agent.agent_name)
    _validate_supported_cap_inputs(agent.agent_home)

    program = parse_program(source_path.read_text(encoding="utf-8"))
    analyze_program(program)

    synced_program = SyncedProgram.from_program(program)
    sync_caps_tree(synced_caps_root(agent.agent_home), _source_caps(program))
    synced_program.save(agent_program_path(agent.agent_home, agent.agent_name))
    ToolangLock.empty().save(toolang_lock_path(agent.agent_home))
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
    sync_root = synced_caps_root(agent.agent_home)

    if not source_path.exists() or not program_path.exists() or not state_path.exists() or not sync_root.exists():
        return False

    state = SyncState.load(state_path)
    fingerprint = _fingerprint(source_path)
    recorded = state.inputs.get(source_path.name)
    if recorded != fingerprint:
        return False

    synced_program = SyncedProgram.load(program_path)
    return _has_expected_synced_caps(sync_root, synced_program)


def _validate_supported_cap_inputs(agent_home: Path) -> None:
    config_path = toolang_config_path(agent_home)
    if config_path.exists():
        config = ToolangConfig.load(config_path)
        for kind in CAP_KINDS:
            if getattr(config, kind):
                raise ToolangError(
                    "Managed caps from toolang.toml are not supported yet by the current sync implementation."
                )

    for kind in CAP_KINDS:
        directory = shared_caps_dir(agent_home, kind)
        if directory.exists() and any(directory.iterdir()):
            raise ToolangError(
                "Local shared caps are not supported yet by the current sync implementation."
            )


def _source_caps(program) -> list[SyncedCap]:
    caps: list[SyncedCap] = []
    for declaration in program.declarations:
        kind = SOURCE_DECL_TO_CAP_KIND.get(declaration.kind)
        if kind is None:
            continue
        caps.append(
            SyncedCap(
                kind=kind,
                name=declaration.name,
                language=declaration.language,
                body=declaration.body,
                params=[
                    CapParam(name=param.name, optional=param.optional)
                    for param in declaration.params
                ],
            )
        )
    return caps


def _has_expected_synced_caps(sync_root: Path, synced_program: SyncedProgram) -> bool:
    expected = {
        sync_root / cap.kind / f"{cap.name}.json"
        for cap in _source_caps(synced_program.to_program())
    }
    actual: set[Path] = set()

    for kind in CAP_KINDS:
        kind_dir = sync_root / kind
        if not kind_dir.exists():
            return False
        actual.update(kind_dir.iterdir())

    return actual == expected


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
