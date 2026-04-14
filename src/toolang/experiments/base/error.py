"""Shared base-layer error types."""

from __future__ import annotations


class ToolangError(Exception):
    """Raised when one Toolang plugin contract or runtime input is invalid."""
