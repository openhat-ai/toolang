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


@dataclass(frozen=True, slots=True)
class LoadedPlugin:
    """One loaded plugin instance with its authority source."""

    entry_point_name: str
    name: str
    plugin: object
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
    for loaded in load_plugins_with_sources(group=group, config=config):
        plugins.setdefault(loaded.name, loaded.plugin)
    return plugins


def load_plugins_with_sources(
    *,
    group: str,
    config: Mapping[str, Mapping[str, Any]] | None = None,
    built_ins_first: bool = False,
) -> tuple[LoadedPlugin, ...]:
    """Load installed plugins while retaining entry-point authority sources."""

    plugins: list[LoadedPlugin] = []
    plugin_config = dict(config or {})
    installed = [
        (entry_point, _entry_point_plugin_source(entry_point))
        for entry_point in entry_points(group=group)
    ]
    if built_ins_first:
        installed.sort(key=lambda item: item[1] != "built-in")
    for entry_point, source in installed:
        try:
            factory = cast(Callable[[Mapping[str, Any]], object], entry_point.load())
        except ModuleNotFoundError:
            continue
        plugin = factory(_fresh_config(plugin_config.get(entry_point.name)))
        plugin_name = _plugin_name(plugin, fallback=entry_point.name)
        plugins.append(
            LoadedPlugin(
                entry_point_name=entry_point.name,
                name=plugin_name,
                plugin=plugin,
                source=source,
            )
        )
    return tuple(plugins)


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
    return "external"


def _normalize_distribution_name(name: str) -> str:
    return name.replace("_", "-").replace(".", "-").lower()
