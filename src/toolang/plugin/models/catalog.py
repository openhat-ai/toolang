"""Models.dev-compatible catalog loading, snapshots, filtering, and export."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import asyncio
from dataclasses import dataclass, replace
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
from typing import cast

from toolang.base.protocols.model import ModelCatalog
from toolang.base.types.model import Model, ModelCatalogSnapshot, ModelInfo, Provider
from toolang.common.layout import AgentLayout
from toolang.common.selectors import filter_value_matches, parse_selector

MODEL_CATALOG_ENV = "TOOLANG_MODEL_CATALOG"
DEFAULT_MAX_CATALOG_BYTES = 32 * 1024 * 1024
PACKAGED_MODEL_CATALOG = Path(__file__).parent / "data" / "models.json"

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


@dataclass(frozen=True, slots=True)
class ModelsDevModelCatalog(ModelCatalog):
    """One complete models.dev-compatible file-backed catalog."""

    path: Path
    max_bytes: int = DEFAULT_MAX_CATALOG_BYTES
    name: str = "models_dev"

    async def snapshot(self) -> ModelCatalogSnapshot:
        """Load and validate the selected catalog file."""

        return read_model_catalog_snapshot(self.path, max_bytes=self.max_bytes)


@dataclass(frozen=True, slots=True)
class MergedModelCatalog(ModelCatalog):
    """Merge exact provider/model records from ordered catalog sources."""

    sources: tuple[ModelCatalog, ...]
    name: str = "merged"

    async def snapshot(self) -> ModelCatalogSnapshot:
        """Load sources in order and reject conflicting exact identities."""

        snapshots = list(
            await asyncio.gather(*(source.snapshot() for source in self.sources))
        )
        if not snapshots:
            return ModelCatalogSnapshot(providers={}, models=(), revision="sha256:0")
        providers: dict[str, Provider] = {}
        models: dict[tuple[str, str], Model] = {}
        for source, raw_snapshot in zip(self.sources, snapshots, strict=True):
            snapshot = _with_catalog_origin(raw_snapshot, source.name)
            for provider_id, provider in snapshot.providers.items():
                existing = providers.get(provider_id)
                if existing is not None and not (existing.local and provider.local):
                    raise ValueError(f"duplicate catalog provider: {provider_id}")
                providers[provider_id] = provider
            for model in snapshot.models:
                identity = (model.provider_id, model.id)
                if identity in models:
                    raise ValueError(f"duplicate catalog model: {model.identity}")
                models[identity] = model
        return ModelCatalogSnapshot(
            providers=providers,
            models=tuple(models[key] for key in sorted(models)),
            revision=snapshots[0].revision,
            source=snapshots[0].source,
        )


def create_models_dev_model_catalog(config: Mapping[str, object]) -> ModelCatalog:
    """Create the built-in models.dev file catalog plugin."""

    value = config.get("path")
    if not isinstance(value, str | Path):
        raise ValueError("models_dev catalog requires path")
    max_bytes = config.get("max_bytes", DEFAULT_MAX_CATALOG_BYTES)
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int):
        raise TypeError("models_dev catalog max_bytes must be an integer")
    return ModelsDevModelCatalog(Path(value), max_bytes=max_bytes)


def resolve_model_catalog_path(
    layout: AgentLayout,
    *,
    explicit: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """Resolve one complete catalog using explicit, home, root, package precedence."""

    if explicit is not None:
        path = explicit.expanduser().resolve(strict=False)
        _require_catalog_candidate(path, label="explicit model catalog")
        return path
    values = environ or {}
    configured = str(values.get(MODEL_CATALOG_ENV, "")).strip()
    if configured:
        path = Path(configured).expanduser().resolve(strict=False)
        _require_catalog_candidate(path, label=MODEL_CATALOG_ENV)
        return path
    for path in (layout.home / "models.json", layout.root / "models.json"):
        if path.is_file() or path.is_symlink():
            return path.resolve(strict=False)
    _require_catalog_candidate(PACKAGED_MODEL_CATALOG, label="packaged model catalog")
    return PACKAGED_MODEL_CATALOG.resolve(strict=False)


def read_model_catalog_snapshot(
    path: Path,
    *,
    max_bytes: int = DEFAULT_MAX_CATALOG_BYTES,
) -> ModelCatalogSnapshot:
    """Load one complete validated models.dev-compatible provider map."""

    resolved = path.expanduser().resolve(strict=True)
    payload_bytes = resolved.read_bytes()
    if len(payload_bytes) > max_bytes:
        raise ValueError(f"model catalog exceeds {max_bytes} bytes: {resolved}")
    try:
        payload = json.loads(
            payload_bytes,
            parse_float=Decimal,
            parse_constant=_reject_json_constant,
        )
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid model catalog JSON: {resolved}: {exc}") from exc
    providers = parse_model_catalog_data(payload)
    models = tuple(
        provider.models[model_id]
        for provider_id in sorted(providers)
        for model_id in sorted(providers[provider_id].models)
        for provider in (providers[provider_id],)
    )
    return _with_catalog_origin(
        ModelCatalogSnapshot(
            providers=providers,
            models=models,
            revision=f"sha256:{sha256(payload_bytes).hexdigest()}",
            source=resolved,
        ),
        "models_dev",
    )


def parse_model_catalog_data(data: object) -> dict[str, Provider]:
    """Validate parsed JSON and return typed providers."""

    if not isinstance(data, Mapping):
        raise TypeError("model catalog must be a provider object")
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


def catalog_json_dumps(data: object, *, indent: int | None = 2) -> str:
    """Serialize catalog JSON without converting Decimal values through float."""

    separator = ": " if indent is not None else ":"
    item_separator = "," if indent is None else ","

    def encode(value: object, level: int) -> str:
        if value is None:
            return "null"
        if value is True:
            return "true"
        if value is False:
            return "false"
        if isinstance(value, str):
            return json.dumps(value, ensure_ascii=False)
        if isinstance(value, int) and not isinstance(value, bool):
            return str(value)
        if isinstance(value, Decimal):
            if not value.is_finite():
                raise ValueError("catalog decimals must be finite")
            return format(value, "f")
        if isinstance(value, float):
            return format(Decimal(str(value)), "f")
        if isinstance(value, Mapping):
            items = sorted(value.items(), key=lambda item: str(item[0]))
            if not items:
                return "{}"
            if indent is None:
                return (
                    "{"
                    + item_separator.join(
                        json.dumps(str(key), ensure_ascii=False)
                        + separator
                        + encode(item, level + 1)
                        for key, item in items
                    )
                    + "}"
                )
            prefix = " " * indent * (level + 1)
            closing = " " * indent * level
            return (
                "{\n"
                + ",\n".join(
                    prefix
                    + json.dumps(str(key), ensure_ascii=False)
                    + separator
                    + encode(item, level + 1)
                    for key, item in items
                )
                + f"\n{closing}}}"
            )
        if isinstance(value, Sequence) and not isinstance(
            value, (str, bytes, bytearray)
        ):
            if not value:
                return "[]"
            if indent is None:
                return (
                    "["
                    + item_separator.join(encode(item, level + 1) for item in value)
                    + "]"
                )
            prefix = " " * indent * (level + 1)
            closing = " " * indent * level
            return (
                "[\n"
                + ",\n".join(prefix + encode(item, level + 1) for item in value)
                + f"\n{closing}]"
            )
        raise TypeError(f"unsupported catalog JSON value: {type(value).__name__}")

    return encode(data, 0) + "\n"


def filter_catalog_models(
    snapshot: ModelCatalogSnapshot,
    selectors: Sequence[str],
    *,
    include_local: bool = True,
    available: set[str] | None = None,
    adapters: Mapping[str, str] | None = None,
) -> tuple[Model, ...]:
    """Filter catalog models with model selector syntax and OR repeated selectors."""

    if not selectors:
        return tuple(
            model for model in snapshot.models if include_local or not model.local
        )
    parsed = tuple(parse_selector(value, domain="model") for value in selectors)
    return tuple(
        model
        for model in snapshot.models
        if include_local or not model.local
        if any(
            _model_matches(
                model,
                selector.pattern,
                selector.filters,
                available=available,
                adapter=(adapters or {}).get(model.identity),
            )
            for selector in parsed
        )
    )


def model_info_from_catalog(
    model: Model,
    *,
    adapter: str | None = None,
    revision: str | None = None,
) -> ModelInfo:
    """Build the one-cycle execution/listing projection for a catalog model."""

    context = model.limit.get("context")
    output = model.limit.get("output")
    input_price = _cost_per_token(model.cost, "input")
    output_price = _cost_per_token(model.cost, "output")
    selectors = [model.id, model.identity, model.name]
    if model.family:
        selectors.append(model.family)
    return ModelInfo(
        ref=model.identity,
        provider=model.provider_id,
        name=model.name,
        model=model.id,
        selectors=tuple(dict.fromkeys(selectors)),
        adapter=adapter
        or (model.resolved.adapter if model.resolved else None)
        or "unknown",
        scope="local" if model.local else "remote",
        tools=model.tool_call is True,
        streaming=True,
        context_window=context,
        max_output_tokens=output,
        input_price=input_price,
        output_price=output_price,
        details=model.description,
        metadata={
            "catalog": model.catalog,
            "catalog_revision": model.catalog_revision or revision,
            "resolved_api": model.resolved.api if model.resolved else None,
            "resolved_ready": model.resolved.ready if model.resolved else False,
            "family": model.family,
            "reasoning": model.reasoning,
            "reasoning_options": [
                dict(option) for option in model.reasoning_options or ()
            ],
            "tool_call": model.tool_call,
            "temperature": model.temperature,
            "structured_output": model.structured_output,
            "attachment": model.attachment,
            "modalities": {key: list(value) for key, value in model.modalities.items()},
            "status": model.status,
            "experimental": (
                dict(model.experimental) if model.experimental is not None else None
            ),
            "provider": dict(model.provider) if model.provider is not None else None,
            "local": model.local,
        },
    )


def _with_catalog_origin(
    snapshot: ModelCatalogSnapshot,
    name: str,
) -> ModelCatalogSnapshot:
    """Attach runtime-only source provenance to every raw catalog record."""

    catalog = "models.dev" if name == "models_dev" else name
    models = {
        (model.provider_id, model.id): replace(
            model,
            catalog=model.catalog or catalog,
            catalog_revision=model.catalog_revision or snapshot.revision,
        )
        for model in snapshot.models
    }
    providers: dict[str, Provider] = {}
    for provider_id, provider in snapshot.providers.items():
        provider_models = {
            model_id: models.get((provider_id, model_id), model)
            for model_id, model in provider.models.items()
        }
        providers[provider_id] = replace(
            provider,
            models=provider_models,
            catalog=provider.catalog or catalog,
            catalog_revision=provider.catalog_revision or snapshot.revision,
        )
    return ModelCatalogSnapshot(
        providers=providers,
        models=tuple(
            models[(model.provider_id, model.id)] for model in snapshot.models
        ),
        revision=snapshot.revision,
        source=snapshot.source,
    )


def _parse_provider(provider_id: str, data: Mapping[str, object]) -> Provider:
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
        provider_id=provider_id,
        id=model_id,
        name=_required_text(data.get("name"), label=f"{label} name"),
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


def _model_matches(
    model: Model,
    pattern: str,
    filters: Mapping[str, tuple[str, ...]],
    *,
    available: set[str] | None,
    adapter: str | None,
) -> bool:
    from fnmatch import fnmatchcase

    identity = model.identity
    text = pattern.strip() or "*"
    if text != "*" and not any(
        fnmatchcase(value, text)
        for value in (identity, model.id, model.name, model.family or "")
    ):
        return False
    for raw_key, expected in filters.items():
        key = "tool_call" if raw_key == "tools" else raw_key
        actual = _model_filter_values(
            model,
            key,
            available=available,
            adapter=adapter,
        )
        if not actual or not any(
            filter_value_matches(value, expected) for value in actual
        ):
            return False
    return True


def _model_filter_values(
    model: Model,
    key: str,
    *,
    available: set[str] | None,
    adapter: str | None,
) -> tuple[str, ...]:
    if key == "provider":
        return (model.provider_id,)
    if key == "family":
        return (model.family,) if model.family is not None else ()
    if key in {
        "reasoning",
        "tool_call",
        "temperature",
        "structured_output",
        "attachment",
        "open_weights",
    }:
        value = getattr(model, key)
        return (("true" if value else "false"),) if isinstance(value, bool) else ()
    if key == "status":
        return (model.status,) if model.status is not None else ()
    if key in {"available", "availability"} and available is not None:
        return ("true" if model.identity in available else "false",)
    if key == "adapter":
        return (adapter,) if adapter is not None else ()
    if key == "scope":
        return ("local" if model.local else "remote",)
    if key.startswith("modalities."):
        return model.modalities.get(key.partition(".")[2], ())
    return ()


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


def _cost_per_token(cost: Mapping[str, object] | None, key: str) -> float | None:
    if cost is None:
        return None
    value = cost.get(key)
    if isinstance(value, bool) or not isinstance(value, int | Decimal):
        return None
    return float(Decimal(value) / Decimal(1_000_000))


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON numeric constant: {value}")


def _require_catalog_candidate(path: Path, *, label: str) -> None:
    if not path.is_file():
        raise FileNotFoundError(f"{label} not found: {path}")
