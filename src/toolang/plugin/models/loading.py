"""Model catalog and adapter plugin loading."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from toolang.base.protocols.model import ModelAdapter, ModelCatalog

from toolang.plugin.loading import load_plugins


def load_model_adapters() -> dict[str, ModelAdapter]:
    return cast(
        dict[str, ModelAdapter],
        load_plugins(group="toolang.model_adapter"),
    )


def load_model_catalogs(
    config: Mapping[str, Mapping[str, Any]],
) -> dict[str, ModelCatalog]:
    """Load installed model catalog plugins with explicit runtime inputs."""

    return cast(
        dict[str, ModelCatalog],
        load_plugins(group="toolang.model_catalog", config=config),
    )
