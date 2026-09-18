"""Derived model context projection cache."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from pathlib import Path
import re
from typing import cast

from toolang.base.types.model import ModelCatalogSnapshot, ModelInfo, env_names
from toolang.common.cache import (
    CACHE_SCHEMA,
    canonical_value,
    digest,
    load_document,
    require_fields,
    revision_hex,
    store_document,
    text_digest,
)
from toolang.common.query import MatchUnion
from toolang.plugin.models.collections import (
    MODEL_SCHEMA,
    ModelCostView,
    ModelLimitView,
    ModelModalitiesView,
    ModelParametersView,
    ModelQueryView,
    ModelReasoningParametersView,
    ModelRouteView,
)

_CONTEXT_FILE = "effective.json"
_CONTEXT_IDENTITY_FILE = "identity.json"
_REVISION_NAME_RE = re.compile(r"^[0-9a-f]{64}$")
_CONTEXT_FIELDS = frozenset(
    {
        "schema",
        "kind",
        "key",
        "models",
        "queries",
        "catalog_queries",
    }
)
_MODEL_INFO_FIELDS = frozenset(
    {
        "ref",
        "provider",
        "name",
        "model",
        "selectors",
        "adapter",
        "scope",
        "tags",
        "tools",
        "streaming",
        "context_window",
        "max_output_tokens",
        "input_price",
        "output_price",
        "details",
        "metadata",
    }
)
_MODEL_QUERY_FIELDS = frozenset(
    {
        "key",
        "provider",
        "model",
        "name",
        "description",
        "family",
        "scope",
        "available",
        "adapter",
        "catalog",
        "alias",
        "route",
        "tags",
        "streaming",
        "attachment",
        "reasoning",
        "tool_call",
        "structured_output",
        "temperature",
        "open_weights",
        "status",
        "release_date",
        "last_updated",
        "modalities",
        "limit",
        "cost",
        "parameters",
    }
)
_UNCACHED_MODEL_METADATA_FIELDS = frozenset({"experimental", "provider"})


@dataclass(frozen=True, slots=True)
class CachedModelProjection:
    """Validated secret-free model context projection."""

    key: str
    model_infos: tuple[ModelInfo, ...]
    query_views: tuple[ModelQueryView, ...]
    catalog_query_views: tuple[ModelQueryView, ...] = ()

    def __post_init__(self) -> None:
        revision_hex(self.key)
        refs = tuple(info.ref for info in self.model_infos)
        if len(refs) != len(set(refs)):
            raise ValueError("cached model projection contains duplicate refs")
        if len(self.query_views) != len(self.model_infos):
            raise ValueError("cached model facts are not aligned")
        keys = tuple(view.key for view in self.query_views)
        if len(keys) != len(set(keys)):
            raise ValueError("cached model projection contains duplicate keys")
        if keys != refs:
            raise ValueError("cached model query keys do not match model refs")
        catalog_keys = tuple(view.key for view in self.catalog_query_views)
        if len(catalog_keys) != len(set(catalog_keys)):
            raise ValueError("cached catalog projection contains duplicate keys")
        for view in (*self.query_views, *self.catalog_query_views):
            if view.key != f"{view.provider}/{view.model}":
                raise ValueError("cached model query identity does not match its key")


class ModelProjectionCache:
    """Load and atomically publish portable model context projections."""

    def __init__(self, context_directory: Path) -> None:
        self._context_directory = context_directory

    def load_context(self, key: str) -> CachedModelProjection | None:
        """Load one validated model context projection, or return a miss."""

        path = self._context_path(key)
        try:
            document = load_document(
                path,
                kind="context",
                key=key,
                fast_json=True,
            )
            require_fields(document, _CONTEXT_FIELDS, label="model context")
            raw_models = document.get("models")
            if not isinstance(raw_models, list):
                return None
            infos = tuple(
                _model_info_from_cache_data(cast(Mapping[str, object], item))
                for item in raw_models
                if isinstance(item, Mapping)
            )
            if len(infos) != len(raw_models):
                return None
            raw_queries = document.get("queries")
            if not isinstance(raw_queries, list):
                return None
            queries = tuple(
                _model_query_view_from_cache_data(cast(Mapping[str, object], item))
                for item in raw_queries
                if isinstance(item, Mapping)
            )
            if len(queries) != len(raw_queries):
                return None
            raw_catalog_queries = document.get("catalog_queries")
            if not isinstance(raw_catalog_queries, list):
                return None
            catalog_queries = tuple(
                _model_query_view_from_cache_data(cast(Mapping[str, object], item))
                for item in raw_catalog_queries
                if isinstance(item, Mapping)
            )
            if len(catalog_queries) != len(raw_catalog_queries):
                return None
            return CachedModelProjection(
                key=key,
                model_infos=infos,
                query_views=queries,
                catalog_query_views=catalog_queries,
            )
        except (OSError, KeyError, TypeError, ValueError):
            return None

    def store_context(
        self,
        *,
        key: str,
        model_infos: Sequence[ModelInfo],
        query_views: Sequence[ModelQueryView],
        catalog_query_views: Sequence[ModelQueryView] = (),
        environment_names: Sequence[str] = (),
    ) -> None:
        """Store all safe facts for one derived model context."""

        projection = CachedModelProjection(
            key=key,
            model_infos=tuple(model_infos),
            query_views=tuple(query_views),
            catalog_query_views=tuple(catalog_query_views),
        )
        store_document(
            self._context_path(key),
            kind="context",
            key=key,
            document={
                "models": [
                    _cache_model_info_data(info) for info in projection.model_infos
                ],
                "queries": [
                    _model_query_view_cache_data(view)
                    for view in projection.query_views
                ],
                "catalog_queries": [
                    _model_query_view_cache_data(view)
                    for view in projection.catalog_query_views
                ],
            },
        )
        store_document(
            self._context_identity_path(key),
            kind="context_identity",
            key=key,
            document={
                "environment_names": sorted(set(environment_names)),
                "models": [
                    [view.provider, view.model]
                    for view in projection.catalog_query_views
                ],
            },
        )

    def catalog_identity_misses(
        self,
        *,
        kind: str,
        scope: str,
        catalog_revisions: Sequence[tuple[str, str]],
        setup_config: object,
        environ: Mapping[str, str],
        plugin_provenance: Sequence[object],
        allow_models: Sequence[str] | None,
        queries: MatchUnion,
    ) -> bool | None:
        """Check a matching context's identities without hydrating its models."""

        directory = self._context_directory / "contexts" / "revs"
        for path in sorted(directory.glob(f"*/{_CONTEXT_IDENTITY_FILE}")):
            revision = path.parent.name
            if _REVISION_NAME_RE.fullmatch(revision) is None:
                continue
            key = f"sha256:{revision}"
            try:
                identities, names = _load_context_catalog_identity(path, key=key)
                expected = model_projection_key(
                    kind=kind,
                    scope=scope,
                    catalog_revisions=catalog_revisions,
                    setup_config=setup_config,
                    environment_readiness={
                        name: bool(str(environ.get(name, "")).strip()) for name in names
                    },
                    plugin_provenance=plugin_provenance,
                    allow_models=allow_models,
                )
                if expected != key:
                    continue
                if not identities:
                    return True
                return not any(
                    MODEL_SCHEMA.identity_matches(identity, match)
                    for identity in identities
                    for match in queries.matches
                )
            except (OSError, KeyError, TypeError, ValueError):
                continue
        return None

    def _context_path(self, key: str) -> Path:
        revision = revision_hex(key)
        return self._context_directory / "contexts" / "revs" / revision / _CONTEXT_FILE

    def _context_identity_path(self, key: str) -> Path:
        revision = revision_hex(key)
        return (
            self._context_directory
            / "contexts"
            / "revs"
            / revision
            / _CONTEXT_IDENTITY_FILE
        )


