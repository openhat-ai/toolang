"""Language parsing, validation, and formatting errors."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace

from toolang.base.errors import ToolangError

from .types import RelatedLocation, SourceDiagnostic, SourceLocation


class _DiagnosticError(Exception):
    """Shared payload and compatibility accessors for source exceptions."""

    def __init__(
        self,
        message: str | SourceDiagnostic,
        *,
        line: int | None = None,
        column: int | None = None,
        related: tuple[RelatedLocation, ...] = (),
    ):
        self.diagnostic = (
            message
            if isinstance(message, SourceDiagnostic)
            else SourceDiagnostic(
                message,
                SourceLocation(line, column) if line is not None else None,
                related,
            )
        )
        super().__init__(self.diagnostic.reason)

    def __str__(self) -> str:
        from .diagnostics import render_diagnostic

        return render_diagnostic(self.diagnostic)

    @property
    def line(self) -> int | None:
        return self.diagnostic.location.line if self.diagnostic.location else None

    @line.setter
    def line(self, value: int | None) -> None:
        location = self.diagnostic.location
        self.diagnostic = replace(
            self.diagnostic,
            location=(
                replace(location, line=value) if location else SourceLocation(value)
            )
            if value is not None
            else None,
        )

    @property
    def column(self) -> int | None:
        return self.diagnostic.location.column if self.diagnostic.location else None

    @column.setter
    def column(self, value: int | None) -> None:
        if location := self.diagnostic.location:
            self.diagnostic = replace(
                self.diagnostic, location=replace(location, column=value)
            )


class ToolangSourceError(_DiagnosticError, ToolangError):
    """A source error carrying a structured diagnostic."""


class ToolangSyntaxError(ToolangSourceError):
    """Raised for syntax errors reported by tree-sitter."""


class ToolangValidationError(ToolangSourceError):
    """Raised for invalid semantic AST programs."""


class ToolangOutputError(ToolangError):
    """Raised when a runnable result violates its declared output type."""


class ToolangFormatError(_DiagnosticError, ValueError):
    """Raised when source formatting cannot be completed safely."""


@contextmanager
def source_location(line: int | None, column: int | None = None) -> Iterator[None]:
    """Anchor an error without replacing a more specific nested location."""
    try:
        yield
    except ToolangSourceError as exc:
        if exc.line is None and line is not None:
            exc.line, exc.column = line, column
        elif line is not None and exc.line == line and exc.column is None:
            exc.column = column
        raise
    except ToolangError as exc:
        raise ToolangValidationError(str(exc), line=line, column=column) from exc
