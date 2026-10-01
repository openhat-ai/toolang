"""Transient native TQ matching of public model records."""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from tq import Query

from toolang.base.types.model import Model
from toolang.common.errors import ToolangError
from toolang.common.types import SetOperator

from .records import model_record


def match_model_branches(
    models: Sequence[Model], queries: Sequence[str]
) -> tuple[tuple[int, int, Model], ...]:
    query = Query.parse(queries).validate({"key": "ref"})
    return tuple(
        (branch, position, model)
        for position, model in enumerate(models)
        if (branch := query.match(model_record(model))) is not None
    )


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
    matches = match_model_branches(records, queries)
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
