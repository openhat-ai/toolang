"""Materialize model selectors against one effective model-list payload."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
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
    matches = tuple(
        ref
        for item in _model_items(payload)
        if (ref := _text(item.get("ref"))) is not None
        and _model_item_matches(item, ref=ref, selector=parsed)
    )
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise ValueError(f"model selector did not match an available model: {selector}")
    joined = ", ".join(matches)
    raise ValueError(f"model selector is ambiguous: {selector} (matches {joined})")


def model_ref_is_exact_route(ref: str) -> bool:
    """Return whether a ref is already an exact provider-qualified route."""

    parsed = parse_model_selector(ref)
    return (
        "/" in parsed.pattern
        and not parsed.filters
        and not any(char in parsed.pattern for char in "*?[")
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
    if not selector_identity_matches(
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
            )
            if value is not None
        ),
    ):
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
    if key == "reasoning":
        parameters = item.get("parameters")
        reasoning = (
            parameters.get("reasoning") if isinstance(parameters, Mapping) else None
        )
        efforts = reasoning.get("effort") if isinstance(reasoning, Mapping) else None
        return (
            "true" if isinstance(efforts, list | tuple) and bool(efforts) else "false",
        )
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
