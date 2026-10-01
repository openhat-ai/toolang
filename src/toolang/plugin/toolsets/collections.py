"""Public query view for model-facing tools."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import cast

from toolang.base.errors import ToolangError
from toolang.base.protocols.tool import Tool
from tq import Query
from toolang.common.types import SetOperator

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


@dataclass(frozen=True, slots=True)
class ToolEntry:
    """One effective model-facing tool published for execution."""

    key: str
    ref: str
    model_name: str
    tool: Tool

    def __post_init__(self) -> None:
        if not self.key or self.key != self.key.strip():
            raise ValueError("tool entry requires a canonical key")
        if not self.ref or self.ref != self.ref.strip():
            raise ValueError("tool entry requires a canonical ref")


@dataclass(frozen=True, slots=True, eq=False, init=False)
class ToolCollection(Mapping[str, Tool]):
    """Immutable effective tools with exact lookup indexes."""

    entries: tuple[ToolEntry, ...]
    _by_key: Mapping[str, ToolEntry]
    _by_ref: Mapping[str, ToolEntry]
    _by_name: Mapping[str, ToolEntry]
    _views: tuple[ToolQueryView, ...]
    _view_by_name: Mapping[str, ToolQueryView]

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
        _validate_tool_entries(values, query_views)
        self._initialize(values, query_views=query_views)

    def _initialize(
        self,
        values: tuple[ToolEntry, ...],
        *,
        query_views: tuple[ToolQueryView, ...],
    ) -> None:
        _validate_tool_entries(values, query_views)
        by_key = {entry.key: entry for entry in values}
        by_ref = {entry.ref: entry for entry in values}
        by_name = {entry.model_name: entry for entry in values}
        view_by_name = {view.model_name: view for view in query_views}
        object.__setattr__(self, "entries", values)
        object.__setattr__(self, "_by_key", MappingProxyType(by_key))
        object.__setattr__(self, "_by_ref", MappingProxyType(by_ref))
        object.__setattr__(self, "_by_name", MappingProxyType(by_name))
        object.__setattr__(self, "_views", query_views)
        object.__setattr__(self, "_view_by_name", MappingProxyType(view_by_name))

    @classmethod
    def from_tools(
        cls,
        tools: Mapping[str, Tool],
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
                tool=cast(Tool, view.record),
            )
            for view in views
        )
        return cls(
            entries,
            views=views,
        )

    @property
    def runtime(self) -> ToolCollection:
        """Registered runtime tools, independent of user resource selectors."""

        return self._derive(
            tuple(entry for entry in self.entries if entry.ref.startswith("_toolang/"))
        )

    @property
    def user(self) -> ToolCollection:
        """Tools governed by user resource selectors."""

        return self._derive(
            tuple(
                entry for entry in self.entries if not entry.ref.startswith("_toolang/")
            )
        )

    def query(
        self, queries: str | Sequence[str] | None = None
    ) -> tuple[ToolQueryView, ...]:
        if queries is None:
            return self._views
        query = Query.parse(queries).validate({"key": "ref"})
        return tuple(
            view for view in self._views if query.match(tool_record(view)) is not None
        )

    def match(
        self,
        queries: str | Sequence[str] | None = None,
    ) -> ToolCollection:
        """Return the stable-order subset accepted by collection queries."""

        if queries is None:
            return self
        keys = {item.model_name for item in self.query(queries)}
        return self._derive(
            tuple(entry for entry in self.entries if entry.model_name in keys)
        )

    def apply(
        self,
        operations: Sequence[tuple[SetOperator, str | Sequence[str]]],
    ) -> ToolCollection:
        """Apply set operations against this immutable collection base."""

        if not operations:
            return self
        available = set(self._by_name)
        active = set(available)
        for operator, query in operations:
            matched = {item.model_name for item in self.query(query)} & available
            if operator == "=":
                active.intersection_update(matched)
            elif operator == "+=":
                active.update(matched)
            elif operator == "-=":
                active.difference_update(matched)
            else:  # pragma: no cover - SetOperator is a closed vocabulary
                raise ToolangError(f"unknown collection set operator: {operator!r}")
        return self._derive(
            tuple(entry for entry in self.entries if entry.model_name in active)
        )

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

    def subset(self, keys: Sequence[str]) -> ToolCollection:
        """Resolve an ordered persisted-key subset without querying."""

        return self._derive(tuple(self.entry(key) for key in keys))

    def compact(self) -> ToolCollection:
        """Fix this subset as a standalone collection."""

        return ToolCollection(self.entries, views=self._views)

    def require_each(self, queries: Sequence[str], *, label: str = "tool") -> None:
        """Require every query to match within this collection subset."""

        available = set(self._by_name)
        missing = [
            query
            for query in queries
            if not any(item.model_name in available for item in self.query(query))
        ]
        if missing:
            raise ToolangError(f"{label} query matched no items: {', '.join(missing)}")

    def contains(self, ref: str) -> bool:
        return ref in self._by_ref

    def refs(self) -> tuple[str, ...]:
        return tuple(entry.ref for entry in self.entries)

    def __getitem__(self, key: str) -> Tool:
        return self._by_name[key].tool

    def __iter__(self) -> Iterator[str]:
        return iter(self._by_name)

    def __len__(self) -> int:
        return len(self.entries)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, ToolCollection) and self.entries == other.entries

    def _derive(self, entries: tuple[ToolEntry, ...]) -> ToolCollection:
        if entries == self.entries:
            return self
        derived = object.__new__(ToolCollection)
        derived._initialize(
            entries,
            query_views=tuple(
                self._view_by_name[entry.model_name] for entry in entries
            ),
        )
        return derived


def _validate_tool_entries(
    values: tuple[ToolEntry, ...],
    query_views: tuple[ToolQueryView, ...],
) -> None:
    if len(query_views) != len(values):
        raise ValueError("tool collection entries and query views must align")
    if any(
        view.model_name != entry.model_name
        or view.record is not entry.tool
        or f"{view.toolset}/{view.name}" != entry.ref
        for entry, view in zip(values, query_views, strict=True)
    ):
        raise ValueError("tool collection query view does not describe its entry")
    if len({entry.key for entry in values}) != len(values):
        raise ValueError("tool collection contains duplicate entry keys")
    if len({entry.ref for entry in values}) != len(values):
        raise ValueError("tool collection contains duplicate public refs")
    if len({entry.model_name for entry in values}) != len(values):
        raise ValueError("tool collection contains duplicate model-facing names")


def _tool_view(
    model_name: str,
    tool: Tool,
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


def tool_record(view: ToolQueryView, *, allowed: bool = True) -> dict[str, object]:
    return {
        "ref": f"{view.toolset}/{view.name}",
        "toolset": view.toolset,
        "name": view.name,
        "plugin": view.plugin,
        "source": view.source,
        "description": view.description,
        "parameters": list(view.parameters),
        "tags": ["ready" if allowed else "not_allowed"],
    }
