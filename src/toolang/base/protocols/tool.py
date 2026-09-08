"""Shared tool protocols."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from typing import Any, Literal, Protocol, runtime_checkable

from ..types.tool import ToolContext, ToolDefinition, ToolResult


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

    async def run(self, runnable: str, input: Mapping[str, Any]) -> ToolResult: ...

    async def execute(self, runnable: str, input: Mapping[str, Any]) -> ToolResult: ...

    async def reload(self) -> ToolResult: ...

    async def compact(self, thread: str, begin: str | None, end: str) -> ToolResult: ...

    async def pick(self, kind: Literal["skill", "service"], ref: str) -> ToolResult: ...

    async def honor(self, paths: tuple[tuple[str, str], ...]) -> ToolResult:
        """Recall rules for normalized (workspace, relative path) pairs."""


class Tool(ABC):
    """One tool exposed by one plugin."""

    name: str

    @abstractmethod
    def definition(self) -> ToolDefinition:
        """Return one stable tool definition."""

    @abstractmethod
    async def invoke(
        self,
        arguments: Mapping[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        """Execute one tool call."""

    def summary(
        self, arguments: Mapping[str, Any], result: ToolResult | None = None
    ) -> str | None:
        """Plain call wording from arguments/result only; None uses executor wording."""
        return None

    def paths(
        self, arguments: Mapping[str, Any], context: ToolContext
    ) -> Mapping[str, tuple[str, ...]] | None:
        """Workspace-relative paths, without executing the requested operation.

        None means unsupported; an empty mapping means no workspace paths.
        """
        return None


@runtime_checkable
class Toolset(Protocol):
    """Minimal toolset plugin contract."""

    name: str
    description: str | None

    def tools(self) -> Mapping[str, Tool]:
        """Return one stable mapping of leaf tools exposed by this plugin."""
