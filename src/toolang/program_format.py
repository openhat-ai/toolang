"""Compatibility facade for Toolang source formatting APIs."""

from __future__ import annotations

from .lang.format import ToolangFormatError, format_source

__all__ = ["ToolangFormatError", "format_source"]
