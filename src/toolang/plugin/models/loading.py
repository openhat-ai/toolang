"""Model catalog and adapter plugin loading."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from toolang.base.protocols.model import ModelAdapter, ModelCatalog

from toolang.plugin.loading import create_plugin, load_plugins


def load_model_adapters(
    config: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, ModelAdapter]:
    """Load installed model adapters with their plugin-owned configuration."""

    return cast(
        dict[str, ModelAdapter],
        load_plugins(group="toolang.model_adapter", config=config),
    )


def load_model_catalogs(
    config: Mapping[str, Mapping[str, Any]],
) -> dict[str, ModelCatalog]:
    """Load installed model catalog plugins with explicit runtime inputs."""

    catalogs: dict[str, ModelCatalog] = {}
    for name, plugin_config in config.items():
        try:
            catalog = cast(
                ModelCatalog,
                create_plugin(
                    name,
                    group="toolang.model_catalog",
                    config=plugin_config,
                ),
            )
        except ModuleNotFoundError:
            continue
        catalog_name = catalog.name.strip() or name
        catalogs.setdefault(catalog_name, catalog)
    return catalogs
