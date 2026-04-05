"""Sandbox plugin loading helpers."""

from __future__ import annotations

from typing import Any

from toolang.plugins import create_plugin, load_plugin_factory

from .contracts import SandboxPlugin, SandboxPluginFactory
from .plugins import create_docker_sandbox_plugin, create_host_sandbox_plugin

_BUILTIN_SANDBOX_FACTORIES: dict[str, SandboxPluginFactory] = {
    "host": create_host_sandbox_plugin,
    "docker": create_docker_sandbox_plugin,
}


def load_sandbox_plugin_factory(plugin: str) -> SandboxPluginFactory:
    """Load one named sandbox plugin factory."""

    return load_plugin_factory(
        plugin,
        group="toolang.sandbox",
        builtins=_BUILTIN_SANDBOX_FACTORIES,
        kind="sandbox plugin",
    )


def create_sandbox_plugin(
    plugin: str,
    *,
    config: dict[str, Any] | None = None,
) -> SandboxPlugin:
    """Instantiate one named sandbox plugin."""

    return create_plugin(
        plugin,
        group="toolang.sandbox",
        builtins=_BUILTIN_SANDBOX_FACTORIES,
        kind="sandbox plugin",
        config=config,
    )
