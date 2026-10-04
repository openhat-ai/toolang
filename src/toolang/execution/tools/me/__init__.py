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
    "list": "List supported files in the current agent home, with exact-byte SHA-256 digests. Reads latest disk files; no kind or revision selector.",
    "get": "Read complete current home file content and its SHA-256 digest by relative path. Returns utf-8 text or base64 for non-UTF-8 bytes. Reads latest authored files, not bound Run code.",
    "create": "Create one complete home file, failing if it already exists. Allowed: agent.too, config.toml, flows/*.too, psyches/services/prompts/*.md, skills/*/SKILL.md, skills/*/assets/**, tasks/*.md, chores/*.md. Saving does not validate content, publish State, or switch running code.",
    "update": "Replace one complete current home file. Requires if_digest from get/list or a successful write; reread and reconcile on conflict. Preserves exact content bytes; saving does not validate content or switch running code or Setup.",
    "delete": "Delete exactly one current home file using required if_digest. Never recursively removes skill assets, archives jobs, or cancels Runs.",
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
        "List, read, create, replace, and delete supported files in the current agent home."
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
