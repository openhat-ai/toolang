"""Persisted origin metadata for visiting agents."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from toolang.concepts.identity import AgentKind, AgentRef


class AgentOriginState(BaseModel):
    """Persisted origin metadata for one visiting agent."""

    version: int = 1
    kind: AgentKind
    name: str
    uri: str

    @classmethod
    def from_agent(cls, agent: AgentRef) -> "AgentOriginState":
        """Build one origin record from a resolved visiting agent."""

        if agent.kind != "visiting":
            raise ValueError("AgentOriginState only applies to visiting agents.")
        return cls(kind=agent.kind, name=agent.name, uri=agent.uri)

    @classmethod
    def load(cls, path: Path) -> "AgentOriginState":
        """Load one origin-state document from disk."""

        return cls.model_validate_json(path.read_text(encoding="utf-8"))

    def save(self, path: Path) -> None:
        """Write this origin-state document to disk."""

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            self.model_dump_json(indent=2, exclude_none=True),
            encoding="utf-8",
        )
