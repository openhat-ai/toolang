"""Public query views for model, provider, and adapter collections."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Literal, cast

from toolang.base.types.model import Model, ModelCatalogSnapshot, Provider
from toolang.common.query import (
    CollectionDefinition,
    CollectionSchema,
    ColumnSpec,
    IdentitySpec,
    QueryDataset,
)


@dataclass(frozen=True, slots=True)
class ModelModalitiesView:
    """Queryable model modality lists."""

    input: tuple[str, ...]
    output: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ModelLimitView:
    """Queryable model token limits."""

    context: int | None
    output: int | None


@dataclass(frozen=True, slots=True)
class ModelCostView:
    """Queryable model per-million-token costs."""

    input: Decimal | None
    output: Decimal | None


@dataclass(frozen=True, slots=True)
class CatalogModelView:
    """Explicitly public catalog-model query representation."""

    key: str
    record: Model
    provider: str
    id: str
    name: str
    description: str | None
    family: str | None
    scope: Literal["local", "remote"]
    available: bool
    adapter: str | None
    catalog: str | None
    attachment: bool | None
    reasoning: bool | None
    tool_call: bool | None
    structured_output: bool | None
    temperature: bool | None
    open_weights: bool | None
    status: str | None
    release_date: date | None
    last_updated: date | None
    modalities: ModelModalitiesView
    limit: ModelLimitView
    cost: ModelCostView


CATALOG_MODEL_SCHEMA = CollectionSchema.from_type(
    "models",
    CatalogModelView,
    key="key",
    identity=IdentitySpec(
        paths=("provider", "id"),
        labels=("provider", "model"),
        separator="/",
    ),
    exclude=("key", "record"),
    columns=(
        ColumnSpec("MODEL", ("provider", "id"), "identity"),
        ColumnSpec("AVAILABLE", ("available",), "bool"),
        ColumnSpec("CONTEXT", ("limit.context",), "integer"),
        ColumnSpec("OUTPUT", ("limit.output",), "integer"),
        ColumnSpec("INPUT", ("modalities.input",), "join"),
        ColumnSpec(
            "CAPABILITIES",
            ("tool_call", "reasoning", "temperature", "structured_output"),
            "bool-labels",
        ),
        ColumnSpec(
            "PRICE ($/1M)",
            ("cost.input", "cost.output"),
            "currency-pair",
        ),
    ),
)
CATALOG_MODEL_DEFINITION = CollectionDefinition(CATALOG_MODEL_SCHEMA)


@dataclass(frozen=True, slots=True)
class CatalogProviderView:
    """Explicitly public provider query representation."""

    id: str
    record: Provider
    name: str
    catalog: str | None
    local: bool
    offline: bool
    ready: bool
    available_models: int
    model_count: int
    adapters: tuple[str, ...]
    api: str | None
    env_requirements: tuple[str, ...]
    required_env: tuple[str, ...]
    missing_env: tuple[str, ...]


CATALOG_PROVIDER_SCHEMA = CollectionSchema.from_type(
    "providers",
    CatalogProviderView,
    key="id",
    identity=IdentitySpec(paths=("id",), labels=("provider",)),
    exclude=("record",),
    columns=(
        ColumnSpec("PROVIDER", ("id",), "identity"),
        ColumnSpec("NAME", ("name",)),
        ColumnSpec("AVAILABLE", ("available_models", "model_count"), "ratio"),
        ColumnSpec("ADAPTERS", ("adapters",), "join"),
        ColumnSpec("API", ("api",)),
        ColumnSpec("ENV", ("env_requirements", "missing_env"), "env"),
    ),
)
CATALOG_PROVIDER_DEFINITION = CollectionDefinition(CATALOG_PROVIDER_SCHEMA)


@dataclass(frozen=True, slots=True)
class PluginInventoryView:
    """Public installed-plugin inventory item."""

    name: str
    source: str


def plugin_inventory_definition(
    collection: str,
    *,
    identity_label: str,
) -> CollectionDefinition[PluginInventoryView]:
    """Create one named plugin-inventory definition from shared typed data."""

    return CollectionDefinition(
        CollectionSchema.from_type(
            collection,
            PluginInventoryView,
            key="name",
            identity=IdentitySpec(paths=("name",), labels=(identity_label,)),
            columns=(
                ColumnSpec(identity_label.upper(), ("name",), "identity"),
                ColumnSpec("SOURCE", ("source",)),
            ),
        )
    )


ADAPTER_DEFINITION = plugin_inventory_definition(
    "adapters",
    identity_label="adapter",
)


def catalog_model_dataset(
    snapshot: ModelCatalogSnapshot,
    *,
    include_local: bool = True,
    available: set[str] | None = None,
    adapters: Mapping[str, str] | None = None,
) -> QueryDataset[CatalogModelView]:
    """Materialize one model catalog snapshot for generic querying."""

    available_identities = available or set()
    adapter_by_identity = adapters or {}
    items = tuple(
        _catalog_model_view(
            model,
            available=model.identity in available_identities,
            adapter=adapter_by_identity.get(model.identity),
        )
        for model in snapshot.models
        if include_local or not model.local
    )
    return CATALOG_MODEL_DEFINITION.dataset(items)


def catalog_provider_dataset(
    providers: Sequence[Provider],
    *,
    available: set[str],
    adapters: Mapping[str, Sequence[str]],
    apis: Mapping[str, str | None],
    env_requirements: Mapping[str, Sequence[str]],
    required_env: Mapping[str, Sequence[str]],
    missing_env: Mapping[str, Sequence[str]],
) -> QueryDataset[CatalogProviderView]:
    """Materialize providers and runtime-derived query values."""

    items = tuple(
        CatalogProviderView(
            id=provider.id,
            record=provider,
            name=provider.name,
            catalog=provider.catalog,
            local=provider.local,
            offline=_provider_offline(provider),
            ready=provider.resolved.ready if provider.resolved is not None else False,
            available_models=sum(
                f"{provider.id}/{model_id}" in available for model_id in provider.models
            ),
            model_count=len(provider.models),
            adapters=tuple(adapters.get(provider.id, ())),
            api=apis.get(provider.id),
            env_requirements=tuple(env_requirements.get(provider.id, ())),
            required_env=tuple(required_env.get(provider.id, ())),
            missing_env=tuple(missing_env.get(provider.id, ())),
        )
        for provider in providers
    )
    return CATALOG_PROVIDER_DEFINITION.dataset(items)


def _provider_offline(provider: Provider) -> bool:
    runtime = provider.extra.get("runtime")
    return (
        isinstance(runtime, Mapping)
        and cast(Mapping[str, object], runtime).get("status") == "offline"
    )


def plugin_inventory_dataset(
    definition: CollectionDefinition[PluginInventoryView],
    values: Sequence[tuple[str, str]],
) -> QueryDataset[PluginInventoryView]:
    """Materialize a complete installed-plugin inventory."""

    return definition.dataset(
        tuple(PluginInventoryView(name=name, source=source) for name, source in values)
    )


def _catalog_model_view(
    model: Model,
    *,
    available: bool,
    adapter: str | None,
) -> CatalogModelView:
    return CatalogModelView(
        key=model.identity,
        record=model,
        provider=model.provider_id,
        id=model.id,
        name=model.name,
        description=model.description,
        family=model.family,
        scope="local" if model.local else "remote",
        available=available,
        adapter=adapter,
        catalog=model.catalog,
        attachment=model.attachment,
        reasoning=model.reasoning,
        tool_call=model.tool_call,
        structured_output=model.structured_output,
        temperature=model.temperature,
        open_weights=model.open_weights,
        status=model.status,
        release_date=_optional_date(model.release_date),
        last_updated=_optional_date(model.last_updated),
        modalities=ModelModalitiesView(
            input=tuple(model.modalities.get("input", ())),
            output=tuple(model.modalities.get("output", ())),
        ),
        limit=ModelLimitView(
            context=model.limit.get("context"),
            output=model.limit.get("output"),
        ),
        cost=ModelCostView(
            input=_optional_decimal((model.cost or {}).get("input")),
            output=_optional_decimal((model.cost or {}).get("output")),
        ),
    )


def _optional_date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value is not None else None


def _optional_decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, Decimal | int | float):
        return None
    return Decimal(str(value))


__all__ = [
    "ADAPTER_DEFINITION",
    "CATALOG_MODEL_DEFINITION",
    "CATALOG_MODEL_SCHEMA",
    "CATALOG_PROVIDER_DEFINITION",
    "CATALOG_PROVIDER_SCHEMA",
    "CatalogModelView",
    "CatalogProviderView",
    "PluginInventoryView",
    "catalog_model_dataset",
    "catalog_provider_dataset",
    "plugin_inventory_dataset",
    "plugin_inventory_definition",
]
