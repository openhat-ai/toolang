"""Runtime toolset: argument validation and per-call runtime operations."""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Mapping
from typing import Any, Literal

from toolang.base.errors import ToolangError
from toolang.base.protocols.tool import AgentTool, Toolset
from toolang.base.types.tool import ToolContext, ToolDefinition

TOOLSET_NAME = "_toolang"


@dataclass(frozen=True, slots=True)
class RuntimeTool(AgentTool):
    """One stateless tool using authority supplied by its executor."""

    name: Literal["reload", "run", "execute"]
    description: str
    parameters: dict[str, object]

    def definition(self) -> ToolDefinition:
        return ToolDefinition(self.name, self.description, dict(self.parameters))

    async def invoke(
        self, arguments: Mapping[str, Any], context: ToolContext
    ) -> dict[str, Any]:
        runtime = context.runtime
        if runtime is None:
            raise ToolangError("runtime operations are unavailable for this tool call")
        if self.name == "reload":
            if arguments:
                raise ToolangError("_toolang/reload does not accept input")
            return await runtime.reload()
        unknown = sorted(set(arguments) - {"runnable", "input"})
        if unknown:
            raise ToolangError(
                f"unknown _toolang/{self.name} input fields: {', '.join(unknown)}"
            )
        runnable = arguments.get("runnable")
        if not isinstance(runnable, str) or not runnable.strip():
            raise ToolangError(
                f"_toolang/{self.name} requires a non-empty runnable ref"
            )
        input = arguments.get("input", {})
        if not isinstance(input, Mapping) or any(not isinstance(k, str) for k in input):
            raise ToolangError(f"_toolang/{self.name} input must be an object")
        if self.name == "run":
            return await runtime.run(runnable, input)
        return await runtime.execute(runnable, input)


@dataclass(frozen=True, slots=True)
class RuntimeToolset(Toolset):
    name: str = TOOLSET_NAME
    description: str | None = "Run, transfer, and reload the current execution."

    def tools(self) -> Mapping[str, AgentTool]:
        return {tool.name: tool for tool in _TOOLS}


def create_toolset(config: Mapping[str, Any]) -> Toolset:
    """Register runtime tools through the standard toolset factory."""

    return RuntimeToolset()


_RUN_PARAMETERS: dict[str, object] = {
    "type": "object",
    "properties": {
        "runnable": {
            "type": "string",
            "description": "Public runnable ref: name, agic:name, or flow:name.",
        },
        "input": {
            "type": "object",
            "description": (
                "Runnable input; '_' is primary input and other properties are "
                "named parameters."
            ),
            "additionalProperties": True,
        },
    },
    "required": ["runnable"],
    "additionalProperties": False,
}

_TOOLS = (
    RuntimeTool(
        "run",
        "Run an authorized hand as a child Run, wait for its result, then continue. "
        "Call it only when its result is required now. Read the target input "
        "signature and do not invent missing values.",
        _RUN_PARAMETERS,
    ),
    RuntimeTool(
        "execute",
        "Transfer the remainder of this Run to an authorized handoff target. "
        "The caller never resumes, and this must be the only tool call in the "
        "Model Call. Prefer run when either behavior would satisfy the intent.",
        _RUN_PARAMETERS,
    ),
    RuntimeTool(
        "reload",
        "Apply the newest valid Agent State when this Run must observe authored "
        "changes now. A future root Run uses the latest valid State without reload.",
        {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
    ),
)


__all__ = ["create_toolset"]
