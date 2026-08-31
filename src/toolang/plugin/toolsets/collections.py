"""Public query view for model-facing tools."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import cast

from toolang.base.errors import ToolangError
from toolang.base.protocols.tool import AgentTool
from toolang.common.query import (
    CollectionDefinition,
    CollectionSchema,
    ColumnSpec,
    IdentitySpec,
    MatchUnion,
    QueryDataset,
    SetOperator,
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
    exclude=("model_name", "record"),
    columns=(
        ColumnSpec("TOOLSET", ("toolset",), "identity-component"),
        ColumnSpec("TOOL", ("name",), "identity-component"),
        ColumnSpec("PLUGIN", ("plugin",)),
        ColumnSpec("SOURCE", ("source",)),
        ColumnSpec("DESCRIPTION", ("description",), "truncate"),
    ),
)
TOOL_DEFINITION = CollectionDefinition(TOOL_SCHEMA)


@dataclass(frozen=True, slots=True)
class ToolEntry:
    """One effective model-facing tool published for execution."""

    key: str
    ref: str
    model_name: str
    tool: AgentTool

    def __post_init__(self) -> None:
        if not self.key or self.key != self.key.strip():
            raise ValueError("tool entry requires a canonical key")
        if not self.ref or self.ref != self.ref.strip():
            raise ValueError("tool entry requires a canonical ref")


class ToolCollection(Mapping[str, AgentTool]):
    """Immutable effective tools with one shared matcher and exact indexes."""

    __slots__ = (
        "entries",
        "_by_key",
        "_by_ref",
        "_by_name",
        "_matcher",
        "_views",
    )

    def __init__(
        self,
        entries: Sequence[ToolEntry] = (),
        *,
        views: Sequence[ToolQueryView] | None = None,
    ) -> None:
        values = tuple(entries)
        query_views = (
            tuple(views)
            if views is not None
            else tuple(_tool_view(entry.model_name, entry.tool) for entry in values)
        )
        if len(query_views) != len(values):
            raise ValueError("tool collection entries and query views must align")
        if any(
            view.model_name != entry.model_name
            or view.record is not entry.tool
            or f"{view.toolset}/{view.name}" != entry.ref
            for entry, view in zip(values, query_views, strict=True)
        ):
            raise ValueError("tool collection query view does not describe its entry")
        by_key = {entry.key: entry for entry in values}
        by_ref = {entry.ref: entry for entry in values}
        by_name = {entry.model_name: entry for entry in values}
        if len(by_key) != len(values):
            raise ValueError("tool collection contains duplicate entry keys")
        if len(by_ref) != len(values):
            raise ValueError("tool collection contains duplicate public refs")
        if len(by_name) != len(values):
            raise ValueError("tool collection contains duplicate model-facing names")
        self.entries = values
        self._by_key = by_key
        self._by_ref = by_ref
        self._by_name = by_name
        self._views = query_views
        self._matcher = TOOL_DEFINITION.dataset(query_views)

    @classmethod
    def from_tools(
        cls,
        tools: Mapping[str, AgentTool],
        *,
        plugin_sources: Mapping[str, str] | None = None,
    ) -> ToolCollection:
        """Build one deterministic collection from installed model-facing tools."""

        sources = plugin_sources or {}
        views = sorted(
            (
                _tool_view(model_name, tool, plugin_sources=sources)
                for model_name, tool in tools.items()
            ),
            key=lambda item: (item.toolset, item.name, item.plugin, item.model_name),
        )
        entries = tuple(
            ToolEntry(
                key=view.model_name,
                ref=f"{view.toolset}/{view.name}",
                model_name=view.model_name,
                tool=cast(AgentTool, view.record),
            )
            for view in views
        )
        return cls(
            entries,
            views=views,
        )

    def match(
        self,
        queries: MatchUnion | str | Sequence[str] | None = None,
    ) -> ToolCollection:
        """Return the stable-order subset accepted by collection queries."""

        selected = self._matcher.query(queries)
        keys = {item.model_name for item in selected}
        return self._subset(keys)

    def apply(
        self,
        operations: Sequence[tuple[SetOperator, MatchUnion | str | Sequence[str]]],
    ) -> ToolCollection:
        """Apply set operations against this immutable collection base."""

        selected = self._matcher.apply(operations)
        keys = {item.model_name for item in selected}
        return self._subset(keys)

    def resolve(self, ref: str) -> ToolEntry:
        """Resolve one exact public tool ref in O(1)."""

        entry = self._by_ref.get(ref)
        if entry is None:
            raise ToolangError(f"tool ref is unavailable: {ref}")
        return entry

    def entry(self, key: str) -> ToolEntry:
        """Resolve one stable tool key in O(1)."""

        entry = self._by_key.get(key)
        if entry is None:
            raise ToolangError(f"run tool resource is unavailable: {key}")
        return entry

    def contains(self, ref: str) -> bool:
        return ref in self._by_ref

    def refs(self) -> tuple[str, ...]:
        return tuple(entry.ref for entry in self.entries)

    def __getitem__(self, key: str) -> AgentTool:
        return self._by_name[key].tool

    def __iter__(self) -> Iterator[str]:
        return iter(self._by_name)

    def __len__(self) -> int:
        return len(self.entries)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, ToolCollection) and self.entries == other.entries

    def _subset(self, model_names: set[str]) -> ToolCollection:
        indexes = tuple(
            index
            for index, entry in enumerate(self.entries)
            if entry.model_name in model_names
        )
        return ToolCollection(
            tuple(self.entries[index] for index in indexes),
            views=tuple(self._views[index] for index in indexes),
        )


def tool_dataset(
    tools: Mapping[str, AgentTool],
    *,
    plugin_sources: Mapping[str, str] | None = None,
) -> QueryDataset[ToolQueryView]:
    """Materialize a complete ordered model-facing tool snapshot."""

    sources = plugin_sources or {}
    if isinstance(tools, ToolCollection) and not plugin_sources:
        return TOOL_DEFINITION.dataset(tools._views)
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
    plugin_sources: Mapping[str, str] | None = None,
) -> ToolQueryView:
    plugin_sources = plugin_sources or {}
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
    "ToolCollection",
    "ToolEntry",
    "ToolQueryView",
    "tool_dataset",
]
