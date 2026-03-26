"""Prepared runtime inputs for one agent.

This module turns a resolved agent reference into sync-backed runtime inputs
without starting loops or executing turns.
"""

from __future__ import annotations

from dataclasses import dataclass

from toolang.caps import CapScopeSelection, ensure_agent_synced
from toolang.caps.view import build_effective_program
from toolang.program import Program
from toolang.concepts.identity import AgentRef


@dataclass(frozen=True, slots=True)
class PreparedAgent:
    """One sync-backed agent snapshot ready for execution."""

    ref: AgentRef
    program: Program
    cap_scopes: CapScopeSelection
    source_text: str


def prepare_agent(
    agent: AgentRef,
    *,
    cap_scopes: CapScopeSelection = CapScopeSelection(),
) -> PreparedAgent:
    """Prepare one resolved agent for runtime execution."""
    if not agent.source.exists():
        if agent.kind == "visiting":
            raise FileNotFoundError(
                f"Visiting agent is not materialized locally: {agent.uri} -> {agent.source}"
            )
        raise FileNotFoundError(f"Agent source not found: {agent.source}")

    source_text = agent.source.read_text(encoding="utf-8")
    synced_program = ensure_agent_synced(agent)
    source_program = synced_program.to_program()
    return PreparedAgent(
        ref=agent,
        program=build_effective_program(source_program, agent, cap_scopes=cap_scopes),
        cap_scopes=cap_scopes,
        source_text=source_text,
    )
