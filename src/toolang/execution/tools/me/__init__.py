"""Compact current-agent authored-resource toolset plugin."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from toolang.base.protocols.tool import Tool, Toolset
from toolang.base.types.tool import ToolContext, ToolDefinition, ToolResult

from .handlers import execute
from .schemas import Operation, decode_request, tool_parameters
from .errors import ResourceError

_DESCRIPTIONS: dict[Operation, str] = {
    "list": (
        "List current-agent authored resources of one kind. Task and chore "
        "results include ready documents only; large content is omitted. Program lists "
        "main-source declarations with whole-file digests and run/authored versions."
    ),
    "get": (
        "Get one current-agent authored resource by key, where key is a "
        "task/chore id, cap/flow name, or program kind:name. For program, omit key "
        "to read complete main source. Reads latest authored source, not bound Run code."
    ),
    "create": (
        "Create one current-agent authored resource. Task/chore keys are "
        "allocated; named cap/flow kinds require key. Content fields depend "
        "on kind. Program create appends one matching declaration and requires "
        "if_digest from a fresh get/list."
    ),
    "update": (
        "Update fields on one current-agent authored resource by key. Omitted "
        "content fields are preserved; if_digest is an optional concurrency "
        "precondition. Program update requires if_digest; omit key for whole-source "
        "replacement, or use kind:name for one declaration. Validated saving does "
        "not switch the running program version."
    ),
    "delete": (
        "Delete one authored psyche, skill, service, prompt, or flow by key. "
        "Program delete removes one declaration and its attached comments, requires "
        "key and if_digest, and validates the remaining program. Task and chore "
        "lifecycle is not delete."
    ),
}


@dataclass(frozen=True, slots=True)
class MeTool(Tool):
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
    ) -> ToolResult:
        try:
            request = decode_request(self.operation, arguments)
            return ToolResult(await asyncio.to_thread(execute, request, context))
        except ResourceError as exc:
            return exc.result


@dataclass(slots=True)
class MeToolset:
    """Tools for managing the current agent's authored resources."""

    config: dict[str, Any]
    name: str = "me"
    description: str | None = (
        "List, get, create, update, and delete this agent's tasks, chores, "
        "psyches, skills, services, prompts, flows, and main program."
    )
    _tools: dict[str, Tool] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        operations: tuple[Operation, ...] = (
            "list",
            "get",
            "create",
            "update",
            "delete",
        )
        self._tools = {operation: MeTool(operation) for operation in operations}

    def tools(self) -> Mapping[str, Tool]:
        return dict(self._tools)


def create_toolset(config: Mapping[str, Any]) -> Toolset:
    """Create the `me` toolset plugin."""

    return MeToolset(config=dict(config))


__all__ = ["MeTool", "MeToolset", "create_toolset"]
