"""Channel plugin loading."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from toolang.base.protocols.channel import AgentChannel

from toolang.plugin.loading import create_plugin


def create_channel(
    name: str,
    *,
    config: Mapping[str, Any] | None = None,
) -> AgentChannel:
    return cast(
        AgentChannel,
        create_plugin(name, group="toolang.channel", config=config),
    )
