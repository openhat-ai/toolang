"""Caller-facing capability protocol schemas."""

from __future__ import annotations

from dataclasses import dataclass

from .types import EntryForm, EntryKind, EntryOrigin, EntryScope


@dataclass(frozen=True, slots=True)
class CapInfo:
    """Summary of one effective capability."""

    kind: EntryKind
    name: str
    description: str | None
    scope: EntryScope
    origin: EntryOrigin
    form: EntryForm
    ref: str
    definition_file: str
    editable: bool
    line: int | None = None

@dataclass(frozen=True, slots=True)
class CapDetail(CapInfo):
    """Complete representation of one effective capability."""

    content: str | None = None
    files: tuple[str, ...] | None = None
