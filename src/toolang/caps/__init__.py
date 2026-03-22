"""Capability facade.

This package exposes the stable capability surface:
- scope selection for runtime visibility
- syncing source and local caps into materialized state
- loading effective visible caps for one prepared agent
"""

from .scope import CapScopeSelection
from .sync import ensure_agent_synced, sync_agent
from .view import load_prepared_caps

__all__ = [
    "CapScopeSelection",
    "ensure_agent_synced",
    "load_prepared_caps",
    "sync_agent",
]
