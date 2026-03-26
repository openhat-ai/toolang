"""Persisted local work documents for tasks, chores, and will."""

from __future__ import annotations

import base64
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from dateutil.rrule import rrulestr
import frontmatter
from pydantic import BaseModel, ConfigDict, field_validator


TaskStatus = Literal["todo", "doing", "done", "cancelled"]
DEFAULT_SCHEDULE_RRULE = "FREQ=MINUTELY;INTERVAL=5"


class _MarkdownDocument(BaseModel):
    """Base model for one markdown work document with front matter."""

    model_config = ConfigDict(extra="allow")

    paused: bool = False
    body: str = ""

    @classmethod
    def _load_markdown(cls, path: Path) -> "_MarkdownDocument":
        post = frontmatter.loads(path.read_text(encoding="utf-8"))
        return cls.model_validate({**dict(post.metadata), "body": post.content})

    def _save_markdown(self, path: Path) -> None:
        metadata = self.model_dump(
            mode="python",
            exclude={"body"},
            exclude_none=True,
        )
        post = frontmatter.Post(self.body, **metadata)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(frontmatter.dumps(post), encoding="utf-8")

    def content_hash(self) -> str:
        """Return one stable textual signature for this document."""

        metadata = self.model_dump(
            mode="python",
            exclude={"body"},
            exclude_none=True,
        )
        return work_content_hash(metadata=metadata, body=self.body)


class TaskFile(_MarkdownDocument):
    """One durable local task document."""

    id: str | None = None
    requester: str = "owner"
    status: TaskStatus = "todo"

    @classmethod
    def load(cls, path: Path, *, persist_id: bool = False) -> "TaskFile":
        """Load one task document from disk."""

        loaded = cls._load_markdown(path)
        document = cls.model_validate(loaded.model_dump(mode="python")).with_id()
        if persist_id and document.id != getattr(loaded, "id", None):
            document.save(path)
        return document

    def with_id(self) -> "TaskFile":
        """Return this task document with one stable short id."""

        text = (self.id or "").strip()
        if text:
            if text == self.id:
                return self
            return self.model_copy(update={"id": text})
        return self.model_copy(update={"id": generate_task_id()})

    def save(self, path: Path) -> None:
        """Write this task document to disk."""

        self.with_id()._save_markdown(path)

    def thread_id(self) -> str:
        """Return the canonical runtime thread id for this task."""

        return f"task:local:{self.task_id()}"

    def task_id(self) -> str:
        """Return the stable task id for this document."""

        return str(self.with_id().id)

    def render_input(self, *, fallback_name: str) -> str:
        """Return the textual prompt input for this task document."""

        body = self.body.strip()
        if body:
            return body
        return fallback_name.strip()


class _ScheduledDocument(_MarkdownDocument):
    """Base model for scheduled local work documents."""

    title: str | None = None
    rrule: str = DEFAULT_SCHEDULE_RRULE

    @field_validator("rrule", mode="before")
    @classmethod
    def _normalize_rrule(cls, value: object) -> str:
        return normalize_rrule(
            interval_sec_to_rrule(value) if isinstance(value, int) else value
        )

    def render_input(self, *, fallback_title: str) -> str:
        """Return the textual prompt input for this work document."""

        title = (self.title or "").strip() or fallback_title.strip()
        body = self.body.strip()
        if title and body:
            return f"# {title}\n\n{body}"
        if body:
            return body
        return title

class ChoreFile(_ScheduledDocument):
    """One scheduled local chore document."""

    @classmethod
    def load(cls, path: Path) -> "ChoreFile":
        """Load one chore document from disk."""

        loaded = cls._load_markdown(path)
        return cls.model_validate(_scheduled_document_payload(loaded))

    def save(self, path: Path) -> None:
        """Write this chore document to disk."""

        self._save_markdown(path)


class WillFile(_ScheduledDocument):
    """One long-lived local will document."""

    @classmethod
    def load(cls, path: Path) -> "WillFile":
        """Load one will document from disk."""

        loaded = cls._load_markdown(path)
        return cls.model_validate(_scheduled_document_payload(loaded))

    def save(self, path: Path) -> None:
        """Write this will document to disk."""

        self._save_markdown(path)


def task_terminal(status: TaskStatus) -> bool:
    """Return whether one task status is terminal."""

    return status in {"done", "cancelled"}


def work_content_hash(*, metadata: dict[str, Any], body: str) -> str:
    """Return one stable textual signature for a work document."""

    import hashlib
    import json

    payload = json.dumps(
        {"metadata": metadata, "body": body},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def normalize_rrule(value: object) -> str:
    """Return one validated RRULE string."""

    text = str(value or "").strip()
    candidate = text or DEFAULT_SCHEDULE_RRULE
    rrulestr(candidate, dtstart=datetime(2026, 1, 1, tzinfo=timezone.utc))
    return candidate


def next_scheduled_at(
    rrule_text: str,
    *,
    anchor: datetime,
    not_before: datetime,
    inclusive: bool,
) -> datetime | None:
    """Return the next occurrence for one RRULE, normalized to UTC."""

    anchor_utc = _as_utc(anchor)
    floor_utc = _as_utc(not_before)
    schedule = rrulestr(normalize_rrule(rrule_text), dtstart=anchor_utc)
    candidate = schedule.after(floor_utc, inc=inclusive)
    if candidate is None:
        return None
    return _as_utc(candidate)


def interval_sec_to_rrule(value: object) -> str:
    """Convert one legacy interval value into an equivalent RRULE."""

    try:
        seconds = int(str(value).strip())
    except (TypeError, ValueError):
        return DEFAULT_SCHEDULE_RRULE
    if seconds <= 0:
        return DEFAULT_SCHEDULE_RRULE
    if seconds % 3600 == 0:
        return f"FREQ=HOURLY;INTERVAL={seconds // 3600}"
    if seconds % 60 == 0:
        return f"FREQ=MINUTELY;INTERVAL={seconds // 60}"
    return f"FREQ=SECONDLY;INTERVAL={seconds}"


def generate_task_id() -> str:
    """Return one short local task id."""

    return base64.b32encode(secrets.token_bytes(5)).decode("ascii").lower()


def _scheduled_document_payload(loaded: _MarkdownDocument) -> dict[str, Any]:
    data = loaded.model_dump(mode="python")
    legacy_interval_sec = data.pop("interval_sec", None)
    data.pop("thread_id", None)
    data.pop("thunk", None)
    data.pop("model", None)
    if not data.get("rrule"):
        data["rrule"] = interval_sec_to_rrule(legacy_interval_sec)
    return data


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def task_id_from_thread_id(thread_id: str) -> str | None:
    """Extract one local task id from a canonical local task thread id."""

    prefix = "task:local:"
    if not thread_id.startswith(prefix):
        return None
    task_id = thread_id.removeprefix(prefix).strip()
    return task_id or None


def find_local_task(root: Path, task_id: str) -> tuple[Path, TaskFile] | None:
    """Find one local task file by stable task id."""

    if not root.exists():
        return None
    for path in sorted(root.rglob("*.md")):
        if not path.is_file():
            continue
        task = TaskFile.load(path, persist_id=True)
        if task.task_id() == task_id:
            return path, task
    return None
