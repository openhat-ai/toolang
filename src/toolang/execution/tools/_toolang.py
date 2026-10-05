"""The _toolang toolset: validation and per-call runtime operations."""

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
from toolang.base.utils.workspace_paths import resolve_input_path

TOOLSET_NAME = "_toolang"


@dataclass(frozen=True, slots=True)
class ToolangTool(Tool):
    """One stateless tool using authority supplied by its executor."""

    name: Literal["run", "spawn", "exec", "pick", "honor", "compact", "chdir"]
    description: str
    parameters: dict[str, object]

    def definition(self) -> ToolDefinition:
        return ToolDefinition(self.name, self.description, dict(self.parameters))

    def summary(
        self,
        arguments: Mapping[str, Any],
        result: ToolResult | None = None,
    ) -> str | None:
        if self.name == "spawn":
            if result is not None and not result.error:
                return f"Spawned {result.output.get('id')} in {result.output.get('thread')}"
            return action_summary(
                result,
                ("spawn", "Spawning", "Spawned"),
                str(arguments.get("runnable", "runnable")),
            )
        labels = {
            "pick": "guidance",
            "compact": "thread history",
            "honor": "rules",
        }
        if self.name not in labels:
            return None
        verbs = (
            ("compact", "Compacting", "Compacted")
            if self.name == "compact"
            else ("load", "Loading", "Loaded")
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

    def paths(
        self, arguments: Mapping[str, Any], context: ToolContext
    ) -> Mapping[str, tuple[str, ...]]:
        if self.name != "chdir":
            return {}
        path = arguments.get("path")
        if not isinstance(path, str) or not path or set(arguments) != {"path"}:
            raise ToolangError("_toolang/chdir requires only a non-empty path")
        target, _name, _relative = resolve_input_path(path, context)
        if not target.is_dir():
            raise ToolangError(f"chdir target is not a directory: {path}")
        # Changing the working location does not access the directory's contents.
        # Load its rules before the first operation that reads or mutates a path.
        return {}

    async def invoke(
        self, arguments: Mapping[str, Any], context: ToolContext
    ) -> ToolResult:
        if not isinstance(context, RuntimeToolContext):
            raise ToolangError("runtime operations are unavailable for this tool call")
        runtime = context.runtime
        if self.name == "chdir":
            path = arguments.get("path")
            if not isinstance(path, str) or not path or set(arguments) != {"path"}:
                raise ToolangError("_toolang/chdir requires only a non-empty path")
            return await runtime.chdir(path, context)
        if self.name == "compact":
            if arguments:
                raise ToolangError("compact does not accept arguments")
            return await runtime.compact()
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
                raise ToolangError("_toolang/pick requires an exact capability ref")
            return await runtime.pick(kind, ref)
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
        if self.name == "spawn":
            return await runtime.spawn(runnable, input)
        return await runtime.exec(runnable, input)


@dataclass(frozen=True, slots=True)
class ToolangToolset(Toolset):
    name: str = TOOLSET_NAME
    description: str | None = "Run, transfer, and recall guidance."

    def tools(self) -> Mapping[str, Tool]:
        return {tool.name: tool for tool in _TOOLS}


def create_toolset(config: Mapping[str, Any]) -> Toolset:
    """Register runtime tools through the standard toolset factory."""

    return ToolangToolset()


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
    ToolangTool(
        "chdir",
        "Switch this Run's workdir. Call it alone in a Model Call. "
        "The target must exist and be a directory.",
        {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
    ),
    ToolangTool(
        "compact",
        "Compact the calling Run's thread history before the next model call.",
        {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
    ),
    ToolangTool(
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
    ToolangTool(
        "pick",
        "Recall allowed skill or service guidance using the ref from its trigger. "
        "Pick applicable guidance missing from the visible messages. "
        "This does not connect to a service or grant tools.",
        {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["skill", "service"]},
                "ref": {
                    "type": "string",
                    "description": "Exact skill-trigger or service-trigger ref, such as skill/testing.",
                },
            },
            "required": ["kind", "ref"],
            "additionalProperties": False,
        },
    ),
    ToolangTool(
        "run",
        "Schedule an authorized hand as a child Run. The tool reply acknowledges "
        "scheduling; a separate runtime message supplies its outcome before you continue. "
        "Use run when the caller needs the result for further processing. "
        "Follow the latest hands scope and requested_only policy. Read the target "
        "input signature and do not invent missing values. Acceptance selects the latest "
        "published version and rejects missing targets or changed signatures.",
        _RUN_PARAMETERS,
    ),
    ToolangTool(
        "exec",
        "Transfer the remainder of this Run to an authorized handoff target. "
        "The caller never resumes, and this must be the only tool call in the "
        "Model Call. Use exec for a named invocation with no requested follow-up. "
        "Follow the latest handoffs scope and requested_only policy.",
        _RUN_PARAMETERS,
    ),
    ToolangTool(
        "spawn",
        "Start an authorized hand as an independent root in a new empty thread. "
        "Returns id, thread, and the admission-time status without waiting. "
        "Use the id with history tools; no completion message is injected. "
        "Work continues after this Run ends, until completion or executor shutdown. "
        "Follow the same hands scope and requested_only policy as run, read the "
        "target input signature, and supply explicit inputs.",
        _RUN_PARAMETERS,
    ),
)


__all__ = ["create_toolset"]
