"""History toolset: validate queries and delegate to read-only per-call access."""

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from toolang.base.errors import ToolangError
from toolang.base.protocols.tool import Tool, Toolset
from toolang.base.types.tool import (
    ToolContext,
    ToolDefinition,
    ToolResult,
    HistoryToolContext,
)


@dataclass(frozen=True, slots=True)
class HistoryTool(Tool):
    name: Literal["read_threads", "read_runs", "read_steps", "read_output"]
    description: str
    parameters: dict[str, Any]

    def definition(self) -> ToolDefinition:
        return ToolDefinition(self.name, self.description, dict(self.parameters))

    async def invoke(
        self, arguments: Mapping[str, Any], context: ToolContext
    ) -> ToolResult:
        if not isinstance(context, HistoryToolContext):
            raise ToolangError("history is unavailable for this tool call")
        history = context.history
        unknown = set(arguments) - self.parameters["properties"].keys()
        if unknown:
            raise ToolangError(
                f"unknown history input fields: {', '.join(sorted(unknown))}"
            )
        cursor = arguments.get("cursor")
        if cursor is not None:
            if not isinstance(cursor, str) or not cursor:
                raise ToolangError("history cursor must be non-empty text or null")
            if set(arguments) != {"cursor"}:
                raise ToolangError("history continuation accepts cursor only")
        else:
            for name in ("thread", "run", "begin", "end"):
                value = arguments.get(name)
                if value is not None and (not isinstance(value, str) or not value):
                    raise ToolangError(f"history {name} must be a reference or null")
            if self.name in {"read_steps", "read_output"} and not arguments.get("run"):
                raise ToolangError("history requires a run reference")
            limit = arguments.get("limit", 20)
            if type(limit) is not int or limit <= 0:
                raise ToolangError("history limit must be a positive integer")
            if type(arguments.get("from_end", False)) is not bool:
                raise ToolangError("history from_end must be a boolean")
        return ToolResult(
            await asyncio.to_thread(getattr(history, self.name), **arguments)
        )


def _parameters(*, target: str | None = None, ranged: bool = False) -> dict[str, Any]:
    properties: dict[str, Any] = {
        "cursor": {
            "type": ["string", "null"],
            "description": "Continue with this cursor alone.",
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "default": 20,
            "description": "Maximum primary records per page; dependencies do not count.",
        },
    }
    if target is not None:
        properties[target] = {"type": ["string", "null"]}
    if ranged:
        properties.update(
            {
                "begin": {
                    "type": ["string", "null"],
                    "description": "Inclusive reference; omitted/null is open.",
                },
                "end": {
                    "type": ["string", "null"],
                    "description": "Exclusive reference; omitted/null is open.",
                },
                "from_end": {
                    "type": "boolean",
                    "default": False,
                    "description": "Select from the tail; return each page in natural order.",
                },
            }
        )
    return {"type": "object", "properties": properties, "additionalProperties": False}


_TOOLS = (
    HistoryTool(
        "read_threads",
        "List the current agent's Threads, newest updated first. Continue with cursor only.",
        _parameters(),
    ),
    HistoryTool(
        "read_runs",
        "Read root Run records in a logical Thread (defaults to the caller's). Bounds are Run refs in [begin, end); continue with cursor only.",
        _parameters(target="thread", ranged=True),
    ),
    HistoryTool(
        "read_steps",
        "Read one Run's Steps and controls without child internals. Supply run on the first page. Bounds are Step refs in [begin, end). Entries may split tool exchanges; dependencies can recur. Continue with cursor only.",
        _parameters(target="run", ranged=True),
    ),
    HistoryTool(
        "read_output",
        "Read one Run's status and resolved typed output, including partial output. A missing output is null.",
        {
            "type": "object",
            "properties": {"run": {"type": "string"}},
            "required": ["run"],
            "additionalProperties": False,
        },
    ),
)


@dataclass(frozen=True, slots=True)
class HistoryToolset(Toolset):
    name: str = "history"
    description: str | None = (
        "Read this agent's durable Threads, Runs, Steps, and outputs."
    )

    def tools(self) -> Mapping[str, Tool]:
        return {tool.name: tool for tool in _TOOLS}


def create_toolset(config: Mapping[str, Any]) -> Toolset:
    """Register ordinary, selectable history tools without retaining authority."""

    return HistoryToolset()