def model_projection_key(
    *,
    kind: str,
    scope: str,
    catalog_revisions: Sequence[tuple[str, str]],
    setup_config: object,
    environment_readiness: Mapping[str, bool],
    plugin_provenance: Sequence[object],
    allow_models: Sequence[str] | None,
) -> str:
    """Build a portable digest over every derived model-context input."""

    payload = {
        "schema": CACHE_SCHEMA,
        "query_schema": text_digest(MODEL_SCHEMA.to_json()),
        "kind": kind,
        "scope": scope,
        "catalogs": list(catalog_revisions),
        "setup_config": canonical_value(setup_config),
        "environment_readiness": dict(sorted(environment_readiness.items())),
        "plugins": canonical_value(plugin_provenance),
        "allow_models": list(allow_models) if allow_models is not None else None,
    }
    return digest(payload)


def hydrate_model_infos(
    cached: Sequence[ModelInfo],
    snapshot: ModelCatalogSnapshot,
) -> tuple[ModelInfo, ...] | None:
    """Attach current runtime route facts to one cached ordered subset."""

    models = {model.identity: model for model in snapshot.models}
    hydrated: list[ModelInfo] = []
    for info in cached:
        model = models.get(info.ref)
        if model is None or model.resolved is None:
            return None
        metadata = dict(info.metadata)
        metadata["resolved_api"] = model.resolved.api
        metadata["resolved_ready"] = model.resolved.ready
        metadata["experimental"] = (
            dict(model.experimental) if model.experimental is not None else None
        )
        hydrated.append(
            replace(
                info,
                adapter=model.resolved.adapter or "unknown",
                metadata=metadata,
            )
        )
    return tuple(hydrated)


