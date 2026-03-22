"""Agent identity and preparation.

This package owns agent reference resolution, prepared runtime inputs, and the
small public facade for agent-focused runtime entry points.
"""

from .prepared import PreparedAgent, prepare_agent
from .refs import AgentRef, resolve_agent_ref

__all__ = [
    "PreparedAgent",
    "AgentRef",
    "prepare_agent",
    "resolve_agent_ref",
]
