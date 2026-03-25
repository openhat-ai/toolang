"""Contracts for Toolang runtime tool providers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from toolang.concepts.identity import AgentRef
from toolang.concepts.tools import ToolDefinition, ToolFamily


@dataclass(frozen=True, slots=True)
class ToolContext:
    """One resolved runtime context passed into local tool providers."""

    agent: AgentRef
    working_directory: Path
    sandbox: str


class ToolProvider(Protocol):
    """Protocol implemented by one local runtime tool provider."""

    family: ToolFamily

    def definition(self) -> ToolDefinition:
        """Return the model-facing definition for this tool family."""

    def invoke(self, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        """Execute one local tool call and return structured output."""


ToolProviderFactory = Callable[[dict[str, Any]], ToolProvider]
