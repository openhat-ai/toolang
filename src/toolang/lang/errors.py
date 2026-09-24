"""Language parsing, validation, and formatting errors."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from toolang.base.errors import ToolangError


class ToolangSourceError(ToolangError):
    """A source error with an optional one-based diagnostic anchor."""

    def __init__(
        self, message: str, *, line: int | None = None, column: int | None = None
    ):
        super().__init__(message)
        self.line = line
        self.column = column


class ToolangSyntaxError(ToolangSourceError):
    """Raised for syntax errors reported by tree-sitter."""


class ToolangValidationError(ToolangSourceError):
    """Raised for invalid semantic AST programs."""


class ToolangOutputError(ToolangError):
    """Raised when a runnable result violates its declared output type."""


class ToolangFormatError(ValueError):
    """Raised when source formatting cannot be completed safely."""


@contextmanager
def source_location(line: int, column: int | None = None) -> Iterator[None]:
    """Anchor an error without replacing a more specific nested location."""
    try:
        yield
    except ToolangSourceError as exc:
        if exc.line is None:
            exc.line, exc.column = line, column
        raise
    except ToolangError as exc:
        raise ToolangValidationError(str(exc), line=line, column=column) from exc
