"""Prepared-snapshot refresh helpers."""

from __future__ import annotations

from toolang.agent.prepared import PreparedAgent, prepare_agent


def refresh_prepared(prepared: PreparedAgent) -> PreparedAgent:
    """Refresh one prepared snapshot from durable source when possible."""

    try:
        return prepare_agent(prepared.ref, cap_scopes=prepared.cap_scopes)
    except FileNotFoundError:
        return prepared
