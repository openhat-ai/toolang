"""Slash commands for terminal chat."""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, TypeAlias, cast

import click

from toolang.cli.common.model_selection import materialize_model_selection
from toolang.common.errors import ToolangError
from toolang.execution.policy import parse_setting_override
from toolang.execution.types import ModelOverride, RunOverride, SessionSetting

from .base import AppContext, ChatClient, ChatResult, as_text, friendly_error
from .input import QuickCommand
from .policy import (
    materialize_runnable_list_ref,
    run_override_help_lines,
    setting_slash_usage,
)
from .shortcuts import help_lines as shortcut_help_lines

SlashOutcomeKind = Literal["success", "result", "usage", "error"]
SlashArgument = Literal["none", "optional", "required"]


@dataclass(frozen=True, slots=True)
class SlashText:
    """One concise slash outcome with optional supporting lines."""

    summary: str
    details: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SlashTable:
    """One presentation-neutral slash table."""

    summary: str
    headers: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]


@dataclass(frozen=True, slots=True)
class SlashRunResult:
    """One durable result returned by `/show`."""

    result: ChatResult


SlashContent: TypeAlias = SlashText | SlashTable | SlashRunResult


@dataclass(frozen=True, slots=True)
class SlashOutcome:
    """Semantic result of one submitted slash command."""

    kind: SlashOutcomeKind
    content: SlashContent


@dataclass(frozen=True, slots=True)
class SlashCommand:
    names: tuple[str, ...]
    summary: str
    run: Callable[[AppContext, str, str], SlashOutcome | None]
    usage: str = ""
    argument: SlashArgument = "optional"

    @property
    def primary(self) -> str:
        return self.names[0]

    @property
    def display_usage(self) -> str:
        return self.usage or f"/{self.primary}"


def handle(app: AppContext, quick: QuickCommand) -> SlashOutcome | None:
    """Dispatch one registered, structurally parsed slash command."""

    command = quick.name
    argument = quick.tail or ""
    slash = _SLASH_BY_NAME.get(command)
    if slash is None:
        raise KeyError(f"unregistered slash command: {command}")
    if slash.argument == "required" and not argument:
        return _usage(slash)
    if slash.argument == "none" and argument:
        return error_outcome(f"/{command} does not accept an argument")

    try:
        return slash.run(app, command, argument)
    except (ToolangError, ValueError) as exc:
        return error_outcome(str(exc))
    except click.ClickException as exc:
        return error_outcome(exc.message)


def is_registered(command: str) -> bool:
    """Return whether one structural slash name belongs to the registry."""

    return command in _SLASH_BY_NAME


def unrecognized_diagnostic(command: str) -> str:
    """Return actionable status text for an undispatchable slash input."""

    if not command:
        return "Enter a command after / · See /? for help"
    return f"Unknown command /{command} · See /? for help"


def error_outcome(message: str) -> SlashOutcome:
    """Build one user-visible slash error."""

    return SlashOutcome("error", SlashText(f"Error: {friendly_error(message)}"))


def outcome_lines(outcome: SlashOutcome) -> tuple[str, ...]:
    """Project one outcome to deterministic plain-text lines."""

    content = outcome.content
    if isinstance(content, SlashText):
        return (content.summary, *content.details)
    if isinstance(content, SlashTable):
        widths = tuple(
            max(len(header), *(len(row[index]) for row in content.rows))
            for index, header in enumerate(content.headers)
        )
        table = (
            _plain_table_row(content.headers, widths),
            *(_plain_table_row(row, widths) for row in content.rows),
        )
        return (content.summary, *table)
    return (f"{content.result.run_id} result",)


def _plain_table_row(values: Sequence[str], widths: Sequence[int]) -> str:
    return "  ".join(
        value if index == len(values) - 1 else value.ljust(widths[index])
        for index, value in enumerate(values)
    ).rstrip()


def _usage(command: SlashCommand) -> SlashOutcome:
    return SlashOutcome("usage", SlashText(f"Usage: {command.display_usage}"))


