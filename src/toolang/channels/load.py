"""Channel plugin loading helpers."""

from __future__ import annotations

from typing import Any

from toolang.plugins import create_plugin, load_plugin_factory

from .contracts import ChannelPlugin, ChannelPluginFactory
from .plugins.telegram import create_telegram_plugin
from .plugins.webhook import create_webhook_plugin

_BUILTIN_CHANNEL_FACTORIES: dict[str, ChannelPluginFactory] = {
    "telegram": create_telegram_plugin,
    "webhook": create_webhook_plugin,
}


def load_channel_plugin_factory(plugin: str) -> ChannelPluginFactory:
    """Load one named channel plugin factory."""

    return load_plugin_factory(
        plugin,
        group="toolang.channel",
        builtins=_BUILTIN_CHANNEL_FACTORIES,
        kind="channel plugin",
    )


def create_channel_plugin(plugin: str, *, config: dict[str, Any] | None = None) -> ChannelPlugin:
    """Instantiate one named channel plugin."""

    return create_plugin(
        plugin,
        group="toolang.channel",
        builtins=_BUILTIN_CHANNEL_FACTORIES,
        kind="channel plugin",
        config=config,
    )
