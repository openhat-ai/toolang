"""Query, export, and hydrate the flat catalog owned by runtime setup."""

from __future__ import annotations

from collections.abc import Hashable, Mapping, Sequence
from dataclasses import dataclass, fields, replace
from functools import cached_property
from typing import cast

import msgspec

from toolang.base.protocols.model import ModelAdapter
from toolang.base.types.model import (
    Model,
    ModelFacts,
    ModelCatalogSnapshot,
    ModelProvider,
    ModelToolang,
    Provider,
    ProviderToolang,
)
from toolang.common.cache import canonical_value
from toolang.common.query import CollectionSchema, QueryDataset, QueryField, ScalarValue
from toolang.plugin.models.collections import (
    MODEL_SCHEMA,
    ModelCollection,
    _optional_float,
    _reasoning_efforts,
    parse_model_query_date,
)

from .cache import _snapshot_document
from .models import order_models
from .records import CatalogRecords, ModelRecord, ProviderRecord
from .routes import resolve_catalog_providers

_FACT_NAMES = tuple(
    field.name for field in fields(ModelFacts) if field.name != "provider"
)

_RECORD_SCHEMA = cast(
    CollectionSchema[ModelRecord],
    replace(MODEL_SCHEMA, item_type=ModelRecord, key_paths=(("provider",), ("id",))),
)


class _RecordDataset(QueryDataset[ModelRecord]):
    """Read existing query fields directly from validated cache records."""

    def _field_values(
        self, item: ModelRecord, key: Hashable, field: QueryField
    ) -> tuple[ScalarValue, ...]:
        name = field.name
        value: object
        if name == "model":
            value = item.id
        elif name == "available":
            value = item.ready
        elif name in {"catalog", "streaming"}:
            value = None
        elif name == "tags":
            value = ()
        elif name == "route.provider":
            value = item.provider
        elif name == "route.adapter":
            value = item.adapter
        elif name in {"release_date", "last_updated"}:
            value = parse_model_query_date(getattr(item, name))
        elif name.startswith("modalities."):
            value = item.modalities.get(name.split(".")[1], ())
        elif name.startswith("limit."):
            value = item.limit.get(name.split(".")[1])
        elif name.startswith("cost."):
            value = _optional_float((item.cost or {}).get(name.split(".")[1]))
        elif name == "parameters.reasoning.effort":
            value = _reasoning_efforts(item.reasoning_options or ())
        else:
            value = getattr(item, name)
        return (
            tuple(cast(Sequence[ScalarValue], value))
            if field.multiple
            else (cast(ScalarValue, value),)
        )


@dataclass(frozen=True)
class ModelListing:
    """A complete catalog with lazily reused query datasets and provider index."""

    records: CatalogRecords

    @cached_property
    def providers(self) -> dict[str, ProviderRecord]:
        return {provider.id: provider for provider in self.records.providers}

    @cached_property
    def all(self) -> QueryDataset[ModelRecord]:
        return _RecordDataset(_RECORD_SCHEMA, self.records.models, _prevalidated=True)

    @cached_property
    def effective_models(self) -> tuple[ModelRecord, ...]:
        return tuple(
            sorted(
                (
                    model
                    for model in self.records.models
                    if model.allowed_order is not None and model.ready
                ),
                key=lambda model: cast(int, model.allowed_order),
            )
        )

    @cached_property
    def default(self) -> QueryDataset[ModelRecord]:
        return _RecordDataset(_RECORD_SCHEMA, self.effective_models, _prevalidated=True)

    def resolve(
        self,
        models: Sequence[ModelRecord],
        *,
        adapters: Mapping[str, ModelAdapter],
        environ: Mapping[str, str],
        revision: str,
        include_empty_providers: bool = False,
    ) -> ModelCatalogSnapshot:
        """Hydrate selected declarations and resolve routes in the current setup."""

        provider_ids = {model.provider for model in models}
        providers = {
            provider.id: Provider(
                id=provider.id,
                name=provider.name,
                env=provider.env,
                npm=provider.npm,
                api=provider.api,
                doc=provider.doc,
                _toolang=ProviderToolang(
                    adapter=provider.adapter, env=provider.env_rule
                ),
            )
            for provider in self.records.providers
            if include_empty_providers or provider.id in provider_ids
        }
        values = tuple(
            Model(
                **{name: getattr(model, name) for name in _FACT_NAMES},
                provider=(
                    msgspec.convert(model.connection, type=ModelProvider)
                    if model.connection is not None
                    else None
                ),
                _toolang=ModelToolang(provider=model.provider),
            )
            for model in models
        )
        snapshot = ModelCatalogSnapshot(
            providers=providers, models=values, revision=revision
        )
        return resolve_catalog_providers(snapshot, adapters=adapters, environ=environ)

    def export(self, models: Sequence[ModelRecord]) -> dict[str, object]:
        """Reconstruct the public nested JSON only for selected export records."""

        grouped: dict[str, dict[str, object]] = {}
        for model in models:
            data = msgspec.to_builtins(model, enc_hook=_encode_mapping)
            for name in (
                "provider",
                "adapter",
                "api_present",
                "env_present",
                "allowed_order",
            ):
                data.pop(name)
            data["limit"] = dict(model.limit)
            data["modalities"] = {
                key: list(value) for key, value in model.modalities.items()
            }
            if model.reasoning_options is not None:
                data["reasoning_options"] = list(data["reasoning_options"])
            connection = data.pop("connection", None)
            if connection is not None:
                data["provider"] = {
                    key: value for key, value in connection.items() if key != "_toolang"
                }
            grouped.setdefault(model.provider, {})[model.id] = {
                key: value for key, value in data.items() if value is not None
            }
        result: dict[str, object] = {}
        for provider_id in sorted(grouped):
            data = msgspec.to_builtins(self.providers[provider_id])
            data.pop("adapter", None)
            data.pop("env_rule", None)
            data["env"] = list(self.providers[provider_id].env)
            result[provider_id] = {
                **{key: value for key, value in data.items() if value is not None},
                "models": dict(sorted(grouped[provider_id].items())),
            }
        return result


def _encode_mapping(value: object) -> object:
    if isinstance(value, Mapping):
        return canonical_value(value)
    raise TypeError(f"unsupported catalog value: {type(value).__name__}")


def build_model_listing(
    snapshot: ModelCatalogSnapshot, *, allow_models: tuple[str, ...] | None
) -> ModelListing:
    """Build policy once and discard temporary query/runtime representations."""

    allowed = order_models(ModelCollection(snapshot.models), allow_models)
    ranks = {ref: rank for rank, ref in enumerate(allowed.refs())}
    source = _snapshot_document(snapshot)
    providers: list[dict[str, object]] = []
    for row in cast(dict[str, dict[str, object]], source["providers"]).values():
        declared = cast(dict[str, object], row.pop("_toolang"))
        row.update(adapter=declared["adapter"], env_rule=declared["env"])
        providers.append(row)
    models: list[dict[str, object]] = []
    for model, row in zip(
        snapshot.models, cast(list[dict[str, object]], source["models"]), strict=True
    ):
        row.pop("_toolang")
        connection = row.pop("provider", None)
        route = model._toolang.route
        row.update(
            provider=model._toolang.provider,
            connection=connection,
            adapter=route.adapter,
            api_present=route.api is not None,
            env_present=route.env is not None,
            allowed_order=ranks.get(model.ref),
        )
        models.append(row)
    return ModelListing(
        msgspec.convert({"providers": providers, "models": models}, type=CatalogRecords)
    )
