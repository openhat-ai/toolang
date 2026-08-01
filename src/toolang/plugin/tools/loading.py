"""Tool plugin loading and runtime selection."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

from toolang.base.protocols.tool import AgentTool, AgentToolSet
from toolang.base.types.tool import ToolContext, ToolDefinition

from toolang.plugin.loading import load_plugins
from .registry import (
    ToolRef,
    parse_tool_registration_key,
    selected_tool_names,
    tool_ref_for_model_tool,
)


@dataclass(frozen=True, slots=True)
class LoadedTool(AgentTool):
    """One model-facing tool loaded from a named plugin."""

    plugin_name: str
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


def load_tool_plugins(
    *,
    config: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, AgentTool]:
    tools: dict[str, AgentTool] = {}
    plugins = cast(
        dict[str, AgentToolSet],
        load_plugins(group="toolang.tool", config=config),
    )
    for plugin in plugins.values():
        for leaf_name, leaf_tool in plugin.tools().items():
            ref = parse_tool_registration_key(plugin.name, leaf_name, leaf_tool.name)
            loaded = LoadedTool(
                plugin_name=plugin.name,
                ref=ref,
                leaf_tool=leaf_tool,
            )
            if loaded.name in tools:
                raise ValueError(f"duplicate tool name: {loaded.public_name}")
            tools[loaded.name] = loaded
    return tools


def load_runtime_tools(
    *,
    plugin_config: Mapping[str, Mapping[str, Any]],
    selectors: Sequence[str] | None = None,
) -> dict[str, AgentTool]:
    tools = load_tool_plugins(config=plugin_config)
    return select_tools(tools, selectors)


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
        selector
        for selector in selectors
        if not selected_tool_names(refs, (selector,))
    ]
    if missing:
        raise ValueError(f"tool selector matched no tools: {', '.join(missing)}")
