"""Shared tool value types."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..protocols.tool import ToolHistory, ToolRuntime

ToolSummary = Callable[[Mapping[str, Any], "ToolResult | None"], str | None]
ToolPaths = Callable[
    [Mapping[str, Any], "ToolContext"], Mapping[str, tuple[str, ...]] | None
]


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
    """One effective service in a captured tool call."""

    name: str
    meta: Mapping[str, object]
    environ: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "meta", MappingProxyType(dict(self.meta)))
        object.__setattr__(self, "environ", MappingProxyType(dict(self.environ)))


@dataclass(frozen=True, slots=True)
class ToolResult:
    """A completed tool operation; output contains only JSON-compatible data."""

    output: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


@dataclass(frozen=True, slots=True)
class ToolContext:
    """Resolved context passed into one tool call."""

    home: Path
    room: Path
    workspaces: Mapping[str, Path] = field(default_factory=dict)
    # A context belongs to one invocation. Reusing resolutions binds preflight
    # and execution to the same targets without a second execution protocol.
    _paths: dict[tuple[Path, str, bool], tuple[Path, str]] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "workspaces", MappingProxyType(dict(self.workspaces)))


@dataclass(frozen=True, slots=True, kw_only=True)
class RuntimeToolContext(ToolContext):
    """Executor authority supplied only to the runtime toolset."""

    runtime: ToolRuntime


@dataclass(frozen=True, slots=True, kw_only=True)
class HistoryToolContext(ToolContext):
    """Read-only history supplied only to the history toolset."""

    history: ToolHistory


@dataclass(frozen=True, slots=True, kw_only=True)
class ServiceToolContext(ToolContext):
    """Effective services supplied only to the service toolset."""

    services: tuple[ToolService, ...]
