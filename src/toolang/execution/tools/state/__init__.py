"""Compact current-agent authored-resource toolset plugin."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from toolang.base.protocols.tool import Tool, Toolset
from toolang.base.types.tool import ToolContext, ToolDefinition, ToolResult
from toolang.base.utils.tool_descriptions import action_summary

from .handlers import execute, synchronize
from .schemas import Operation, decode_request, tool_parameters
from .errors import ResourceError

_DESCRIPTIONS: dict[Operation, str] = {
    "list": "List supported files in the current agent home, with exact-byte SHA-256 digests. Returns {files: [{key, digest, bytes}]}. Reads latest disk files; no kind or revision selector.",
    "get": "Read complete current home file content and its SHA-256 digest by relative path. Returns {key, digest, bytes, content, encoding}, with utf-8 text or base64 for non-UTF-8 bytes. Reads latest authored files, not bound Run code.",
    "create": "Create one complete home file, failing if it already exists. Returns {key, digest}. Allowed: agent.too, config.toml, runnables/*.too, psyches/services/prompts/*.md, skills/*/SKILL.md, skills/*/assets/**, chores/*.md. Saving does not validate content, publish State, or switch running code.",
    "update": "Replace one complete current home file and return {key, digest}. Requires if_digest from get/list or a successful write; reread and reconcile on conflict. Preserves exact content bytes; saving does not validate content or switch running code or Setup.",
    "sync": (
        "Wait for one check and publication of the current agent's tracked root/home sources. "
        "Finish writes first and ensure no program modifies those sources until this call returns. "
        "Returns {revision, files: [{scope, key, digest}]} sorted by scope/key, including shadowed inputs and assets. "
        "Deleted/untracked files are absent; Markdown chores are outside State. "
        "Errors return {error, message, revision, files, differences, diagnostics}; "
        "codes are state_rejected, io_error, or sync_unavailable. "
        "Revision/files identify that check's last-valid State, or null/[] if unavailable. "
        "Differences list unequal {scope, key, disk_digest, state_digest}; "
        "null digest means absence; differences=null means the complete disk manifest could not be read. "
        "Diagnostics preserve preparation errors. Does not replace running code or refresh Setup."
    ),
    "delete": "Delete exactly one current home file using required if_digest. Returns {key, digest: null}. Never recursively removes skill assets, cancels Runs.",
}


@dataclass(frozen=True, slots=True)
class StateTool(Tool):
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

    def summary(
        self, arguments: Mapping[str, Any], result: ToolResult | None = None
    ) -> str:
        verbs, label = {
            "list": (("list", "Listing", "Listed"), "agent files"),
            "get": (("read", "Reading", "Read"), "agent file"),
            "create": (("create", "Creating", "Created"), "agent file"),
            "update": (("update", "Updating", "Updated"), "agent file"),
            "delete": (("delete", "Deleting", "Deleted"), "agent file"),
            "sync": (("sync", "Syncing", "Synced"), "agent state"),
        }[self.operation]
        if self.operation not in {"list", "sync"} and isinstance(
            key := arguments.get("key"), str
        ):
            label += f": {key}"
        return action_summary(result, verbs, label)

    async def invoke(
        self,
        arguments: Mapping[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        try:
            request = decode_request(self.operation, arguments)
            if request.operation == "sync":
                return ToolResult(await synchronize(context))
            return ToolResult(await asyncio.to_thread(execute, request, context))
        except ResourceError as exc:
            return exc.result


@dataclass(slots=True)
class StateToolset:
    """Tools for managing the current agent's authored resources."""

    config: dict[str, Any]
    name: str = "state"
    description: str | None = (
        "Manage current home files and synchronize authored sources with Agent State."
    )
    _tools: dict[str, Tool] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        operations: tuple[Operation, ...] = (
            "list",
            "get",
            "create",
            "update",
            "delete",
            "sync",
        )
        self._tools = {operation: StateTool(operation) for operation in operations}

    def tools(self) -> Mapping[str, Tool]:
        return dict(self._tools)


def create_toolset(config: Mapping[str, Any]) -> Toolset:
    """Create the `state` toolset plugin."""

    return StateToolset(config=dict(config))


__all__ = ["StateTool", "StateToolset", "create_toolset"]
