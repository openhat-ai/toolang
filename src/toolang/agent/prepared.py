"""Prepared runtime inputs for one agent.

This module turns a resolved agent reference into sync-backed runtime inputs
without starting loops or executing turns.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from toolang.caps import CapScopeSelection, build_effective_program
from toolang.layout import agent_sync_path
from toolang.sync import ensure_agent_synced
from toolang.program import Program
from toolang.concepts.identity import AgentRef


@dataclass(frozen=True, slots=True)
class PreparedAgent:
    """One sync-backed agent snapshot ready for execution."""

    ref: AgentRef
    source_path: Path
    sync_state_path: Path
    program: Program
    cap_scopes: CapScopeSelection


def prepare_agent(
    agent: AgentRef,
    *,
    cap_scopes: CapScopeSelection = CapScopeSelection(),
) -> PreparedAgent:
    """Prepare one resolved agent for runtime execution."""
    source_path = agent.source
    if not source_path.exists():
        if agent.kind == "visiting":
            raise FileNotFoundError(
                f"Visiting agent is not materialized locally: {agent.uri} -> {source_path}"
            )
        raise FileNotFoundError(f"Agent source not found: {source_path}")

    synced_program = ensure_agent_synced(agent)
    source_program = synced_program.to_program()
    return PreparedAgent(
        ref=agent,
        source_path=source_path,
        sync_state_path=agent_sync_path(agent.home, agent.name),
        program=build_effective_program(source_program, agent, cap_scopes=cap_scopes),
        cap_scopes=cap_scopes,
    )
