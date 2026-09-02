"""Compact current-agent authored-resource toolset plugin."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from toolang.base.protocols.tool import AgentTool, Toolset
from toolang.base.types.tool import ToolContext, ToolDefinition

from .handlers import execute
from .schemas import Operation, decode_request, tool_parameters

_DESCRIPTIONS: dict[Operation, str] = {
    "list": (
        "List current-agent authored resources of one kind. Task and chore "
        "results include ready documents only; large content is omitted."
    ),
    "get": (
        "Get one current-agent authored resource by key, where key is a "
        "task/chore id or a cap/flow name."
    ),
    "create": (
        "Create one current-agent authored resource. Task/chore keys are "
        "allocated; named cap/flow kinds require key. Content fields depend "
        "on kind."
    ),
    "update": (
        "Update fields on one current-agent authored resource by key. Omitted "
        "content fields are preserved; if_digest is an optional concurrency "
        "precondition."
    ),
    "delete": (
        "Delete one authored psyche, skill, service, prompt, or flow by key. "
        "Task and chore lifecycle is not delete."
    ),
}


@dataclass(frozen=True, slots=True)
class AgentStateActionTool(AgentTool):
    """One operation over the compact current-agent resource protocol."""

    operation: Operation

    @property
    def name(self) -> str:
        return self.operation

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=_DESCRIPTIONS[self.operation],
            parameters=tool_parameters(self.operation),
        )

    async def invoke(
        self,
        arguments: Mapping[str, Any],
        context: ToolContext,
    ) -> dict[str, Any]:
        request = decode_request(self.operation, arguments)
        return await asyncio.to_thread(execute, request, context)


@dataclass(slots=True)
class AgentStateToolset:
    """Tools for managing the current agent's authored resources."""

    config: dict[str, Any]
    name: str = "_me"
    description: str | None = (
        "List, get, create, update, and delete this agent's tasks, chores, "
        "psyches, skills, services, prompts, and flows."
    )
    _tools: dict[str, AgentTool] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        operations: tuple[Operation, ...] = (
            "list",
            "get",
            "create",
            "update",
            "delete",
        )
        self._tools = {
            operation: AgentStateActionTool(operation) for operation in operations
        }

    def tools(self) -> Mapping[str, AgentTool]:
        return dict(self._tools)


def create_toolset(config: Mapping[str, Any]) -> Toolset:
    """Create the `_me` toolset plugin."""

    return AgentStateToolset(config=dict(config))


__all__ = ["AgentStateActionTool", "AgentStateToolset", "create_toolset"]
