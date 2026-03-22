"""Persisted polling state for one channel binding."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class PollState(BaseModel):
    """Persisted poll cursor and plugin-owned metadata."""

    cursor: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "PollState":
        """Load one poll-state document from disk."""

        return cls.model_validate_json(path.read_text(encoding="utf-8"))

    def save(self, path: Path) -> None:
        """Write this poll-state document to disk."""

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            self.model_dump_json(indent=2, exclude_none=True),
            encoding="utf-8",
        )
