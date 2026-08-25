"""Generic Toolang plugin loading."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from importlib.metadata import entry_points
from typing import Any, Literal, TypeVar, cast

PluginSource = Literal["built-in", "external"]
FactoryT = TypeVar("FactoryT", bound=Callable[..., object])


@dataclass(frozen=True, slots=True)
class PluginInfo:
    """One discoverable plugin entry point."""

    name: str
    source: PluginSource


def list_plugin_names(*, group: str) -> list[str]:
    """Return installed plugin entry point names for one family."""

    return sorted(entry_point.name for entry_point in entry_points(group=group))


def list_plugin_infos(*, group: str) -> list[PluginInfo]:
    """Return installed plugin entry point names and sources for one family."""

    return sorted(
        (
            PluginInfo(
                name=entry_point.name,
                source=_entry_point_plugin_source(entry_point),
            )
            for entry_point in entry_points(group=group)
        ),
        key=lambda item: item.name,
    )


def load_plugin_factory(name: str, *, group: str) -> FactoryT:
    """Load one plugin factory by entry point name."""

    for entry_point in entry_points(group=group):
        if entry_point.name == name:
            return cast(FactoryT, entry_point.load())
    raise ValueError(f"unknown {group} plugin: {name}")


def create_plugin(
    name: str,
    *,
    group: str,
    config: Mapping[str, Any] | None = None,
) -> object:
    """Create one plugin instance by entry point name."""

    factory = cast(
        Callable[[Mapping[str, Any]], object], load_plugin_factory(name, group=group)
    )
    return factory(_fresh_config(config))


def load_plugins(
    *,
    group: str,
    config: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, object]:
    """Load all installed plugin instances for one family."""

    plugins: dict[str, object] = {}
    plugin_config = dict(config or {})
    for entry_point in entry_points(group=group):
        try:
            factory = cast(Callable[[Mapping[str, Any]], object], entry_point.load())
        except ModuleNotFoundError:
            continue
        plugin = factory(_fresh_config(plugin_config.get(entry_point.name)))
        plugin_name = _plugin_name(plugin, fallback=entry_point.name)
        if plugin_name in plugins:
            continue
        plugins[plugin_name] = plugin
    return plugins


def _plugin_name(plugin: object, *, fallback: str) -> str:
    name = getattr(plugin, "name", fallback)
    if not isinstance(name, str):
        return fallback
    text = name.strip()
    return text or fallback


def _fresh_config(config: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return one factory-owned copy of a plugin configuration mapping."""

    return deepcopy(dict(config or {}))


def _entry_point_plugin_source(entry_point: object) -> PluginSource:
    dist = getattr(entry_point, "dist", None)
    metadata = getattr(dist, "metadata", None)
    if metadata is not None:
        name = metadata.get("Name")
        if isinstance(name, str) and _normalize_distribution_name(name) == "toolang":
            return "built-in"
    value = getattr(entry_point, "value", None)
    if isinstance(value, str) and value.startswith("toolang."):
        return "built-in"
    return "external"


def _normalize_distribution_name(name: str) -> str:
    return name.replace("_", "-").replace(".", "-").lower()
