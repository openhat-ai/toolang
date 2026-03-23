"""Persisted local work documents for tasks, chores, and will."""

from __future__ import annotations

import base64
import secrets
from pathlib import Path
from typing import Any, Literal

import frontmatter
from pydantic import BaseModel, ConfigDict, Field


TaskStatus = Literal["todo", "doing", "done", "cancelled"]


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
    thunk: str | None = None
    model: str | None = None
    thread_id: str | None = None

    def render_input(self, *, fallback_title: str) -> str:
        """Return the textual prompt input for this work document."""

        title = (self.title or "").strip() or fallback_title.strip()
        body = self.body.strip()
        if title and body:
            return f"# {title}\n\n{body}"
        if body:
            return body
        return title

    def effective_thread_id(self, default_thread_id: str) -> str:
        """Return the explicit thread id or one call-site default."""

        text = (self.thread_id or "").strip()
        return text or default_thread_id


class ChoreFile(_ScheduledDocument):
    """One scheduled local chore document."""

    interval_sec: int = Field(default=300, ge=1)

    @classmethod
    def load(cls, path: Path) -> "ChoreFile":
        """Load one chore document from disk."""

        loaded = cls._load_markdown(path)
        return cls.model_validate(loaded.model_dump(mode="python"))

    def save(self, path: Path) -> None:
        """Write this chore document to disk."""

        self._save_markdown(path)


class WillFile(_ScheduledDocument):
    """One long-lived local will document."""

    interval_sec: int = Field(default=300, ge=1)

    @classmethod
    def load(cls, path: Path) -> "WillFile":
        """Load one will document from disk."""

        loaded = cls._load_markdown(path)
        return cls.model_validate(loaded.model_dump(mode="python"))

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


def generate_task_id() -> str:
    """Return one short local task id."""

    return base64.b32encode(secrets.token_bytes(5)).decode("ascii").lower()
