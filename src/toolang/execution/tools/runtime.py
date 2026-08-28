"""Executor-owned model action definitions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from toolang.base.types.tool import ToolDefinition
from toolang.base.utils.tools import encode_tool_name

TOOLSET_NAME = "_too"
RELOAD_ACTION = "reload"
RUN_ACTION = "run"
EXECUTE_ACTION = "execute"
RuntimeActionName = Literal["reload", "run", "execute"]


@dataclass(frozen=True, slots=True)
class RuntimeAction:
    """One trusted executor action exposed only at a Model Call boundary."""

    name: RuntimeActionName
    definition: ToolDefinition


def runtime_actions(
    *,
    run: bool,
    execute: bool,
    reload: bool,
) -> dict[str, RuntimeAction]:
    """Return effective executor actions keyed by their model names."""

    selected = (
        *((_ACTIONS[RUN_ACTION],) if run else ()),
        *((_ACTIONS[EXECUTE_ACTION],) if execute else ()),
        *((_ACTIONS[RELOAD_ACTION],) if reload else ()),
    )
    return {item.definition.name: item for item in selected}


def _action(
    name: RuntimeActionName,
    description: str,
    parameters: dict[str, object],
) -> RuntimeAction:
    return RuntimeAction(
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

_ACTIONS: dict[RuntimeActionName, RuntimeAction] = {
    RUN_ACTION: _action(
        RUN_ACTION,
        "Run an authorized hand as a child Run, wait for its result, then continue. "
        "Call it only when the user explicitly requests delegation or the current "
        "runnable's authored instructions explicitly require it. Read the target "
        "input signature and do not invent missing values.",
        _RUN_PARAMETERS,
    ),
    EXECUTE_ACTION: _action(
        EXECUTE_ACTION,
        "Transfer the remainder of this Run to an authorized handoff target. "
        "Call it only when the user explicitly requests delegation or the current "
        "runnable's authored instructions explicitly require it. Read the target "
        "input signature and do not invent missing values.",
        _RUN_PARAMETERS,
    ),
    RELOAD_ACTION: _action(
        RELOAD_ACTION,
        "Check authored Agent State and apply the newest valid revision.",
        {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
    ),
}


__all__ = [
    "EXECUTE_ACTION",
    "RELOAD_ACTION",
    "RUN_ACTION",
    "RuntimeAction",
    "runtime_actions",
]
