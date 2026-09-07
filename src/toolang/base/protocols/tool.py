"""Shared tool protocols."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, Protocol, runtime_checkable

from ..types.tool import ToolContext, ToolDefinition


class ToolHistory(Protocol):
    """Read-only history operations bound to the current agent and caller Thread."""

    def read_threads(
        self, *, limit: int = 20, cursor: str | None = None
    ) -> dict[str, Any]: ...

    def read_runs(
        self,
        *,
        thread: str | None = None,
        begin: str | None = None,
        end: str | None = None,
        limit: int = 20,
        from_end: bool = False,
        cursor: str | None = None,
    ) -> dict[str, Any]: ...

    def read_steps(
        self,
        *,
        run: str | None = None,
        begin: str | None = None,
        end: str | None = None,
        limit: int = 20,
        from_end: bool = False,
        cursor: str | None = None,
    ) -> dict[str, Any]: ...

    def read_output(self, *, run: str) -> dict[str, Any]: ...


class ToolRuntime(Protocol):
    """Trusted operations bound by the executor to one runtime Tool Step."""

    async def run(self, runnable: str, input: Mapping[str, Any]) -> dict[str, Any]: ...

    async def execute(
        self, runnable: str, input: Mapping[str, Any]
    ) -> dict[str, Any]: ...

    async def reload(self) -> dict[str, Any]: ...

    async def pick(
        self, kind: Literal["skill", "service"], ref: str
    ) -> dict[str, Any]: ...

    async def honor(self, paths: tuple[tuple[str, str], ...]) -> dict[str, Any]:
        """Recall rules for normalized (workspace, relative path) pairs."""


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
