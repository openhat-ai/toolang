"""Project prepared capability state into caller-facing schemas."""

from __future__ import annotations

from pathlib import Path

from .schemas import CapDetail, CapInfo
from .state import (
    AgentState,
    PreparedCap,
    entry_definition_file,
    entry_form,
    entry_line,
    entry_origin,
    entry_ref,
    entry_scope,
)
from .types import EntryKind


class CapProjector:
    """Project immutable agent state into stable capability views."""

    def __init__(self, state: AgentState, *, agent_name: str) -> None:
        self.state = state
        self.agent_name = agent_name

    def list(self, *, kind: EntryKind | None = None) -> tuple[CapInfo, ...]:
        return tuple(
            self.info(entry)
            for entry in self.state.caps
            if kind is None or entry.kind == kind
        )

    def detail(self, kind: EntryKind, name: str) -> CapDetail | None:
        entry = next(
            (
                entry
                for entry in self.state.caps
                if entry.kind == kind and entry.name == name
            ),
            None,
        )
        if entry is None:
            return None
        return self.detail_entry(entry)

    def detail_entry(self, entry: PreparedCap) -> CapDetail:
        """Project one prepared capability into its detail."""

        content_path = Path(entry.path)
        content = entry.read_text() if content_path.is_file() else None
        files = None
        if entry.shape == "dir":
            files = tuple(
                sorted(
                    str(path.relative_to(content_path.parent))
                    for path in content_path.parent.rglob("*")
                    if path.is_file()
                )
            )
        info = self.info(entry)
        return CapDetail(
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
            content=content,
            files=files,
        )

    def info(self, entry: PreparedCap) -> CapInfo:
        """Project one prepared capability into its summary."""

        description = entry.meta.get("description")
        return CapInfo(
            kind=entry.kind,
            name=entry.name,
            description=str(description) if description is not None else None,
            scope=entry_scope(entry, agent_name=self.agent_name),
            origin=entry_origin(entry),
            form=entry_form(entry),
            ref=entry_ref(entry, agent_name=self.agent_name),
            definition_file=entry_definition_file(entry),
            editable=entry.source.form == "file",
            line=entry_line(entry),
        )
