"""Shared Toolang error types exposed to plugins."""

from __future__ import annotations


class ToolangError(Exception):
    """Raised when Toolang input, configuration, or runtime behavior is invalid."""