def environment_readiness(
    snapshot: ModelCatalogSnapshot,
    environ: Mapping[str, str],
) -> dict[str, bool]:
    """Return presence facts for catalog-declared environment inputs."""

    names = {
        name
        for provider in snapshot.providers.values()
        for name in env_names(provider.env)
    }
    return {name: bool(str(environ.get(name, "")).strip()) for name in sorted(names)}


def _cache_model_info_data(info: ModelInfo) -> dict[str, object]:
    data = info.to_data()
    metadata = data.get("metadata")
    if isinstance(metadata, Mapping):
        data["metadata"] = {
            str(key): value
            for key, value in metadata.items()
            if key
            not in {
                "resolved_api",
                "resolved_ready",
                *_UNCACHED_MODEL_METADATA_FIELDS,
            }
        }
    data["adapter"] = "unresolved"
    return data


def _model_info_from_cache_data(data: Mapping[str, object]) -> ModelInfo:
    require_fields(data, _MODEL_INFO_FIELDS, label="cached model info")
    metadata = data.get("metadata")
    if not isinstance(metadata, Mapping) or any(
        not isinstance(name, str) for name in metadata
    ):
        raise TypeError("cached model metadata must be an object")
    info = ModelInfo(
        ref=_text(data, "ref"),
        provider=_text(data, "provider"),
        name=_text(data, "name"),
        model=_text(data, "model"),
        selectors=_strings(data, "selectors"),
        adapter=_text(data, "adapter"),
        scope=_optional_text(data, "scope"),
        tags=_strings(data, "tags"),
        tools=_bool(data, "tools"),
        streaming=_bool(data, "streaming"),
        context_window=_optional_int(data, "context_window"),
        max_output_tokens=_optional_int(data, "max_output_tokens"),
        input_price=_optional_float(data, "input_price"),
        output_price=_optional_float(data, "output_price"),
        details=_optional_text(data, "details"),
        metadata={str(name): value for name, value in metadata.items()},
    )
    forbidden_metadata = {
        "resolved_api",
        "resolved_ready",
        *_UNCACHED_MODEL_METADATA_FIELDS,
    }
    if info.adapter != "unresolved" or forbidden_metadata.intersection(info.metadata):
        raise ValueError("cached model info contains runtime-bound data")
    return info


