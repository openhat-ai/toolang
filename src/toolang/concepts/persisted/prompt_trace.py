"""Trace file written for one completed or failed prompt build."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class PromptTrace(BaseModel):
    """Persisted prompt-build trace for one run."""

    version: int = 1
    run_id: str
    created_at: datetime
    agent_uri: str
    agent_id: str
    agent_name: str
    source_file: str
    working_directory: str
    thunk_name: str | None = None
    origin: str
    thread_id: str | None = None
    sandbox: str
    cap_scopes: list[str] = Field(default_factory=list)
    model: str
    raw_input: str | None = None
    expanded_input: str | None = None
    message_context: dict[str, Any] | None = None
    runtime_context: dict[str, Any] = Field(default_factory=dict)
    developer_message: str
    messages: list[dict[str, Any]] = Field(default_factory=list)
    source_text: str
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    response_text: str | None = None
    error: str | None = None

    @classmethod
    def load(cls, path: Path) -> "PromptTrace":
        """Load one prompt trace document from disk."""

        return cls.model_validate_json(path.read_text(encoding="utf-8"))

    def save(self, path: Path) -> None:
        """Write this prompt trace document to disk."""

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            self.model_dump_json(indent=2, exclude_none=True),
            encoding="utf-8",
        )
