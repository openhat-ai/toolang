"""Resolved model snapshot cache.

The cache stores the *resolved* catalog snapshot: every provider and model with
its `_toolang` facts. A hit therefore reproduces the effective routes without
re-resolving configuration or environment readiness.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
import re
from typing import cast

from toolang.base.types.model import (
    Model,
    ModelCatalogSnapshot,
    ModelToolang,
    Provider,
    ProviderToolang,
    ResolvedEnv,
    env_names,
)
from toolang.common.cache import (
    CACHE_SCHEMA,
    canonical_value,
    digest,
    load_document,
    require_fields,
    store_document,
)

_CATALOG_FILE = "effective.json"
_IDENTITY_FILE = "identity.json"
_REVISION_NAME_RE = re.compile(r"^[0-9a-f]{64}$")

_PROVIDER_FIELDS = frozenset(
    {"id", "name", "npm", "api", "doc", "env", "extra", "_toolang", "models"}
)
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
        "extra",
        "_toolang",
    }
)


@dataclass(frozen=True, slots=True)
class CachedModelProjection:
    """One cached resolved catalog snapshot."""

    snapshot: ModelCatalogSnapshot
    environment_names: tuple[str, ...] = ()
    key: str = ""


class ModelProjectionCache:
    """File-backed cache for the resolved model snapshot of one context."""

    def __init__(self, context_directory: Path) -> None:
        self._directory = context_directory

    def load_context(self, key: str) -> CachedModelProjection | None:
        """Read one cached resolved snapshot, treating any damage as a miss."""

        path = self._context_path(key)
        if not path.is_file():
            return None
        try:
            document = load_document(
                path,
                kind="context",
                key=key,
                fast_json=True,
            )
            snapshot = _snapshot_from_document(document)
            names = document.get("environment_names", ())
            environment_names = (
                tuple(str(name) for name in names)
                if isinstance(names, list | tuple)
                else ()
            )
        except Exception:
            return None
        return CachedModelProjection(
            snapshot=snapshot,
            environment_names=environment_names,
            key=key,
        )

    def store_context(
        self,
        key: str,
        *,
        snapshot: ModelCatalogSnapshot,
        environment_names: Sequence[str] = (),
    ) -> None:
        """Write one resolved snapshot; unsafe or oversized payloads are skipped."""

        document = _snapshot_document(
            snapshot,
            environment_names=tuple(environment_names),
        )
        self._directory.mkdir(parents=True, exist_ok=True)
        store_document(
            self._context_path(key),
            kind="context",
            key=key,
            document=document,
        )

    def catalog_identity_misses(
        self,
        *,
        kind: str,
        scope: str,
        catalog_revisions: Sequence[tuple[str, str]],
        setup_config: object,
        environ: object,
        plugin_provenance: object,
        allow_models: Sequence[str] | None,
        queries: object = None,
    ) -> bool | None:
        """Return whether a cached identity proves no match; unknown by default."""

        del kind, scope, catalog_revisions, setup_config, environ
        del plugin_provenance, allow_models, queries
        return None

    def _context_path(self, key: str) -> Path:
        return self._directory / _revision_name(key) / _CATALOG_FILE

    def _context_identity_path(self, key: str) -> Path:
        return self._directory / _revision_name(key) / _IDENTITY_FILE


def _revision_name(key: str) -> str:
    name = key.partition(":")[2] or key
    if not _REVISION_NAME_RE.match(name):
        raise ValueError(f"model cache key is not a content revision: {key!r}")
    return name


def model_projection_key(
    *,
    kind: str,
    scope: str,
    catalog_revisions: Sequence[tuple[str, str]],
    setup_config: object,
    environment_readiness: Mapping[str, bool],
    plugin_provenance: object,
    allow_models: Sequence[str] | None,
) -> str:
    """Return the content key for one resolved model context."""

    payload = {
        "schema": CACHE_SCHEMA,
        "kind": kind,
        "scope": scope,
        "catalogs": [list(item) for item in catalog_revisions],
        "config": canonical_value(setup_config),
        "readiness": {
            name: bool(value) for name, value in sorted(environment_readiness.items())
        },
        "provenance": canonical_value(plugin_provenance),
        "allow": None if allow_models is None else list(allow_models),
    }
    return digest(payload)


def environment_readiness(
    snapshot: ModelCatalogSnapshot,
    environ: Mapping[str, str],
) -> dict[str, bool]:
    """Return whether each declared environment name is present."""

    readiness: dict[str, bool] = {}
    for provider in snapshot.providers.values():
        for name in env_names(provider._toolang.env):
            readiness[name] = bool(str(environ.get(name, "")).strip())
    return readiness


# --------------------------------------------------------------------------- #
# codec
# --------------------------------------------------------------------------- #


def _snapshot_document(
    snapshot: ModelCatalogSnapshot,
    *,
    environment_names: tuple[str, ...],
) -> dict[str, object]:
    return {
        "providers": {
            provider_id: _provider_to_data(provider)
            for provider_id, provider in sorted(snapshot.providers.items())
        },
        "models": [_model_to_data(model) for model in snapshot.models],
        "local": snapshot.local,
        "environment_names": list(environment_names),
    }


def _snapshot_from_document(document: Mapping[str, object]) -> ModelCatalogSnapshot:
    require_fields(
        document,
        frozenset({"providers", "models"}),
        label="model context",
    )
    raw_providers = document["providers"]
    raw_models = document["models"]
    if not isinstance(raw_providers, Mapping) or not isinstance(raw_models, list):
        raise TypeError("model context payload must hold providers and models")
    providers = {
        str(provider_id): _provider_from_data(
            str(provider_id),
            cast(Mapping[str, object], raw_provider),
        )
        for provider_id, raw_provider in cast(
            Mapping[object, object], raw_providers
        ).items()
    }
    models = tuple(
        _model_from_data(cast(Mapping[str, object], raw_model))
        for raw_model in raw_models
    )
    return ModelCatalogSnapshot(
        providers=providers,
        models=models,
        revision="cache",
        local=bool(document.get("local", False)),
    )


def _provider_to_data(provider: Provider) -> dict[str, object]:
    return {
        "id": provider.id,
        "name": provider.name,
        "npm": provider.npm,
        "api": provider.api,
        "doc": provider.doc,
        "env": list(provider.env),
        "extra": dict(provider.extra),
        "models": {
            model_id: _model_to_data(model)
            for model_id, model in sorted(provider.models.items())
        },
        "_toolang": {
            "env": _env_to_data(provider._toolang.env),
            "adapter": provider._toolang.adapter,
            "local": provider._toolang.local,
        },
    }


def _provider_from_data(
    provider_id: str,
    data: Mapping[str, object],
) -> Provider:
    toolang = _mapping(data, "_toolang")
    raw_models = data.get("models")
    if not isinstance(raw_models, Mapping):
        raise TypeError("cached provider models must be an object")
    models = {
        str(model_id): _model_from_data(
            cast(Mapping[str, object], raw_model),
        )
        for model_id, raw_model in cast(Mapping[object, object], raw_models).items()
    }
    return Provider(
        id=provider_id,
        name=_text(data, "name"),
        models=models,
        _toolang=ProviderToolang(
            env=_env_from_data(toolang.get("env")),
            adapter=_optional_text(toolang, "adapter"),
            local=bool(toolang.get("local", False)),
        ),
        npm=_optional_text(data, "npm"),
        api=_optional_text(data, "api"),
        doc=_optional_text(data, "doc"),
        env=_string_list(data.get("env")),
        extra={
            str(key): value
            for key, value in data.items()
            if key not in _PROVIDER_FIELDS
        },
    )


def _model_to_data(model: Model) -> dict[str, object]:
    data = model.to_data()
    if model.provider is not None:
        data["provider"] = dict(model.provider)
    data["_toolang"] = {
        "ready": model._toolang.ready,
        "provider": model._toolang.provider,
    }
    return data


def _model_from_data(data: Mapping[str, object]) -> Model:
    toolang = _mapping(data, "_toolang")
    provider = _optional_mapping(data.get("provider"))
    return Model(
        id=_text(data, "id"),
        name=_text(data, "name"),
        _toolang=ModelToolang(
            ready=bool(toolang.get("ready", False)),
            provider=_text(toolang, "provider"),
        ),
        description=_optional_text(data, "description"),
        family=_optional_text(data, "family"),
        attachment=_optional_bool(data, "attachment"),
        reasoning=_optional_bool(data, "reasoning"),
        reasoning_options=_optional_mappings(data.get("reasoning_options")),
        tool_call=_optional_bool(data, "tool_call"),
        interleaved=_optional_mapping(data.get("interleaved")),
        structured_output=_optional_bool(data, "structured_output"),
        temperature=_optional_bool(data, "temperature"),
        knowledge=_optional_text(data, "knowledge"),
        release_date=_optional_text(data, "release_date"),
        last_updated=_optional_text(data, "last_updated"),
        modalities=_modalities(data.get("modalities")),
        open_weights=_optional_bool(data, "open_weights"),
        limit=_int_mapping(data.get("limit")),
        status=_optional_text(data, "status"),
        experimental=_optional_mapping(data.get("experimental")),
        provider=provider,
        cost=_optional_mapping(data.get("cost")),
        extra={
            str(key): value for key, value in data.items() if key not in _MODEL_FIELDS
        },
    )


def _env_to_data(env: ResolvedEnv) -> list[object]:
    return [
        alternative if isinstance(alternative, str) else list(alternative)
        for alternative in env
    ]


def _env_from_data(value: object) -> ResolvedEnv:
    if not isinstance(value, list | tuple):
        return ()
    alternatives: list[str | tuple[str, ...]] = []
    for item in value:
        if isinstance(item, str):
            alternatives.append(item)
        elif isinstance(item, list | tuple):
            alternatives.append(tuple(str(name) for name in item))
    return tuple(alternatives)


def _mapping(data: Mapping[str, object], name: str) -> Mapping[str, object]:
    value = data.get(name)
    if not isinstance(value, Mapping):
        raise TypeError(f"model context {name} must be an object")
    return cast(Mapping[str, object], value)


def _optional_mapping(value: object) -> dict[str, object] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise TypeError("model context field must be an object")
    return {
        str(key): item for key, item in cast(Mapping[object, object], value).items()
    }


def _optional_mappings(value: object) -> tuple[Mapping[str, object], ...] | None:
    if value is None:
        return None
    if not isinstance(value, list | tuple):
        raise TypeError("model context field must be an array")
    return tuple(_optional_mapping(item) or {} for item in value)


def _modalities(value: object) -> dict[str, tuple[str, ...]]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise TypeError("model modalities must be an object")
    return {
        str(key): tuple(str(item) for item in items)
        for key, items in cast(Mapping[object, object], value).items()
        if isinstance(items, list | tuple)
    }


def _int_mapping(value: object) -> dict[str, int]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise TypeError("model limit must be an object")
    return {
        str(key): int(item)
        for key, item in cast(Mapping[object, object], value).items()
        if isinstance(item, int) and not isinstance(item, bool)
    }


def _string_list(value: object) -> tuple[str, ...]:
    if not isinstance(value, list | tuple):
        return ()
    return tuple(str(item) for item in value)


def _text(data: Mapping[str, object], name: str) -> str:
    value = data.get(name)
    if not isinstance(value, str) or not value:
        raise TypeError(f"model context {name} must be text")
    return value


def _optional_text(data: Mapping[str, object], name: str) -> str | None:
    value = data.get(name)
    return value if isinstance(value, str) and value else None


def _optional_bool(data: Mapping[str, object], name: str) -> bool | None:
    value = data.get(name)
    return value if isinstance(value, bool) else None


__all__ = [
    "CachedModelProjection",
    "ModelProjectionCache",
    "environment_readiness",
    "model_projection_key",
]
