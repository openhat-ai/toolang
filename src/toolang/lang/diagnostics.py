"""Common language diagnostic types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from toolang.base.error import ToolangError

from .ast import Span

DiagnosticSeverity = Literal["error", "warning", "hint"]


@dataclass(frozen=True, slots=True)
class Diagnostic:
    message: str
    span: Span | None = None
    severity: DiagnosticSeverity = "error"


class ToolangSyntaxError(ToolangError):
    """Raised for CST syntax errors from tree-sitter lowering."""


class ToolangValidationError(ToolangError):
    """Raised for invalid semantic AST programs."""


class ToolangFormatError(ValueError):
    """Raised when source formatting cannot be completed safely."""
