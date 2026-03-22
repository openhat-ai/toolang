"""Channel plugin loading helpers."""

from __future__ import annotations

from importlib.metadata import entry_points
from typing import Any

from toolang.errors import ToolangError

from .contracts import ChannelPlugin, ChannelPluginFactory
from .plugins.telegram import create_telegram_plugin
from .plugins.webhook import create_webhook_plugin

_BUILTIN_CHANNEL_FACTORIES: dict[str, ChannelPluginFactory] = {
    "telegram": create_telegram_plugin,
    "webhook": create_webhook_plugin,
}


def load_channel_plugin_factory(plugin: str) -> ChannelPluginFactory:
    """Load one named channel plugin factory."""

    builtin = _BUILTIN_CHANNEL_FACTORIES.get(plugin)
    if builtin is not None:
        return builtin
    for entry_point in entry_points(group="toolang.channel"):
        if entry_point.name == plugin:
            loaded = entry_point.load()
            return loaded
    raise ToolangError(f"unknown channel plugin: {plugin}")


def create_channel_plugin(plugin: str, *, config: dict[str, Any] | None = None) -> ChannelPlugin:
    """Instantiate one named channel plugin."""

    factory = load_channel_plugin_factory(plugin)
    return factory(dict(config or {}))
