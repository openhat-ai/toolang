"""Toolset plugin loading and leaf-tool selection."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

from toolang.base.errors import ToolangError
from toolang.base.protocols.tool import AgentTool, Toolset
from toolang.base.types.tool import ToolContext, ToolDefinition

from toolang.plugin.loading import LoadedPlugin, load_plugins_with_sources
from .registry import (
    ToolRef,
    parse_tool_registration_key,
    require_toolset_plugin_name,
)


@dataclass(frozen=True, slots=True)
class LoadedTool(AgentTool):
    """One model-facing tool loaded from a named toolset."""

    plugin_name: str
    ref: ToolRef
    leaf_tool: AgentTool

    @property
    def name(self) -> str:
        return self.ref.model_name

    @property
    def toolset(self) -> str:
        return self.ref.toolset

    @property
    def public_name(self) -> str:
        return self.ref.identity

    def definition(self) -> ToolDefinition:
        definition = self.leaf_tool.definition()
        return ToolDefinition(
            name=self.name,
            description=definition.description,
            parameters=dict(definition.parameters),
        )

    async def invoke(
        self,
        arguments: Mapping[str, Any],
        context: ToolContext,
    ) -> dict[str, Any]:
        return await self.leaf_tool.invoke(arguments, context)


def load_toolsets(
    *,
    config: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Toolset]:
    """Load all installed toolsets with their plugin-owned configuration."""

    return {
        name: cast(Toolset, loaded.plugin)
        for name, loaded in _load_toolsets_with_sources(config=config).items()
    }


def load_tools(
    *,
    toolset_config: Mapping[str, Mapping[str, Any]] | None = None,
    queries: Sequence[str] | None = None,
) -> dict[str, AgentTool]:
    """Load leaf tools from installed toolsets and apply collection queries."""

    tools: dict[str, AgentTool] = {}
    toolsets = _load_toolsets_with_sources(config=toolset_config)
    registrations: list[tuple[str, ToolRef, AgentTool]] = []
    model_names: set[str] = set()
    for plugin_name, loaded in toolsets.items():
        toolset = cast(Toolset, loaded.plugin)
        require_toolset_plugin_name(plugin_name, source=loaded.source)
        for leaf_name, leaf_tool in toolset.tools().items():
            ref = parse_tool_registration_key(
                plugin_name,
                leaf_name,
                leaf_tool.name,
                source=loaded.source,
            )
            if ref.model_name in model_names:
                raise ValueError(f"duplicate tool name: {ref.identity}")
            model_names.add(ref.model_name)
            registrations.append((plugin_name, ref, leaf_tool))

    for plugin_name, ref, leaf_tool in registrations:
        loaded = LoadedTool(
            plugin_name=plugin_name,
            ref=ref,
            leaf_tool=leaf_tool,
        )
        tools[loaded.name] = loaded
    return query_tools(tools, queries)


def _load_toolsets_with_sources(
    *,
    config: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, LoadedPlugin]:
    toolsets: dict[str, LoadedPlugin] = {}
    loaded_plugins = load_plugins_with_sources(
        group="toolang.toolset",
        config=config,
        built_ins_first=True,
    )
    for loaded in loaded_plugins:
        require_toolset_plugin_name(loaded.entry_point_name, source=loaded.source)
        raw_name = getattr(loaded.plugin, "name", None)
        if not isinstance(raw_name, str):
            raise ToolangError("toolset plugin name must be text")
        require_toolset_plugin_name(raw_name, source=loaded.source)
        if raw_name != loaded.name:
            raise ToolangError("toolset plugin name must not be normalized")
        existing = toolsets.get(loaded.name)
        if existing is not None:
            raise ToolangError(
                f"duplicate toolset plugin name {loaded.name!r}: "
                f"{existing.source} entry point {existing.entry_point_name!r} "
                f"conflicts with {loaded.source} entry point "
                f"{loaded.entry_point_name!r}"
            )
        toolsets[loaded.name] = loaded
    return toolsets


def query_tools(
    tools: dict[str, AgentTool],
    queries: Sequence[str] | None,
) -> dict[str, AgentTool]:
    from .collections import tool_dataset

    if queries is None:
        return tools
    if not queries:
        return {}
    return {
        item.model_name: cast(AgentTool, item.record)
        for item in tool_dataset(tools).query(queries)
    }


def validate_tool_queries(
    tools: dict[str, AgentTool],
    queries: Sequence[str] | None,
) -> None:
    from .collections import tool_dataset

    if not queries:
        return
    try:
        tool_dataset(tools).require_each(queries, label="tool")
    except ToolangError as error:
        raise ValueError(str(error)) from error
