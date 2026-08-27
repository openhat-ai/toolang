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
    require_toolset_name,
    selected_tool_names,
    tool_ref_for_model_tool,
)


@dataclass(frozen=True, slots=True)
class LoadedTool(AgentTool):
    """One model-facing tool loaded from a named toolset."""

    toolset_name: str
    ref: ToolRef
    leaf_tool: AgentTool

    @property
    def name(self) -> str:
        return self.ref.model_name

    @property
    def namespace(self) -> str:
        return self.ref.namespace

    @property
    def public_name(self) -> str:
        return self.ref.selector

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
    selectors: Sequence[str] | None = None,
) -> dict[str, AgentTool]:
    """Load leaf tools from installed toolsets and apply selectors."""

    tools: dict[str, AgentTool] = {}
    toolsets = _load_toolsets_with_sources(config=toolset_config)
    registrations: list[tuple[str, ToolRef, AgentTool]] = []
    model_names: set[str] = set()
    for toolset_name, loaded in toolsets.items():
        toolset = cast(Toolset, loaded.plugin)
        require_toolset_name(toolset_name, source=loaded.source)
        for leaf_name, leaf_tool in toolset.tools().items():
            ref = parse_tool_registration_key(
                toolset_name,
                leaf_name,
                leaf_tool.name,
                source=loaded.source,
            )
            if ref.model_name in model_names:
                raise ValueError(f"duplicate tool name: {ref.selector}")
            model_names.add(ref.model_name)
            registrations.append((toolset_name, ref, leaf_tool))

    for toolset_name, ref, leaf_tool in registrations:
        loaded = LoadedTool(
            toolset_name=toolset_name,
            ref=ref,
            leaf_tool=leaf_tool,
        )
        tools[loaded.name] = loaded
    return select_tools(tools, selectors)


def _load_toolsets_with_sources(
    *,
    config: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, LoadedPlugin]:
    toolsets: dict[str, LoadedPlugin] = {}
    loaded_plugins = load_plugins_with_sources(
        group="toolang.toolset",
        config=config,
    )
    for loaded in loaded_plugins:
        require_toolset_name(loaded.entry_point_name, source=loaded.source)
        raw_name = getattr(loaded.plugin, "name", None)
        if not isinstance(raw_name, str):
            raise ToolangError("toolset plugin name must be text")
        require_toolset_name(raw_name, source=loaded.source)
        if raw_name != loaded.name:
            raise ToolangError("toolset plugin name must not be normalized")
        toolsets.setdefault(loaded.name, loaded)
    return toolsets


def select_tools(
    tools: dict[str, AgentTool],
    selectors: Sequence[str] | None,
) -> dict[str, AgentTool]:
    if selectors is None:
        return tools
    if not selectors:
        return {}
    refs = {name: tool_ref_for_model_tool(name, tool) for name, tool in tools.items()}
    return {
        name: tools[name]
        for name in selected_tool_names(refs, selectors)
        if name in tools
    }


def validate_tool_selectors(
    tools: dict[str, AgentTool],
    selectors: Sequence[str] | None,
) -> None:
    if not selectors:
        return
    refs = {name: tool_ref_for_model_tool(name, tool) for name, tool in tools.items()}
    missing = [
        selector for selector in selectors if not selected_tool_names(refs, (selector,))
    ]
    if missing:
        raise ValueError(f"tool selector matched no tools: {', '.join(missing)}")
