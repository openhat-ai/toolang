"""Local task and chore document helpers."""

from __future__ import annotations

import base64
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Literal

from dateutil.rrule import rrulestr
import frontmatter
from pydantic import BaseModel, ConfigDict, field_validator


TaskStatus = Literal["todo", "doing", "done", "cancelled"]
DEFAULT_CHORE_RRULE = "FREQ=HOURLY;INTERVAL=1"
_TASK_STATUS_ALIASES = {
    "in_progress": "doing",
}


class _MarkdownDocument(BaseModel):
    """Base model for one markdown work document."""

    model_config = ConfigDict(extra="allow")

    paused: bool = False
    body: str = ""

    @classmethod
    def _load_markdown(cls, path: Path) -> "_MarkdownDocument":
        post = frontmatter.loads(path.read_text(encoding="utf-8"))
        return cls.model_validate({**dict(post.metadata), "body": post.content})

    @classmethod
    def _parse_markdown(cls, text: str) -> "_MarkdownDocument":
        post = frontmatter.loads(text)
        return cls.model_validate({**dict(post.metadata), "body": post.content})

    def _save_markdown(self, path: Path) -> None:
        metadata = self.model_dump(mode="python", exclude={"body"}, exclude_none=True)
        post = frontmatter.Post(self.body, **metadata)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(frontmatter.dumps(post), encoding="utf-8")

    def content_hash(self) -> str:
        """Return one stable textual signature for this document."""

        metadata = self.model_dump(mode="python", exclude={"body"}, exclude_none=True)
        payload = json.dumps(
            {"metadata": metadata, "body": self.body},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class TaskFile(_MarkdownDocument):
    """One local task document."""

    id: str | None = None
    requester: str = "owner"
    status: TaskStatus = "todo"

    @field_validator("status", mode="before")
    @classmethod
    def _normalize_status(cls, value: object) -> object:
        if value is None:
            return None
        text = str(value).strip().lower()
        return _TASK_STATUS_ALIASES.get(text, text)

    @classmethod
    def load(cls, path: Path, *, persist_id: bool = False) -> "TaskFile":
        loaded = cls._load_markdown(path)
        document = cls.model_validate(loaded.model_dump(mode="python")).with_id()
        if persist_id and document.id != getattr(loaded, "id", None):
            document.save(path)
        return document

    @classmethod
    def parse_text(cls, text: str) -> "TaskFile":
        loaded = cls._parse_markdown(text)
        return cls.model_validate(loaded.model_dump(mode="python")).with_id()

    def with_id(self) -> "TaskFile":
        text = (self.id or "").strip()
        if text:
            if text == self.id:
                return self
            return self.model_copy(update={"id": text})
        return self.model_copy(update={"id": _generate_task_id()})

    def save(self, path: Path) -> None:
        self.with_id()._save_markdown(path)

    def task_id(self) -> str:
        return str(self.with_id().id)

    def thread_id(self) -> str:
        return f"task:local:{self.task_id()}"

    def render_input(self, *, fallback_name: str) -> str:
        """Return the prompt input for this task."""

        body = self.body.strip()
        if body:
            return body
        return fallback_name.strip()


class ChoreFile(_MarkdownDocument):
    """One local chore document."""

    title: str | None = None
    rrule: str = DEFAULT_CHORE_RRULE

    @field_validator("rrule", mode="before")
    @classmethod
    def _normalize_rrule(cls, value: object) -> str:
        text = str(value or "").strip() or DEFAULT_CHORE_RRULE
        rrulestr(text, dtstart=datetime(2026, 1, 1, tzinfo=timezone.utc))
        return text

    @classmethod
    def load(cls, path: Path) -> "ChoreFile":
        loaded = cls._load_markdown(path)
        return cls.model_validate(loaded.model_dump(mode="python"))

    @classmethod
    def parse_text(cls, text: str) -> "ChoreFile":
        loaded = cls._parse_markdown(text)
        return cls.model_validate(loaded.model_dump(mode="python"))

    def save(self, path: Path) -> None:
        self._save_markdown(path)

    def render_input(self, *, fallback_title: str) -> str:
        """Return the prompt input for this chore."""

        title = (self.title or "").strip() or fallback_title.strip()
        body = self.body.strip()
        if title and body:
            return f"# {title}\n\n{body}"
        if body:
            return body
        return title


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

    return status in {"done", "cancelled"}


def next_scheduled_at(
    rrule_text: str,
    *,
    anchor: datetime,
    not_before: datetime,
    inclusive: bool,
) -> datetime | None:
    """Return the next UTC occurrence for one RRULE."""

    anchor_utc = _as_utc(anchor)
    floor_utc = _as_utc(not_before)
    schedule = rrulestr(rrule_text, dtstart=anchor_utc)
    candidate = schedule.after(floor_utc, inc=inclusive)
    if candidate is None:
        return None
    return _as_utc(candidate)


def task_id_from_thread_id(thread_id: str) -> str | None:
    """Extract one local task id from its canonical thread id."""

    prefix = "task:local:"
    if not thread_id.startswith(prefix):
        return None
    task_id = thread_id.removeprefix(prefix).strip()
    return task_id or None


def task_path(toolang_root: Path, agent_name: str, task_name: str) -> Path:
    """Return one local task path."""

    return _work_dir(toolang_root, agent_name, kind="task") / _relative_name(task_name).with_suffix(".md")


def chore_path(toolang_root: Path, agent_name: str, chore_name: str) -> Path:
    """Return one local chore path."""

    return _work_dir(toolang_root, agent_name, kind="chore") / _relative_name(chore_name).with_suffix(".md")


def list_tasks(toolang_root: Path, agent_name: str) -> tuple[TaskEntry, ...]:
    """List local tasks for one agent."""

    root = _work_dir(toolang_root, agent_name, kind="task")
    if not root.exists():
        return ()
    items = [
        TaskEntry(
            name=str(path.relative_to(root).with_suffix("")),
            path=path,
            document=TaskFile.load(path, persist_id=True),
        )
        for path in sorted(root.rglob("*.md"))
    ]
    return tuple(items)


def find_task(toolang_root: Path, agent_name: str, task_id: str) -> TaskEntry | None:
    """Find one local task by stable task id."""

    root = _work_dir(toolang_root, agent_name, kind="task")
    if not root.exists():
        return None
    for path in sorted(root.rglob("*.md")):
        document = TaskFile.load(path, persist_id=True)
        if document.task_id() == task_id:
            return TaskEntry(
                name=str(path.relative_to(root).with_suffix("")),
                path=path,
                document=document,
            )
    return None


def list_chores(toolang_root: Path, agent_name: str) -> tuple[ChoreEntry, ...]:
    """List local chores for one agent."""

    root = _work_dir(toolang_root, agent_name, kind="chore")
    if not root.exists():
        return ()
    items = [
        ChoreEntry(
            name=str(path.relative_to(root).with_suffix("")),
            path=path,
            document=ChoreFile.load(path),
        )
        for path in sorted(root.rglob("*.md"))
    ]
    return tuple(items)


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
    TaskFile.parse_text(text).save(path)
    return path


def put_chore_text(toolang_root: Path, agent_name: str, chore_name: str, text: str) -> Path:
    """Create or replace one validated chore document."""

    path = chore_path(toolang_root, agent_name, chore_name)
    ChoreFile.parse_text(text).save(path)
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


def _work_dir(toolang_root: Path, agent_name: str, *, kind: Literal["task", "chore"]) -> Path:
    return toolang_root / "agents" / agent_name / ("tasks" if kind == "task" else "chores")


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


def _generate_task_id() -> str:
    return base64.b32encode(secrets.token_bytes(5)).decode("ascii").lower()
