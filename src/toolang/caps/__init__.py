"""Capability support for refs, materialization, and runtime views.

This package owns capability fetch/source operations, synced sidecar
materialization, scope-aware overlay rules, and the effective runtime view of
visible caps.
"""

from .scope import CapScope, CapScopeSelection
from .view import CapView, CapsView, SkillCapView, build_effective_program, load_prepared_caps

__all__ = [
    "CapScope",
    "CapScopeSelection",
    "CapView",
    "CapsView",
    "SkillCapView",
    "build_effective_program",
    "load_prepared_caps",
]
