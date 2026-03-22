"""Agent resolution, preparation, and registry.

This package owns agent selector resolution, managed local-agent operations,
prepared runtime inputs, and the local known-agent registry.
"""

from .prepared import PreparedAgent, prepare_agent
from .refs import resolve_agent_ref

__all__ = [
    "PreparedAgent",
    "prepare_agent",
    "resolve_agent_ref",
]
