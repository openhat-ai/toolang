"""Public query view for model-facing tools."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from toolang.base.protocols.tool import AgentTool
from toolang.common.query import (
    CollectionDefinition,
    CollectionSchema,
    ColumnSpec,
    IdentitySpec,
    QueryDataset,
)

from .registry import ToolRef, tool_ref_for_model_tool


@dataclass(frozen=True, slots=True)
class ToolQueryView:
    """Explicitly public tool query representation."""

    model_name: str
    record: object
    toolset: str
    name: str
    plugin: str
    source: str
    description: str
    parameters: tuple[str, ...]


TOOL_SCHEMA = CollectionSchema.from_type(
    "tools",
    ToolQueryView,
    key="model_name",
    identity=IdentitySpec(
        paths=("toolset", "name"),
        labels=("toolset", "tool"),
        separator="/",
    ),
    exclude=("record",),
    columns=(
        ColumnSpec("TOOLSET", ("toolset",), "identity-component"),
        ColumnSpec("TOOL", ("name",), "identity-component"),
        ColumnSpec("PLUGIN", ("plugin",)),
        ColumnSpec("SOURCE", ("source",)),
        ColumnSpec("DESCRIPTION", ("description",), "truncate"),
    ),
)
TOOL_DEFINITION = CollectionDefinition(TOOL_SCHEMA)


def tool_dataset(
    tools: Mapping[str, AgentTool],
    *,
    plugin_sources: Mapping[str, str] | None = None,
) -> QueryDataset[ToolQueryView]:
    """Materialize a complete ordered model-facing tool snapshot."""

    sources = plugin_sources or {}
    items = sorted(
        (
            _tool_view(model_name, tool, plugin_sources=sources)
            for model_name, tool in tools.items()
        ),
        key=lambda item: (item.toolset, item.name, item.plugin, item.model_name),
    )
    return TOOL_DEFINITION.dataset(items)


def _tool_view(
    model_name: str,
    tool: AgentTool,
    *,
    plugin_sources: Mapping[str, str],
) -> ToolQueryView:
    ref = tool_ref_for_model_tool(model_name, tool)
    plugin = ref.plugin
    if plugin == "-" and ref.toolset in plugin_sources:
        plugin = ref.toolset
    normalized = ToolRef(plugin=plugin, toolset=ref.toolset, name=ref.name)
    tool_source = getattr(tool, "source", None)
    source = (
        tool_source
        if isinstance(tool_source, str) and tool_source
        else plugin_sources.get(normalized.plugin, "-")
    )
    definition = tool.definition()
    properties = definition.parameters.get("properties")
    parameter_names = (
        tuple(sorted(str(name) for name in properties))
        if isinstance(properties, Mapping)
        else ()
    )
    return ToolQueryView(
        model_name=model_name,
        record=tool,
        toolset=normalized.toolset,
        name=normalized.name,
        plugin=normalized.plugin,
        source=source,
        description=" ".join(definition.description.split()),
        parameters=parameter_names,
    )


__all__ = [
    "TOOL_DEFINITION",
    "TOOL_SCHEMA",
    "ToolQueryView",
    "tool_dataset",
]
