"""Model configuration loading helpers."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, cast

from toolang.base.error import ToolangError
from toolang.base.types.model import ModelAlias


@dataclass(frozen=True, slots=True)
class ModelProviderConfig:
    """Local configuration overrides for one model provider."""

    name: str
    endpoint: str | None = None
    key_env: str | None = None
    adapter: str | None = None
    scope: str | None = None
    options: dict[str, Any] = field(default_factory=dict)
    details: str | None = None


def parse_model_aliases(
    config_layers: Sequence[Mapping[str, object]],
) -> dict[str, ModelAlias]:
    """Parse named model aliases from resolved config layers."""

    aliases: dict[str, ModelAlias] = {}
    for payload in config_layers:
        models_table = _models_table(payload)
        raw_aliases = models_table.get("aliases")
        if not isinstance(raw_aliases, Mapping):
            continue
        for name, value in raw_aliases.items():
            if not isinstance(name, str) or not isinstance(value, Mapping):
                continue
            aliases[name] = parse_model_alias(
                name, cast(Mapping[str, object], value)
            )
    return aliases


def parse_model_provider_configs(
    config_layers: Sequence[Mapping[str, object]],
) -> dict[str, ModelProviderConfig]:
    """Parse model provider configuration overrides."""

    configs: dict[str, ModelProviderConfig] = {}
    for payload in config_layers:
        models_table = _models_table(payload)
        raw_providers = models_table.get("providers")
        if not isinstance(raw_providers, Mapping):
            continue
        for name, value in raw_providers.items():
            if not isinstance(name, str) or not isinstance(value, Mapping):
                continue
            configs[name] = parse_model_provider_config(
                name, cast(Mapping[str, object], value)
            )
    return configs


def parse_default_models(
    config_layers: Sequence[Mapping[str, object]],
) -> tuple[str, ...]:
    """Parse default model selectors from resolved config layers."""

    defaults: tuple[str, ...] = ()
    for payload in config_layers:
        models_table = _models_table(payload)
        raw_default = models_table.get("default")
        if isinstance(raw_default, str):
            defaults = (raw_default.strip(),) if raw_default.strip() else ()
        elif isinstance(raw_default, Sequence) and not isinstance(
            raw_default, (str, bytes, bytearray)
        ):
            defaults = tuple(str(item).strip() for item in raw_default if str(item).strip())
    return defaults


def parse_model_alias(name: str, payload: Mapping[str, object]) -> ModelAlias:
    """Parse one `[models.aliases.<name>]` table."""

    ref = _required_model_config_str(payload, "ref", config_name=name, kind="model alias")
    provider = _optional_model_config_str(payload.get("provider")) or "custom"
    model = _optional_model_config_str(payload.get("model"))
    display_name = _optional_model_config_str(payload.get("name"))
    adapter = _optional_model_config_str(payload.get("adapter"))
    endpoint = _optional_model_config_str(payload.get("endpoint"))
    key_env = _optional_model_config_str(payload.get("key_env"))
    scope = _optional_model_config_str(payload.get("scope"))
    tags = _model_config_str_tuple(payload.get("tags"))
    tools = _optional_model_config_bool(payload.get("tools"))
    streaming = _optional_model_config_bool(payload.get("streaming"))
    headers = _model_config_string_table(payload.get("headers"))
    options = (
        dict(cast(Mapping[str, object], payload.get("options", {})))
        if isinstance(payload.get("options"), Mapping)
        else {}
    )
    details = _optional_model_config_str(payload.get("details"))
    return ModelAlias(
        name=name,
        ref=ref,
        provider=provider,
        model=model,
        display_name=display_name,
        adapter=adapter,
        endpoint=endpoint,
        key_env=key_env,
        scope=scope,
        tags=tags,
        tools=tools,
        streaming=streaming,
        headers=headers,
        options=options,
        details=details,
    )


def parse_model_provider_config(
    name: str, payload: Mapping[str, object]
) -> ModelProviderConfig:
    """Parse one `[models.providers.<name>]` table."""

    options = (
        dict(cast(Mapping[str, object], payload.get("options", {})))
        if isinstance(payload.get("options"), Mapping)
        else {}
    )
    return ModelProviderConfig(
        name=name,
        endpoint=_optional_model_config_str(payload.get("endpoint")),
        key_env=_optional_model_config_str(payload.get("key_env")),
        adapter=_optional_model_config_str(payload.get("adapter")),
        scope=_optional_model_config_str(payload.get("scope")),
        options=options,
        details=_optional_model_config_str(payload.get("details")),
    )


def _models_table(payload: Mapping[str, object]) -> Mapping[str, object]:
    raw_models = payload.get("models")
    if not isinstance(raw_models, Mapping):
        return {}
    return cast(Mapping[str, object], raw_models)


def _required_model_config_str(
    payload: Mapping[str, object],
    key: str,
    *,
    config_name: str,
    kind: str,
) -> str:
    value = _optional_model_config_str(payload.get(key))
    if value is None:
        raise ToolangError(f"{kind} {config_name!r} is missing {key}")
    return value


def _optional_model_config_str(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _optional_model_config_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    return None


def _model_config_str_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(
        value, (str, bytes, bytearray)
    ):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())


def _model_config_string_table(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, str] = {}
    for key, item in value.items():
        text_key = _optional_model_config_str(key)
        text_value = _optional_model_config_str(item)
        if text_key is None or text_value is None:
            continue
        result[text_key] = text_value
    return result
