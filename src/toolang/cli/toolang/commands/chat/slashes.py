"""Slash commands for terminal chat."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

import click

from toolang.common.errors import ToolangError
from toolang.cli.common.model_selection import materialize_model_selection
from toolang.execution.policy import parse_setting_override
from toolang.execution.types import ModelOverride, RunOverride, SessionSetting
from .base import AppContext, ChatResult, as_text, friendly_error
from .input import QuickCommand
from .policy import (
    materialize_runnable_list_ref,
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
    if not argument:
        raise ValueError("/model requires a model or parameter assignment.")
    client = app.get_client()
    update = parse_setting_override("model", argument)
    model = update.model
    if model is None:  # pragma: no cover - parser invariant
        raise RuntimeError("model setting did not contain a model override")
    if model.identity not in {None, "default", "none"}:
        payload = client.list_models()
        resolved = _chat_resolve_model_command(payload, model.identity)
        if resolved is None:
            raise ValueError(
                f"Model selection is unknown or ambiguous: {model.identity}"
            )
        update = RunOverride(
            model=ModelOverride(identity=resolved[0], effort=model.effort)
        )
    app.set_setting(client.apply_setting(app.get_setting(), update))
    app.refresh_status()
    return None


def _runnable(app: AppContext, command: str, argument: str) -> SlashOutput:
    if not argument:
        raise ValueError(f"/{command} requires a runnable identity.")
    kind = "runnable" if command == "runnable" else command
    update = parse_setting_override(command, argument)
    runnable = update.runnable
    if runnable is None:  # pragma: no cover - parser invariant
        raise RuntimeError("runnable setting did not contain a runnable override")
    if runnable != "default":
        payload = app.get_client().list_runnables(kind)
        resolved = _resolve_runnable_command(payload, runnable, kind=kind)
        if resolved is None:
            raise ValueError(f"Runnable selection is unknown or ambiguous: {runnable}")
        update = RunOverride(runnable=resolved)
    _apply_setting(app, update)
    return None


def _setting(app: AppContext, command: str, argument: str) -> SlashOutput:
    if not argument:
        raise ValueError(f"/{command} requires at least one field=value assignment.")
    _apply_setting(app, parse_setting_override(command, argument))
    return None


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
        "Set the session model or effort.",
        _model,
        "/model MODEL? effort=VALUE",
    ),
    SlashCommand(("agic",), "Switch the session agic.", _runnable, "/agic AGIC"),
    SlashCommand(("flow",), "Switch the session flow.", _runnable, "/flow FLOW"),
    SlashCommand(
        ("runnable",),
        "Switch the session runnable.",
        _runnable,
        "/runnable RUNNABLE",
    ),
    SlashCommand(
        ("allow",),
        "Set session resource ceilings.",
        _setting,
        "/allow FIELD=QUERY...",
    ),
    SlashCommand(
        ("limit",),
        "Set session run limits.",
        _setting,
        "/limit FIELD=VALUE...",
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


def _apply_setting(app: AppContext, update: RunOverride) -> None:
    app.set_setting(app.get_client().apply_setting(app.get_setting(), update))
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
    setting: SessionSetting,
) -> str:
    if setting.model is None:
        return "none"
    labels = _chat_resolve_model_command_labels(models_payload, (setting.model.ref,))
    label = labels[0] if labels else setting.model.ref
    reasoning = setting.model.parameters.reasoning
    if reasoning is None:
        return label
    value = (
        reasoning.effort
        if reasoning.effort is not None
        else str(reasoning.budget_tokens)
    )
    return f"{label} · {value}"


def _items(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        return []
    return [
        cast(Mapping[str, Any], item) for item in raw_items if isinstance(item, Mapping)
    ]


def _model_label(item: Mapping[str, Any]) -> str:
    return as_text(item.get("name")) or as_text(item.get("ref")) or "runtime model"
