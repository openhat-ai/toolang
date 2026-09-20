"""Public query views for the models collection."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from types import MappingProxyType
from typing import cast

from toolang.base.errors import ToolangError
from toolang.base.types.model import (
    Model,
    ModelCatalogSnapshot,
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
    available: bool
    adapter: str | None
    catalog: str | None
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


@dataclass(frozen=True, slots=True, eq=False, init=False)
class ModelCollection:
    """Immutable effective models with one shared matcher and exact indexes."""

    models: tuple[Model, ...]
    _by_ref: Mapping[str, Model]
    _matcher: QueryDataset[ModelQueryView]

    def __init__(
        self,
        models: Sequence[Model] = (),
        *,
        query_views: Sequence[ModelQueryView] | None = None,
    ) -> None:
        values = tuple(models)
        _validate_models(values)
        if query_views is None:
            views = tuple(
                _catalog_model_view(model, available=True, adapter=None)
                for model in values
            )
        else:
            raw_views = tuple(query_views)
            if tuple(view.key for view in raw_views) != tuple(
                model.ref for model in values
            ):
                raise ValueError("model query views must match collection refs")
            views = tuple(
                replace(view, record=model)
                for model, view in zip(values, raw_views, strict=True)
            )
        matcher = MODEL_DEFINITION.dataset(
            views,
            _prevalidated=query_views is not None,
        )
        self._initialize(values, matcher=matcher)

    def _initialize(
        self,
        values: tuple[Model, ...],
        *,
        matcher: QueryDataset[ModelQueryView],
    ) -> None:
        _validate_models(values)
        by_ref = {model.ref: model for model in values}
        object.__setattr__(self, "models", values)
        object.__setattr__(self, "_by_ref", MappingProxyType(by_ref))
        object.__setattr__(self, "_matcher", matcher)

    @property
    def entries(self) -> tuple[Model, ...]:
        """Return the effective models in collection order."""

        return self.models

    def match(
        self,
        queries: MatchUnion | str | Sequence[str] | None = None,
    ) -> ModelCollection:
        """Return matches in authored match order and collection order within each."""

        if queries is None:
            return self
        parsed = (
            queries if isinstance(queries, MatchUnion) else MODEL_SCHEMA.parse(queries)
        )
        selected: list[Model] = []
        seen: set[str] = set()
        for match in parsed.matches:
            matched = {
                cast(Model, item.record).ref
                for item in self._matcher.query(MatchUnion((match,)))
            }
            for model in self.models:
                if model.ref in matched and model.ref not in seen:
                    selected.append(model)
                    seen.add(model.ref)
        return self._derive(tuple(selected))

    def apply(
        self,
        operations: Sequence[tuple[SetOperator, MatchUnion | str | Sequence[str]]],
    ) -> ModelCollection:
        """Apply set operations against this immutable collection base."""

        if not operations:
            return self
        available = set(self._by_ref)
        active = set(available)
        for operator, query in operations:
            matched = {
                cast(Model, item.record).ref for item in self._matcher.query(query)
            } & available
            if operator == "=":
                active.intersection_update(matched)
            elif operator == "+=":
                active.update(matched)
            elif operator == "-=":
                active.difference_update(matched)
            else:  # pragma: no cover - SetOperator is a closed vocabulary
                raise ToolangError(f"unknown collection set operator: {operator!r}")
        return self._derive(
            tuple(model for model in self.models if model.ref in active)
        )

    def resolve(self, ref: str) -> Model:
        """Resolve one exact public model ref in O(1)."""

        model = self._by_ref.get(ref)
        if model is None:
            raise ToolangError(f"model ref is unavailable: {ref}")
        return model

    def entry(self, key: str) -> Model:
        """Resolve one persisted model resource key in O(1)."""

        return self.resolve(key)

    def subset(self, keys: Sequence[str]) -> ModelCollection:
        """Resolve an ordered persisted-key subset without interpreting queries."""

        return self._derive(tuple(self.resolve(key) for key in keys))

    def compact(self) -> ModelCollection:
        """Fix this subset as a standalone publication matcher."""

        return ModelCollection(self.models, query_views=self.query_views())

    def contains(self, ref: str) -> bool:
        """Return whether one exact public ref is available."""

        return ref in self._by_ref

    def refs(self) -> tuple[str, ...]:
        """Return public refs in collection order."""

        return tuple(model.ref for model in self.models)

    def keys(self) -> tuple[str, ...]:
        """Return stable resource keys in collection order."""

        return self.refs()

    def effective_default(self, preferred: str | None) -> str | None:
        """Return a preferred available ref, then the first collection ref."""

        if preferred is not None and self.contains(preferred):
            return preferred
        return self.models[0].ref if self.models else None

    def query_views(self) -> tuple[ModelQueryView, ...]:
        """Return query facts aligned with the effective collection models."""

        by_key = {view.key: view for view in self._matcher.items}
        return tuple(by_key[model.ref] for model in self.models)

    def __bool__(self) -> bool:
        return bool(self.models)

    def __len__(self) -> int:
        return len(self.models)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, ModelCollection) and self.models == other.models

    def _derive(self, models: tuple[Model, ...]) -> ModelCollection:
        if models == self.models:
            return self
        derived = object.__new__(ModelCollection)
        derived._initialize(models, matcher=self._matcher)
        return derived


def _validate_models(values: tuple[Model, ...]) -> None:
    if len({model.ref for model in values}) != len(values):
        raise ValueError("model collection contains duplicate public refs")


@dataclass(frozen=True, slots=True)
class CatalogProviderView:
    """Provider-list presentation values."""

    id: str
    record: Provider
    name: str
    catalog: str | None
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
    available: set[str] | None = None,
    adapters: Mapping[str, str] | None = None,
    query_views: Sequence[ModelQueryView] | None = None,
) -> QueryDataset[ModelQueryView]:
    """Materialize one model catalog snapshot for generic querying."""

    available_identities = available or set()
    adapter_by_identity = adapters or {}

    models = snapshot.models
    if query_views is None:
        items = tuple(
            _catalog_model_view(
                model,
                available=model.identity in available_identities,
                adapter=adapter_by_identity.get(model.identity),
            )
            for model in models
        )
    else:
        raw_views = tuple(query_views)
        if tuple(view.key for view in raw_views) != tuple(
            model.identity for model in models
        ):
            raise ValueError("catalog query views must match catalog model identities")
        items = tuple(
            replace(view, record=model)
            for model, view in zip(models, raw_views, strict=True)
        )
    return MODEL_DEFINITION.dataset(
        items,
        _prevalidated=query_views is not None,
    )


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
            catalog=None,
            ready=any(model._toolang.ready for model in provider.models.values()),
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


def _catalog_model_view(
    model: Model,
    *,
    available: bool,
    adapter: str | None,
) -> ModelQueryView:
    return ModelQueryView(
        key=model.identity,
        record=model,
        provider=model._toolang.provider,
        model=model.id,
        name=model.name,
        description=model.description,
        family=model.family,
        available=available,
        adapter=adapter,
        catalog=None,
        route=ModelRouteView(
            provider=model._toolang.provider,
            adapter=adapter,
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


def _reasoning_efforts(
    options: Sequence[Mapping[str, object]],
) -> tuple[str, ...]:
    values: list[str] = []
    for option in options:
        if option.get("type") != "effort":
            continue
        raw = option.get("values")
        if not isinstance(raw, list | tuple):
            continue
        for value in raw:
            if isinstance(value, str) and value not in values:
                values.append(value)
    return tuple(values)


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
