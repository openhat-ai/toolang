"""Model provider loading."""

from __future__ import annotations

from collections.abc import Mapping
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any, Callable, cast

from toolang.base.protocols.model import ModelProvider
from toolang.models.config import load_model_provider_configs
from toolang.models.providers.custom import create_model as create_custom_model
from toolang.models.providers.ollama import create_model as create_ollama_model
from toolang.models.providers.openai import create_model as create_openai_model
from toolang.models.providers.openrouter import create_model as create_openrouter_model


def load_model_providers(
    toolang_root: Path | None = None,
    agent_name: str | None = None,
) -> dict[str, ModelProvider]:
    """Load all installed model providers for one uptime."""

    provider_configs = (
        load_model_provider_configs(toolang_root, agent_name)
        if toolang_root is not None and agent_name is not None
        else {}
    )
    providers: dict[str, ModelProvider] = {
        "custom": create_custom_model({}),
        "openai": create_openai_model(_provider_config_payload(provider_configs.get("openai"))),
        "openrouter": create_openrouter_model(_provider_config_payload(provider_configs.get("openrouter"))),
        "ollama": create_ollama_model(_provider_config_payload(provider_configs.get("ollama"))),
    }
    for entry_point in entry_points(group="toolang.model"):
        try:
            factory = cast(Callable[[Mapping[str, Any]], ModelProvider], entry_point.load())
        except ModuleNotFoundError:
            continue
        provider = factory(_provider_config_payload(provider_configs.get(entry_point.name)))
        if provider.name in providers:
            continue
        providers[provider.name] = provider
    return providers


def _provider_config_payload(config: object | None) -> Mapping[str, object]:
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
