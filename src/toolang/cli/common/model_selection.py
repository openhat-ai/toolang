"""Materialize model selectors against one effective model-list payload."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from fnmatch import fnmatchcase
from typing import Any, cast

from toolang.common.selectors import (
    Selector,
    filter_value_matches,
    selector_identity_matches,
)
from toolang.plugin.models.resolution import parse_model_selector


def materialize_model_list_ref(
    payload: Mapping[str, Any],
    selector: str,
) -> str:
    """Resolve one selector to exactly one ref exposed by a model list."""

    parsed = parse_model_selector(selector)
    items = _model_items(payload)
    if _selector_is_exact_route(parsed):
        exact_matches = tuple(
            ref
            for item in items
            if (ref := _text(item.get("ref"))) is not None and ref == parsed.pattern
        )
        if len(exact_matches) == 1:
            return exact_matches[0]
        if len(exact_matches) > 1:
            joined = ", ".join(exact_matches)
            raise ValueError(
                f"model selector is ambiguous: {selector} (matches {joined})"
            )
    matches = tuple(
        ref
        for item in items
        if (ref := _text(item.get("ref"))) is not None
        and _model_item_matches(item, ref=ref, selector=parsed)
    )
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise ValueError(f"model selector did not match an available model: {selector}")
    joined = ", ".join(matches)
    raise ValueError(f"model selector is ambiguous: {selector} (matches {joined})")


def _selector_is_exact_route(selector: Selector) -> bool:
    return (
        "/" in selector.pattern
        and not selector.filters
        and not any(char in selector.pattern for char in "*?[")
    )


def _model_items(payload: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    raw = payload.get("items")
    if not isinstance(raw, list | tuple):
        return ()
    return tuple(
        cast(Mapping[str, Any], item) for item in raw if isinstance(item, Mapping)
    )


def _model_item_matches(
    item: Mapping[str, Any],
    *,
    ref: str,
    selector: Selector,
) -> bool:
    provider = _text(item.get("provider")) or ref.partition("/")[0]
    prefix, separator, suffix = ref.partition("/")
    family = provider or prefix
    name = suffix if separator and prefix == family else ref
    route_model = suffix if separator else ref
    route_name = route_model.rpartition("/")[2]
    identity_matches = selector_identity_matches(
        family=family,
        name=name,
        selector=selector,
        extra_values=tuple(
            value
            for value in (
                ref,
                _text(item.get("name")),
                provider,
                _text(item.get("model")),
                route_name,
            )
            if value is not None
        ),
    ) or ("/" in selector.pattern and fnmatchcase(route_model, selector.pattern))
    if not identity_matches:
        return False
    for key, allowed in selector.filters.items():
        actual = _model_filter_values(item, key=key, provider=provider)
        if not actual or not any(
            filter_value_matches(value, allowed) for value in actual
        ):
            return False
    return True


def _model_filter_values(
    item: Mapping[str, Any],
    *,
    key: str,
    provider: str,
) -> tuple[str, ...]:
    if key == "provider":
        return (provider,)
    value = item.get(key)
    if isinstance(value, bool):
        return ("true" if value else "false",)
    if isinstance(value, str):
        return (value,)
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return tuple(candidate for candidate in value if isinstance(candidate, str))
    return ()


def _text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None
