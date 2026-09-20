"""Installed plugin discovery and typed factory loading."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from importlib.metadata import entry_points
from typing import Any, TypeVar, cast

from toolang.base.protocols.channel import AgentChannel
from toolang.base.protocols.model import ModelAdapter, ModelCatalog
from toolang.base.protocols.sandbox import Sandbox
from .types import LoadedPlugin, PluginInfo, PluginProvenance, PluginSource

FactoryT = TypeVar("FactoryT", bound=Callable[..., object])


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


def plugin_provenance(*, group: str) -> tuple[PluginProvenance, ...]:
    """Return deterministic entry-point provenance for cache invalidation."""

    values: list[PluginProvenance] = []
    for entry_point in entry_points(group=group):
        dist = getattr(entry_point, "dist", None)
        metadata = getattr(dist, "metadata", None)
        distribution = None
        if metadata is not None:
            raw_name = metadata.get("Name")
            if isinstance(raw_name, str) and raw_name.strip():
                distribution = raw_name.strip()
        raw_version = getattr(dist, "version", None)
        version = (
            raw_version.strip()
            if isinstance(raw_version, str) and raw_version.strip()
            else None
        )
        values.append(
            PluginProvenance(
                name=entry_point.name,
                value=entry_point.value,
                distribution=distribution,
                version=version,
            )
        )
    return tuple(
        sorted(
            values,
            key=lambda item: (
                item.name,
                item.value,
                item.distribution or "",
                item.version or "",
            ),
        )
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
    names: Sequence[str] | None = None,
) -> tuple[LoadedPlugin, ...]:
    """Load installed plugins while retaining entry-point authority sources."""

    plugins: list[LoadedPlugin] = []
    plugin_config = dict(config or {})
    installed = [
        (entry_point, _entry_point_plugin_source(entry_point))
        for entry_point in entry_points(group=group)
        if names is None or entry_point.name in names
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


def create_channel(
    name: str,
    *,
    config: Mapping[str, Any] | None = None,
) -> AgentChannel:
    """Create one channel implementation by entry-point name."""

    return cast(
        AgentChannel,
        create_plugin(name, group="toolang.channel", config=config),
    )


def create_sandbox(
    name: str,
    *,
    config: Mapping[str, Any] | None = None,
) -> Sandbox:
    """Create one sandbox implementation by entry-point name."""

    return cast(
        Sandbox,
        create_plugin(name, group="toolang.sandbox", config=config),
    )


def load_model_adapters(
    config: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, ModelAdapter]:
    """Load installed model adapters with their plugin-owned configuration."""

    return cast(
        dict[str, ModelAdapter],
        load_plugins(group="toolang.model_adapter", config=config),
    )


def load_model_catalogs(
    config: Mapping[str, Mapping[str, Any]],
) -> dict[str, ModelCatalog]:
    """Load installed model catalog plugins with explicit runtime inputs."""

    catalogs: dict[str, ModelCatalog] = {}
    for name, plugin_config in config.items():
        try:
            catalog = cast(
                ModelCatalog,
                create_plugin(
                    name,
                    group="toolang.model_catalog",
                    config=plugin_config,
                ),
            )
        except ModuleNotFoundError:
            continue
        catalog_name = catalog.name.strip() or name
        catalogs.setdefault(catalog_name, catalog)
    return catalogs


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
