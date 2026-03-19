from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from toolang.agent_refs import ResolvedAgentRef
from toolang.ast import Program
from toolang.layout import agent_sync_path
from toolang.sync import ensure_agent_synced


@dataclass(frozen=True, slots=True)
class PreparedAgent:
    ref: ResolvedAgentRef
    source_path: Path
    sync_state_path: Path
    program: Program


def prepare_agent(agent: ResolvedAgentRef) -> PreparedAgent:
    source_path = agent.source_path
    if not source_path.exists():
        if agent.agent_kind == "visiting":
            raise FileNotFoundError(
                f"Visiting agent is not materialized locally: {agent.agent_uri} -> {source_path}"
            )
        raise FileNotFoundError(f"Agent source not found: {source_path}")

    synced_program = ensure_agent_synced(agent)
    return PreparedAgent(
        ref=agent,
        source_path=source_path,
        sync_state_path=agent_sync_path(agent.agent_home, agent.agent_name),
        program=synced_program.to_program(),
    )
