"""Flat cata catalog parsing, validation, and snapshot reconstruction."""

from __future__ import annotations

import math
from collections.abc import Mapping
from pathlib import Path
from typing import cast

import msgspec

from toolang.base.types.model import (
    Model,
    ModelCatalogSnapshot,
    ModelProvider,
    ModelToolang,
    Provider,
)


def parse_model_catalog_data(
    data: object,
) -> tuple[dict[str, Provider], tuple[Model, ...]]:
    """Parse the normalized flat ``{providers, models}`` catalog format."""

    if not isinstance(data, Mapping) or set(data) != {"providers", "models"}:
        raise ValueError(
            "model catalog must use the flat cata format with providers and models arrays"
        )
    catalog = cast(Mapping[str, object], data)
    raw_providers = catalog["providers"]
    raw_models = catalog["models"]
    if not isinstance(raw_providers, list):
        raise TypeError("flat model catalog providers must be an array")
    if not isinstance(raw_models, list):
        raise TypeError("flat model catalog models must be an array")

    providers: dict[str, Provider] = {}
    for index, raw_provider in enumerate(raw_providers):
        if not isinstance(raw_provider, Mapping):
            raise TypeError(f"provider at index {index} must be an object")
        provider = _parse_flat_provider(cast(Mapping[str, object], raw_provider))
        if provider.id in providers:
            raise ValueError(f"duplicate catalog provider: {provider.id}")
        providers[provider.id] = provider

    models: list[Model] = []
    identities: set[tuple[str, str]] = set()
    for index, raw_model in enumerate(raw_models):
        if not isinstance(raw_model, Mapping):
            raise TypeError(f"model at index {index} must be an object")
        row = cast(Mapping[str, object], raw_model)
        if "ref" in row:
            raise ValueError(
                f"model at index {index} must have provider and id, not ref"
            )
        if "provider_override" in row:
            raise ValueError(
                f"model at index {index} must use override, not provider_override"
            )
        provider_id = _required_text(
            row.get("provider"), label=f"model at index {index} provider"
        )
        model_id = _required_text(row.get("id"), label=f"model at index {index} id")
        if provider_id not in providers:
            raise ValueError(
                f"model {provider_id}/{model_id} does not name a catalog provider"
            )
        identity = (provider_id, model_id)
        if identity in identities:
            raise ValueError(f"duplicate catalog model: {provider_id}/{model_id}")
        identities.add(identity)
        models.append(_parse_model(provider_id, model_id, row))
    return providers, tuple(models)


def model_catalog_snapshot_from_data(
    data: object,
    *,
    revision: str,
    source: Path | None = None,
) -> ModelCatalogSnapshot:
    """Validate normalized flat catalog data and preserve its file order."""

    providers, models = parse_model_catalog_data(data)
    return ModelCatalogSnapshot(
        providers=providers,
        models=models,
        revision=revision,
        source=source,
    )


def _parse_flat_provider(data: Mapping[str, object]) -> Provider:
    """Validate one provider entry from a flat cata catalog."""

    provider_id = _required_text(data.get("id"), label="provider id")
    name = _required_text(data.get("name"), label=f"provider {provider_id} name")
    npm = _required_text(data.get("npm"), label=f"provider {provider_id} npm")
    env = _string_list(data.get("env"), label=f"provider {provider_id} env")
    return Provider(
        id=provider_id,
        name=name,
        env=env,
        npm=npm,
        api=_optional_text(data.get("api"), label=f"provider {provider_id} api"),
        doc=_optional_text(data.get("doc"), label=f"provider {provider_id} doc"),
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
        provider=_model_provider(data.get("override"), label=f"{label} override"),
        cost=cost,
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
        if item == 0:
            # models.dev reports zero for an unknown or not-applicable limit.
            # Toolang represents an unknown limit by the absence of the key.
            continue
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
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{label} numbers must be finite")
        return
    if value is None or isinstance(value, str | bool | int):
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
    if isinstance(value, int | float):
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


def _model_provider(value: object, *, label: str) -> ModelProvider | None:
    block = _optional_mapping(value, label=label)
    if block is None:
        return None
    # External catalog JSON cannot declare trusted Toolang routing metadata.
    block.pop("_toolang", None)
    return msgspec.convert(block, type=ModelProvider)
