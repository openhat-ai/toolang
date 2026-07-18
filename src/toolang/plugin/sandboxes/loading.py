"""Sandbox plugin loading."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from toolang.base.protocols.sandbox import AgentSandbox

from toolang.plugin.loading import create_plugin


def create_sandbox_plugin(
    name: str,
    *,
    config: Mapping[str, Any] | None = None,
) -> AgentSandbox:
    return cast(
        AgentSandbox,
        create_plugin(name, group="toolang.sandbox", config=config),
    )
