"""The runtime toolset: validation and per-call runtime operations."""

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

TOOLSET_NAME = "runtime"


@dataclass(frozen=True, slots=True)
class RuntimeTool(Tool):
    """One stateless tool using authority supplied by its executor."""

    name: Literal[
        "run",
        "spawn",
        "await",
        "exec",
        "pick",
        "honor",
        "compact",
        "chdir",
        "runnables",
    ]
    description: str
    parameters: dict[str, object]

    def definition(self) -> ToolDefinition:
        return ToolDefinition(self.name, self.description, dict(self.parameters))

    def summary(
        self,
        arguments: Mapping[str, Any],
        result: ToolResult | None = None,
    ) -> str | None:
        verbs, label, key = {
            "runnables": (
                ("discover", "Discovering", "Discovered"),
                "runnables",
                "name",
            ),
            "chdir": (("change", "Changing", "Changed"), "workdir", "path"),
            "run": (("complete", "Running", "Completed"), "child run", "runnable"),
            "spawn": (("spawn", "Spawning", "Spawned"), "new run", "runnable"),
            "await": (("receive", "Waiting for", "Received"), "run result", "target"),
            "exec": (
                ("switch to", "Switching to", "Switched to"),
                "runnable",
                "runnable",
            ),
            "pick": (("load", "Loading", "Loaded"), "guidance", "ref"),
            "honor": (("load", "Loading", "Loaded"), "rules", "paths"),
            "compact": (("compact", "Compacting", "Compacted"), "thread history", ""),
        }[self.name]
        if self.name == "run" and arguments.get("async") is True:
            verbs = ("start", "Starting", "Started")
        value = arguments.get(key)
        target = value if isinstance(value, str) else ""
        if self.name == "runnables" and target:
            label = "runnable"
        if self.name == "pick" and target:
            kind = arguments.get("kind")
            if isinstance(kind, str):
                for scope in ("home", "root", "here", "inline"):
                    target = target.removeprefix(f"{scope}://{kind}s/")
                if not target.startswith(f"{kind}/"):
                    target = f"{kind}/{target}"
        if self.name == "honor":
            paths = value if isinstance(value, list) else []
            labels = [
                workspace_label(item["workspace"], item["path"])
                for item in paths
                if isinstance(item, Mapping)
                and isinstance(item.get("workspace"), str)
                and isinstance(item.get("path"), str)
            ]
            if labels:
                label += " for " + ", ".join(labels)
            target = ""
        return action_summary(result, verbs, label + (f": {target}" if target else ""))

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
            raise ToolangError("runtime/chdir requires only a non-empty path")
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
        if self.name == "runnables":
            name = arguments.get("name")
            if set(arguments) - {"name"} or (
                "name" in arguments
                and (not isinstance(name, str) or not name or name != name.strip())
            ):
                raise ToolangError(
                    "runtime/runnables accepts an optional non-empty exact name"
                )
            return await runtime.runnables(name)
        if self.name == "chdir":
            path = arguments.get("path")
            if not isinstance(path, str) or not path or set(arguments) != {"path"}:
                raise ToolangError("runtime/chdir requires only a non-empty path")
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
                raise ToolangError("runtime/honor requires nonempty paths")
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
                raise ToolangError("runtime/pick requires only kind and ref")
            kind, ref = arguments["kind"], arguments["ref"]
            if not isinstance(kind, str) or kind not in {"skill", "service"}:
                raise ToolangError("runtime/pick kind must be skill or service")
            if not isinstance(ref, str) or not ref or ref != ref.strip():
                raise ToolangError("runtime/pick requires an exact capability ref")
            return await runtime.pick(kind, ref)
        if self.name == "await":
            target = arguments.get("target")
            if (
                set(arguments) != {"target"}
                or not isinstance(target, str)
                or not target
                or target != target.strip()
            ):
                raise ToolangError("runtime/await requires one target reference")
            return await runtime.await_target(target)
        unknown = sorted(
            set(arguments)
            - (
                {"runnable", "input", "async"}
                if self.name == "run"
                else {"runnable", "input"}
            )
        )
        if unknown:
            raise ToolangError(
                f"unknown runtime/{self.name} input fields: {', '.join(unknown)}"
            )
        runnable = arguments.get("runnable")
        if not isinstance(runnable, str) or not runnable.strip():
            raise ToolangError(f"runtime/{self.name} requires a non-empty runnable ref")
        input = arguments.get("input", {})
        if not isinstance(input, Mapping) or any(not isinstance(k, str) for k in input):
            raise ToolangError(f"runtime/{self.name} input must be an object")
        if self.name == "run":
            asynchronous = arguments.get("async", False)
            if not isinstance(asynchronous, bool):
                raise ToolangError("runtime/run async must be a boolean")
            if asynchronous:
                return await runtime.run(runnable, input, asynchronous=True)
            return await runtime.run(runnable, input)
        if self.name == "spawn":
            return await runtime.spawn(runnable, input)
        return await runtime.exec(runnable, input)


