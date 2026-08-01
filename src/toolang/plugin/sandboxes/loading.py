"""Sandbox hosting plugin loading."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from toolang.base.protocols.hosting import Hosting
from toolang.plugin.loading import create_plugin


def load_hosting(
    name: str,
    *,
    config: Mapping[str, Any] | None = None,
) -> Hosting:
    """Load one hosting implementation by sandbox name."""

    return cast(
        Hosting,
        create_plugin(name, group="toolang.sandbox", config=config),
    )
