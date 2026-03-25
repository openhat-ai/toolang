"""Runtime tool-calling concepts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

ToolFamily = Literal[
    "filesystem",
    "shell",
    "web_search",
    "memory_search",
    "service_use",
    "browser_use",
    "computer_use",
]


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """One local tool definition exposed to the model runtime."""

    family: ToolFamily
    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ToolCallResult:
    """One completed local tool invocation recorded during a turn."""

    family: ToolFamily
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    output: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
