"""Toolset plugin loading and leaf-tool selection."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

from toolang.base.errors import ToolangError
from toolang.base.protocols.tool import Tool, Toolset
from toolang.base.types.tool import (
    ToolContext,
    ToolDefinition,
    ToolResult,
)

from toolang.plugin.loading import LoadedPlugin, PluginSource, load_plugins_with_sources
from .registry import (
    ToolRef,
    parse_tool_registration_key,
    require_toolset_plugin_name,
)


@dataclass(frozen=True, slots=True)
class LoadedTool(Tool):
    """One model-facing tool loaded from a named toolset."""

    plugin_name: str
    source: PluginSource
    ref: ToolRef
    leaf_tool: Tool

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

    @property
    def model_callable(self) -> bool:
        return getattr(self.leaf_tool, "model_callable", True)

    def touchpoints(
        self, arguments: Mapping[str, Any], context: ToolContext
    ) -> Mapping[str, tuple[str, ...]] | None:
        return self.leaf_tool.touchpoints(arguments, context)

    def summary(
        self,
        arguments: Mapping[str, Any],
        result: ToolResult | None = None,
    ) -> str | None:
        return self.leaf_tool.summary(arguments, result)

    async def invoke(
        self,
        arguments: Mapping[str, Any],
        context: ToolContext,
    ) -> ToolResult:
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
    toolsets: Sequence[str] | None = None,
) -> dict[str, Tool]:
    """Load leaf tools from installed toolsets and apply collection queries."""

    tools: dict[str, Tool] = {}
    installed = _load_toolsets_with_sources(config=toolset_config, names=toolsets)
    registrations: list[tuple[str, PluginSource, ToolRef, Tool]] = []
    model_names: set[str] = set()
    for plugin_name, loaded in installed.items():
        toolset = cast(Toolset, loaded.plugin)
        require_toolset_plugin_name(plugin_name, source=loaded.source)
        for leaf_name, leaf_tool in toolset.tools().items():
            if not isinstance(leaf_tool, Tool):
                raise ToolangError(f"tool {plugin_name}/{leaf_name} must inherit Tool")
            ref = parse_tool_registration_key(
                plugin_name,
                leaf_name,
                leaf_tool.name,
                source=loaded.source,
            )
            if ref.model_name in model_names:
                raise ValueError(f"duplicate tool name: {ref.identity}")
            model_names.add(ref.model_name)
            registrations.append((plugin_name, loaded.source, ref, leaf_tool))

    for plugin_name, source, ref, leaf_tool in registrations:
        loaded = LoadedTool(
            plugin_name=plugin_name,
            source=source,
            ref=ref,
            leaf_tool=leaf_tool,
        )
        tools[loaded.name] = loaded
    return query_tools(tools, queries)


def _load_toolsets_with_sources(
    *,
    config: Mapping[str, Mapping[str, Any]] | None = None,
    names: Sequence[str] | None = None,
) -> dict[str, LoadedPlugin]:
    toolsets: dict[str, LoadedPlugin] = {}
    loaded_plugins = load_plugins_with_sources(
        group="toolang.toolset",
        config=config,
        built_ins_first=True,
        names=names,
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
    tools: dict[str, Tool],
    queries: Sequence[str] | None,
) -> dict[str, Tool]:
    from .collections import tool_dataset

    if queries is None:
        return tools
    if not queries:
        return {}
    return {
        item.model_name: cast(Tool, item.record)
        for item in tool_dataset(tools).query(queries)
    }


def validate_tool_queries(
    tools: dict[str, Tool],
    queries: Sequence[str] | None,
) -> None:
    from .collections import tool_dataset

    if not queries:
        return
    try:
        tool_dataset(tools).require_each(queries, label="tool")
    except ToolangError as error:
        raise ValueError(str(error)) from error
