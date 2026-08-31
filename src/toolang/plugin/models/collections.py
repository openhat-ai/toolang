"""Public query views for the models collection."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Literal, cast

from toolang.base.errors import ToolangError
from toolang.base.types.model import (
    Model,
    ModelCatalogSnapshot,
    ModelInfo,
    ModelTarget,
    Provider,
)
from toolang.common.query import (
    CollectionDefinition,
    CollectionSchema,
    ColumnSpec,
    IdentitySpec,
    MatchUnion,
    QueryDataset,
    SetOperator,
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
class ModelRouteView:
    """Queryable model route attributes."""

    provider: str
    adapter: str | None
    scope: str | None


@dataclass(frozen=True, slots=True)
class ModelReasoningParametersView:
    """Queryable reasoning parameter values."""

    effort: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ModelParametersView:
    """Queryable model parameter values."""

    reasoning: ModelReasoningParametersView


@dataclass(frozen=True, slots=True)
class ModelQueryView:
    """Shared public model query representation."""

    key: str
    record: object
    provider: str
    model: str
    name: str
    description: str | None
    family: str | None
    scope: str | None
    available: bool
    adapter: str | None
    catalog: str | None
    alias: tuple[str, ...] | None
    route: ModelRouteView
    tags: tuple[str, ...]
    streaming: bool | None
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
    parameters: ModelParametersView


MODEL_SCHEMA = CollectionSchema.from_type(
    "models",
    ModelQueryView,
    key="key",
    identity=IdentitySpec(
        paths=("provider", "model"),
        labels=("provider", "model"),
        separator="/",
    ),
    exclude=("key", "record"),
    columns=(
        ColumnSpec("MODEL", ("provider", "model"), "identity"),
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
MODEL_DEFINITION = CollectionDefinition(MODEL_SCHEMA)


@dataclass(frozen=True, slots=True)
class ModelEntry:
    """One effective model route published for execution."""

    key: str
    ref: str
    target: ModelTarget
    info: ModelInfo

    def __post_init__(self) -> None:
        if not self.key or self.key != self.key.strip():
            raise ValueError("model entry requires a canonical key")
        if not self.ref or self.ref != self.ref.strip():
            raise ValueError("model entry requires a canonical ref")


class ModelCollection:
    """Immutable effective models with one shared matcher and exact indexes."""

    __slots__ = ("entries", "_by_key", "_by_ref", "_matcher")

    def __init__(self, entries: Sequence[ModelEntry] = ()) -> None:
        values = tuple(entries)
        by_key = {entry.key: entry for entry in values}
        by_ref = {entry.ref: entry for entry in values}
        if len(by_key) != len(values):
            raise ValueError("model collection contains duplicate entry keys")
        if len(by_ref) != len(values):
            raise ValueError("model collection contains duplicate public refs")
        self.entries = values
        self._by_key = by_key
        self._by_ref = by_ref
        self._matcher = MODEL_DEFINITION.dataset(
            tuple(_model_entry_view(entry) for entry in values)
        )

    def match(
        self,
        queries: MatchUnion | str | Sequence[str] | None = None,
    ) -> ModelCollection:
        """Return the stable-order subset accepted by collection queries."""

        selected = self._matcher.query(queries)
        return ModelCollection(
            tuple(cast(ModelEntry, item.record) for item in selected)
        )

    def apply(
        self,
        operations: Sequence[tuple[SetOperator, MatchUnion | str | Sequence[str]]],
    ) -> ModelCollection:
        """Apply set operations against this immutable collection base."""

        selected = self._matcher.apply(operations)
        return ModelCollection(
            tuple(cast(ModelEntry, item.record) for item in selected)
        )

    def resolve(self, ref: str) -> ModelEntry:
        """Resolve one exact public model ref in O(1)."""

        entry = self._by_ref.get(ref)
        if entry is None:
            raise ToolangError(f"model ref is unavailable: {ref}")
        return entry

    def entry(self, key: str) -> ModelEntry:
        """Resolve one persisted model resource key in O(1)."""

        entry = self._by_key.get(key)
        if entry is None:
            raise ToolangError(f"run model resource is unavailable: {key}")
        return entry

    def subset(self, keys: Sequence[str]) -> ModelCollection:
        """Resolve an ordered persisted-key subset without interpreting queries."""

        return ModelCollection(tuple(self.entry(key) for key in keys))

    def contains(self, ref: str) -> bool:
        """Return whether one exact public ref is available."""

        return ref in self._by_ref

    def refs(self) -> tuple[str, ...]:
        """Return public refs in collection order."""

        return tuple(entry.ref for entry in self.entries)

    def keys(self) -> tuple[str, ...]:
        """Return stable resource keys in collection order."""

        return tuple(entry.key for entry in self.entries)

    def __bool__(self) -> bool:
        return bool(self.entries)

    def __len__(self) -> int:
        return len(self.entries)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, ModelCollection) and self.entries == other.entries


@dataclass(frozen=True, slots=True)
class CatalogProviderView:
    """Provider-list presentation values."""

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


def catalog_model_dataset(
    snapshot: ModelCatalogSnapshot,
    *,
    include_local: bool = True,
    available: set[str] | None = None,
    adapters: Mapping[str, str] | None = None,
) -> QueryDataset[ModelQueryView]:
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
    return MODEL_DEFINITION.dataset(items)


def catalog_provider_views(
    providers: Sequence[Provider],
    *,
    available: set[str],
    adapters: Mapping[str, Sequence[str]],
    apis: Mapping[str, str | None],
    env_requirements: Mapping[str, Sequence[str]],
    required_env: Mapping[str, Sequence[str]],
    missing_env: Mapping[str, Sequence[str]],
) -> tuple[CatalogProviderView, ...]:
    """Materialize providers and runtime-derived presentation values."""

    return tuple(
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


def _provider_offline(provider: Provider) -> bool:
    runtime = provider.extra.get("runtime")
    return (
        isinstance(runtime, Mapping)
        and cast(Mapping[str, object], runtime).get("status") == "offline"
    )


def _catalog_model_view(
    model: Model,
    *,
    available: bool,
    adapter: str | None,
) -> ModelQueryView:
    scope: Literal["local", "remote"] = "local" if model.local else "remote"
    return ModelQueryView(
        key=model.identity,
        record=model,
        provider=model.provider_id,
        model=model.id,
        name=model.name,
        description=model.description,
        family=model.family,
        scope=scope,
        available=available,
        adapter=adapter,
        catalog=model.catalog,
        alias=None,
        route=ModelRouteView(
            provider=model.provider_id,
            adapter=adapter,
            scope=scope,
        ),
        tags=(),
        streaming=None,
        attachment=model.attachment,
        reasoning=model.reasoning,
        tool_call=model.tool_call,
        structured_output=model.structured_output,
        temperature=model.temperature,
        open_weights=model.open_weights,
        status=model.status,
        release_date=parse_model_query_date(model.release_date),
        last_updated=parse_model_query_date(model.last_updated),
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
        parameters=ModelParametersView(
            reasoning=ModelReasoningParametersView(
                effort=_reasoning_efforts(model.reasoning_options or ())
            )
        ),
    )


def _model_entry_view(entry: ModelEntry) -> ModelQueryView:
    target = entry.target
    info = entry.info
    provider, separator, model = target.ref.partition("/")
    if not separator or not provider or not model:
        provider, model = target.provider, target.model
    metadata = info.metadata
    modalities = metadata.get("modalities")
    input_modalities = (
        modalities.get("input") if isinstance(modalities, Mapping) else None
    )
    output_modalities = (
        modalities.get("output") if isinstance(modalities, Mapping) else None
    )
    return ModelQueryView(
        key=entry.key,
        record=entry,
        provider=provider,
        model=model,
        name=target.name,
        description=info.details,
        family=_metadata_text(metadata, "family"),
        scope=target.scope,
        available=True,
        adapter=target.adapter,
        catalog=target.catalog,
        alias=None,
        route=ModelRouteView(
            provider=target.provider,
            adapter=target.adapter,
            scope=target.scope,
        ),
        tags=tuple(target.tags),
        streaming=target.streaming,
        attachment=_metadata_bool(metadata, "attachment"),
        reasoning=_metadata_bool(metadata, "reasoning"),
        tool_call=target.tools,
        temperature=_metadata_bool(metadata, "temperature"),
        structured_output=target.structured_output,
        open_weights=_metadata_bool(metadata, "open_weights"),
        status=_metadata_text(metadata, "status"),
        release_date=parse_model_query_date(_metadata_text(metadata, "release_date")),
        last_updated=parse_model_query_date(_metadata_text(metadata, "last_updated")),
        modalities=ModelModalitiesView(
            input=_string_values(input_modalities),
            output=_string_values(output_modalities),
        ),
        limit=ModelLimitView(
            context=info.context_window,
            output=info.max_output_tokens,
        ),
        cost=ModelCostView(
            input=_optional_decimal(info.input_price),
            output=_optional_decimal(info.output_price),
        ),
        parameters=ModelParametersView(
            reasoning=ModelReasoningParametersView(
                effort=_reasoning_efforts_from_metadata(metadata)
            )
        ),
    )


def _reasoning_efforts(options: Sequence[Mapping[str, object]]) -> tuple[str, ...]:
    values: list[str] = []
    for option in options:
        raw_values = option.get("values")
        if option.get("type") != "effort" or not isinstance(raw_values, list | tuple):
            continue
        values.extend(value for value in raw_values if isinstance(value, str))
    return tuple(dict.fromkeys(values))


def parse_model_query_date(value: str | None) -> date | None:
    """Parse full or month-precision model metadata for typed queries."""

    if value is None:
        return None
    normalized = f"{value}-01" if len(value) == 7 and value[4] == "-" else value
    return date.fromisoformat(normalized)


def _optional_decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, Decimal | int | float):
        return None
    return Decimal(str(value))


def _metadata_text(metadata: Mapping[str, object], name: str) -> str | None:
    value = metadata.get(name)
    return value if isinstance(value, str) and value else None


def _metadata_bool(metadata: Mapping[str, object], name: str) -> bool | None:
    value = metadata.get(name)
    return value if isinstance(value, bool) else None


def _string_values(value: object) -> tuple[str, ...]:
    if not isinstance(value, list | tuple):
        return ()
    return tuple(item for item in value if isinstance(item, str))


def _reasoning_efforts_from_metadata(
    metadata: Mapping[str, object],
) -> tuple[str, ...]:
    raw = metadata.get("reasoning_options")
    options = (
        tuple(
            cast(Mapping[str, object], item)
            for item in raw
            if isinstance(item, Mapping)
        )
        if isinstance(raw, list | tuple)
        else ()
    )
    return _reasoning_efforts(options)


__all__ = [
    "CatalogProviderView",
    "MODEL_DEFINITION",
    "MODEL_SCHEMA",
    "ModelCollection",
    "ModelCostView",
    "ModelEntry",
    "ModelLimitView",
    "ModelModalitiesView",
    "ModelParametersView",
    "ModelQueryView",
    "ModelReasoningParametersView",
    "ModelRouteView",
    "catalog_model_dataset",
    "catalog_provider_views",
    "parse_model_query_date",
]
