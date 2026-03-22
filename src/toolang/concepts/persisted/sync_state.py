"""Persisted sync state for one agent source file."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field
from toolang.concepts.caps import CapKind
from toolang.program import Program

from .program import SyncedProgram

_REFS_ATTR_BY_KIND: dict[CapKind, str] = {
    "skill": "skills",
    "service": "services",
    "prompt": "prompts",
    "psyche": "psyches",
}


class InputFingerprint(BaseModel):
    """Observed file fingerprint used to detect stale sync state."""

    mtime_ns: int
    size: int | None = None


class LockEntry(BaseModel):
    """Resolved remote reference metadata for one synced capability."""

    ref: str | None = None
    repo: str | None = None
    path: str
    rev: str | None = None


class LockedAgentRefs(BaseModel):
    """Resolved refs grouped by cap kind for one scope layer."""

    skills: dict[str, LockEntry] = Field(default_factory=dict)
    services: dict[str, LockEntry] = Field(default_factory=dict)
    prompts: dict[str, LockEntry] = Field(default_factory=dict)
    psyches: dict[str, LockEntry] = Field(default_factory=dict)

    def entries(self, kind: CapKind) -> dict[str, LockEntry]:
        """Return the ref entries for one capability kind."""

        return getattr(self, _REFS_ATTR_BY_KIND[kind])

    def set_entries(self, kind: CapKind, entries: dict[str, LockEntry]) -> None:
        """Replace the ref entries for one capability kind."""

        setattr(self, _REFS_ATTR_BY_KIND[kind], entries)

    def sorted_copy(self) -> "LockedAgentRefs":
        """Return a copy with each kind map sorted by name."""

        sorted_refs = LockedAgentRefs()
        for kind in _REFS_ATTR_BY_KIND:
            entries = self.entries(kind)
            sorted_refs.set_entries(
                kind,
                {name: entries[name] for name in sorted(entries)},
            )
        return sorted_refs

    def overlay(self, overriding: "LockedAgentRefs") -> "LockedAgentRefs":
        """Return one refs object with later entries overriding earlier ones."""

        merged = LockedAgentRefs()
        for kind in _REFS_ATTR_BY_KIND:
            items = dict(self.entries(kind))
            items.update(overriding.entries(kind))
            merged.set_entries(kind, {name: items[name] for name in sorted(items)})
        return merged


class SyncState(BaseModel):
    """Persisted synced state for one agent source file."""

    version: int = 1
    synced_at: datetime
    source_file: str
    inputs: dict[str, InputFingerprint] = Field(default_factory=dict)
    program: SyncedProgram
    agent_refs: LockedAgentRefs = Field(default_factory=LockedAgentRefs)
    shared_refs: LockedAgentRefs = Field(default_factory=LockedAgentRefs)
    global_refs: LockedAgentRefs = Field(default_factory=LockedAgentRefs)

    @classmethod
    def load(cls, path: Path) -> "SyncState":
        """Load a sync-state document from disk."""

        return cls.model_validate_json(path.read_text(encoding="utf-8"))

    def save(self, path: Path) -> None:
        """Write this sync-state document to disk."""

        path.write_text(
            self.model_dump_json(indent=2, exclude_none=True),
            encoding="utf-8",
        )

    def to_program(self) -> Program:
        """Return the synced syntax program represented by this state."""

        return self.program.to_program()
