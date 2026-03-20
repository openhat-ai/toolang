from __future__ import annotations

from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field


class SandboxRunInfo(BaseModel):
    pid: int | None = None
    port: int | None = None


class SandboxInfo(BaseModel):
    type: str = "host"
    container_name: str | None = None
    image_name: str | None = None
    run: SandboxRunInfo | None = None

    def spec(self) -> str:
        if self.type == "docker" and self.image_name:
            return f"docker:{self.image_name}"
        return self.type or "host"


class AgentRunState(BaseModel):
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
    sandbox: SandboxInfo = Field(default_factory=SandboxInfo)

    @classmethod
    def load(cls, path: Path) -> "AgentRunState":
        return cls.model_validate_json(path.read_text(encoding="utf-8"))

    def save(self, path: Path) -> None:
        path.write_text(
            self.model_dump_json(indent=2, exclude_none=True),
            encoding="utf-8",
        )
