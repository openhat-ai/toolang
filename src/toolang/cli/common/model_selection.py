"""Materialize model selections against one effective model-list payload."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from toolang.base.types.model import ModelRequest


def is_concrete_model_ref(value: str) -> bool:
    """Return whether a selection is already one unambiguous model ref."""

    try:
        ModelRequest(value)
    except (TypeError, ValueError):
        return False
    provider, separator, model = value.partition("/")
    return bool(separator and provider and model)


def materialize_model_selection(
    payload: Mapping[str, Any],
    selection: str,
) -> str:
    """Resolve one user-facing value to one exact model selection."""

    items = _model_items(payload)
    requested = _selection_value(selection)
    exact_matches = tuple(
        ref
        for item in items
        if (ref := _item_ref(item)) is not None and _selection_value(ref) == requested
    )
    if len(exact_matches) == 1:
        return exact_matches[0]
    if len(exact_matches) > 1:
        joined = ", ".join(exact_matches)
        raise ValueError(
            f"model selection is ambiguous: {selection} (matches {joined})"
        )
    matches = tuple(
        ref
        for item in items
        if (ref := _item_ref(item)) is not None
        and any(
            _selection_value(value) == requested
            for field in ("ref", "name", "model", "provider")
            if (value := _text(item.get(field))) is not None
        )
    )
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise ValueError(
            f"model selection did not match an available model: {selection}"
        )
    joined = ", ".join(matches)
    raise ValueError(f"model selection is ambiguous: {selection} (matches {joined})")


def _model_items(payload: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    raw = payload.get("items")
    if not isinstance(raw, list | tuple):
        return ()
    return tuple(
        cast(Mapping[str, Any], item) for item in raw if isinstance(item, Mapping)
    )


def _selection_value(value: str) -> str:
    text = value.strip()
    if text.startswith("[") and text.endswith("]"):
        return text[1:-1].strip()
    return text


def _item_ref(item: Mapping[str, Any]) -> str | None:
    return _text(item.get("ref"))


def _text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None
