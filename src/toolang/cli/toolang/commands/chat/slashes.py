"""Slash commands for terminal chat."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

import click

from toolang.common.errors import ToolangError
from toolang.plugin.models.resolution import split_model_selectors
from .base import AppContext, as_text, friendly_error

SlashOutput = str | Sequence[str] | None


@dataclass(frozen=True, slots=True)
class SlashResult:
    handled: bool
    lines: list[str] | None = None


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


def handle(app: AppContext, message: str) -> SlashResult:
    parsed = _chat_local_command(message)
    if parsed is None:
        return SlashResult(False)
    command, argument = parsed
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

    if output is not None:
        lines = output.splitlines() or [""] if isinstance(output, str) else list(output)
        return SlashResult(True, lines)
    return SlashResult(True)


def _chat_local_command(message: str) -> tuple[str, str] | None:
    stripped = message.strip()
    if not stripped.startswith("/"):
        return None
    command, _, argument = stripped[1:].partition(" ")
    return (command, argument.strip()) if command else None


def _help(_app: AppContext, _command: str, _argument: str) -> list[str]:
    return _chat_help_lines()


def _exit(app: AppContext, _command: str, _argument: str) -> None:
    app.request_exit()
    return None


def _model(app: AppContext, _command: str, argument: str) -> SlashOutput:
    client = app.get_client()
    selects = app.get_selects()
    if not argument:
        return ["Available Models", *_chat_model_list_lines(client.list_models())]
    if app.is_busy():
        app.set_status_error("Cannot change model while a run is active.")
        return None

    selectors = _chat_model_command_selectors(argument)
    labels = (
        _chat_resolve_model_command_labels(client.list_models(), selectors)
        if selectors
        else None
    )
    if labels is None:
        app.set_status_error(
            "/model requires a selector."
            if not selectors
            else f"Model selector matched no models: {', '.join(selectors)}"
        )
        return None

    selects["models"] = list(selectors)
    app.refresh_status()
    return f"model: {', '.join(labels)}"


def _executable(app: AppContext, command: str, argument: str) -> SlashOutput:
    client = app.get_client()
    selects = app.get_selects()
    if not argument:
        selected = as_text(selects.get(command))
        return [
            f"Available {command.title()}s",
            *_chat_executable_list_lines(
                client.list_executables(command), selected=selected
            ),
        ]
    if app.is_busy():
        app.set_status_error(f"Cannot change {command} while a run is active.")
        return None

    _chat_set_executable_selector(selects, kind=command, name=argument)
    app.refresh_status()
    return f"{command}: {argument}"


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
        app.replace_input(queue.pop(index))
    elif app.get_active_run() is None:
        app.set_status_error("No active run to steer.")
    else:
        app.request_steer(queue[index])
        queue.pop(index)
    return None


def _steer(app: AppContext, _command: str, argument: str) -> SlashOutput:
    if not argument:
        app.set_status_error("/steer requires a message.")
        return None
    app.request_steer(argument)
    return None


SLASHES: tuple[SlashCommand, ...] = (
    SlashCommand(("help", "?"), "Show help.", _help, "/help, /?"),
    SlashCommand(
        ("model", "models"),
        "List or switch models.",
        _model,
        "/model [selector]",
    ),
    SlashCommand(("agic",), "List or use an agic.", _executable, "/agic [name]"),
    SlashCommand(("flow",), "List or use a flow.", _executable, "/flow [name]"),
    SlashCommand(
        ("steer", "s"),
        "Steer the active run.",
        _steer,
        "/steer <message>, /s <message>",
    ),
    SlashCommand(
        ("queue", "q"),
        "Inspect or edit queued submissions.",
        _queue,
        "/queue <action>, /q <action>",
    ),
    SlashCommand(("exit", "quit"), "Exit chat.", _exit, "/exit, /quit"),
)
_SLASH_BY_NAME = {name: slash for slash in SLASHES for name in slash.names}


def _chat_help_lines() -> list[str]:
    width = max(len(slash.display_usage) for slash in SLASHES)
    return [
        "Slash Commands",
        "",
        *[
            f"{slash.display_usage:<{width}}  {slash.summary}"
            for slash in SLASHES
        ],
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


def _chat_model_command_selectors(argument: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(split_model_selectors((argument,))))


def _chat_resolve_model_command_labels(
    models_payload: Mapping[str, Any],
    selectors: Sequence[str],
) -> tuple[str, ...] | None:
    items = [
        item for item in _items(models_payload) if isinstance(item.get("selector"), str)
    ]
    labels: list[str] = []
    for selector in selectors:
        target = selector.strip().removeprefix("[").removesuffix("]")
        match = next(
            (
                item
                for item in items
                if any(
                    value.strip().removeprefix("[").removesuffix("]") == target
                    for value in (
                        as_text(item.get("selector")),
                        as_text(item.get("ref")),
                        as_text(item.get("name")),
                        as_text(item.get("model")),
                        as_text(item.get("provider")),
                    )
                    if value is not None
                )
            ),
            None,
        )
        if match is None:
            return None
        labels.append(_model_label(match))
    return tuple(labels)


def chat_model_label(
    models_payload: Mapping[str, Any],
    selects: Mapping[str, object],
) -> str:
    models = selects.get("models")
    if isinstance(models, Sequence) and not isinstance(models, (str, bytes, bytearray)):
        selectors = tuple(str(item) for item in models if str(item))
        labels = _chat_resolve_model_command_labels(models_payload, selectors)
        return ", ".join(labels or selectors) if selectors else "runtime model"

    default = as_text(models_payload.get("default"))
    if default is None:
        return "runtime model"
    labels = _chat_resolve_model_command_labels(models_payload, (default,))
    return labels[0] if labels else default


def _chat_set_executable_selector(
    selects: dict[str, object], *, kind: str, name: str
) -> None:
    selects[kind] = name.strip()
    selects.pop("flow" if kind == "agic" else "agic", None)


def _chat_model_list_lines(payload: Mapping[str, Any]) -> list[str]:
    default = as_text(payload.get("default"))
    lines: list[str] = []
    for item in _items(payload):
        selector = as_text(item.get("selector"))
        if selector is None:
            continue
        columns = [
            selector,
            *(['default'] if selector == default else []),
            *[text for value in (item.get("provider"), item.get("adapter")) if (text := as_text(value))],
        ]
        lines.append("  ".join(columns))
    return lines or ["No available chat models."]


def _chat_executable_list_lines(
    payload: Mapping[str, Any], *, selected: str | None
) -> list[str]:
    default = as_text(payload.get("default"))
    lines: list[str] = []
    for item in _items(payload):
        name = as_text(item.get("name"))
        if name is None:
            continue
        labels = [
            label
            for enabled, label in (
                (name == selected, "current"),
                (name == default, "default"),
            )
            if enabled
        ]
        lines.append(f"{name}  {' '.join(labels)}" if labels else name)
    return lines or ["No available items."]


def _items(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        return []
    return [
        cast(Mapping[str, Any], item) for item in raw_items if isinstance(item, Mapping)
    ]


def _model_label(item: Mapping[str, Any]) -> str:
    ref = as_text(item.get("ref"))
    provider = as_text(item.get("provider"))
    model = as_text(item.get("model"))
    if ref is not None:
        return ref
    if provider is not None and model is not None:
        return f"{provider}/{model}"
    return as_text(item.get("selector")) or as_text(item.get("name")) or "runtime model"
