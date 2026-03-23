"""Persisted scheduler-side state for the pulse runtime loop."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field


class PulseItemState(BaseModel):
    """Stored pulse state for one tracked work item."""

    content_hash: str | None = None
    last_enqueued_at: datetime | None = None
    next_due_at: datetime | None = None


class PulseState(BaseModel):
    """Stored pulse-loop state for local tasks, chores, and will."""

    tasks: dict[str, PulseItemState] = Field(default_factory=dict)
    chores: dict[str, PulseItemState] = Field(default_factory=dict)
    will: PulseItemState = Field(default_factory=PulseItemState)

    @classmethod
    def load(cls, path: Path) -> "PulseState":
        """Load one pulse-state document from disk."""

        return cls.model_validate_json(path.read_text(encoding="utf-8"))

    def save(self, path: Path) -> None:
        """Write this pulse-state document to disk."""

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            self.model_dump_json(indent=2, exclude_none=True),
            encoding="utf-8",
        )
