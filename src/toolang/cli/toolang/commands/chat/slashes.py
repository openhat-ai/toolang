"""Slash commands for terminal chat."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Literal, TypeAlias, cast

import click

from toolang.cli.common.execution_progress.formatting import display_width, wrap_display
from toolang.cli.common.model_selection import materialize_model_selection
from toolang.base.types.model import ModelRequest
from toolang.common.errors import ToolangError
from toolang.execution.policy import parse_setting_override
from toolang.execution.types import ModelOverride, RunOverride, SessionSetting

from .base import AppContext, ChatClient, ChatResult, as_text, friendly_error
from .input import QuickCommand
from .policy import (
    materialize_runnable_list_ref,
    run_override_help_lines,
    validate_model_reasoning_request,
)
from .shortcuts import help_lines as shortcut_help_lines
from .tables import table_lines

SlashOutcomeKind = Literal["success", "result", "usage", "error"]
SlashArgument = Literal["none", "optional", "required"]
SlashCategory = Literal["session", "inspection", "other"]


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
    shrink_order: tuple[int, ...] = ()
    protected_suffixes: tuple[str | None, ...] = ()


@dataclass(frozen=True, slots=True)
class SlashHelpRow:
    """One command entry in the main slash help."""

    command: str
    arguments: str
    description: str
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SlashHelpSection:
    """One titled group in the main slash help."""

    title: str
    rows: tuple[SlashHelpRow, ...]


@dataclass(frozen=True, slots=True)
class SlashHelp:
    """Structured main slash help with shared column widths."""

    sections: tuple[SlashHelpSection, ...]
    footer: str


@dataclass(frozen=True, slots=True)
class SlashRunResult:
    """One durable result returned by `/output`."""

    result: ChatResult


SlashContent: TypeAlias = SlashText | SlashTable | SlashHelp | SlashRunResult


@dataclass(frozen=True, slots=True)
class SlashOutcome:
    """Semantic result of one submitted slash command."""

    kind: SlashOutcomeKind
    content: SlashContent


@dataclass(frozen=True, slots=True)
class SlashCommand:
    name: str
    arguments: str
    description: str
    run: Callable[[AppContext, str, str], SlashOutcome | None]
    aliases: tuple[str, ...] = ()
    category: SlashCategory | None = None
    argument: SlashArgument = "optional"
    focused_help: SlashText | None = None

    @property
    def names(self) -> tuple[str, ...]:
        return (self.name, *self.aliases)

    @property
    def display_usage(self) -> str:
        return f"/{self.name}{f' {self.arguments}' if self.arguments else ''}"


def handle(app: AppContext, quick: QuickCommand) -> SlashOutcome | None:
    """Dispatch one registered, structurally parsed slash command."""

    command = quick.name
    argument = quick.tail or ""
    slash = _SLASH_BY_NAME.get(command)
    if slash is None:
        raise KeyError(f"unregistered slash command: {command}")
    if slash.argument == "required" and not argument:
        return (
            SlashOutcome("usage", slash.focused_help)
            if slash.focused_help is not None
            else _usage(slash)
        )
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


def outcome_lines(
    outcome: SlashOutcome,
    *,
    width: int | None = None,
) -> tuple[str, ...]:
    """Project one outcome to deterministic plain-text lines."""

    content = outcome.content
    if isinstance(content, SlashText):
        lines = (content.summary, *content.details)
        return _wrapped_lines(lines, width=width)
    if isinstance(content, SlashHelp):
        return _help_lines(content, width=width)
    if isinstance(content, SlashTable):
        lines = table_lines(
            content.headers,
            content.rows,
            width=None if width is None else max(1, width - 2),
            shrink_order=content.shrink_order,
            protected_suffixes=content.protected_suffixes,
        )
        return (
            *_wrapped_lines((content.summary,), width=width),
            *(f"  {line}" for line in lines),
        )
    return _wrapped_lines((f"{content.result.run_id} output",), width=width)


def _usage(command: SlashCommand) -> SlashOutcome:
    return SlashOutcome("usage", SlashText(f"Usage: {command.display_usage}"))


def _success(summary: str, *details: str) -> SlashOutcome:
    return SlashOutcome("success", SlashText(summary, tuple(details)))


def _result(summary: str, *details: str) -> SlashOutcome:
    return SlashOutcome("result", SlashText(summary, tuple(details)))


def _help(_app: AppContext, _command: str, _argument: str) -> SlashOutcome:
    sections = tuple(
        SlashHelpSection(
            title,
            tuple(
                SlashHelpRow(
                    f"/{slash.name}",
                    slash.arguments,
                    slash.description,
                    tuple(f"/{alias}" for alias in slash.aliases),
                )
                for slash in SLASHES
                if slash.category == category
            ),
        )
        for category, title in (
            ("session", "Session commands:"),
            ("inspection", "Inspection commands:"),
            ("other", "Other commands:"),
        )
    )
    return SlashOutcome(
        "result",
        SlashHelp(
            sections,
            "To list one-run colon directives, type :?.",
        ),
    )


def run_override_help() -> SlashOutcome:
    """Return the special `:?` help result."""

    return _result(
        "Run overrides change settings for this run only.",
        "Session defaults stay unchanged.",
        "effort=auto inherits model or provider reasoning defaults.",
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
    payload: Mapping[str, Any] | None = None
    if model is None:  # pragma: no cover - parser invariant
        raise RuntimeError("model setting did not contain a model override")
    allowed_model_refs: tuple[str, ...] | None = None
    default_model_ref: str | None = None
    if model.identity == "unset":
        raise ValueError(
            "/model unset is not a session setting; use :model unset for one run"
        )
    if model.identity == "default":
        payload = client.list_models(app.get_setting().allow.models)
        default_model_ref = as_text(payload.get("default"))
        if default_model_ref is None:
            raise ValueError("No models are available for the current allow.models")
        allowed_model_refs = _model_refs(payload)
    elif model.identity is not None:
        payload = client.list_models()
        resolved = _chat_resolve_model_command(payload, model.identity)
        if resolved is None:
            raise ValueError(
                f"Model selection is unknown or ambiguous: {model.identity}"
            )
        update = RunOverride(
            model=ModelOverride(identity=resolved[0], effort=model.effort)
        )
    setting = _candidate_setting(
        app,
        update,
        allowed_model_refs=allowed_model_refs,
        default_model_ref=default_model_ref,
    )
    effort_applicable: bool | None = None
    if setting.model is not None:
        if payload is None:
            payload = client.list_models()
        validate_model_reasoning_request(payload, setting.model)
        effort_applicable = model_effort_applicability(payload, setting.model.ref)
    _commit_setting(app, setting)
    return _success(
        _model_setting_summary(setting, effort_applicable=effort_applicable)
    )


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
        summary, allowed_model_refs, default_model_ref = _allow_summary(
            app.get_client(), update
        )
        setting = _candidate_setting(
            app,
            update,
            allowed_model_refs=allowed_model_refs,
            default_model_ref=default_model_ref,
        )
        details: list[str] = []
        previous_ref = previous.model.ref if previous.model is not None else None
        setting_ref = setting.model.ref if setting.model is not None else None
        if previous_ref != setting_ref:
            if setting_ref is None:
                details.append(
                    f"Model cleared: {previous_ref} is outside allow.models; "
                    "no models available"
                )
            elif previous_ref is None:
                details.append(f"Model selected: {setting_ref}")
            else:
                details.append(
                    f"Model changed: {previous_ref} -> {setting_ref} because "
                    f"{previous_ref} is outside allow.models"
                )
    else:
        setting = _candidate_setting(app, update)
        summary = _limit_summary(setting, update)
        details = []
    _commit_setting(app, setting)
    return _success(summary, *details)


def _resources(app: AppContext, command: str, argument: str) -> SlashOutcome:
    all_available, query = _resource_arguments(argument)
    client = app.get_client()
    if command == "models":
        available, allowed, items = _select_resource_items(
            client.list_models,
            allowed_queries=app.get_setting().allow.models,
            query=query,
            all_available=all_available,
            identity="ref",
        )
        summary = _resource_summary(
            len(items),
            len(available if all_available else allowed),
            "model",
            scope="available" if all_available else "allowed",
            queried=query is not None,
        )
        if not items:
            return _result(summary)
        configured = app.get_setting().model
        default = configured.ref if configured is not None else None
        prices = _model_prices(items)
        allowed_refs = _identity_set(allowed, "ref")
        rows = tuple(
            _with_allowed_column(
                (
                    f"{as_text(item.get('ref')) or '-'}"
                    f"{' *' if as_text(item.get('ref')) == default else ''}",
                    price,
                    _model_efforts(item),
                ),
                allowed=(as_text(item.get("ref")) or "") in allowed_refs,
                enabled=all_available,
            )
            for item, price in zip(items, prices, strict=True)
        )
        headers = _with_allowed_header(
            ("MODEL", "PRICE ($/1M)", "EFFORT"), enabled=all_available
        )
        return SlashOutcome(
            "result",
            SlashTable(
                summary,
                headers,
                rows,
                shrink_order=(3, 0, 2, 1) if all_available else (2, 0, 1),
                protected_suffixes=(" *",) + (None,) * (len(headers) - 1),
            ),
        )
    if command == "tools":
        available, allowed, items = _select_resource_items(
            client.list_tools,
            allowed_queries=app.get_setting().allow.tools,
            query=query,
            all_available=all_available,
            identity="ref",
            normalize=_visible_tools,
        )
        summary = _resource_summary(
            len(items),
            len(available if all_available else allowed),
            "tool",
            scope="available" if all_available else "allowed",
            queried=query is not None,
        )
        if not items:
            return _result(summary)
        allowed_refs = _identity_set(allowed, "ref")
        rows = tuple(
            _with_allowed_column(
                (
                    as_text(item.get("ref")) or "-",
                    as_text(item.get("description")) or "-",
                ),
                allowed=(as_text(item.get("ref")) or "") in allowed_refs,
                enabled=all_available,
            )
            for item in items
        )
        headers = _with_allowed_header(("TOOL", "DESCRIPTION"), enabled=all_available)
        return SlashOutcome(
            "result",
            SlashTable(
                summary,
                headers,
                rows,
                shrink_order=(2, 0, 1) if all_available else (1, 0),
            ),
        )
    available, allowed, items = _select_cap_items(
        app,
        query=query,
        all_available=all_available,
    )
    summary = _resource_summary(
        len(items),
        len(available if all_available else allowed),
        "capability",
        scope="available" if all_available else "allowed",
        queried=query is not None,
    )
    if not items:
        return _result(summary)
    allowed_identities = _identity_set(allowed, "identity")
    rows = tuple(
        _with_allowed_column(
            (
                as_text(item.get("identity")) or "-",
                as_text(item.get("scope")) or "-",
                as_text(item.get("form")) or "-",
                as_text(item.get("summary")) or "-",
            ),
            allowed=(as_text(item.get("identity")) or "") in allowed_identities,
            enabled=all_available,
        )
        for item in items
    )
    headers = _with_allowed_header(
        ("CAP", "SCOPE", "FORM", "DESCRIPTION"), enabled=all_available
    )
    return SlashOutcome(
        "result",
        SlashTable(
            summary,
            headers,
            rows,
            shrink_order=(4, 0, 2, 3, 1) if all_available else (3, 0, 1, 2),
        ),
    )


def _runnables(app: AppContext, command: str, _argument: str) -> SlashOutcome:
    kind = command.removesuffix("s")
    payload = app.get_client().list_runnables(kind)
    items = _items(payload)
    summary = _resource_summary(
        len(items),
        len(items),
        kind,
        scope="available",
        queried=False,
    )
    if not items:
        return _result(summary)
    current = app.get_setting().runnable
    rows = tuple(
        (f"{name}{' *' if current == f'{kind}:{name}' else ''}",)
        for item in items
        for name in (as_text(item.get("name")) or "-",)
    )
    return SlashOutcome(
        "result",
        SlashTable(
            summary,
            (kind.upper(),),
            rows,
            protected_suffixes=(" *",),
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


def _output(app: AppContext, _command: str, argument: str) -> SlashOutcome:
    tokens = argument.split()
    if len(tokens) > 1:
        raise ValueError("/output accepts at most one run id")
    run_id = tokens[0] if tokens else None
    result = app.get_client().get_result(
        run_id,
        thread_id=app.get_thread_id(),
    )
    return SlashOutcome("result", SlashRunResult(result))


_MODEL_HELP = SlashText(
    "/model [MODEL] [effort=VALUE]",
    (
        "",
        "Set the session model or effort",
        "",
        "Examples:",
        "  /model openai/gpt-5 effort=high",
        "  /model openai/gpt-5",
        "  /model effort=high",
    ),
)
_RUNNABLE_HELP = SlashText(
    "/runnable RUNNABLE",
    (
        "/agic     AGIC",
        "/flow     FLOW",
        "",
        "Switch the session runnable",
        "",
        "Examples:",
        "  /runnable flow:review",
        "  /runnable agic:chat",
        "  /runnable default",
        "  /agic chat",
        "  /flow review",
    ),
)
_ALLOW_HELP = SlashText(
    "/allow FIELD=QUERY...",
    (
        "",
        "Set session resource ceilings",
        "",
        "Fields:",
        "  models, tools, psyches, skills, services, prompts",
        "",
        "Examples:",
        "  /allow models=openai/*",
        "  /allow tools=shell/* skills=review*",
        "  /allow models=none",
    ),
)
_LIMIT_HELP = SlashText(
    "/limit FIELD=VALUE...",
    (
        "",
        "Set session run limits",
        "",
        "Fields:",
        "  agic_model_calls, agic_tool_calls, tokens, cost, time",
        "",
        "Examples:",
        "  /limit tokens=2000",
        "  /limit time=120 cost=1.50",
        "  /limit agic_model_calls=100 agic_tool_calls=50",
    ),
)


SLASHES: tuple[SlashCommand, ...] = (
    SlashCommand(
        "model",
        "[MODEL] [effort=VALUE]",
        "Set the session model or effort",
        _model,
        category="session",
        argument="required",
        focused_help=_MODEL_HELP,
    ),
    SlashCommand(
        "runnable",
        "RUNNABLE",
        "Switch the session runnable",
        _runnable,
        category="session",
        argument="required",
        focused_help=_RUNNABLE_HELP,
    ),
    SlashCommand(
        "agic",
        "AGIC",
        "Switch the session agic",
        _runnable,
        category="session",
        argument="required",
        focused_help=_RUNNABLE_HELP,
    ),
    SlashCommand(
        "flow",
        "FLOW",
        "Switch the session flow",
        _runnable,
        category="session",
        argument="required",
        focused_help=_RUNNABLE_HELP,
    ),
    SlashCommand(
        "allow",
        "FIELD=QUERY...",
        "Set session resource ceilings",
        _setting,
        category="session",
        argument="required",
        focused_help=_ALLOW_HELP,
    ),
    SlashCommand(
        "limit",
        "FIELD=VALUE...",
        "Set session run limits",
        _setting,
        category="session",
        argument="required",
        focused_help=_LIMIT_HELP,
    ),
    SlashCommand(
        "models",
        "[-a] [QUERY]",
        "List allowed models (-a: all available)",
        _resources,
        category="inspection",
    ),
    SlashCommand(
        "tools",
        "[-a] [QUERY]",
        "List allowed tools (-a: all available)",
        _resources,
        category="inspection",
    ),
    SlashCommand(
        "caps",
        "[-a] [QUERY]",
        "List allowed capabilities (-a: all available)",
        _resources,
        category="inspection",
    ),
    SlashCommand(
        "agics",
        "",
        "List available agics",
        _runnables,
        category="inspection",
        argument="none",
    ),
    SlashCommand(
        "flows",
        "",
        "List available flows",
        _runnables,
        category="inspection",
        argument="none",
    ),
    SlashCommand(
        "output",
        "[RUN]",
        "Show output from the given or latest run",
        _output,
        aliases=("show",),
        category="inspection",
    ),
    SlashCommand(
        "queue",
        "[ACTION]",
        "Inspect or edit queued submissions",
        _queue,
        aliases=("q",),
    ),
    SlashCommand(
        "steer",
        "MESSAGE",
        "Steer the active run",
        _steer,
        aliases=("s",),
        argument="required",
    ),
    SlashCommand(
        "help",
        "",
        "Show this help",
        _help,
        aliases=("?",),
        category="other",
        argument="none",
    ),
    SlashCommand(
        "keys",
        "",
        "Show keyboard shortcuts",
        _keys,
        category="other",
        argument="none",
    ),
    SlashCommand(
        "exit",
        "",
        "Exit Chat",
        _exit,
        aliases=("quit",),
        category="other",
        argument="none",
    ),
)
_SLASH_BY_NAME = {name: slash for slash in SLASHES for name in slash.names}


def _resource_arguments(argument: str) -> tuple[bool, str | None]:
    tokens = argument.strip().split(maxsplit=1)
    if tokens and tokens[0] == "-a":
        return True, tokens[1].strip() if len(tokens) == 2 else None
    return False, argument.strip() or None


def _select_resource_items(
    fetch: Callable[[Sequence[str] | None], Mapping[str, Any]],
    *,
    allowed_queries: Sequence[str] | None,
    query: str | None,
    all_available: bool,
    identity: str,
    normalize: Callable[[Sequence[Mapping[str, Any]]], Sequence[Mapping[str, Any]]]
    | None = None,
) -> tuple[
    tuple[Mapping[str, Any], ...],
    tuple[Mapping[str, Any], ...],
    tuple[Mapping[str, Any], ...],
]:
    normalize_items = normalize or tuple
    available = (
        tuple(normalize_items(_items(fetch(None))))
        if all_available or allowed_queries is None
        else ()
    )
    allowed = (
        available
        if allowed_queries is None
        else tuple(normalize_items(_items(fetch(allowed_queries))))
    )
    base = available if all_available else allowed
    if query is None:
        return available, allowed, base
    queried = tuple(normalize_items(_items(fetch((query,)))))
    queried_identities = _identity_set(queried, identity)
    return (
        available,
        allowed,
        tuple(
            item
            for item in base
            if (as_text(item.get(identity)) or "") in queried_identities
        ),
    )


def _select_cap_items(
    app: AppContext,
    *,
    query: str | None,
    all_available: bool,
) -> tuple[
    tuple[Mapping[str, Any], ...],
    tuple[Mapping[str, Any], ...],
    tuple[Mapping[str, Any], ...],
]:
    client = app.get_client()
    available = tuple(_items(client.list_caps()))
    available_by_kind = {
        kind: tuple(item for item in available if as_text(item.get("kind")) == kind)
        for kind in ("psyche", "skill", "service", "prompt")
    }
    allowed_items: list[Mapping[str, Any]] = []
    ceiling = app.get_setting().allow
    for field, kind in (
        ("psyches", "psyche"),
        ("skills", "skill"),
        ("services", "service"),
        ("prompts", "prompt"),
    ):
        queries = getattr(ceiling, field)
        if queries is None:
            allowed_items.extend(available_by_kind[kind])
        elif queries:
            allowed_items.extend(_items(client.list_caps(kind, queries)))
    allowed = tuple(allowed_items)
    base = available if all_available else allowed
    if query is None:
        return available, allowed, base
    queried_identities = _identity_set(
        _items(client.list_caps(None, (query,))), "identity"
    )
    return (
        available,
        allowed,
        tuple(
            item
            for item in base
            if (as_text(item.get("identity")) or "") in queried_identities
        ),
    )


def _identity_set(
    items: Sequence[Mapping[str, Any]],
    field: str,
) -> frozenset[str]:
    return frozenset(
        identity for item in items if (identity := as_text(item.get(field))) is not None
    )


def _visible_tools(
    items: Sequence[Mapping[str, Any]],
) -> tuple[Mapping[str, Any], ...]:
    return tuple(
        item
        for item in items
        if not (as_text(item.get("toolset")) or "").startswith("_")
    )


def _with_allowed_header(
    headers: tuple[str, ...],
    *,
    enabled: bool,
) -> tuple[str, ...]:
    return (headers[0], "ALLOWED", *headers[1:]) if enabled else headers


def _with_allowed_column(
    row: tuple[str, ...],
    *,
    allowed: bool,
    enabled: bool,
) -> tuple[str, ...]:
    return (row[0], "yes" if allowed else "no", *row[1:]) if enabled else row


def _resource_summary(
    count: int,
    total: int,
    noun: str,
    *,
    scope: Literal["allowed", "available"],
    queried: bool,
) -> str:
    subject = _plural(count, noun)
    if queried:
        return f"{count} {subject} matched out of {total} {scope}."
    return f"{count} {subject} {scope}."


def _help_lines(help_content: SlashHelp, *, width: int | None) -> tuple[str, ...]:
    rows = tuple(row for section in help_content.sections for row in section.rows)
    command_width = max(display_width(row.command) for row in rows)
    argument_width = max(display_width(row.arguments) for row in rows)
    lines: list[str] = []
    for section_index, section in enumerate(help_content.sections):
        if section_index:
            lines.append("")
        lines.extend((section.title, ""))
        for row in section.rows:
            aliases = f" (alias: {', '.join(row.aliases)})" if row.aliases else ""
            lines.extend(
                _help_row_lines(
                    row,
                    aliases=aliases,
                    command_width=command_width,
                    argument_width=argument_width,
                    width=width,
                )
            )
    lines.extend(("", help_content.footer))
    return tuple(lines[:-1]) + _wrapped_lines((lines[-1],), width=width)


def _help_row_lines(
    row: SlashHelpRow,
    *,
    aliases: str,
    command_width: int,
    argument_width: int,
    width: int | None,
) -> tuple[str, ...]:
    prefix = f"  {row.command:<{command_width}}  {row.arguments:<{argument_width}}  "
    description = f"{row.description}{aliases}"
    if width is None:
        return (f"{prefix}{description}",)
    prefix_width = display_width(prefix)
    if prefix_width < width:
        wrapped = wrap_display(description, width - prefix_width)
        return (
            f"{prefix}{wrapped[0]}",
            *(f"{' ' * prefix_width}{line}" for line in wrapped[1:]),
        )
    command = f"  {row.command}"
    if row.arguments:
        command = f"{command}  {row.arguments}"
    description_indent = "    " if width > 4 else ""
    return (
        *_wrapped_lines((command,), width=width),
        *(
            f"{description_indent}{line}"
            for line in wrap_display(
                description,
                max(1, width - display_width(description_indent)),
            )
        ),
    )


def _wrapped_lines(
    lines: Sequence[str],
    *,
    width: int | None,
) -> tuple[str, ...]:
    if width is None:
        return tuple(lines)
    return tuple(
        wrapped for line in lines for wrapped in wrap_display(line, max(1, width))
    )


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
    allowed_model_refs: Sequence[str] | None = None,
    default_model_ref: str | None = None,
) -> SessionSetting:
    return app.get_client().apply_setting(
        app.get_setting(),
        update,
        allowed_model_refs=allowed_model_refs,
        default_model_ref=default_model_ref,
    )


def _commit_setting(app: AppContext, setting: SessionSetting) -> None:
    app.set_setting(setting)
    app.refresh_status()


def _allow_summary(
    client: ChatClient,
    update: RunOverride,
) -> tuple[str, tuple[str, ...] | None, str | None]:
    counts: list[str] = []
    allowed_model_refs: tuple[str, ...] | None = None
    default_model_ref: str | None = None
    for item in update.allow:
        if item.field == "models":
            payload = client.list_models(item.value)
            models = _items(payload)
            count = len(models)
            allowed_model_refs = _model_refs(payload)
            default_model_ref = as_text(payload.get("default"))
        elif item.field == "tools":
            count = len(_items(client.list_tools(item.value)))
        else:
            count = len(_items(client.list_caps(item.field.rstrip("s"), item.value)))
        counts.append(f"{count} {_plural(count, item.field.rstrip('s'))}")
    return (
        f"Allowed {', '.join(counts)}",
        allowed_model_refs,
        default_model_ref,
    )


def _model_refs(payload: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(
        ref
        for model in _items(payload)
        if (ref := as_text(model.get("ref"))) is not None
    )


def _limit_summary(setting: SessionSetting, update: RunOverride) -> str:
    values = ", ".join(
        f"{item.field}={_setting_value(getattr(setting.limits, item.field))}"
        for item in update.limits
    )
    return f"Limits set to {values}"


def _setting_value(value: object) -> str:
    return "none" if value is None else str(value)


def _model_setting_summary(
    setting: SessionSetting,
    *,
    effort_applicable: bool | None = None,
) -> str:
    model = setting.model
    if model is None:
        return "Model cleared"
    return (
        f"Model set to {model_status_label(model, effort_applicable=effort_applicable)}"
    )


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


def _model_prices(items: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    components = tuple(_model_price_components(item) for item in items)
    input_width = max(6, *(len(input_value) for input_value, _ in components))
    output_width = max(6, *(len(output_value) for _, output_value in components))
    return tuple(
        f"{input_value.rjust(input_width)} / {output_value.rjust(output_width)}"
        for input_value, output_value in components
    )


def _model_price_components(item: Mapping[str, Any]) -> tuple[str, str]:
    price = item.get("price")
    if not isinstance(price, Mapping):
        return "-", "-"
    return _price_component(price.get("input")), _price_component(price.get("output"))


def _price_component(value: object) -> str:
    if value is None or isinstance(value, bool):
        return "-"
    try:
        price = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return "-"
    if not price.is_finite():
        return "-"
    number = f"{price:.2f}"
    return f"${number.rjust(5)}"


def _plural(count: int, noun: str) -> str:
    if count == 1:
        return noun
    if noun.endswith("y"):
        return f"{noun[:-1]}ies"
    return f"{noun}s"


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
    applicable = (
        model_effort_applicability(models_payload, setting.model.ref)
        if setting.model is not None
        else None
    )
    return model_status_label(setting.model, effort_applicable=applicable)


def model_status_label(
    model: ModelRequest | None,
    *,
    effort_applicable: bool | None = None,
) -> str:
    """Return the canonical compact model status segment."""

    if model is None:
        return "[no models available]"
    value = model_reasoning_value(model)
    if value is not None:
        return f"{model.ref} · {value}"
    return f"{model.ref} · auto" if effort_applicable is True else model.ref


def model_effort_applicability(
    models_payload: Mapping[str, Any],
    ref: str,
) -> bool | None:
    """Return effort applicability for one exact model ref in a list payload."""

    item = next(
        (item for item in _items(models_payload) if as_text(item.get("ref")) == ref),
        None,
    )
    return model_effort_applicable(item) if item is not None else None


def model_reasoning_value(model: ModelRequest) -> str | None:
    """Return one explicit effort level or token budget for display."""

    reasoning = model.parameters.reasoning
    if reasoning is None:
        return None
    if reasoning.effort is not None:
        return reasoning.effort
    return str(reasoning.budget_tokens) if reasoning.budget_tokens is not None else None


def model_effort_applicable(item: Mapping[str, Any]) -> bool | None:
    """Read validated effort applicability from one model list item."""

    parameters = item.get("parameters")
    if not isinstance(parameters, Mapping):
        return None
    reasoning = parameters.get("reasoning")
    if not isinstance(reasoning, Mapping):
        return None
    applicable = reasoning.get("applicable")
    return applicable if isinstance(applicable, bool) else None


def _items(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        return []
    return [
        cast(Mapping[str, Any], item) for item in raw_items if isinstance(item, Mapping)
    ]


def _model_label(item: Mapping[str, Any]) -> str:
    return as_text(item.get("name")) or as_text(item.get("ref")) or "runtime model"
