"""Persisted mapping and sync payloads for mirrored remote tasks."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field

from .work import TaskStatus


class TaskMirrorSpec(BaseModel):
    """One remote task snapshot ready to be mirrored into a local task file."""

    provider: str
    remote_ref: str
    name: str
    body: str = ""
    status: TaskStatus = "todo"
    remote_updated_at: datetime | None = None


class TaskMirrorBatch(BaseModel):
    """One chore output payload containing mirrored remote task snapshots."""

    task_mirrors: list[TaskMirrorSpec] = Field(default_factory=list)


class TaskMirrorEntry(BaseModel):
    """One durable mapping from one remote task to one local task file."""

    provider: str
    remote_ref: str
    local_task_id: str
    path: str
    remote_updated_at: datetime | None = None
    last_synced_at: datetime | None = None


class TaskMirrorState(BaseModel):
    """Persisted remote-task mirror mappings for one agent room."""

    entries: list[TaskMirrorEntry] = Field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> "TaskMirrorState":
        """Load one task-mirror state document from disk."""

        return cls.model_validate_json(path.read_text(encoding="utf-8"))

    def save(self, path: Path) -> None:
        """Write this task-mirror state document to disk."""

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            self.model_dump_json(indent=2, exclude_none=True),
            encoding="utf-8",
        )

    def find(self, *, provider: str, remote_ref: str) -> TaskMirrorEntry | None:
        """Return the mirror entry for one remote task, if present."""

        provider_text = provider.strip()
        remote_ref_text = remote_ref.strip()
        for entry in self.entries:
            if (
                entry.provider == provider_text
                and entry.remote_ref == remote_ref_text
            ):
                return entry
        return None

    def find_by_local_task_id(self, local_task_id: str) -> TaskMirrorEntry | None:
        """Return the mirror entry for one local mirrored task id, if present."""

        task_id = local_task_id.strip()
        for entry in self.entries:
            if entry.local_task_id == task_id:
                return entry
        return None

    def upsert(self, entry: TaskMirrorEntry) -> "TaskMirrorState":
        """Return this state with one provider/ref mapping inserted or replaced."""

        updated = [item for item in self.entries if not (
            item.provider == entry.provider and item.remote_ref == entry.remote_ref
        )]
        updated.append(entry)
        updated.sort(key=lambda item: (item.provider, item.remote_ref, item.local_task_id))
        return self.model_copy(update={"entries": updated})