def _success(summary: str, *details: str) -> SlashOutcome:
    return SlashOutcome("success", SlashText(summary, tuple(details)))


def _result(summary: str, *details: str) -> SlashOutcome:
    return SlashOutcome("result", SlashText(summary, tuple(details)))


def _help(_app: AppContext, _command: str, _argument: str) -> SlashOutcome:
    return _result(
        "Slash commands act immediately.",
        "Setting commands change defaults for future runs in this Chat session.",
        "",
        "Submit one slash command by itself; it cannot be combined with run input.",
        "See :? to change settings for one run only.",
        "",
        "Available commands:",
        *_chat_help_lines(),
    )


def run_override_help() -> SlashOutcome:
    """Return the special `:?` help result."""

    return _result(
        "Run overrides change settings for this run only.",
        "Session defaults stay unchanged.",
        "",
        "Put one or more override lines first.",
        "Include the run input in the same submission.",
        "",
        "Available overrides:",
        *run_override_help_lines(),
    )


def _keys(_app: AppContext, _command: str, _argument: str) -> SlashOutcome:
    return _result(
        "These shortcuts control interactive Chat.",
        "Standard cursor and text-editing keys are not listed.",
        "",
        "Available shortcuts:",
        *shortcut_help_lines(),
    )


def _exit(app: AppContext, _command: str, _argument: str) -> None:
    app.request_exit()
    return None


def _model(app: AppContext, _command: str, argument: str) -> SlashOutcome:
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
    setting = _candidate_setting(app, update)
    _commit_setting(app, setting)
    return _success(_model_setting_summary(setting))


def _runnable(app: AppContext, command: str, argument: str) -> SlashOutcome:
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
    setting = _candidate_setting(app, update)
    _commit_setting(app, setting)
    return _success(f"Runnable set to {setting.runnable or 'none'}")


def _setting(app: AppContext, command: str, argument: str) -> SlashOutcome:
    update = parse_setting_override(command, argument)
    previous = app.get_setting()
    if command == "allow":
        summary, allowed_model_refs = _allow_summary(app.get_client(), update)
        setting = _candidate_setting(
            app,
            update,
            allowed_model_refs=allowed_model_refs,
        )
        details: list[str] = []
        if previous.model is not None and setting.model is None:
            details.append(
                f"Model cleared: {previous.model.ref} is outside allow.models"
            )
    else:
        setting = _candidate_setting(app, update)
        summary = _limit_summary(setting, update)
        details = []
    _commit_setting(app, setting)
    return _success(summary, *details)


def _resources(app: AppContext, command: str, argument: str) -> SlashOutcome:
    queries = (argument,) if argument else None
    client = app.get_client()
    if command == "models":
        payload = client.list_models(queries)
        items = _items(payload)
        if not items:
            return _result("No models found")
        current = app.get_setting().model
        current_ref = current.ref if current is not None else None
        default = as_text(payload.get("default"))
        rows = tuple(
            (
                as_text(item.get("ref")) or "-",
                (
                    "current"
                    if as_text(item.get("ref")) == current_ref
                    else "default"
                    if as_text(item.get("ref")) == default
                    else ""
                ),
                _model_efforts(item),
            )
            for item in items
        )
        return SlashOutcome(
            "result",
            SlashTable(
                _found(len(rows), "model"),
                ("MODEL", "STATE", "EFFORT"),
                rows,
            ),
        )
    if command == "tools":
        items = _items(client.list_tools(queries))
        if not items:
            return _result("No tools found")
        rows = tuple(
            (
                as_text(item.get("ref")) or "-",
                as_text(item.get("plugin")) or "-",
                as_text(item.get("description")) or "",
            )
            for item in items
        )
        return SlashOutcome(
            "result",
            SlashTable(
                _found(len(rows), "tool"),
                ("TOOL", "PLUGIN", "DESCRIPTION"),
                rows,
            ),
        )
    items = _items(client.list_caps(None, queries))
    if not items:
        return _result("No caps found")
    rows = tuple(
        (
            as_text(item.get("identity")) or "-",
            as_text(item.get("scope")) or "-",
            as_text(item.get("description")) or "",
        )
        for item in items
    )
    return SlashOutcome(
        "result",
        SlashTable(
            _found(len(rows), "cap"),
            ("CAP", "SCOPE", "DESCRIPTION"),
            rows,
        ),
    )


