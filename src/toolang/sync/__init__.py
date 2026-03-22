"""Sync facade.

This package owns source-to-sync orchestration and the public sync entry points
used by CLI and runtime preparation.
"""

from .core import ensure_agent_synced, sync_agent

__all__ = ["ensure_agent_synced", "sync_agent"]
