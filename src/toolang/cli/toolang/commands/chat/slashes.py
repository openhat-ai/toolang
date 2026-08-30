"""Slash commands for terminal chat."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

import click

from toolang.common.errors import ToolangError
from toolang.base.types.model import ReasoningEffort
from toolang.cli.common.model_selection import materialize_model_selection
from toolang.execution.types import RunOverride
from .base import AppContext, ChatResult, as_text, friendly_error
from .input import QuickCommand
from .policy import (
    apply_model_selection,
    materialize_runnable_list_ref,
    reasoning_effort_from_selects,
)

SlashOutput = str | Sequence[str] | ChatResult | None


@dataclass(frozen=True, slots=True)
class SlashResult:
    handled: bool
    lines: list[str] | None = None
    result: ChatResult | None = None


@dataclass(frozen=True, slots=True)
class SlashCommand:
    names: tuple[str, ...]
    summary: str
    run: Callable[[AppContext, str, str], SlashOutput]
    usage: str = ""

    @property
    def primary(self) -> str:
        return self.names[0]

    @property
    def display_usage(self) -> str:
        return self.usage or f"/{self.primary}"


def handle(app: AppContext, quick: QuickCommand) -> SlashResult:
    command = quick.name
    argument = quick.tail or ""
    slash = _SLASH_BY_NAME.get(command)
    if slash is None:
        app.set_status_error(f"Unknown command: /{command}")
        return SlashResult(True)

    try:
        output = slash.run(app, command, argument)
    except (ToolangError, ValueError) as exc:
        app.set_status_error(friendly_error(str(exc)))
        return SlashResult(True)
    except click.ClickException as exc:
        app.set_status_error(friendly_error(exc.message))
        return SlashResult(True)

    if isinstance(output, ChatResult):
        return SlashResult(True, result=output)
    if output is not None:
        lines = output.splitlines() or [""] if isinstance(output, str) else list(output)
        return SlashResult(True, lines)
    return SlashResult(True)


def _help(_app: AppContext, _command: str, _argument: str) -> list[str]:
    return _chat_help_lines()


def _exit(app: AppContext, _command: str, _argument: str) -> None:
    app.request_exit()
    return None


def _model(app: AppContext, _command: str, argument: str) -> SlashOutput:
    client = app.get_client()
    payload = client.list_models()
    if not argument:
        open_picker = getattr(app, "open_model_picker", None)
        if callable(open_picker):
            open_picker(payload)
            return None
        return ["Available Models", *_chat_model_list_lines(payload)]
    tokens = argument.split()
    resolved = _resolve_model_selection(payload, tokens)
    if resolved is None:
        raise ValueError(
            f"Model selection or reasoning effort is unknown or ambiguous: {argument}"
        )
    ref, effort = resolved
    updated = apply_model_selection(app.get_selects(), ref=ref, effort=effort)
    selects = app.get_selects()
    selects.clear()
    selects.update(updated)
    app.refresh_status()
    return None


def _runnable(app: AppContext, command: str, argument: str) -> SlashOutput:
    client = app.get_client()
    selects = app.get_selects()
    kind = "runnable" if command == "runnable" else command
    payload = client.list_runnables(kind)
    if argument:
        tokens = argument.split()
        if len(tokens) != 1:
            raise ValueError(f"/{command} accepts at most one runnable query.")
        resolved = _resolve_runnable_command(payload, tokens[0], kind=kind)
        if resolved is None:
            raise ValueError(f"Runnable selection is unknown or ambiguous: {tokens[0]}")
        _apply_default(app, field="runnable", value=resolved)
        return None
    selected = as_text(selects.get(command))
    if command == "runnable" and selected is None:
        for kind in ("agic", "flow"):
            if (name := as_text(selects.get(kind))) is not None:
                selected = f"{kind}:{name}"
                break
    return [
        "Available Runnables"
        if command == "runnable"
        else f"Available {command.title()}s",
        *_chat_runnable_list_lines(
            payload,
            selected=selected,
            show_kind=command == "runnable",
        ),
    ]


def _queue(app: AppContext, _command: str, argument: str) -> SlashOutput:
    tokens = argument.split()
    if not tokens:
        return _chat_queue_help_lines()

    action = tokens[0].lower()
    index = None
    queue = app.get_queue()
    if len(tokens) > 1:
        try:
            requested_index = int(tokens[1])
            index = requested_index - 1 if 1 <= requested_index <= len(queue) else None
        except ValueError:
            pass
    if action in {"clear", "c"}:
        queue.clear()
    elif action not in {"steer", "s", "delete", "d", "edit", "e"}:
        app.set_status_error(f"Unknown queue command: {tokens[0]}")
    elif index is None:
        app.set_status_error(f"/queue {tokens[0]} requires an item number.")
    elif action in {"delete", "d"}:
        queue.pop(index)
    elif action in {"edit", "e"}:
        app.replace_input(queue.pop(index).source)
    elif app.get_active_run() is None:
        app.set_status_error("No active run to steer.")
    else:
        app.request_steer(queue[index].source)
        queue.pop(index)
    return None


def _steer(app: AppContext, _command: str, argument: str) -> SlashOutput:
    if not argument:
        app.set_status_error("/steer requires a message.")
        return None
    app.request_steer(argument)
    return None


def _show(app: AppContext, _command: str, argument: str) -> SlashOutput:
    tokens = argument.split()
    if len(tokens) > 1:
        app.set_status_error("/show accepts at most one run id.")
        return None
    run_id = tokens[0] if tokens else None
    return app.get_client().get_result(
        run_id,
        thread_id=app.get_thread_id(),
    )


SLASHES: tuple[SlashCommand, ...] = (
    SlashCommand(("help", "?"), "Show help.", _help, "/help, /?"),
    SlashCommand(
        ("model",),
        "List or switch models.",
        _model,
        "/model [MODEL [EFFORT|auto]]",
    ),
    SlashCommand(("agic",), "List or switch agics.", _runnable, "/agic [AGIC]"),
    SlashCommand(("flow",), "List or switch flows.", _runnable, "/flow [FLOW]"),
    SlashCommand(
        ("runnable",),
        "List or switch runnables.",
        _runnable,
        "/runnable [RUNNABLE]",
    ),
    SlashCommand(
        ("steer", "s"),
        "Steer the active run.",
        _steer,
        "/steer MESSAGE",
    ),
    SlashCommand(
        ("queue", "q"),
        "Inspect or edit queued submissions.",
        _queue,
        "/queue [ACTION]",
    ),
    SlashCommand(
        ("show",),
        "Show a durable run result.",
        _show,
        "/show [RUN_ID]",
    ),
    SlashCommand(("exit", "quit"), "Exit chat.", _exit, "/exit, /quit"),
)
_SLASH_BY_NAME = {name: slash for slash in SLASHES for name in slash.names}


def _chat_help_lines() -> list[str]:
    width = max(len(slash.display_usage) for slash in SLASHES)
    return [
        "Chat Commands",
        "",
        *[f"{slash.display_usage:<{width}}  {slash.summary}" for slash in SLASHES],
    ]


def _chat_queue_help_lines() -> list[str]:
    return [
        "Queue Commands",
        "",
        "/queue steer N   Steer the active run with item #N.",
        "/queue edit N    Edit item #N in the input box.",
        "/queue delete N  Delete item #N.",
        "/queue clear     Clear all items.",
    ]


def _apply_default(app: AppContext, *, field: str, value: str) -> None:
    updated = app.get_client().apply_settings(
        (RunOverride("default", field, value),),
        app.get_selects(),
    )
    selects = app.get_selects()
    selects.clear()
    selects.update(updated)
    app.refresh_status()


def _resolve_runnable_command(
    payload: Mapping[str, Any],
    selector: str,
    *,
    kind: str,
) -> str | None:
    try:
        return materialize_runnable_list_ref(payload, selector, kind=kind)
    except ValueError:
        return None


def _chat_resolve_model_command_labels(
    models_payload: Mapping[str, Any],
    selectors: Sequence[str],
) -> tuple[str, ...] | None:
    resolved = [
        _chat_resolve_model_command(models_payload, selector) for selector in selectors
    ]
    if any(item is None for item in resolved):
        return None
    return tuple(item[1] for item in resolved if item is not None)


def _chat_resolve_model_command(
    models_payload: Mapping[str, Any],
    selector: str,
) -> tuple[str, str] | None:
    try:
        canonical = materialize_model_selection(models_payload, selector)
    except ValueError:
        return None
    match = next(
        (
            item
            for item in _items(models_payload)
            if (as_text(item.get("selector")) or as_text(item.get("ref"))) == canonical
        ),
        None,
    )
    if match is None:
        return None
    return canonical, _model_label(match)


def chat_model_label(
    models_payload: Mapping[str, Any],
    selects: Mapping[str, object],
) -> str:
    model = as_text(selects.get("model"))
    if model in {None, "default"}:
        default = as_text(models_payload.get("default"))
        if default is None:
            return "default"
        labels = _chat_resolve_model_command_labels(models_payload, (default,))
        label = labels[0] if labels else default
        effort = reasoning_effort_from_selects(selects)
        return f"{label} · {effort.title()}" if effort is not None else label
    labels = _chat_resolve_model_command_labels(models_payload, (model,))
    label = labels[0] if labels else model
    effort = reasoning_effort_from_selects(selects)
    return f"{label} · {effort.title()}" if effort is not None else label


def _chat_model_list_lines(payload: Mapping[str, Any]) -> list[str]:
    default = as_text(payload.get("default"))
    lines: list[str] = []
    for item in _items(payload):
        selector = as_text(item.get("selector")) or as_text(item.get("ref"))
        if selector is None:
            continue
        efforts = _model_efforts(item)
        columns = [
            _model_label(item),
            *(["default"] if selector == default else []),
            *[
                text
                for value in (item.get("provider"), item.get("adapter"))
                if (text := as_text(value))
            ],
            *(f"reasoning: {', '.join(efforts)}" for _ in (0,) if efforts),
        ]
        lines.append("  ".join(columns))
    return lines or ["No available chat models."]


def _chat_runnable_list_lines(
    payload: Mapping[str, Any],
    *,
    selected: str | None,
    show_kind: bool = False,
) -> list[str]:
    default = as_text(payload.get("default"))
    lines: list[str] = []
    for item in _items(payload):
        name = as_text(item.get("name"))
        if name is None:
            continue
        kind = as_text(item.get("kind"))
        ref = f"{kind}:{name}" if show_kind and kind is not None else name
        labels = [
            label
            for enabled, label in (
                (ref == selected or name == selected, "current"),
                (ref == default or name == default, "default"),
            )
            if enabled
        ]
        lines.append(f"{ref}  {' '.join(labels)}" if labels else ref)
    return lines or ["No available items."]


def _items(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        return []
    return [
        cast(Mapping[str, Any], item) for item in raw_items if isinstance(item, Mapping)
    ]


def _model_label(item: Mapping[str, Any]) -> str:
    return as_text(item.get("name")) or as_text(item.get("ref")) or "runtime model"


def _model_efforts(item: Mapping[str, Any]) -> tuple[ReasoningEffort, ...]:
    parameters = item.get("parameters")
    reasoning = parameters.get("reasoning") if isinstance(parameters, Mapping) else None
    values = reasoning.get("effort") if isinstance(reasoning, Mapping) else None
    recognized = {"none", "minimal", "low", "medium", "high", "xhigh", "max", "default"}
    return (
        tuple(
            cast(ReasoningEffort, value)
            for value in values
            if isinstance(value, str) and value in recognized
        )
        if isinstance(values, list | tuple)
        else ()
    )


def _resolve_model_selection(
    payload: Mapping[str, Any], tokens: Sequence[str]
) -> tuple[str, ReasoningEffort | None] | None:
    if not 1 <= len(tokens) <= 2:
        return None
    resolved = _chat_resolve_model_command(payload, tokens[0])
    if resolved is None:
        return None
    ref, _label = resolved
    if len(tokens) == 1 or tokens[1].lower() == "auto":
        return ref, None
    item = next(
        (
            item
            for item in _items(payload)
            if (as_text(item.get("selector")) or as_text(item.get("ref"))) == ref
        ),
        None,
    )
    effort = tokens[1].lower()
    if item is None or effort not in _model_efforts(item):
        return None
    return ref, cast(ReasoningEffort, effort)