def _model_query_view_cache_data(view: ModelQueryView) -> dict[str, object]:
    return {
        "key": view.key,
        "provider": view.provider,
        "model": view.model,
        "name": view.name,
        "description": view.description,
        "family": view.family,
        "scope": view.scope,
        "available": view.available,
        "adapter": view.adapter,
        "catalog": view.catalog,
        "alias": list(view.alias) if view.alias is not None else None,
        "route": {
            "provider": view.route.provider,
            "adapter": view.route.adapter,
            "scope": view.route.scope,
        },
        "tags": list(view.tags),
        "streaming": view.streaming,
        "attachment": view.attachment,
        "reasoning": view.reasoning,
        "tool_call": view.tool_call,
        "structured_output": view.structured_output,
        "temperature": view.temperature,
        "open_weights": view.open_weights,
        "status": view.status,
        "release_date": (
            view.release_date.isoformat() if view.release_date is not None else None
        ),
        "last_updated": (
            view.last_updated.isoformat() if view.last_updated is not None else None
        ),
        "modalities": {
            "input": list(view.modalities.input),
            "output": list(view.modalities.output),
        },
        "limit": {
            "context": view.limit.context,
            "output": view.limit.output,
        },
        "cost": {
            "input": str(view.cost.input) if view.cost.input is not None else None,
            "output": str(view.cost.output) if view.cost.output is not None else None,
        },
        "parameters": {"reasoning": {"effort": list(view.parameters.reasoning.effort)}},
    }


def _model_query_view_from_cache_data(
    data: Mapping[str, object],
) -> ModelQueryView:
    require_fields(data, _MODEL_QUERY_FIELDS, label="cached model query")
    route = _mapping(
        data,
        "route",
        fields=frozenset({"provider", "adapter", "scope"}),
    )
    modalities = _mapping(
        data,
        "modalities",
        fields=frozenset({"input", "output"}),
    )
    limit = _mapping(data, "limit", fields=frozenset({"context", "output"}))
    cost = _mapping(data, "cost", fields=frozenset({"input", "output"}))
    parameters = _mapping(data, "parameters", fields=frozenset({"reasoning"}))
    reasoning = _mapping(parameters, "reasoning", fields=frozenset({"effort"}))
    return ModelQueryView(
        key=_text(data, "key"),
        record=None,
        provider=_text(data, "provider"),
        model=_text(data, "model"),
        name=_text(data, "name"),
        description=_optional_text(data, "description"),
        family=_optional_text(data, "family"),
        scope=_optional_text(data, "scope"),
        available=_bool(data, "available"),
        adapter=_optional_text(data, "adapter"),
        catalog=_optional_text(data, "catalog"),
        alias=_optional_strings(data, "alias"),
        route=ModelRouteView(
            provider=_text(route, "provider"),
            adapter=_optional_text(route, "adapter"),
            scope=_optional_text(route, "scope"),
        ),
        tags=_strings(data, "tags"),
        streaming=_optional_bool(data, "streaming"),
        attachment=_optional_bool(data, "attachment"),
        reasoning=_optional_bool(data, "reasoning"),
        tool_call=_optional_bool(data, "tool_call"),
        structured_output=_optional_bool(data, "structured_output"),
        temperature=_optional_bool(data, "temperature"),
        open_weights=_optional_bool(data, "open_weights"),
        status=_optional_text(data, "status"),
        release_date=_optional_date(data, "release_date"),
        last_updated=_optional_date(data, "last_updated"),
        modalities=ModelModalitiesView(
            input=_strings(modalities, "input"),
            output=_strings(modalities, "output"),
        ),
        limit=ModelLimitView(
            context=_optional_int(limit, "context"),
            output=_optional_int(limit, "output"),
        ),
        cost=ModelCostView(
            input=_optional_decimal(cost, "input"),
            output=_optional_decimal(cost, "output"),
        ),
        parameters=ModelParametersView(
            reasoning=ModelReasoningParametersView(effort=_strings(reasoning, "effort"))
        ),
    )


