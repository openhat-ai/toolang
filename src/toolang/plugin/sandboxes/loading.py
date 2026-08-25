"""Sandbox plugin creation."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from toolang.base.protocols.sandbox import Sandbox
from toolang.plugin.loading import create_plugin


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
