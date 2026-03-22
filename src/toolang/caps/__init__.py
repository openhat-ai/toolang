"""Capability runtime facade.

This package exposes the stable runtime-facing capability surface: scope
selection and loading the effective visible caps for one prepared agent.
"""

from .scope import CapScopeSelection
from .view import load_prepared_caps

__all__ = ["CapScopeSelection", "load_prepared_caps"]
