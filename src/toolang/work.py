"""Local task and chore document helpers."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Literal

from dateutil.rrule import rrulestr
import frontmatter
from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from .ids import LOCAL_ID_FAMILY, allocate_id, decode_id

JobState = Literal["active", "inactive", "archived"]
TaskStatus = Literal["todo", "running", "done", "failed"]
DEFAULT_CHORE_SCHEDULE = "FREQ=HOURLY;INTERVAL=1"
_TASK_STATUS_ALIASES = {
    "doing": "running",
    "in_progress": "running",
    "cancelled": "failed",
    "canceled": "failed",
}


class _MarkdownDocument(BaseModel):
    """Base model for one markdown work document."""

    model_config = ConfigDict(extra="ignore")

    state: JobState = "active"
    body: str = ""

    @model_validator(mode="before")
    @classmethod
    def _normalize_legacy_state(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        paused = data.pop("paused", None)
        if "state" not in data and paused is True:
            data["state"] = "inactive"
        return data

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
    status: TaskStatus = "todo"

    @field_validator("id", mode="before")
    @classmethod
    def _normalize_id(cls, value: object) -> object:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @field_validator("status", mode="before")
    @classmethod
    def _normalize_status(cls, value: object) -> object:
        if value is None:
            return "todo"
        text = str(value).strip().lower()
        return _TASK_STATUS_ALIASES.get(text, text or "todo")

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

        return self.state == "active" and self.status == "todo"

    def running(self) -> "TaskFile":
        """Return this task marked as claimed."""

        return self.model_copy(update={"status": "running"})

    def completed(self, *, succeeded: bool) -> "TaskFile":
        """Return this task marked as completed."""

        return self.model_copy(update={"status": "done" if succeeded else "failed"})

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
        if self.status != "todo":
            metadata["status"] = self.status
        return metadata


class ChoreFile(_MarkdownDocument):
    """One local chore document."""

    id: str | None = None
    title: str | None = None
    schedule: str = DEFAULT_CHORE_SCHEDULE

    @model_validator(mode="before")
    @classmethod
    def _normalize_legacy_schedule(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        rrule = data.pop("rrule", None)
        if "schedule" not in data and rrule is not None:
            data["schedule"] = rrule
        return data

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


def task_terminal(status: TaskStatus) -> bool:
    """Return whether one task status is terminal."""

    return status in {"done", "failed"}


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


def task_path(toolang_root: Path, agent_name: str, task_name: str) -> Path:
    """Return one local task path."""

    return _work_dir(toolang_root, agent_name, kind="task") / _relative_name(task_name).with_suffix(".md")


def chore_path(toolang_root: Path, agent_name: str, chore_name: str) -> Path:
    """Return one local chore path."""

    return _work_dir(toolang_root, agent_name, kind="chore") / _relative_name(chore_name).with_suffix(".md")


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


def load_task_text(toolang_root: Path, agent_name: str, task_name: str) -> str:
    """Load one task document."""

    path = task_path(toolang_root, agent_name, task_name)
    if not path.is_file():
        raise FileNotFoundError(f"task not found: {task_name}")
    return path.read_text(encoding="utf-8")


def load_chore_text(toolang_root: Path, agent_name: str, chore_name: str) -> str:
    """Load one chore document."""

    path = chore_path(toolang_root, agent_name, chore_name)
    if not path.is_file():
        raise FileNotFoundError(f"chore not found: {chore_name}")
    return path.read_text(encoding="utf-8")


def put_task_text(toolang_root: Path, agent_name: str, task_name: str, text: str) -> Path:
    """Create or replace one validated task document."""

    path = task_path(toolang_root, agent_name, task_name)
    document = TaskFile.parse_text(text).with_id(lambda: allocate_job_id(toolang_root, agent_name))
    document.save(path)
    return path


def put_chore_text(toolang_root: Path, agent_name: str, chore_name: str, text: str) -> Path:
    """Create or replace one validated chore document."""

    path = chore_path(toolang_root, agent_name, chore_name)
    document = ChoreFile.parse_text(text).with_id(lambda: allocate_job_id(toolang_root, agent_name))
    document.save(path)
    return path


def remove_task(toolang_root: Path, agent_name: str, task_name: str) -> bool:
    """Remove one task document."""

    path = task_path(toolang_root, agent_name, task_name)
    if not path.exists():
        return False
    path.unlink()
    _prune_empty_parents(path.parent, stop=_work_dir(toolang_root, agent_name, kind="task"))
    return True


def remove_chore(toolang_root: Path, agent_name: str, chore_name: str) -> bool:
    """Remove one chore document."""

    path = chore_path(toolang_root, agent_name, chore_name)
    if not path.exists():
        return False
    path.unlink()
    _prune_empty_parents(path.parent, stop=_work_dir(toolang_root, agent_name, kind="chore"))
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
    """Mark one task as finished and archive successful tasks."""

    entry = find_task(toolang_root, agent_name, task_id)
    if entry is None:
        return None
    completed = entry.document.completed(succeeded=succeeded)
    if not succeeded:
        completed.save(entry.path)
        return entry.path
    archived = completed.archived_copy()
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


def _archive_bucket(item_id: str) -> str:
    try:
        started_at = decode_id(item_id, family=LOCAL_ID_FAMILY).bucket_started_at
    except ValueError:
        return (item_id[:4] or "legacy").lower()
    return started_at.astimezone(timezone.utc).strftime("%Y%m%dT%HZ")


def _relative_name(name: str) -> Path:
    relative = Path(name)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"invalid name: {name}")
    return relative


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