@dataclass(frozen=True, slots=True)
class RuntimeToolset(Toolset):
    name: str = TOOLSET_NAME
    description: str | None = "Discover runnables, execute work, and recall guidance."

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
            "description": "Exact visible runnable name or ref, including module-qualified private refs.",
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
        "runnables",
        "Discover runnable documentation and complete signatures, with the current "
        "runnable and its active ancestors. Supply an exact name for one target, "
        "or omit name for all visible targets. Use documentation as route triggers "
        "for the task and signatures to construct input. Discovery does not execute "
        "a target or authorize a call; current and visible ancestor signatures remain queryable.",
        {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Exact runnable name or ref, such as review or agic:review. Omit for all visible runnables.",
                }
            },
            "required": [],
            "additionalProperties": False,
        },
    ),
    RuntimeTool(
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
    RuntimeTool(
        "compact",
        "Compact the calling Run's thread history before the next model call.",
        {
            "type": "object",
            "properties": {},
            "required": [],
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
    RuntimeTool(
        "run",
        "Run an authorized hand as a child Run. By default, wait and return "
        "its completed result as {type, value}, or an error if the child fails or is canceled. "
        "Use run when the caller needs the result for further processing. "
        "Follow the current routes hands restriction. Query runtime__runnables "
        "when the target signature is missing. Read the target "
        "input signature and supply required values explicitly. Acceptance selects the latest "
        "published version and rejects missing targets or changed signatures.",
        {
            **_RUN_PARAMETERS,
            "properties": {
                **dict(_RUN_PARAMETERS["properties"]),
                "async": {
                    "type": "boolean",
                    "default": False,
                    "description": "Start owned background work; returns a handle immediately. Await its id for a result. Unfinished work is canceled when this Run ends or transfers.",
                },
            },
        },
    ),
    RuntimeTool(
        "await",
        "Wait for one async run or spawn admitted by this Run. Return its complete result; repeated waits do not relaunch work.",
        {
            "type": "object",
            "properties": {"target": {"type": "string"}},
            "required": ["target"],
            "additionalProperties": False,
        },
    ),
    RuntimeTool(
        "exec",
        "Transfer the remainder of this Run to an authorized handoff target. "
        "The caller never resumes, and this must be the only tool call in the "
        "Model Call. Use exec for a user-requested named agic or flow invocation "
        "with no requested follow-up; the target supplies the final answer. "
        "Follow the current routes handoffs restriction and query "
        "runtime__runnables when the target signature is missing.",
        _RUN_PARAMETERS,
    ),
    RuntimeTool(
        "spawn",
        "Start an authorized hand as an independent root in a new empty work_ thread. "
        "Returns id, thread, and the admission-time status without waiting. "
        "Use runtime__await with its id for the result; no completion message is injected. "
        "Work continues after this Run ends, until completion or executor shutdown. "
        "Follow the current routes spawns restriction, query runtime__runnables "
        "when the target signature is missing, and supply explicit inputs.",
        _RUN_PARAMETERS,
    ),
)


__all__ = ["create_toolset"]
