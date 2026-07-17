"""Model provider and adapter plugin loading."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from toolang.base.protocols.model import ModelAdapter, ModelProvider

from toolang.plugin.loading import load_plugins
from .config import load_model_provider_configs


def load_model_providers(
    root: Path | None = None,
    name: str | None = None,
) -> dict[str, ModelProvider]:
    configs = (
        load_model_provider_configs(root, name)
        if root is not None and name is not None
        else {}
    )
    return cast(
        dict[str, ModelProvider],
        load_plugins(
            group="toolang.model_provider",
            config={key: _provider_config(value) for key, value in configs.items()},
        ),
    )


def load_model_adapters() -> dict[str, ModelAdapter]:
    return cast(
        dict[str, ModelAdapter],
        load_plugins(group="toolang.model_adapter"),
    )


def _provider_config(config: object | None) -> Mapping[str, Any]:
    if config is None:
        return {}
    return {
        "endpoint": getattr(config, "endpoint", None),
        "key_env": getattr(config, "key_env", None),
        "adapter": getattr(config, "adapter", None),
        "scope": getattr(config, "scope", None),
        "options": getattr(config, "options", {}),
        "details": getattr(config, "details", None),
    }
