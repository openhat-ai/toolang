"""Generic plugin loading helpers shared by plugin families."""

from __future__ import annotations

from importlib.metadata import entry_points
from typing import Any, Callable, Mapping, TypeVar

from toolang.errors import ToolangError

FactoryT = TypeVar("FactoryT", bound=Callable[..., Any])


def load_plugin_factory(
    plugin: str,
    *,
    group: str,
    builtins: Mapping[str, FactoryT],
    kind: str,
) -> FactoryT:
    """Load one plugin factory from builtins or entry points."""

    builtin = builtins.get(plugin)
    if builtin is not None:
        return builtin
    for entry_point in entry_points(group=group):
        if entry_point.name == plugin:
            loaded = entry_point.load()
            return loaded
    raise ToolangError(f"unknown {kind}: {plugin}")


def create_plugin(
    plugin: str,
    *,
    group: str,
    builtins: Mapping[str, FactoryT],
    kind: str,
    config: dict[str, Any] | None = None,
) -> Any:
    """Instantiate one named plugin with explicit config."""

    factory = load_plugin_factory(
        plugin,
        group=group,
        builtins=builtins,
        kind=kind,
    )
    return factory(dict(config or {}))
