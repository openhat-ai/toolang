"""Shared tool protocols."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

from ..types.tool import ToolContext, ToolDefinition


class ToolRuntime(Protocol):
    """Trusted operations bound by the executor to one runtime Tool Step."""

    async def run(self, runnable: str, input: Mapping[str, Any]) -> dict[str, Any]: ...

    async def execute(
        self, runnable: str, input: Mapping[str, Any]
    ) -> dict[str, Any]: ...

    async def reload(self) -> dict[str, Any]: ...


@runtime_checkable
class AgentTool(Protocol):
    """One tool exposed by one plugin."""

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
class Toolset(Protocol):
    """Minimal toolset plugin contract."""

    name: str
    description: str | None

    def tools(self) -> Mapping[str, AgentTool]:
        """Return one stable mapping of leaf tools exposed by this plugin."""
