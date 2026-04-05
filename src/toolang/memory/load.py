"""Memory plugin loading helpers."""

from __future__ import annotations

from typing import Any

from toolang.plugins import create_plugin, load_plugin_factory

from .contracts import MemoryPlugin, MemoryPluginFactory

_BUILTIN_MEMORY_FACTORIES: dict[str, MemoryPluginFactory] = {}


def load_memory_plugin_factory(plugin: str) -> MemoryPluginFactory:
    """Load one named memory plugin factory."""

    return load_plugin_factory(
        plugin,
        group="toolang.memory",
        builtins=_BUILTIN_MEMORY_FACTORIES,
        kind="memory plugin",
    )


def create_memory_plugin(
    plugin: str,
    *,
    config: dict[str, Any] | None = None,
) -> MemoryPlugin:
    """Instantiate one named memory plugin."""

    return create_plugin(
        plugin,
        group="toolang.memory",
        builtins=_BUILTIN_MEMORY_FACTORIES,
        kind="memory plugin",
        config=config,
    )
