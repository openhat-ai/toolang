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
