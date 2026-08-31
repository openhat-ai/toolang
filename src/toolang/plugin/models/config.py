"""Model configuration loading helpers."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, cast

from toolang.base.types.model import Provider


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    """Local configuration overrides for one model provider."""

    name: str
    endpoint: str | None = None
    key_env: str | None = None
    adapter: str | None = None
    scope: str | None = None
    options: dict[str, Any] = field(default_factory=dict)
    details: str | None = None


def configure_catalog_providers(
    providers: Mapping[str, Provider],
    configs: Mapping[str, ProviderConfig],
) -> dict[str, Provider]:
    """Apply runtime-only provider configuration without changing catalog data."""

    configured = dict(providers)
    for provider_id, config in configs.items():
        if provider_id in configured:
            continue
        configured[provider_id] = Provider(
            id=provider_id,
            name=provider_id,
            env=(),
            npm="@ai-sdk/openai-compatible",
            models={},
        )
    configured.setdefault(
        "custom",
        Provider(
            id="custom",
            name="Custom",
            env=(),
            npm="@ai-sdk/openai-compatible",
            models={},
        ),
    )
    return configured


def parse_provider_configs(
    config_layers: Sequence[Mapping[str, object]],
) -> dict[str, ProviderConfig]:
    """Parse model provider configuration overrides."""

    configs: dict[str, ProviderConfig] = {}
    for payload in config_layers:
        models_table = _models_table(payload)
        raw_providers = models_table.get("providers")
        if raw_providers is None:
            continue
        if not isinstance(raw_providers, Mapping):
            raise TypeError("models providers config must be a table")
        for name, value in raw_providers.items():
            if not isinstance(name, str):  # pragma: no cover - TOML key invariant
                raise TypeError("model provider config names must be strings")
            if not isinstance(value, Mapping):
                raise TypeError(f"model provider config must be a table: {name}")
            configs[name] = parse_provider_config(
                name, cast(Mapping[str, object], value)
            )
    return configs


def parse_provider_config(name: str, payload: Mapping[str, object]) -> ProviderConfig:
    """Parse one `[models.providers.<name>]` table."""

    allowed = {"endpoint", "key_env", "adapter", "scope", "options", "details"}
    unknown = sorted(str(field) for field in payload if field not in allowed)
    if unknown:
        raise ValueError(
            f"unknown model provider config field for {name}: {', '.join(unknown)}"
        )
    raw_options = payload.get("options", {})
    if not isinstance(raw_options, Mapping):
        raise TypeError(f"model provider options must be a table: {name}")
    options = dict(cast(Mapping[str, object], raw_options))
    return ProviderConfig(
        name=name,
        endpoint=_optional_model_config_str(payload.get("endpoint"), "endpoint"),
        key_env=_optional_model_config_str(payload.get("key_env"), "key_env"),
        adapter=_optional_model_config_str(payload.get("adapter"), "adapter"),
        scope=_optional_model_config_str(payload.get("scope"), "scope"),
        options=options,
        details=_optional_model_config_str(payload.get("details"), "details"),
    )


def _models_table(payload: Mapping[str, object]) -> Mapping[str, object]:
    raw_models = payload.get("models")
    if raw_models is None:
        return {}
    if not isinstance(raw_models, Mapping):
        raise TypeError("models config must be a table")
    models = cast(Mapping[str, object], raw_models)
    if "default" in models:
        raise ValueError(
            "[models].default is not supported; use [default].model with an exact ref"
        )
    if "aliases" in models:
        raise ValueError(
            "[models.aliases] is not supported; configure concrete catalog models"
        )
    unknown = sorted(str(name) for name in models if name != "providers")
    if unknown:
        raise ValueError(f"unknown models config field: {', '.join(unknown)}")
    return models


def _optional_model_config_str(value: object, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"model provider {field} must be a string")
    text = value.strip()
    return text or None
