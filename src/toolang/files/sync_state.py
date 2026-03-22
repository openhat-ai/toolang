"""Persisted sync state for one agent source file."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field

from toolang.files.program import SyncedProgram


class InputFingerprint(BaseModel):
    """Observed file fingerprint used to detect stale sync state."""

    mtime_ns: int
    size: int | None = None


class LockEntry(BaseModel):
    """Resolved remote reference metadata for one synced cap."""

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

    def to_program(self):
        """Return the synced syntax program represented by this state."""

        return self.program.to_program()
