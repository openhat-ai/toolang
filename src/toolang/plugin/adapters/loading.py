"""Model adapter plugin loading."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from toolang.base.protocols.model import ModelAdapter

from toolang.plugin.loading import load_plugins


def load_model_adapters(
    config: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, ModelAdapter]:
    """Load installed model adapters with their plugin-owned configuration."""

    return cast(
        dict[str, ModelAdapter],
        load_plugins(group="toolang.model_adapter", config=config),
    )
