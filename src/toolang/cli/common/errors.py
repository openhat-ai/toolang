"""CLI runtime client errors."""

from __future__ import annotations


class RuntimeClientError(RuntimeError):
    """Raised when the local runtime API cannot satisfy a CLI request."""
