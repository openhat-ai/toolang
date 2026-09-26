"""Transient tq-json operations over model records."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import cast
from dataclasses import replace
import re

from tq import Match as TQMatch
from tq import Query, QueryError

from toolang.base.types.model import Model
from toolang.common.errors import ToolangError
from toolang.common.query import SetOperator

from .collections import MODEL_SCHEMA
from .resolution import model_reasoning_efforts

_SEQUENCE_FIELDS = frozenset(
    {"modalities.input", "modalities.output", "parameters.reasoning.effort", "tags"}
)

_MODEL_QUERY_SCHEMA = {
    "key": "ref",
    "filter": tuple(MODEL_SCHEMA.fields),
}


def model_query_record(model: Model) -> dict[str, object]:
    """Project one model to public query fields without retaining an index."""

    route = model._toolang.route
    cost = model.cost or {}
    return {
        "ref": model.ref,
        "provider": model._toolang.provider,
        "model": model.id,
        "name": model.name,
        "description": model.description,
        "family": model.family,
        "available": model._toolang.routable,
        "allowed": model._toolang.allowed,
        "ready": model._toolang.effective_ready,
        "adapter": route.adapter,
        "catalog": None,
        "route": {"provider": model._toolang.provider, "adapter": route.adapter},
        "tags": (),
        "streaming": None,
        "attachment": model.attachment,
        "reasoning": model.reasoning,
        "tool_call": model.tool_call,
        "structured_output": model.structured_output,
        "temperature": model.temperature,
        "open_weights": model.open_weights,
        "status": model.status,
        "release_date": _date_value(model.release_date),
        "last_updated": _date_value(model.last_updated),
        "modalities": {
            "input": tuple(model.modalities.get("input", ())),
            "output": tuple(model.modalities.get("output", ())),
        },
        "limit": {
            "context": model.limit.get("context"),
            "output": model.limit.get("output"),
        },
        "cost": {
            "input": _number_value(cost.get("input")),
            "output": _number_value(cost.get("output")),
        },
        "parameters": {"reasoning": {"effort": model_reasoning_efforts(model)}},
    }


def _prepare_model_query(
    queries: Sequence[str], *, allow_policy: bool = False
) -> tuple[Query, tuple[frozenset[str], ...]]:
    if isinstance(queries, str):
        raise TypeError("model queries must be a sequence of query strings")
    if not queries:
        raise ValueError("model query cannot be empty")
    try:
        parsed = Query.parse(tuple(queries))
        # Toolang's model identity has provider/model components. A bare glob
        # selects the model-id component; tq-json matches the full ref key.
        branches: list[TQMatch] = []
        nonempty: list[frozenset[str]] = []
        for branch in cast(tuple[TQMatch, ...], getattr(parsed, "matches")):
            identity = branch.identity
            if not branch.exact and "/" not in identity:
                identity = f"*/{identity}"
            # Collection-query equality on sequence fields was membership;
            # tq-json spells that `has`. Preserve the existing selection for
            # = and != while keeping explicit tq-json `has no` on empty lists.
            negative = frozenset(
                predicate.field.name
                for predicate in branch.predicates
                if predicate.field.name in _SEQUENCE_FIELDS
                and predicate.operator == "!="
                and predicate.values != (None,)
            )
            nonempty.append(negative)
            predicates = tuple(
                replace(
                    predicate,
                    operator=(
                        {"=": "has", "!=": "has no"}.get(
                            predicate.operator, predicate.operator
                        )
                        if predicate.field.name in _SEQUENCE_FIELDS
                        and predicate.values != (None,)
                        else predicate.operator
                    ),
                    values=tuple(
                        _canonical_date_literal(predicate.field.name, value)
                        for value in predicate.values
                    ),
                )
                for predicate in branch.predicates
            )
            branches.append(replace(branch, identity=identity, predicates=predicates))
        schema = _MODEL_QUERY_SCHEMA
        if allow_policy:
            schema = {
                **_MODEL_QUERY_SCHEMA,
                "filter": tuple(
                    name
                    for name in MODEL_SCHEMA.fields
                    if name not in {"allowed", "ready"}
                ),
            }
        return Query(tuple(branches)).validate(schema), tuple(nonempty)
    except QueryError as error:
        raise ValueError(str(error)) from error


def _canonical_date_literal(field: str, value: object) -> object:
    """Normalize month-precision dates to their first day for string comparison."""

    if field in {"release_date", "last_updated"} and isinstance(value, str):
        if re.fullmatch(r"\d{4}-\d{2}", value):
            return f"{value}-01"
    return value


def match_model_branches(
    models: Sequence[Model],
    queries: Sequence[str],
    *,
    allow_policy: bool = False,
) -> tuple[tuple[int, int, Model], ...]:
    """Return TQ matches as (first branch, source position, model)."""

    query, nonempty = _prepare_model_query(queries, allow_policy=allow_policy)
    branch_queries = (
        tuple(
            Query((branch,))
            for branch in cast(tuple[TQMatch, ...], getattr(query, "matches"))
        )
        if any(nonempty)
        else ()
    )
    matches: list[tuple[int, int, Model]] = []
    for position, model in enumerate(models):
        record = model_query_record(model)
        if branch_queries:
            branch = next(
                (
                    index
                    for index, branch_query in enumerate(branch_queries)
                    if all(
                        _sequence_field_nonempty(record, field)
                        for field in nonempty[index]
                    )
                    and branch_query.match(record, key="ref") is not None
                ),
                None,
            )
        else:
            branch = query.match(record, key="ref")
        if branch is not None:
            matches.append((branch, position, model))
    return tuple(matches)


def _sequence_field_nonempty(record: dict[str, object], field: str) -> bool:
    value: object = record
    for part in field.split("."):
        if not isinstance(value, Mapping):
            return False
        value = cast(Mapping[str, object], value).get(part)
    return bool(value)


def filter_models(
    models: Sequence[Model], queries: Sequence[str] | None
) -> tuple[Model, ...]:
    """Return query matches in branch order, stable by source position."""

    if queries is None:
        return tuple(models)
    if not queries:
        return ()
    matches = match_model_branches(models, queries)
    return tuple(item[2] for item in sorted(matches, key=lambda item: item[:2]))


def order_and_allow_models(
    models: Sequence[Model], queries: Sequence[str] | None
) -> tuple[tuple[Model, ...], frozenset[str]]:
    """Apply allow-query branch order without dropping unmatched records."""

    records = tuple(models)
    if queries is None:
        return records, frozenset(model.ref for model in records)
    if not queries:
        return records, frozenset()
    matches = match_model_branches(records, queries, allow_policy=True)
    ordered_matches = sorted(matches, key=lambda item: item[:2])
    allowed = frozenset(item[2].ref for item in matches)
    matched_positions = {item[1] for item in matches}
    unmatched = tuple(
        model
        for position, model in enumerate(records)
        if position not in matched_positions
    )
    return (*tuple(item[2] for item in ordered_matches), *unmatched), allowed


def apply_model_operations(
    models: Sequence[Model],
    operations: Sequence[tuple[SetOperator, Sequence[str]]],
) -> tuple[Model, ...]:
    """Apply model set directives with transient TQ queries, preserving source order."""

    records = tuple(models)
    active = {model.ref for model in records}
    for operator, queries in operations:
        matched = {model.ref for model in filter_models(records, queries)}
        if operator == "=":
            active.intersection_update(matched)
        elif operator == "+=":
            active.update(matched)
        elif operator == "-=":
            active.difference_update(matched)
        else:  # pragma: no cover - SetOperator is a closed vocabulary
            raise ValueError(f"unknown model set operator: {operator!r}")
    return tuple(model for model in records if model.ref in active)


def first_model_ref(
    models: Sequence[Model], preferred: str | None = None
) -> str | None:
    """Return an available preferred ref, or the first model ref."""

    if preferred is not None and any(model.ref == preferred for model in models):
        return preferred
    return models[0].ref if models else None


def resolve_model(models: Sequence[Model], ref: str) -> Model:
    """Resolve one exact ref without retaining an index on the setup."""

    for model in models:
        if model.ref == ref:
            return model
    raise ToolangError(f"model ref is unavailable: {ref}")


def _date_value(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    if len(text) == 7:
        return f"{text}-01"
    return text


def _number_value(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def subset_models(models: Sequence[Model], refs: Sequence[str]) -> tuple[Model, ...]:
    """Resolve an ordered reference subset using only a temporary exact map."""

    by_ref = {model.ref: model for model in models}
    selected: list[Model] = []
    for ref in refs:
        model = by_ref.get(ref)
        if model is None:
            raise ToolangError(f"model ref is unavailable: {ref}")
        selected.append(model)
    return tuple(selected)


def model_refs(models: Iterable[Model]) -> tuple[str, ...]:
    """Return exact model refs in current record order."""

    return tuple(model.ref for model in models)
