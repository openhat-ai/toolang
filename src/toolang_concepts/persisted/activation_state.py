"""Persisted state for one running agent activation."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field

from toolang_concepts.sandbox import SandboxState


class ActivationState(BaseModel):
    """Persisted local state for one active agent activation."""

    version: int = 1
    agent_uri: str
    agent_id: str
    agent_name: str
    agent_home: str
    source_file: str
    pid: int
    status: str
    endpoint: str | None = None
    started_at: datetime
    heartbeat_at: datetime
    sandbox: SandboxState = Field(default_factory=SandboxState)

    @classmethod
    def load(cls, path: Path) -> "ActivationState":
        """Load one activation-state document from disk."""

        return cls.model_validate_json(path.read_text(encoding="utf-8"))

    def save(self, path: Path) -> None:
        """Write this activation-state document to disk."""

        path.write_text(
            self.model_dump_json(indent=2, exclude_none=True),
            encoding="utf-8",
        )
