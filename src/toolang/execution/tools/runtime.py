"""Executor-owned inner runtime tool definitions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from toolang.base.types.tool import ToolDefinition
from toolang.base.utils.tools import encode_tool_name

TOOLSET_NAME = "_too"
RELOAD_TOOL = "reload"
RUN_TOOL = "run"
EXECUTE_TOOL = "execute"
RuntimeToolName = Literal["reload", "run", "execute"]


@dataclass(frozen=True, slots=True)
class RuntimeTool:
    """One trusted executor tool exposed only at a Model Call boundary."""

    name: RuntimeToolName
    definition: ToolDefinition


def runtime_tools() -> dict[str, RuntimeTool]:
    """Return the always-present inner runtime tools by model name."""

    return {item.definition.name: item for item in _TOOLS.values()}


def _tool(
    name: RuntimeToolName,
    description: str,
    parameters: dict[str, object],
) -> RuntimeTool:
    return RuntimeTool(
        name=name,
        definition=ToolDefinition(
            name=encode_tool_name(TOOLSET_NAME, name),
            description=description,
            parameters=parameters,
        ),
    )


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

_TOOLS: dict[RuntimeToolName, RuntimeTool] = {
    RUN_TOOL: _tool(
        RUN_TOOL,
        "Run an authorized hand as a child Run, wait for its result, then continue. "
        "Call it only when its result is required now. Read the target input "
        "signature and do not invent missing values.",
        _RUN_PARAMETERS,
    ),
    EXECUTE_TOOL: _tool(
        EXECUTE_TOOL,
        "Transfer the remainder of this Run to an authorized handoff target. "
        "The caller never resumes, and this must be the only tool call in the "
        "Model Call. Prefer run when either behavior would satisfy the intent.",
        _RUN_PARAMETERS,
    ),
    RELOAD_TOOL: _tool(
        RELOAD_TOOL,
        "Apply the newest valid Agent State when this Run must observe authored "
        "changes now. A future root Run uses the latest valid State without reload.",
        {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
    ),
}


__all__ = [
    "EXECUTE_TOOL",
    "RELOAD_TOOL",
    "RUN_TOOL",
    "RuntimeTool",
    "runtime_tools",
]