def _queue(app: AppContext, _command: str, argument: str) -> SlashOutcome:
    tokens = argument.split()
    if not tokens:
        return _result("Queue commands", *_chat_queue_help_lines())

    action = tokens[0].lower()
    queue = app.get_queue()
    if action in {"clear", "c"}:
        if len(tokens) != 1:
            raise ValueError("/queue clear does not accept an item number")
        count = len(queue)
        queue.clear()
        return _success(f"Cleared {count} queued {_plural(count, 'submission')}")
    if action not in {"steer", "s", "delete", "d", "edit", "e"}:
        raise ValueError(f"Unknown queue command: {tokens[0]}")
    if len(tokens) != 2:
        raise ValueError(f"/queue {tokens[0]} requires an item number")
    try:
        requested_index = int(tokens[1])
    except ValueError as exc:
        raise ValueError(f"/queue {tokens[0]} requires an item number") from exc
    index = requested_index - 1
    if index < 0 or index >= len(queue):
        raise ValueError(f"Queue item does not exist: {requested_index}")
    if action in {"delete", "d"}:
        queue.pop(index)
        return _success(f"Deleted queue item {requested_index}")
    if action in {"edit", "e"}:
        app.replace_input(queue[index].source)
        queue.pop(index)
        return _success(f"Moved queue item {requested_index} to input")
    if app.get_active_run() is None:
        raise ValueError("No active run to steer")
    app.request_steer(queue[index].source)
    queue.pop(index)
    return _success(f"Accepted queue item {requested_index} as steer")


def _steer(app: AppContext, _command: str, argument: str) -> SlashOutcome:
    if app.get_active_run() is None:
        raise ValueError("No active run to steer")
    app.request_steer(argument)
    return _success("Steer accepted")


def _show(app: AppContext, _command: str, argument: str) -> SlashOutcome:
    tokens = argument.split()
    if len(tokens) > 1:
        raise ValueError("/show accepts at most one run id")
    run_id = tokens[0] if tokens else None
    result = app.get_client().get_result(
        run_id,
        thread_id=app.get_thread_id(),
    )
    return SlashOutcome("result", SlashRunResult(result))


SLASHES: tuple[SlashCommand, ...] = (
    SlashCommand(
        ("model",),
        "Set the session model or effort",
        _model,
        setting_slash_usage("model"),
        "required",
    ),
    SlashCommand(
        ("agic",),
        "Switch the session agic",
        _runnable,
        setting_slash_usage("agic"),
        "required",
    ),
    SlashCommand(
        ("flow",),
        "Switch the session flow",
        _runnable,
        setting_slash_usage("flow"),
        "required",
    ),
    SlashCommand(
        ("runnable",),
        "Switch the session runnable",
        _runnable,
        setting_slash_usage("runnable"),
        "required",
    ),
    SlashCommand(
        ("allow",),
        "Set session resource ceilings",
        _setting,
        setting_slash_usage("allow"),
        "required",
    ),
    SlashCommand(
        ("limit",),
        "Set session run limits",
        _setting,
        setting_slash_usage("limit"),
        "required",
    ),
    SlashCommand(("models",), "Find models", _resources, "/models [QUERY]"),
    SlashCommand(("tools",), "Find tools", _resources, "/tools [QUERY]"),
    SlashCommand(("caps",), "Find capabilities", _resources, "/caps [QUERY]"),
    SlashCommand(
        ("queue", "q"),
        "Inspect or edit queued submissions",
        _queue,
        "/queue [ACTION]",
    ),
    SlashCommand(
        ("steer", "s"),
        "Steer the active run",
        _steer,
        "/steer MESSAGE",
        "required",
    ),
    SlashCommand(("show",), "Show a run result", _show, "/show [RUN_ID]"),
    SlashCommand(("keys",), "Show keyboard shortcuts", _keys, "/keys", "none"),
    SlashCommand(("help", "?"), "Show this help", _help, "/help, /?", "none"),
    SlashCommand(("exit", "quit"), "Exit Chat", _exit, "/exit", "none"),
)
_SLASH_BY_NAME = {name: slash for slash in SLASHES for name in slash.names}


