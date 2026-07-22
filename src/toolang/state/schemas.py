"""Caller-facing capability protocol schemas."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .state import (
    PreparedCap,
    entry_definition_file,
    entry_form,
    entry_line,
    entry_origin,
    entry_ref,
    entry_scope,
)
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

    @classmethod
    def from_cap(cls, cap: PreparedCap, *, agent_name: str) -> CapInfo:
        """Build caller-facing capability information from prepared state."""

        description = cap.meta.get("description")
        return cls(
            kind=cap.kind,
            name=cap.name,
            description=str(description) if description is not None else None,
            scope=entry_scope(cap, agent_name=agent_name),
            origin=entry_origin(cap),
            form=entry_form(cap),
            ref=entry_ref(cap, agent_name=agent_name),
            definition_file=entry_definition_file(cap),
            editable=cap.source.form == "file",
            line=entry_line(cap),
        )


@dataclass(frozen=True, slots=True)
class CapDetail(CapInfo):
    """Complete representation of one effective capability."""

    content: str | None = None
    files: tuple[str, ...] | None = None

    @classmethod
    def from_cap(cls, cap: PreparedCap, *, agent_name: str) -> CapDetail:
        """Build complete caller-facing capability detail from prepared state."""

        info = CapInfo.from_cap(cap, agent_name=agent_name)
        content_path = Path(cap.path)
        files = None
        if cap.shape == "dir":
            files = tuple(
                sorted(
                    str(path.relative_to(content_path.parent))
                    for path in content_path.parent.rglob("*")
                    if path.is_file()
                )
            )
        return cls(
            kind=info.kind,
            name=info.name,
            description=info.description,
            scope=info.scope,
            origin=info.origin,
            form=info.form,
            ref=info.ref,
            definition_file=info.definition_file,
            editable=info.editable,
            line=info.line,
            content=cap.read_text() if content_path.is_file() else None,
            files=files,
        )
