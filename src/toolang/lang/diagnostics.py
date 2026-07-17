"""Common language diagnostic types."""

from __future__ import annotations

from toolang.base.error import ToolangError


class ToolangSyntaxError(ToolangError):
    """Raised for syntax errors reported by tree-sitter."""


class ToolangValidationError(ToolangError):
    """Raised for invalid semantic AST programs."""


class ToolangFormatError(ValueError):
    """Raised when source formatting cannot be completed safely."""
