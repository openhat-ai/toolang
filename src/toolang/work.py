"""Local task and chore document helpers."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Literal

from dateutil.rrule import rrulestr
import frontmatter
from pydantic import BaseModel, ConfigDict, field_validator

from .ids import LOCAL_ID_FAMILY, allocate_id, decode_id

JobState = Literal["active", "inactive", "archived"]
TaskStage = Literal["todo", "running", "done", "failed"]
DEFAULT_CHORE_SCHEDULE = "FREQ=HOURLY;INTERVAL=1"
REMOTE_REF_PATTERN = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b")


class _MarkdownDocument(BaseModel):
    """Base model for one markdown work document."""

    model_config = ConfigDict(extra="ignore")

    state: JobState = "active"
    body: str = ""

    @field_validator("state", mode="before")
    @classmethod
    def _normalize_state(cls, value: object) -> object:
        if value is None:
            return "active"
        return str(value).strip().lower() or "active"

    @classmethod
    def _load_markdown(cls, path: Path) -> "_MarkdownDocument":
        post = frontmatter.loads(path.read_text(encoding="utf-8"))
        return cls.model_validate({**dict(post.metadata), "body": post.content})

    @classmethod
    def _parse_markdown(cls, text: str) -> "_MarkdownDocument":
        post = frontmatter.loads(text)
        return cls.model_validate({**dict(post.metadata), "body": post.content})

    def _save_markdown(self, path: Path) -> None:
        post = frontmatter.Post(self.body, None, **self.markdown_metadata())
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(frontmatter.dumps(post), encoding="utf-8")

    def markdown_metadata(self) -> dict[str, object]:
        """Return persisted frontmatter metadata."""

        metadata: dict[str, object] = {}
        if self.state != "active":
            metadata["state"] = self.state
        return metadata

    def content_hash(self) -> str:
        """Return one stable textual signature for this document."""

        payload = json.dumps(
            {"metadata": self.markdown_metadata(), "body": self.body},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def active(self) -> bool:
        """Return whether this document participates in execution."""

        return self.state == "active"

    def archived(self) -> bool:
        """Return whether this document is archived."""

        return self.state == "archived"


class TaskFile(_MarkdownDocument):
    """One local task document."""

    id: str | None = None
    title: str | None = None
    stage: TaskStage = "todo"

    @field_validator("id", mode="before")
    @classmethod
    def _normalize_id(cls, value: object) -> object:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @field_validator("stage", mode="before")
    @classmethod
    def _normalize_stage(cls, value: object) -> object:
        if value is None:
            return "todo"
        text = str(value).strip().lower()
        return text or "todo"

    @classmethod
    def load(
        cls,
        path: Path,
        *,
        id_factory: Callable[[], str] | None = None,
        persist_id: bool = False,
        archived: bool = False,
    ) -> "TaskFile":
        loaded = cls._load_markdown(path)
        document = cls.model_validate(loaded.model_dump(mode="python"))
        if archived and document.state != "archived":
            document = document.model_copy(update={"state": "archived"})
        original = document
        if document.id is None and id_factory is not None:
            document = document.model_copy(update={"id": id_factory()})
        if document.id is None and persist_id:
            raise ValueError(f"task is missing id: {path}")
        if persist_id and document != original:
            document.save(path)
        return document

    @classmethod
    def parse_text(cls, text: str) -> "TaskFile":
        loaded = cls._parse_markdown(text)
        return cls.model_validate(loaded.model_dump(mode="python"))

    def with_id(self, id_factory: Callable[[], str]) -> "TaskFile":
        """Return this task with a stable id."""

        if self.id is not None:
            return self
        return self.model_copy(update={"id": id_factory()})

    def save(self, path: Path) -> None:
        if self.id is None:
            raise ValueError("task id is required")
        self._save_markdown(path)

    def task_id(self) -> str:
        if self.id is None:
            raise ValueError("task id is required")
        return self.id

    def thread_id(self) -> str:
        return task_thread_id(self.task_id())

    def claimable(self) -> bool:
        """Return whether this task can be claimed by a run."""

        return self.state == "active" and self.stage == "todo"

    def running(self) -> "TaskFile":
        """Return this task marked as claimed."""

        return self.model_copy(update={"stage": "running"})

    def completed(self, *, succeeded: bool) -> "TaskFile":
        """Return this task marked as completed."""

        return self.model_copy(update={"stage": "done" if succeeded else "failed"})

    def remote_stage(self) -> TaskStage | None:
        """Return the local stage implied by a mirrored remote status."""

        status = self.remote_status()
        if status is None:
            return None
        normalized = status.lower()
        if normalized in {"done", "completed", "complete", "closed", "resolved"}:
            return "done"
        if normalized in {"canceled", "cancelled"}:
            return "failed"
        return "todo"

    def remote_status(self) -> str | None:
        """Return the first explicit remote status line from the task body."""

        for line in self.body.splitlines():
            key, separator, value = line.partition(":")
            if separator != ":":
                continue
            normalized_key = key.strip().lower()
            if normalized_key in {"status", "remote status"}:
                text = value.strip()
                return text or None
        return None

    def remote_ref(self) -> str | None:
        """Return a stable remote work-item key when one is present."""

        text = "\n".join(part for part in (self.title or "", self.body) if part)
        match = REMOTE_REF_PATTERN.search(text)
        if match is None:
            return None
        return match.group(0)

    def archived_copy(self) -> "TaskFile":
        """Return this task marked as archived."""

        return self.model_copy(update={"state": "archived"})

    def render_input(self, *, fallback_name: str) -> str:
        """Return the prompt input for this task."""

        body = self.body.strip()
        if body:
            return body
        return fallback_name.strip()

    def display_title(self, *, fallback_name: str) -> str:
        """Return a short title for UI projection."""

        return _display_title(self.title, self.body, fallback=fallback_name)

    def markdown_metadata(self) -> dict[str, object]:
        metadata = super().markdown_metadata()
        if self.id is not None:
            metadata["id"] = self.id
        if self.title is not None and self.title.strip():
            metadata["title"] = self.title.strip()
        if self.stage != "todo":
            metadata["stage"] = self.stage
        return metadata


class ChoreFile(_MarkdownDocument):
    """One local chore document."""

    id: str | None = None
    title: str | None = None
    schedule: str = DEFAULT_CHORE_SCHEDULE

    @field_validator("id", mode="before")
    @classmethod
    def _normalize_id(cls, value: object) -> object:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @field_validator("schedule", mode="before")
    @classmethod
    def _normalize_schedule(cls, value: object) -> str:
        text = str(value or "").strip() or DEFAULT_CHORE_SCHEDULE
        rrulestr(text, dtstart=datetime(2026, 1, 1, tzinfo=timezone.utc))
        return text

    @classmethod
    def load(
        cls,
        path: Path,
        *,
        id_factory: Callable[[], str] | None = None,
        persist_id: bool = False,
        archived: bool = False,
    ) -> "ChoreFile":
        loaded = cls._load_markdown(path)
        document = cls.model_validate(loaded.model_dump(mode="python"))
        if archived and document.state != "archived":
            document = document.model_copy(update={"state": "archived"})
        original = document
        if document.id is None and id_factory is not None:
            document = document.model_copy(update={"id": id_factory()})
        if document.id is None and persist_id:
            raise ValueError(f"chore is missing id: {path}")
        if persist_id and document != original:
            document.save(path)
        return document

    @classmethod
    def parse_text(cls, text: str) -> "ChoreFile":
        loaded = cls._parse_markdown(text)
        return cls.model_validate(loaded.model_dump(mode="python"))

    def with_id(self, id_factory: Callable[[], str]) -> "ChoreFile":
        """Return this chore with a stable id."""

        if self.id is not None:
            return self
        return self.model_copy(update={"id": id_factory()})

    def save(self, path: Path) -> None:
        if self.id is None:
            raise ValueError("chore id is required")
        self._save_markdown(path)

    def chore_id(self) -> str:
        if self.id is None:
            raise ValueError("chore id is required")
        return self.id

    def thread_id(self) -> str:
        return chore_thread_id(self.chore_id())

    def render_input(self, *, fallback_title: str) -> str:
        """Return the prompt input for this chore."""

        title = (self.title or "").strip() or fallback_title.strip()
        body = self.body.strip()
        if title and body:
            return f"# {title}\n\n{body}"
        if body:
            return body
        return title

    def display_title(self, *, fallback_name: str) -> str:
        """Return a short title for UI projection."""

        return _display_title(self.title, self.body, fallback=fallback_name)

    def archived_copy(self) -> "ChoreFile":
        """Return this chore marked as archived."""

        return self.model_copy(update={"state": "archived"})

    def markdown_metadata(self) -> dict[str, object]:
        metadata = super().markdown_metadata()
        if self.id is not None:
            metadata["id"] = self.id
        if self.title is not None and self.title.strip():
            metadata["title"] = self.title.strip()
        metadata["schedule"] = self.schedule
        return metadata


@dataclass(frozen=True, slots=True)
class TaskEntry:
    """One listed task document."""

    name: str
    path: Path
    document: TaskFile


@dataclass(frozen=True, slots=True)
class ChoreEntry:
    """One listed chore document."""

    name: str
    path: Path
    document: ChoreFile


def task_terminal(stage: TaskStage) -> bool:
    """Return whether one task stage is terminal."""

    return stage in {"done", "failed"}


def next_scheduled_at(
    schedule_text: str,
    *,
    anchor: datetime,
    not_before: datetime,
    inclusive: bool,
) -> datetime | None:
    """Return the next UTC occurrence for one RRULE."""

    anchor_utc = _as_utc(anchor)
    floor_utc = _as_utc(not_before)
    schedule = rrulestr(schedule_text, dtstart=anchor_utc)
    candidate = schedule.after(floor_utc, inc=inclusive)
    if candidate is None:
        return None
    return _as_utc(candidate)


def task_thread_id(task_id: str) -> str:
    """Return the normalized thread id for one task."""

    return f"task_{task_id.strip()}"


def chore_thread_id(chore_id: str) -> str:
    """Return the normalized thread id for one chore."""

    return f"chore_{chore_id.strip()}"


def task_id_from_thread_id(thread_id: str) -> str | None:
    """Extract one local task id from its canonical thread id."""

    if not thread_id.startswith("task_"):
        return None
    task_id = thread_id.removeprefix("task_").strip()
    return task_id or None


def chore_id_from_thread_id(thread_id: str) -> str | None:
    """Extract one local chore id from its canonical thread id."""

    if not thread_id.startswith("chore_"):
        return None
    chore_id = thread_id.removeprefix("chore_").strip()
    return chore_id or None


def task_path(toolang_root: Path, agent_name: str, task_id: str) -> Path:
    """Return one local task path."""

    return _work_dir(toolang_root, agent_name, kind="task") / _relative_id_path(task_id)


def chore_path(toolang_root: Path, agent_name: str, chore_id: str) -> Path:
    """Return one local chore path."""

    return _work_dir(toolang_root, agent_name, kind="chore") / _relative_id_path(chore_id)


def list_tasks(
    toolang_root: Path,
    agent_name: str,
    *,
    include_archived: bool = False,
) -> tuple[TaskEntry, ...]:
    """List local tasks for one agent."""

    items = list(_task_entries(toolang_root, agent_name, archived=False))
    if include_archived:
        items.extend(_task_entries(toolang_root, agent_name, archived=True))
    return tuple(sorted(items, key=lambda item: str(item.path)))


def list_archived_tasks(toolang_root: Path, agent_name: str) -> tuple[TaskEntry, ...]:
    """List archived local tasks for one agent."""

    return tuple(sorted(_task_entries(toolang_root, agent_name, archived=True), key=lambda item: str(item.path)))


def find_task(
    toolang_root: Path,
    agent_name: str,
    task_id: str,
    *,
    include_archived: bool = False,
) -> TaskEntry | None:
    """Find one local task by stable task id."""

    for entry in list_tasks(toolang_root, agent_name, include_archived=include_archived):
        if entry.document.task_id() == task_id:
            return entry
    return None


def find_archived_task(toolang_root: Path, agent_name: str, task_id: str) -> TaskEntry | None:
    """Find one archived local task by stable task id."""

    for entry in list_archived_tasks(toolang_root, agent_name):
        if entry.document.task_id() == task_id:
            return entry
    return None


def list_chores(
    toolang_root: Path,
    agent_name: str,
    *,
    include_archived: bool = False,
) -> tuple[ChoreEntry, ...]:
    """List local chores for one agent."""

    items = list(_chore_entries(toolang_root, agent_name, archived=False))
    if include_archived:
        items.extend(_chore_entries(toolang_root, agent_name, archived=True))
    return tuple(sorted(items, key=lambda item: str(item.path)))


def list_archived_chores(toolang_root: Path, agent_name: str) -> tuple[ChoreEntry, ...]:
    """List archived local chores for one agent."""

    return tuple(sorted(_chore_entries(toolang_root, agent_name, archived=True), key=lambda item: str(item.path)))


def find_chore(
    toolang_root: Path,
    agent_name: str,
    chore_id: str,
    *,
    include_archived: bool = False,
) -> ChoreEntry | None:
    """Find one local chore by stable chore id."""

    for entry in list_chores(toolang_root, agent_name, include_archived=include_archived):
        if entry.document.chore_id() == chore_id:
            return entry
    return None


def find_archived_chore(toolang_root: Path, agent_name: str, chore_id: str) -> ChoreEntry | None:
    """Find one archived local chore by stable chore id."""

    for entry in list_archived_chores(toolang_root, agent_name):
        if entry.document.chore_id() == chore_id:
            return entry
    return None


def load_task_text(toolang_root: Path, agent_name: str, task_id: str) -> str:
    """Load one task document."""

    entry = find_task(toolang_root, agent_name, task_id)
    if entry is None:
        raise FileNotFoundError(f"task not found: {task_id}")
    return entry.path.read_text(encoding="utf-8")


def load_chore_text(toolang_root: Path, agent_name: str, chore_id: str) -> str:
    """Load one chore document."""

    entry = find_chore(toolang_root, agent_name, chore_id)
    if entry is None:
        raise FileNotFoundError(f"chore not found: {chore_id}")
    return entry.path.read_text(encoding="utf-8")


def create_task_text(toolang_root: Path, agent_name: str, text: str) -> Path:
    """Create one validated task document with an auto-generated id path."""

    document = TaskFile.parse_text(text).model_copy(
        update={"id": allocate_job_id(toolang_root, agent_name)}
    )
    path = task_path(toolang_root, agent_name, document.task_id())
    document.save(path)
    return path


def create_chore_text(toolang_root: Path, agent_name: str, text: str) -> Path:
    """Create one validated chore document with an auto-generated id path."""

    document = ChoreFile.parse_text(text).model_copy(
        update={"id": allocate_job_id(toolang_root, agent_name)}
    )
    path = chore_path(toolang_root, agent_name, document.chore_id())
    document.save(path)
    return path


def update_task_text(toolang_root: Path, agent_name: str, task_id: str, text: str) -> Path:
    """Replace one task document, preserving its stable id."""

    entry = find_task(toolang_root, agent_name, task_id)
    if entry is None:
        raise FileNotFoundError(f"task not found: {task_id}")
    document = TaskFile.parse_text(text)
    document = _task_with_existing_id(document, task_id=entry.document.task_id())
    return save_task_entry(toolang_root, agent_name, entry, document)


def update_chore_text(toolang_root: Path, agent_name: str, chore_id: str, text: str) -> Path:
    """Replace one chore document, preserving its stable id."""

    entry = find_chore(toolang_root, agent_name, chore_id)
    if entry is None:
        raise FileNotFoundError(f"chore not found: {chore_id}")
    document = ChoreFile.parse_text(text)
    document = _chore_with_existing_id(document, chore_id=entry.document.chore_id())
    return save_chore_entry(toolang_root, agent_name, entry, document)


def save_task_entry(
    toolang_root: Path,
    agent_name: str,
    entry: TaskEntry,
    document: TaskFile,
) -> Path:
    """Save a task entry, moving it when its state changes archive placement."""

    document = _task_with_existing_id(document, task_id=entry.document.task_id())
    return _save_job_document(
        toolang_root,
        agent_name,
        kind="task",
        item_id=document.task_id(),
        current_path=entry.path,
        document=document,
    )


def save_chore_entry(
    toolang_root: Path,
    agent_name: str,
    entry: ChoreEntry,
    document: ChoreFile,
) -> Path:
    """Save a chore entry, moving it when its state changes archive placement."""

    document = _chore_with_existing_id(document, chore_id=entry.document.chore_id())
    return _save_job_document(
        toolang_root,
        agent_name,
        kind="chore",
        item_id=document.chore_id(),
        current_path=entry.path,
        document=document,
    )


def pause_task(toolang_root: Path, agent_name: str, task_id: str) -> Path | None:
    """Mark one task inactive."""

    entry = find_task(toolang_root, agent_name, task_id)
    if entry is None:
        return None
    document = entry.document.model_copy(update={"state": "inactive"})
    return save_task_entry(toolang_root, agent_name, entry, document)


def resume_task(toolang_root: Path, agent_name: str, task_id: str) -> Path | None:
    """Mark one task active."""

    entry = find_task(toolang_root, agent_name, task_id)
    if entry is None:
        return None
    document = entry.document.model_copy(update={"state": "active"})
    return save_task_entry(toolang_root, agent_name, entry, document)


def pause_chore(toolang_root: Path, agent_name: str, chore_id: str) -> Path | None:
    """Mark one chore inactive."""

    entry = find_chore(toolang_root, agent_name, chore_id)
    if entry is None:
        return None
    document = entry.document.model_copy(update={"state": "inactive"})
    return save_chore_entry(toolang_root, agent_name, entry, document)


def resume_chore(toolang_root: Path, agent_name: str, chore_id: str) -> Path | None:
    """Mark one chore active."""

    entry = find_chore(toolang_root, agent_name, chore_id)
    if entry is None:
        return None
    document = entry.document.model_copy(update={"state": "active"})
    return save_chore_entry(toolang_root, agent_name, entry, document)


def remove_task(toolang_root: Path, agent_name: str, task_id: str) -> bool:
    """Remove one task document."""

    entry = find_task(toolang_root, agent_name, task_id)
    if entry is None:
        return False
    entry.path.unlink()
    _prune_empty_parents(entry.path.parent, stop=_work_dir(toolang_root, agent_name, kind="task"))
    return True


def remove_chore(toolang_root: Path, agent_name: str, chore_id: str) -> bool:
    """Remove one chore document."""

    entry = find_chore(toolang_root, agent_name, chore_id)
    if entry is None:
        return False
    entry.path.unlink()
    _prune_empty_parents(entry.path.parent, stop=_work_dir(toolang_root, agent_name, kind="chore"))
    return True


def remove_archived_task(toolang_root: Path, agent_name: str, task_id: str) -> bool:
    """Remove one archived task document."""

    entry = find_archived_task(toolang_root, agent_name, task_id)
    if entry is None:
        return False
    entry.path.unlink()
    _prune_empty_parents(entry.path.parent, stop=_archive_dir(toolang_root, agent_name, kind="task"))
    return True


def remove_archived_chore(toolang_root: Path, agent_name: str, chore_id: str) -> bool:
    """Remove one archived chore document."""

    entry = find_archived_chore(toolang_root, agent_name, chore_id)
    if entry is None:
        return False
    entry.path.unlink()
    _prune_empty_parents(entry.path.parent, stop=_archive_dir(toolang_root, agent_name, kind="chore"))
    return True


def claim_task(path: Path) -> TaskFile:
    """Mark one task document as running."""

    task = TaskFile.load(path, persist_id=True)
    if not task.claimable():
        raise ValueError(f"task cannot be claimed: {path}")
    claimed = task.running()
    claimed.save(path)
    return claimed


def finish_task(
    toolang_root: Path,
    agent_name: str,
    task_id: str,
    *,
    succeeded: bool,
) -> Path | None:
    """Mark one task as finished without archiving it."""

    entry = find_task(toolang_root, agent_name, task_id)
    if entry is None:
        return None
    completed = entry.document.completed(succeeded=succeeded)
    completed.save(entry.path)
    return entry.path


def archive_task(toolang_root: Path, agent_name: str, task_id: str) -> Path | None:
    """Archive one task by id."""

    entry = find_task(toolang_root, agent_name, task_id)
    if entry is None:
        return None
    archived = entry.document.archived_copy()
    target = _archive_path(toolang_root, agent_name, kind="task", item_id=task_id)
    archived.save(target)
    entry.path.unlink(missing_ok=True)
    _prune_empty_parents(entry.path.parent, stop=_work_dir(toolang_root, agent_name, kind="task"))
    return target


def archive_chore(toolang_root: Path, agent_name: str, chore_id: str) -> Path | None:
    """Archive one active chore by id."""

    entry = find_chore(toolang_root, agent_name, chore_id)
    if entry is None:
        return None
    archived = entry.document.archived_copy()
    target = _archive_path(toolang_root, agent_name, kind="chore", item_id=chore_id)
    archived.save(target)
    entry.path.unlink(missing_ok=True)
    _prune_empty_parents(entry.path.parent, stop=_work_dir(toolang_root, agent_name, kind="chore"))
    return target


def unarchive_task(
    toolang_root: Path,
    agent_name: str,
    task_id: str,
    *,
    state: Literal["active", "inactive"] = "active",
    stage: TaskStage | None = None,
) -> Path | None:
    """Move one archived task back to the active task directory."""

    entry = find_archived_task(toolang_root, agent_name, task_id)
    if entry is None:
        return None
    updates: dict[str, object] = {"state": state}
    if stage is not None:
        updates["stage"] = stage
    document = entry.document.model_copy(update=updates)
    return save_task_entry(toolang_root, agent_name, entry, document)


def unarchive_chore(
    toolang_root: Path,
    agent_name: str,
    chore_id: str,
    *,
    state: Literal["active", "inactive"] = "active",
) -> Path | None:
    """Move one archived chore back to the active chore directory."""

    entry = find_archived_chore(toolang_root, agent_name, chore_id)
    if entry is None:
        return None
    document = entry.document.model_copy(update={"state": state})
    return save_chore_entry(toolang_root, agent_name, entry, document)


def _task_entries(toolang_root: Path, agent_name: str, *, archived: bool) -> Iterable[TaskEntry]:
    root = _archive_dir(toolang_root, agent_name, kind="task") if archived else _work_dir(toolang_root, agent_name, kind="task")
    if not root.exists():
        return ()
    return tuple(
        TaskEntry(
            name=str(path.relative_to(root).with_suffix("")),
            path=path,
            document=TaskFile.load(
                path,
                id_factory=(None if archived else lambda: allocate_job_id(toolang_root, agent_name)),
                persist_id=True,
                archived=archived,
            ),
        )
        for path in sorted(root.rglob("*.md"))
    )


def _chore_entries(toolang_root: Path, agent_name: str, *, archived: bool) -> Iterable[ChoreEntry]:
    root = _archive_dir(toolang_root, agent_name, kind="chore") if archived else _work_dir(toolang_root, agent_name, kind="chore")
    if not root.exists():
        return ()
    return tuple(
        ChoreEntry(
            name=str(path.relative_to(root).with_suffix("")),
            path=path,
            document=ChoreFile.load(
                path,
                id_factory=(None if archived else lambda: allocate_job_id(toolang_root, agent_name)),
                persist_id=True,
                archived=archived,
            ),
        )
        for path in sorted(root.rglob("*.md"))
    )


def _work_dir(toolang_root: Path, agent_name: str, *, kind: Literal["task", "chore"]) -> Path:
    return toolang_root / "agents" / agent_name / ("tasks" if kind == "task" else "chores")


def _archive_dir(toolang_root: Path, agent_name: str, *, kind: Literal["task", "chore"]) -> Path:
    return toolang_root / "agents" / agent_name / "archive" / ("tasks" if kind == "task" else "chores")


def _archive_path(
    toolang_root: Path,
    agent_name: str,
    *,
    kind: Literal["task", "chore"],
    item_id: str,
) -> Path:
    return _archive_dir(toolang_root, agent_name, kind=kind) / _archive_bucket(item_id) / f"{item_id}.md"


def _job_path_for_state(
    toolang_root: Path,
    agent_name: str,
    *,
    kind: Literal["task", "chore"],
    item_id: str,
    state: JobState,
) -> Path:
    if state == "archived":
        return _archive_path(toolang_root, agent_name, kind=kind, item_id=item_id)
    if kind == "task":
        return task_path(toolang_root, agent_name, item_id)
    return chore_path(toolang_root, agent_name, item_id)


def _save_job_document(
    toolang_root: Path,
    agent_name: str,
    *,
    kind: Literal["task", "chore"],
    item_id: str,
    current_path: Path,
    document: TaskFile | ChoreFile,
) -> Path:
    target = _job_path_for_state(
        toolang_root,
        agent_name,
        kind=kind,
        item_id=item_id,
        state=document.state,
    )
    document.save(target)
    if current_path != target:
        current_path.unlink(missing_ok=True)
        _prune_empty_parents(
            current_path.parent,
            stop=_job_parent_root(toolang_root, agent_name, kind=kind, path=current_path),
        )
    return target


def _job_parent_root(
    toolang_root: Path,
    agent_name: str,
    *,
    kind: Literal["task", "chore"],
    path: Path,
) -> Path:
    archive = _archive_dir(toolang_root, agent_name, kind=kind)
    try:
        path.relative_to(archive)
    except ValueError:
        return _work_dir(toolang_root, agent_name, kind=kind)
    return archive


def _archive_bucket(item_id: str) -> str:
    try:
        started_at = decode_id(item_id, family=LOCAL_ID_FAMILY).bucket_started_at
    except ValueError:
        return (item_id[:4] or "legacy").lower()
    return started_at.astimezone(timezone.utc).strftime("%Y%m%dT%HZ")


def _relative_id_path(value: str) -> Path:
    text = value.strip()
    if not text or "/" in text or "\\" in text or text in {".", ".."}:
        raise ValueError(f"invalid id: {value}")
    return Path(text).with_suffix(".md")


def _task_with_existing_id(document: TaskFile, *, task_id: str) -> TaskFile:
    if document.id is None:
        return document.model_copy(update={"id": task_id})
    if document.id != task_id:
        raise ValueError(f"task id cannot be changed: {task_id} -> {document.id}")
    return document


def _chore_with_existing_id(document: ChoreFile, *, chore_id: str) -> ChoreFile:
    if document.id is None:
        return document.model_copy(update={"id": chore_id})
    if document.id != chore_id:
        raise ValueError(f"chore id cannot be changed: {chore_id} -> {document.id}")
    return document


def _prune_empty_parents(path: Path, *, stop: Path) -> None:
    current = path
    while current != stop and current.exists():
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _id_state_path(toolang_root: Path, agent_name: str) -> Path:
    return toolang_root / "agents" / agent_name / ".runtime" / "ids.json"


def allocate_job_id(toolang_root: Path, agent_name: str) -> str:
    """Allocate one local job id for an agent."""

    return allocate_id(
        _id_state_path(toolang_root, agent_name),
        family=LOCAL_ID_FAMILY,
        exists=lambda value: _job_id_exists(toolang_root, agent_name, value),
    ).value


def _job_id_exists(toolang_root: Path, agent_name: str, value: str) -> bool:
    for path in _job_document_paths(toolang_root, agent_name):
        try:
            post = frontmatter.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if path.stem == value:
            return True
        if str(post.metadata.get("id", "")).strip() == value:
            return True
    return False


def _job_document_paths(toolang_root: Path, agent_name: str) -> Iterable[Path]:
    roots = (
        _work_dir(toolang_root, agent_name, kind="task"),
        _work_dir(toolang_root, agent_name, kind="chore"),
        _archive_dir(toolang_root, agent_name, kind="task"),
        _archive_dir(toolang_root, agent_name, kind="chore"),
    )
    for root in roots:
        if not root.exists():
            continue
        yield from sorted(root.rglob("*.md"))


def _display_title(title: str | None, body: str, *, fallback: str) -> str:
    if title is not None and title.strip():
        return title.strip()
    for line in body.splitlines():
        text = line.strip().lstrip("#").strip()
        if text:
            return text[:80]
    return fallback.strip()