def _catalog_identity_from_cache_data(
    data: Mapping[str, object],
    *,
    key: str,
) -> tuple[tuple[tuple[str, str], ...], tuple[str, ...]]:
    require_fields(
        data,
        frozenset({"environment_names", "key", "kind", "models", "schema"}),
        label="catalog identity",
    )
    if (
        data.get("schema") != CACHE_SCHEMA
        or data.get("kind") != "context_identity"
        or data.get("key") != key
    ):
        raise ValueError("catalog identity does not match its context")
    raw_environment_names = data.get("environment_names")
    if not isinstance(raw_environment_names, list) or any(
        not isinstance(name, str) for name in raw_environment_names
    ):
        raise TypeError("catalog identity environment names must be strings")
    environment_names = tuple(cast(list[str], raw_environment_names))
    if environment_names != tuple(sorted(set(environment_names))) or any(
        not name for name in environment_names
    ):
        raise ValueError(
            "catalog identity environment names must be unique and ordered"
        )
    raw_models = data.get("models")
    if not isinstance(raw_models, list):
        raise TypeError("catalog identity models must be an array")
    identities: list[tuple[str, str]] = []
    for raw_item in raw_models:
        if (
            not isinstance(raw_item, list)
            or len(raw_item) != 2
            or not isinstance(raw_item[0], str)
            or not isinstance(raw_item[1], str)
        ):
            raise TypeError("catalog identity model entries must be string pairs")
        provider = raw_item[0]
        model = raw_item[1]
        if not provider or not model:
            raise ValueError("catalog identity model entries must be non-empty")
        identities.append((provider, model))
    if len(identities) != len(set(identities)):
        raise ValueError("catalog identity contains duplicate models")
    return tuple(identities), environment_names


def _load_context_catalog_identity(
    path: Path,
    *,
    key: str,
) -> tuple[tuple[tuple[str, str], ...], tuple[str, ...]]:
    document = load_document(path, kind="context_identity", key=key)
    return _catalog_identity_from_cache_data(document, key=key)


def _mapping(
    data: Mapping[str, object],
    name: str,
    *,
    fields: frozenset[str],
) -> Mapping[str, object]:
    value = data.get(name)
    if not isinstance(value, Mapping):
        raise TypeError(f"cached model {name} must be an object")
    result = cast(Mapping[str, object], value)
    require_fields(result, fields, label=f"cached model {name}")
    return result


def _text(data: Mapping[str, object], name: str) -> str:
    value = data.get(name)
    if not isinstance(value, str) or not value:
        raise TypeError(f"cached model {name} must be non-empty text")
    return value


def _optional_text(data: Mapping[str, object], name: str) -> str | None:
    value = data.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"cached model {name} must be text or null")
    return value


def _bool(data: Mapping[str, object], name: str) -> bool:
    value = data.get(name)
    if not isinstance(value, bool):
        raise TypeError(f"cached model {name} must be boolean")
    return value


def _optional_bool(data: Mapping[str, object], name: str) -> bool | None:
    value = data.get(name)
    if value is None:
        return None
    if not isinstance(value, bool):
        raise TypeError(f"cached model {name} must be boolean or null")
    return value


def _strings(data: Mapping[str, object], name: str) -> tuple[str, ...]:
    value = data.get(name)
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise TypeError(f"cached model {name} must be a string array")
    return tuple(cast(list[str], value))


def _optional_strings(data: Mapping[str, object], name: str) -> tuple[str, ...] | None:
    return None if data.get(name) is None else _strings(data, name)


def _optional_int(data: Mapping[str, object], name: str) -> int | None:
    value = data.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"cached model {name} must be an integer or null")
    return value


def _optional_decimal(data: Mapping[str, object], name: str) -> Decimal | None:
    value = data.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, Decimal | int | float | str):
        raise TypeError(f"cached model {name} must be a decimal or null")
    return Decimal(str(value))


def _optional_float(data: Mapping[str, object], name: str) -> float | None:
    value = data.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, Decimal | int | float):
        raise TypeError(f"cached model {name} must be a number or null")
    return float(value)


def _optional_date(data: Mapping[str, object], name: str) -> date | None:
    value = _optional_text(data, name)
    return date.fromisoformat(value) if value is not None else None


__all__ = [
    "CachedModelProjection",
    "ModelProjectionCache",
    "environment_readiness",
    "hydrate_model_infos",
    "model_projection_key",
]
