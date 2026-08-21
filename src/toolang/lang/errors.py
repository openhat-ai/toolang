"""Language parsing, validation, and formatting errors."""

from __future__ import annotations

from toolang.base.errors import ToolangError


class ToolangSyntaxError(ToolangError):
    """Raised for syntax errors reported by tree-sitter."""


class ToolangValidationError(ToolangError):
    """Raised for invalid semantic AST programs."""


class ToolangOutputError(ToolangError):
    """Raised when a runnable result violates its declared output type."""


class ToolangFormatError(ValueError):
    """Raised when source formatting cannot be completed safely."""
