"""Persisted local work documents for tasks, chores, and will."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import frontmatter
from pydantic import BaseModel, ConfigDict, Field


TaskStatus = Literal["open", "doing", "done", "cancelled"]


class _MarkdownDocument(BaseModel):
    """Base model for one markdown work document with front matter."""

    model_config = ConfigDict(extra="allow")

    title: str | None = None
    thunk: str | None = None
    model: str | None = None
    thread_id: str | None = None
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

    status: TaskStatus = "open"
    assignee: str | None = None

    @classmethod
    def load(cls, path: Path) -> "TaskFile":
        """Load one task document from disk."""

        loaded = cls._load_markdown(path)
        return cls.model_validate(loaded.model_dump(mode="python"))

    def save(self, path: Path) -> None:
        """Write this task document to disk."""

        self._save_markdown(path)


class ChoreFile(_MarkdownDocument):
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


class WillFile(_MarkdownDocument):
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
