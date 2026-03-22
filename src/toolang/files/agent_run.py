"""Stored state for one running agent process."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field


class SandboxRuntimeInfo(BaseModel):
    """Runtime details reported by the active sandbox process."""

    pid: int | None = None
    port: int | None = None


class SandboxState(BaseModel):
    """Sandbox identity and runtime data for one running agent."""

    type: str = "host"
    container_name: str | None = None
    image_name: str | None = None
    run: SandboxRuntimeInfo | None = None

    def spec(self) -> str:
        """Return the canonical sandbox spec string for persisted state."""

        if self.type == "docker" and self.image_name:
            return f"docker:{self.image_name}"
        return self.type or "host"


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
        """Load one agent run state document from disk."""

        return cls.model_validate_json(path.read_text(encoding="utf-8"))

    def save(self, path: Path) -> None:
        """Write this agent run state document to disk."""

        path.write_text(
            self.model_dump_json(indent=2, exclude_none=True),
            encoding="utf-8",
        )
