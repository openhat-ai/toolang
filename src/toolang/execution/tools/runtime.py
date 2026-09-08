"""Runtime toolset: argument validation and per-call runtime operations."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Literal

from toolang.base.errors import ToolangError
from toolang.base.protocols.tool import Tool, Toolset
from toolang.base.types.tool import (
    ToolContext,
    ToolDefinition,
    ToolResult,
    RuntimeToolContext,
)
from toolang.base.utils.tool_descriptions import action_summary, workspace_label

TOOLSET_NAME = "_toolang"


@dataclass(frozen=True, slots=True)
class RuntimeTool(Tool):
    """One stateless tool using authority supplied by its executor."""

    name: Literal["reload", "run", "execute", "pick", "honor", "compact"]
    description: str
    parameters: dict[str, object]

    def definition(self) -> ToolDefinition:
        return ToolDefinition(self.name, self.description, dict(self.parameters))

    def summary(
        self,
        arguments: Mapping[str, Any],
        result: ToolResult | None = None,
    ) -> str | None:
        labels = {
            "pick": "guidance",
            "reload": "agent state",
            "compact": "thread history",
            "honor": "rules",
        }
        if self.name not in labels:
            return None
        verbs = (
            ("compact", "Compacting", "Compacted")
            if self.name == "compact"
            else ("load", "Loading", "Loaded")
            if self.name in {"pick", "honor"}
            else ("reload", "Reloading", "Reloaded")
        )
        target = labels[self.name]
        if self.name == "pick" and isinstance(arguments.get("ref"), str):
            kind = arguments.get("kind")
            ref = arguments["ref"]
            for scope in ("home", "root", "here", "inline"):
                prefix = f"{scope}://{kind}s/"
                if ref.startswith(prefix):
                    ref = ref.removeprefix(prefix)
                    break
            target += f": {kind}/{ref}"
        elif self.name == "honor":
            files = [
                workspace_label(item["workspace"], item["path"])
                for control in (result.output if result else {}).get("controls", ())
                if (item := control.get("target", {})).get("kind") == "rules"
            ]
            if files:
                target += ": " + ", ".join(files)
        return action_summary(result, verbs, target)

    @property
    def model_callable(self) -> bool:
        return self.name not in {"honor", "compact"}

    async def invoke(
        self, arguments: Mapping[str, Any], context: ToolContext
    ) -> ToolResult:
        if not isinstance(context, RuntimeToolContext):
            raise ToolangError("runtime operations are unavailable for this tool call")
        runtime = context.runtime
        if self.name == "compact":
            if not {"thread", "end"} <= set(arguments) or set(arguments) - {
                "thread",
                "begin",
                "end",
            }:
                raise ToolangError("compact requires thread, optional begin, and end")
            thread, begin, end = (
                arguments["thread"],
                arguments.get("begin"),
                arguments["end"],
            )
            if (
                not isinstance(thread, str)
                or not isinstance(end, str)
                or (begin is not None and not isinstance(begin, str))
            ):
                raise ToolangError(
                    "compact requires string references and a nullable begin"
                )
            return await runtime.compact(thread, begin, end)
        if self.name == "honor":
            if (
                set(arguments) != {"paths"}
                or not isinstance(arguments["paths"], list)
                or not arguments["paths"]
            ):
                raise ToolangError("_toolang/honor requires nonempty paths")
            paths = []
            for item in arguments["paths"]:
                if not isinstance(item, Mapping) or set(item) != {"workspace", "path"}:
                    raise ToolangError("honor paths require workspace and path")
                workspace, path = item["workspace"], item["path"]
                if not isinstance(workspace, str) or not workspace:
                    raise ToolangError("honor requires a workspace name")
                if (
                    not isinstance(path, str)
                    or not path.startswith("/")
                    or path.startswith("//")
                    or PurePosixPath(path).as_posix() != path
                    or ".." in PurePosixPath(path).parts
                ):
                    raise ToolangError(
                        "honor requires normalized workspace-relative paths"
                    )
                paths.append((workspace, path))
            return await runtime.honor(tuple(paths))
        if self.name == "pick":
            if set(arguments) != {"kind", "ref"}:
                raise ToolangError("_toolang/pick requires only kind and ref")
            kind, ref = arguments["kind"], arguments["ref"]
            if not isinstance(kind, str) or kind not in {"skill", "service"}:
                raise ToolangError("_toolang/pick kind must be skill or service")
            if not isinstance(ref, str) or not ref or ref != ref.strip():
                raise ToolangError("_toolang/pick requires an exact catalog ref")
            return await runtime.pick(kind, ref)
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
    description: str | None = "Run, transfer, reload, and recall guidance."

    def tools(self) -> Mapping[str, Tool]:
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
        "compact",
        "Compact a complete history prefix before the next model call.",
        {
            "type": "object",
            "properties": {
                "thread": {"type": "string"},
                "begin": {"type": ["string", "null"]},
                "end": {"type": "string"},
            },
            "required": ["thread", "end"],
            "additionalProperties": False,
        },
    ),
    RuntimeTool(
        "honor",
        "Recall applicable workspace rules before a path-aware operation.",
        {
            "type": "object",
            "properties": {
                "paths": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "properties": {
                            "workspace": {"type": "string"},
                            "path": {"type": "string"},
                        },
                        "required": ["workspace", "path"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["paths"],
            "additionalProperties": False,
        },
    ),
    RuntimeTool(
        "pick",
        "Recall allowed skill or service guidance from its exact catalog ref. "
        "Pick applicable guidance missing from the visible messages. "
        "This does not connect to a service or grant tools.",
        {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["skill", "service"]},
                "ref": {"type": "string", "description": "Exact ref from its catalog."},
            },
            "required": ["kind", "ref"],
            "additionalProperties": False,
        },
    ),
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
