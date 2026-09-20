"""Models.dev-compatible catalog record parsing, validation, and snapshot rebuild."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from typing import cast

from toolang.base.types.model import (
    Model,
    ModelCatalogSnapshot,
    ModelToolang,
    Provider,
    ProviderToolang,
    normalized_env,
)

_PROVIDER_FIELDS = frozenset({"id", "env", "npm", "api", "name", "doc", "models"})
_MODEL_FIELDS = frozenset(
    {
        "id",
        "name",
        "description",
        "family",
        "attachment",
        "reasoning",
        "reasoning_options",
        "tool_call",
        "interleaved",
        "structured_output",
        "temperature",
        "knowledge",
        "release_date",
        "last_updated",
        "modalities",
        "open_weights",
        "limit",
        "status",
        "experimental",
        "provider",
        "cost",
    }
)


def parse_model_catalog_data(data: object) -> dict[str, Provider]:
    """Validate parsed JSON and return typed providers."""

    data = _provider_map_from_catalog_data(data)
    providers: dict[str, Provider] = {}
    for raw_provider_id, raw_provider in data.items():
        if not isinstance(raw_provider_id, str) or not raw_provider_id.strip():
            raise TypeError("model catalog provider keys must be non-empty strings")
        provider_id = raw_provider_id.strip()
        if not isinstance(raw_provider, Mapping):
            raise TypeError(f"provider {provider_id!r} must be an object")
        providers[provider_id] = _parse_provider(
            provider_id,
            cast(Mapping[str, object], raw_provider),
        )
    return providers


def model_catalog_snapshot_from_data(
    data: object,
    *,
    revision: str,
    source: Path | None = None,
) -> ModelCatalogSnapshot:
    """Validate normalized catalog data and rebuild one immutable snapshot."""

    providers = parse_model_catalog_data(data)
    models = tuple(
        provider.models[model_id]
        for provider_id in sorted(providers)
        for model_id in sorted(providers[provider_id].models)
        for provider in (providers[provider_id],)
    )
    return ModelCatalogSnapshot(
        providers=providers,
        models=models,
        revision=revision,
        source=source,
    )


def _provider_map_from_catalog_data(data: object) -> Mapping[object, object]:
    if not isinstance(data, Mapping):
        raise TypeError("model catalog must be a provider or combined catalog object")
    mapping = cast(Mapping[object, object], data)
    if set(mapping) == {"models", "providers"}:
        raw_models = mapping["models"]
        raw_providers = mapping["providers"]
        if not isinstance(raw_models, Mapping):
            raise ValueError("combined model catalog models must be an object")
        if not isinstance(raw_providers, Mapping):
            raise ValueError("combined model catalog providers must be an object")
        return cast(Mapping[object, object], raw_providers)
    if _is_provider_agnostic_model_map(mapping):
        raise ValueError(
            "models.dev models.json contains provider-agnostic metadata and cannot "
            "configure model execution; use https://models.dev/catalog.json or "
            "https://models.dev/api.json"
        )
    return mapping


def _is_provider_agnostic_model_map(data: Mapping[object, object]) -> bool:
    if not data:
        return False
    for key, value in data.items():
        if not isinstance(key, str) or "/" not in key or not isinstance(value, Mapping):
            return False
        record = cast(Mapping[object, object], value)
        if record.get("id") != key or "models" in record:
            return False
    return True


def _parse_provider(
    provider_id: str,
    data: Mapping[str, object],
) -> Provider:
    parsed_id = _required_text(data.get("id"), label=f"provider {provider_id} id")
    if parsed_id != provider_id:
        raise ValueError(
            f"provider key {provider_id!r} does not match id {parsed_id!r}"
        )
    raw_models = data.get("models")
    if not isinstance(raw_models, Mapping):
        raise TypeError(f"provider {provider_id!r} models must be an object")
    models: dict[str, Model] = {}
    for raw_model_id, raw_model in raw_models.items():
        if not isinstance(raw_model_id, str) or not raw_model_id.strip():
            raise TypeError(f"provider {provider_id!r} model keys must be strings")
        model_id = raw_model_id.strip()
        if not isinstance(raw_model, Mapping):
            raise TypeError(f"model {provider_id}/{model_id} must be an object")
        models[model_id] = _parse_model(
            provider_id,
            model_id,
            cast(Mapping[str, object], raw_model),
        )
    env = _string_list(data.get("env"), label=f"provider {provider_id} env")
    return Provider(
        id=provider_id,
        name=_required_text(data.get("name"), label=f"provider {provider_id} name"),
        env=env,
        npm=_required_text(data.get("npm"), label=f"provider {provider_id} npm"),
        api=_optional_text(data.get("api"), label=f"provider {provider_id} api"),
        doc=_optional_text(data.get("doc"), label=f"provider {provider_id} doc"),
        models=models,
        _toolang=ProviderToolang(env=normalized_env(env)),
        extra={
            key: value for key, value in data.items() if key not in _PROVIDER_FIELDS
        },
    )


def _parse_model(
    provider_id: str,
    model_id: str,
    data: Mapping[str, object],
) -> Model:
    parsed_id = _required_text(
        data.get("id"), label=f"model {provider_id}/{model_id} id"
    )
    if parsed_id != model_id:
        raise ValueError(
            f"model key {provider_id}/{model_id} does not match id {parsed_id!r}"
        )
    label = f"model {provider_id}/{model_id}"
    modalities = _modalities(data.get("modalities"), label=label)
    limit = _limits(data.get("limit"), label=label)
    cost = _optional_mapping(data.get("cost"), label=f"{label} cost")
    if cost is not None:
        _validate_non_negative_numbers(cost, label=f"{label} cost")
    reasoning_options = _reasoning_options(data.get("reasoning_options"), label=label)
    interleaved = data.get("interleaved")
    if interleaved is not None and not isinstance(interleaved, bool | Mapping):
        raise TypeError(f"{label} interleaved must be a boolean or object")
    return Model(
        id=model_id,
        name=_required_text(data.get("name"), label=f"{label} name"),
        _toolang=ModelToolang(provider=provider_id),
        description=_optional_text(
            data.get("description"), label=f"{label} description"
        ),
        family=_optional_text(data.get("family"), label=f"{label} family"),
        attachment=_optional_bool(data.get("attachment"), label=f"{label} attachment"),
        reasoning=_optional_bool(data.get("reasoning"), label=f"{label} reasoning"),
        reasoning_options=reasoning_options,
        tool_call=_optional_bool(data.get("tool_call"), label=f"{label} tool_call"),
        interleaved=(
            dict(cast(Mapping[str, object], interleaved))
            if isinstance(interleaved, Mapping)
            else interleaved
        ),
        structured_output=_optional_bool(
            data.get("structured_output"), label=f"{label} structured_output"
        ),
        temperature=_optional_bool(
            data.get("temperature"), label=f"{label} temperature"
        ),
        knowledge=_optional_text(data.get("knowledge"), label=f"{label} knowledge"),
        release_date=_optional_text(
            data.get("release_date"), label=f"{label} release_date"
        ),
        last_updated=_optional_text(
            data.get("last_updated"), label=f"{label} last_updated"
        ),
        modalities=modalities,
        open_weights=_optional_bool(
            data.get("open_weights"), label=f"{label} open_weights"
        ),
        limit=limit,
        status=_optional_text(data.get("status"), label=f"{label} status"),
        experimental=_optional_mapping(
            data.get("experimental"), label=f"{label} experimental"
        ),
        provider=_optional_mapping(data.get("provider"), label=f"{label} provider"),
        cost=cost,
        extra={key: value for key, value in data.items() if key not in _MODEL_FIELDS},
    )


def _required_text(value: object, *, label: str) -> str:
    text = _optional_text(value, label=label)
    if text is None:
        raise ValueError(f"{label} is required")
    return text


def _optional_text(value: object, *, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{label} must be text")
    text = value.strip()
    return text or None


def _optional_bool(value: object, *, label: str) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise TypeError(f"{label} must be a boolean")
    return value


def _optional_mapping(value: object, *, label: str) -> dict[str, object] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} must be an object")
    result = {str(key): item for key, item in value.items()}
    _validate_json(result, label=label)
    return result


def _string_list(value: object, *, label: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise TypeError(f"{label} must be an array")
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise TypeError(f"{label} must contain non-empty strings")
    return tuple(cast(str, item).strip() for item in value)


def _modalities(value: object, *, label: str) -> dict[str, tuple[str, ...]]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} modalities must be an object")
    result: dict[str, tuple[str, ...]] = {}
    for key, items in value.items():
        if not isinstance(key, str):
            raise TypeError(f"{label} modality keys must be strings")
        result[key] = _string_list(items, label=f"{label} modalities.{key}")
    return result


def _limits(value: object, *, label: str) -> dict[str, int]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} limit must be an object")
    result: dict[str, int] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise TypeError(f"{label} limit keys must be strings")
        if isinstance(item, bool) or not isinstance(item, int) or item < 0:
            raise TypeError(f"{label} limit.{key} must be a non-negative integer")
        result[key] = item
    return result


def _reasoning_options(
    value: object,
    *,
    label: str,
) -> tuple[Mapping[str, object], ...] | None:
    if value is None:
        return None
    if not isinstance(value, list) or any(
        not isinstance(item, Mapping) for item in value
    ):
        raise TypeError(f"{label} reasoning_options must be an array of objects")
    options = tuple(dict(cast(Mapping[str, object], item)) for item in value)
    _validate_json(options, label=f"{label} reasoning_options")
    return options


def _validate_json(value: object, *, label: str) -> None:
    if value is None or isinstance(value, str | bool | int | Decimal):
        return
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError(f"{label} object keys must be strings")
        for item in value.values():
            _validate_json(item, label=label)
        return
    if isinstance(value, list | tuple):
        for item in value:
            _validate_json(item, label=label)
        return
    raise TypeError(f"{label} contains unsupported {type(value).__name__}")


def _validate_non_negative_numbers(value: object, *, label: str) -> None:
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return
    if isinstance(value, int | Decimal):
        if value < 0:
            raise ValueError(f"{label} must not contain negative numbers")
        return
    if isinstance(value, Mapping):
        for item in value.values():
            _validate_non_negative_numbers(item, label=label)
        return
    if isinstance(value, list | tuple):
        for item in value:
            _validate_non_negative_numbers(item, label=label)
