"""Executor-owned runtime action definitions for model tool calling."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from toolang.base.protocols.tool import AgentTool, Toolset
from toolang.base.types.tool import ToolContext, ToolDefinition
from toolang.common.errors import ToolangError
from toolang.plugin.toolsets.registry import ToolRef

TOOLSET_NAME = "_too"
RELOAD_ACTION = "reload"
RUN_ACTION = "run"


@dataclass(frozen=True, slots=True)
class _RuntimeActionTool:
    """One schema-only action that generic tool dispatch must never invoke."""

    name: str
    description: str
    parameters: dict[str, Any]

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=self.description,
            parameters=dict(self.parameters),
        )

    async def invoke(
        self,
        arguments: Mapping[str, Any],
        context: ToolContext,
    ) -> dict[str, Any]:
        del arguments, context
        raise ToolangError(
            f"runtime action must be handled by the agic executor: {self.name}"
        )


@dataclass(slots=True)
class RuntimeToolset:
    """Model-facing definitions for executor-owned runtime actions."""

    config: dict[str, Any]
    name: str = TOOLSET_NAME
    description: str | None = "Reload Agent State or run a public runnable."
    _tools: dict[str, AgentTool] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._tools = {
            RELOAD_ACTION: _RuntimeActionTool(
                name=RELOAD_ACTION,
                description=(
                    "Check authored Agent State and apply the newest valid revision "
                    "to the active root run before its next step."
                ),
                parameters={
                    "type": "object",
                    "properties": {},
                    "required": [],
                    "additionalProperties": False,
                },
            ),
            RUN_ACTION: _RuntimeActionTool(
                name=RUN_ACTION,
                description=(
                    "Run one public agic or flow from the active Agent State as a "
                    "normal child run."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "runnable": {
                            "type": "string",
                            "description": (
                                "Public runnable ref: name, agic:name, or flow:name."
                            ),
                        },
                        "input": {
                            "type": "object",
                            "description": (
                                "Runnable input; '_' is primary input and other "
                                "properties are named parameters."
                            ),
                            "additionalProperties": True,
                        },
                    },
                    "required": ["runnable"],
                    "additionalProperties": False,
                },
            ),
        }

    def tools(self) -> Mapping[str, AgentTool]:
        return dict(self._tools)


def runtime_action(tool: AgentTool | None) -> str | None:
    """Return one trusted built-in runtime action for a loaded model tool."""

    ref = getattr(tool, "ref", None)
    if not isinstance(ref, ToolRef):
        return None
    if ref.plugin != TOOLSET_NAME or ref.toolset != TOOLSET_NAME:
        return None
    return ref.name if ref.name in {RELOAD_ACTION, RUN_ACTION} else None


def create_toolset(config: Mapping[str, Any]) -> Toolset:
    """Create the built-in runtime action toolset."""

    return RuntimeToolset(config=dict(config))


__all__ = [
    "RELOAD_ACTION",
    "RUN_ACTION",
    "TOOLSET_NAME",
    "RuntimeToolset",
    "create_toolset",
    "runtime_action",
]