def _chat_help_lines() -> list[str]:
    width = max(len(slash.display_usage) for slash in SLASHES)
    return [f"{slash.display_usage:<{width}}  {slash.summary}" for slash in SLASHES]


def _chat_queue_help_lines() -> list[str]:
    return [
        "/queue steer N   Steer the active run with item #N.",
        "/queue edit N    Edit item #N in the input box.",
        "/queue delete N  Delete item #N.",
        "/queue clear     Clear all items.",
    ]


def _candidate_setting(
    app: AppContext,
    update: RunOverride,
    *,
    allowed_model_refs: Collection[str] | None = None,
) -> SessionSetting:
    return app.get_client().apply_setting(
        app.get_setting(),
        update,
        allowed_model_refs=allowed_model_refs,
    )


def _commit_setting(app: AppContext, setting: SessionSetting) -> None:
    app.set_setting(setting)
    app.refresh_status()


def _allow_summary(
    client: ChatClient,
    update: RunOverride,
) -> tuple[str, Collection[str] | None]:
    counts: list[str] = []
    allowed_model_refs: Collection[str] | None = None
    for item in update.allow:
        if item.field == "models":
            models = _items(client.list_models(item.value))
            count = len(models)
            allowed_model_refs = frozenset(
                ref
                for model in models
                if (ref := as_text(model.get("ref"))) is not None
            )
        elif item.field == "tools":
            count = len(_items(client.list_tools(item.value)))
        else:
            count = len(_items(client.list_caps(item.field.rstrip("s"), item.value)))
        counts.append(f"{count} {_plural(count, item.field.rstrip('s'))}")
    return f"Allowed {', '.join(counts)}", allowed_model_refs


def _limit_summary(setting: SessionSetting, update: RunOverride) -> str:
    values = ", ".join(
        f"{item.field}={_setting_value(getattr(setting.limits, item.field))}"
        for item in update.limits
    )
    return f"Limits set to {values}"


def _setting_value(value: object) -> str:
    return "none" if value is None else str(value)


def _model_setting_summary(setting: SessionSetting) -> str:
    model = setting.model
    if model is None:
        return "Model cleared"
    reasoning = model.parameters.reasoning
    if reasoning is None:
        return f"Model set to {model.ref}"
    effort = (
        reasoning.effort
        if reasoning.effort is not None
        else str(reasoning.budget_tokens)
    )
    return f"Model set to {model.ref} · {effort}"


def _model_efforts(item: Mapping[str, Any]) -> str:
    parameters = item.get("parameters")
    if not isinstance(parameters, Mapping):
        return "-"
    reasoning = parameters.get("reasoning")
    if not isinstance(reasoning, Mapping):
        return "-"
    efforts = reasoning.get("effort")
    if not isinstance(efforts, list | tuple):
        return "-"
    values = tuple(value for value in efforts if isinstance(value, str) and value)
    return ", ".join(values) or "-"


def _found(count: int, noun: str) -> str:
    return f"Found {count} {_plural(count, noun)}"


def _plural(count: int, noun: str) -> str:
    return noun if count == 1 else f"{noun}s"


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
    resolved = _chat_resolve_model_command(models_payload, setting.model.ref)
    label = resolved[1] if resolved is not None else setting.model.ref
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
