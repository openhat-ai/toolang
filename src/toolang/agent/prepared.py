from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from toolang.caps import CapScopeSelection, build_effective_program
from toolang.layout import agent_sync_path
from toolang.sync import ensure_agent_synced
from toolang.syntax import Program

from .refs import ResolvedAgentRef


@dataclass(frozen=True, slots=True)
class PreparedAgent:
    ref: ResolvedAgentRef
    source_path: Path
    sync_state_path: Path
    program: Program
    cap_scopes: CapScopeSelection


def prepare_agent(
    agent: ResolvedAgentRef,
    *,
    cap_scopes: CapScopeSelection = CapScopeSelection(),
) -> PreparedAgent:
    source_path = agent.source_path
    if not source_path.exists():
        if agent.agent_kind == "visiting":
            raise FileNotFoundError(
                f"Visiting agent is not materialized locally: {agent.agent_uri} -> {source_path}"
            )
        raise FileNotFoundError(f"Agent source not found: {source_path}")

    synced_program = ensure_agent_synced(agent)
    source_program = synced_program.to_program()
    return PreparedAgent(
        ref=agent,
        source_path=source_path,
        sync_state_path=agent_sync_path(agent.agent_home, agent.agent_name),
        program=build_effective_program(source_program, agent, cap_scopes=cap_scopes),
        cap_scopes=cap_scopes,
    )
