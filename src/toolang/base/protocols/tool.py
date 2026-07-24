"""Shared tool protocols."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

from ..types.tool import ToolContext, ToolDefinition


@runtime_checkable
class AgentTool(Protocol):
    """One model-facing tool exposed by one plugin."""

    name: str

    def definition(self) -> ToolDefinition:
        """Return one stable tool definition."""

    async def invoke(
        self,
        arguments: Mapping[str, Any],
        context: ToolContext,
    ) -> dict[str, Any]:
        """Execute one tool call."""


@runtime_checkable
class AgentToolSet(Protocol):
    """Minimal tool plugin contract."""

    name: str
    description: str | None

    def tools(self) -> Mapping[str, AgentTool]:
        """Return one stable mapping of leaf tools exposed by this plugin."""
