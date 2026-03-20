from .cleanup import (
    remove_legacy_agent_programs,
    remove_legacy_lock_files,
    remove_stale_sync_root_entries,
)
from .core import (
    has_expected_agent_scope_caps,
    has_expected_scope_caps,
    sync_agent_caps,
    sync_scope_caps,
)

__all__ = [
    "has_expected_agent_scope_caps",
    "has_expected_scope_caps",
    "remove_legacy_agent_programs",
    "remove_legacy_lock_files",
    "remove_stale_sync_root_entries",
    "sync_agent_caps",
    "sync_scope_caps",
]
