"""Shared tool value types."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """One model-facing tool definition."""

    name: str
    description: str
    parameters: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_data(cls, payload: Mapping[str, Any]) -> ToolDefinition:
        name = payload.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError("tool definition name must be non-empty text")
        description = payload.get("description")
        if not isinstance(description, str):
            raise ValueError("tool definition description must be text")
        parameters = payload.get("parameters", {})
        if not isinstance(parameters, Mapping):
            raise ValueError("tool definition parameters must be an object")
        return cls(
            name=name,
            description=description,
            parameters=dict(parameters),
        )

    def to_data(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": dict(self.parameters),
        }


@dataclass(frozen=True, slots=True)
class ToolService:
    """One effective service exposed to tools for an immutable run."""

    name: str
    meta: Mapping[str, object]
    environ: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "meta", MappingProxyType(dict(self.meta)))
        object.__setattr__(self, "environ", MappingProxyType(dict(self.environ)))


@dataclass(frozen=True, slots=True)
class ToolContext:
    """Resolved context passed into one tool call."""

    run_id: str
    home: Path
    room: Path
    wd: Path
    services: tuple[ToolService, ...] = ()
