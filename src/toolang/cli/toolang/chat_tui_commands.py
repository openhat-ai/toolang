"""Slash-command and selector helpers for the chat TUI."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import click
import typer

from ...models.resolution import split_model_selectors
from .chat_tui_values import _int_or_none, _list, _mapping, _text


def _chat_local_command(message: str) -> tuple[str, str] | None:
    stripped = message.strip()
    if not stripped.startswith("/"):
        return None
    command, _, argument = stripped[1:].partition(" ")
    if not command:
        return None
    return command, argument.strip()


def _chat_model_command_selectors(argument: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(split_model_selectors((argument,))))


def _chat_initial_model_label(selector_payload: Mapping[str, object]) -> str:
    requested = _chat_requested_model_selectors(selector_payload)
    return ", ".join(requested) if requested else "runtime model"


def _chat_resolved_model_label(
    ctx: typer.Context,
    selector_payload: Mapping[str, object],
    *,
    deps: Any,
) -> str:
    requested = _chat_requested_model_selectors(selector_payload)
    fallback = _chat_initial_model_label(selector_payload)
    try:
        payload = deps.runtime_json(ctx, "/api/v1/chat/models")
    except Exception:
        return fallback
    items = [_mapping(item) for item in _list(payload.get("items"))]
    if requested:
        labels = [
            _chat_model_item_label(item) if item is not None else selector
            for selector in requested
            for item in (_chat_find_model_item(items, selector),)
        ]
        return ", ".join(label for label in labels if label) or fallback
    default_selector = _text(payload.get("default"))
    if default_selector is not None:
        item = _chat_find_model_item(items, default_selector)
        if item is not None:
            return _chat_model_item_label(item)
        return default_selector
    if items:
        return _chat_model_item_label(items[0])
    return fallback


def _chat_resolve_model_command_labels(
    ctx: typer.Context,
    selectors: Sequence[str],
    *,
    deps: Any,
) -> tuple[str, ...] | None:
    try:
        payload = deps.runtime_json(ctx, "/api/v1/chat/models")
    except click.ClickException:
        return None
    items = [_mapping(item) for item in _list(payload.get("items"))]
    labels: list[str] = []
    for selector in selectors:
        item = _chat_find_model_item(items, selector)
        if item is None:
            return None
        labels.append(_chat_model_item_label(item))
    return tuple(labels)


def _chat_requested_model_selectors(selector_payload: Mapping[str, object]) -> tuple[str, ...]:
    models = selector_payload.get("models")
    if not isinstance(models, Sequence) or isinstance(models, (str, bytes, bytearray)):
        return ()
    return tuple(str(item) for item in models if str(item))


def _chat_find_model_item(items: Sequence[Mapping[str, Any]], selector: str) -> Mapping[str, Any] | None:
    normalized = _chat_model_selector_key(selector)
    for item in items:
        values = (
            _text(item.get("selector")),
            _text(item.get("ref")),
            _text(item.get("name")),
            _text(item.get("model")),
            _text(item.get("provider")),
        )
        if any(_chat_model_selector_key(value) == normalized for value in values if value is not None):
            return item
    return None


def _chat_model_selector_key(selector: str) -> str:
    return selector.strip().removeprefix("[").removesuffix("]")


def _chat_model_item_label(item: Mapping[str, Any]) -> str:
    ref = _text(item.get("ref"))
    if ref is not None:
        return ref
    provider = _text(item.get("provider"))
    model = _text(item.get("model"))
    if provider is not None and model is not None:
        return f"{provider}/{model}"
    return _text(item.get("selector")) or _text(item.get("name")) or "runtime model"


def _chat_set_executable_selector(selector_payload: dict[str, object], *, kind: str, name: str) -> None:
    selector_payload[kind] = name.strip()
    if kind == "thunk":
        selector_payload.pop("flow", None)
    elif kind == "flow":
        selector_payload.pop("thunk", None)


def _chat_executable_status_label(selector_payload: Mapping[str, object]) -> str:
    flow = _text(selector_payload.get("flow"))
    if flow:
        return f"flow:{flow}"
    thunk = _text(selector_payload.get("thunk"))
    if thunk:
        return f"thunk:{thunk}"
    return ""


def _chat_status_segments(label: str) -> list[tuple[str, str]]:
    pieces = [piece for piece in label.split("  ") if piece]
    if not pieces:
        return []
    segments: list[tuple[str, str]] = [("class:status.model", pieces[0])]
    for piece in pieces[1:]:
        if piece.startswith("thunk:"):
            segments.append(("class:status.text", "  "))
            segments.append(("class:status.thunk", piece))
        elif piece.startswith("flow:"):
            segments.append(("class:status.text", "  "))
            segments.append(("class:status.flow", piece))
        else:
            segments.append(("class:status.text", f"  {piece}"))
    return segments


def _chat_help_lines() -> list[str]:
    return [
        "Slash Commands",
        "",
        "/help, /?          Show help.",
        "/model [SELECTOR]  List or switch models.",
        "/thunk [NAME]      List or use a thunk.",
        "/flow [NAME]       List or use a flow.",
        "/queue             Show queue commands.",
        "/exit, /quit       Exit chat.",
    ]


def _chat_queue_help_lines() -> list[str]:
    return [
        "Queue Commands",
        "",
        "/queue steer N   Steer the active run with item #N.",
        "/queue edit N    Edit item #N in the input box.",
        "/queue delete N  Delete item #N.",
        "/queue clear     Clear all items.",
        "/q s N           First-letter abbreviations are accepted.",
    ]


def _chat_queue_command_index(value: str, item_count: int) -> int | None:
    index = _int_or_none(value)
    if index is None or index < 1 or index > item_count:
        return None
    return index - 1


def _chat_model_list_lines(payload: Mapping[str, Any]) -> list[str]:
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        return ["No available chat models."]
    default = _text(payload.get("default"))
    lines: list[str] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        selector = _text(item.get("selector"))
        if selector is None:
            continue
        suffix = " default" if selector == default else ""
        detail = _chat_model_item_detail(item)
        lines.append(f"{selector}{suffix}{f'  {detail}' if detail else ''}")
    return lines or ["No available chat models."]


def _chat_executable_list_lines(payload: Mapping[str, Any], *, selected: str | None) -> list[str]:
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        return ["No available items."]
    default = _text(payload.get("default"))
    lines: list[str] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        name = _text(item.get("name"))
        if name is None:
            continue
        labels: list[str] = []
        if name == selected:
            labels.append("current")
        if name == default:
            labels.append("default")
        suffix = f"  {' '.join(labels)}" if labels else ""
        lines.append(f"{name}{suffix}")
    return lines or ["No available items."]


def _chat_model_item_detail(item: Mapping[str, Any]) -> str:
    pieces = [
        _text(item.get("provider")),
        _text(item.get("adapter")),
    ]
    return " ".join(piece for piece in pieces if piece)
