"""Standalone slash and colon commands for terminal chat."""

from __future__ import annotations

import math
import posixpath
import shlex
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Literal, TypeAlias, cast

from typer._click.exceptions import ClickException

from toolang.cli.common.execution_progress.formatting import (
    display_width,
    truncate,
    wrap_display,
)
from toolang.cli.common.model_selection import materialize_model_selection
from toolang.cli.common.model_formatting import model_reasoning_value
from toolang.base.types.model import ModelRequest
from toolang.common.errors import ToolangError
from toolang.execution.input import (
    RUN_OVERRIDE_NAMES,
    SLASH_COMMAND_NAMES,
    parse_setting_override,
)
from toolang.execution.types import RunOverride, SessionSetting

from .base import AppContext, ChatClient, ChatResult, as_text, friendly_error
from .input import ChatCommand
from .policy import (
    materialize_runnable_list_ref,
    validate_model_reasoning_request,
)
from .shortcuts import HELP_GROUPS
from .tables import table_lines


CommandOutcomeKind = Literal["success", "result", "usage", "error"]
CommandArgument = Literal["none", "optional", "required"]
CommandCategory = Literal["session", "inspection", "other"]
HELP_DESCRIPTION_MIN_WIDTH = 24


@dataclass(frozen=True, slots=True)
class CommandText:
    """One concise command outcome with optional supporting lines."""

    summary: str
    details: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CommandTable:
    """One presentation-neutral command table."""

    summary: str
    headers: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    shrink_order: tuple[int, ...] = ()
    protected_suffixes: tuple[str | None, ...] = ()


@dataclass(frozen=True, slots=True)
class CommandHelpRow:
    """One command or shortcut entry in structured help."""

    command: str
    arguments: str
    description: str
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CommandHelpSection:
    """One titled group in the main command help."""

    title: str
    rows: tuple[CommandHelpRow, ...]


@dataclass(frozen=True, slots=True)
class CommandHelp:
    """Structured help with shared column widths."""

    sections: tuple[CommandHelpSection, ...]
    footer: str
    intro: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CommandRunResult:
    """One durable result returned by `/output`."""

    result: ChatResult


CommandContent: TypeAlias = CommandText | CommandTable | CommandHelp | CommandRunResult


@dataclass(frozen=True, slots=True)
class CommandOutcome:
    """Semantic result of one submitted standalone command."""

    kind: CommandOutcomeKind
    content: CommandContent


@dataclass(frozen=True, slots=True)
class CommandSpec:
    name: str
    arguments: str
    description: str
    run: Callable[[AppContext, str, str], CommandOutcome | None]
    prefix: Literal["/", ":"] = "/"
    aliases: tuple[str, ...] = ()
    category: CommandCategory | None = None
    argument: CommandArgument = "optional"
    focused_help: CommandText | None = None

    @property
    def names(self) -> tuple[str, ...]:
        return (self.name, *self.aliases)

    @property
    def display_usage(self) -> str:
        return (
            f"{self.prefix}{self.name}{f' {self.arguments}' if self.arguments else ''}"
        )


def handle(app: AppContext, submission: ChatCommand) -> CommandOutcome | None:
    """Dispatch one registered, structurally parsed command."""

    command = submission.name
    argument = submission.tail or ""
    spec = _COMMAND_BY_KEY.get((submission.prefix, command))
    if spec is None:
        raise KeyError(f"unregistered command: {submission.prefix}{command}")
    if spec.argument == "required" and not argument:
        return (
            CommandOutcome("usage", spec.focused_help)
            if spec.focused_help is not None
            else _usage(spec)
        )
    if spec.argument == "none" and argument:
        return error_outcome(
            f"{submission.prefix}{command} does not accept an argument"
        )

    try:
        return spec.run(app, command, argument)
    except (ToolangError, ValueError) as exc:
        return error_outcome(str(exc))
    except ClickException as exc:
        return error_outcome(exc.message)


def is_registered(command: str, *, prefix: str = "/") -> bool:
    """Return whether one command name belongs to the selected namespace."""

    return (prefix, command) in _COMMAND_BY_KEY


def unrecognized_diagnostic(command: str, *, prefix: str = "/") -> str:
    """Return actionable status text for an undispatchable command."""

    help_command = ":?" if prefix == ":" else "/help"
    if not command:
        return f"Enter a command after {prefix} · See {help_command} for help"
    return f"Unknown command {prefix}{command} · See {help_command} for help"


def error_outcome(message: str) -> CommandOutcome:
    """Build one user-visible command error."""

    return CommandOutcome("error", CommandText(f"Error: {friendly_error(message)}"))


def outcome_lines(
    outcome: CommandOutcome,
    *,
    width: int | None = None,
) -> tuple[str, ...]:
    """Project one outcome to deterministic plain-text lines."""

    content = outcome.content
    if isinstance(content, CommandText):
        lines = (content.summary, *content.details)
        return _wrapped_lines(lines, width=width)
    if isinstance(content, CommandHelp):
        return _help_lines(content, width=width)
    if isinstance(content, CommandTable):
        indent = "  " if width is None or width >= 4 else ""
        lines = table_lines(
            content.headers,
            content.rows,
            width=(None if width is None else max(1, width - display_width(indent))),
            shrink_order=content.shrink_order,
            protected_suffixes=content.protected_suffixes,
        )
        if not content.rows:
            return _wrapped_lines((content.summary,), width=width)
        return (
            *_wrapped_lines((content.summary,), width=width),
            "",
            *(_bounded_line(f"{indent}{line}", width=width) for line in lines),
        )
    return _wrapped_lines((f"{content.result.run_id} output",), width=width)


def _usage(command: CommandSpec) -> CommandOutcome:
    return CommandOutcome("usage", CommandText(f"Usage: {command.display_usage}"))


def _success(summary: str, *details: str) -> CommandOutcome:
    return CommandOutcome("success", CommandText(summary, tuple(details)))


def _result(summary: str, *details: str) -> CommandOutcome:
    return CommandOutcome("result", CommandText(summary, tuple(details)))


def _command_help_section(category: CommandCategory, title: str) -> CommandHelpSection:
    return CommandHelpSection(
        title,
        tuple(
            CommandHelpRow(
                f"{spec.prefix}{spec.name}",
                spec.arguments,
                spec.description,
                tuple(f"{spec.prefix}{alias}" for alias in spec.aliases),
            )
            for spec in COMMANDS
            if spec.category == category
        ),
    )


def _help(_app: AppContext, _command: str, _argument: str) -> CommandOutcome:
    sections = (
        _command_help_section("session", "Session settings (colon commands)"),
        _command_help_section("inspection", "Inspection (slash commands)"),
        _command_help_section("other", "Other slash commands"),
    )
    directives = run_directive_help().content
    assert isinstance(directives, CommandHelp)
    return CommandOutcome(
        "result",
        CommandHelp(
            (*sections, *directives.sections),
            "-a lists all available resources.\n"
            "/output without RUN shows the latest successful output in this thread.\n"
            ":? shows session settings.\n\n" + directives.footer,
            intro=directives.intro,
        ),
    )


def _session_help(_app: AppContext, _command: str, _argument: str) -> CommandOutcome:
    return CommandOutcome(
        "result",
        CommandHelp(
            (_command_help_section("session", "Session settings (colon commands)"),),
            "Session settings apply to later runs. /help shows commands and run directives.",
        ),
    )


def run_directive_help() -> CommandOutcome:
    """Describe run directives and their input boundaries for the main help."""

    rows = (
        CommandHelpRow(
            "/model", "[MODEL] [PARAM=VALUE...]", "Override model or parameters"
        ),
        CommandHelpRow("/allow", "FIELD=QUERY...", "Narrow allowed resources"),
        CommandHelpRow("/limit", "FIELD=VALUE...", "Override run limits"),
        CommandHelpRow("/workdir", "PATH", "Override workdir for this run"),
        CommandHelpRow("/RUNNABLE", "[NAME=VALUE...]", "Call a root runnable"),
        CommandHelpRow("/agic:NAME", "[NAME=VALUE...]", "Call a kind-qualified agic"),
        CommandHelpRow("/flow:NAME", "[NAME=VALUE...]", "Call a kind-qualified flow"),
        CommandHelpRow(
            "$PROMPT", "[NAME=VALUE...]", "Expand a prompt (dollar directive)"
        ),
        CommandHelpRow("@RESOURCE", "", "Include a resource (at directive)"),
    )
    return CommandOutcome(
        "result",
        CommandHelp(
            (CommandHelpSection("Run directives", rows),),
            "\n".join(
                (
                    "Slash directives",
                    "  Put /model, /allow, /limit, and /workdir before input or a root call.",
                    "  An override prefix requires primary or named input.",
                    "  Direct /NAME calls may omit input when the signature permits it.",
                    "  Built-in names win; use /agic:NAME or /flow:NAME for collisions.",
                    "  /runnable NAME, /agic NAME, and /flow NAME are not selector forms.",
                    "",
                    "Input",
                    "  Named arguments precede input; quotes protect spaces in values.",
                    "  -- TEXT captures nonempty line input; a root call permits no later content.",
                    "  - ends the header and captures the remaining stream, including empty input.",
                    "  A root header followed by a body also captures the remaining stream.",
                    "  --- fenced input is only for $PROMPT calls; close it with an exact --- line.",
                    "  Following content is outside the prompt, within the same root input.",
                    "",
                    "Model and policy",
                    "  effort=LEVEL|TOKENS|auto; max_output=POSITIVE_INT|auto",
                    "  auto clears a parameter; default restores the surface model.",
                    "  unset removes the model binding for this run.",
                    "  Selecting a model clears unspecified parameter overrides.",
                    "  max_output caps one model call; /limit tokens caps the run.",
                    "  allow: models, tools, psyches, skills, services, prompts",
                    "  limit: agic_model_calls, agic_tool_calls, tokens, cost, time",
                    "  allow accepts queries, all, or none; limit accepts none to disable.",
                    "  time is in seconds.",
                    "",
                    "Example",
                    "  /model effort=high max_output=8192",
                    "  /limit tokens=12000",
                    "  /review focus=security -- Review this change.",
                )
            ),
            intro=(
                "Commands occupy a whole submission; run directives describe one run.",
                "Colon commands set session defaults; slash overrides apply only to this run.",
            ),
        ),
    )


def _keys(_app: AppContext, _command: str, _argument: str) -> CommandOutcome:
    sections = tuple(
        CommandHelpSection(
            title,
            tuple(
                CommandHelpRow(shortcut.help_label, "", shortcut.summary)
                for shortcut in group
            ),
        )
        for title, group in HELP_GROUPS
    )
    return CommandOutcome("result", CommandHelp(sections, footer=""))


def _exit(app: AppContext, _command: str, _argument: str) -> None:
    app.request_exit()
    return None


def _model(app: AppContext, _command: str, argument: str) -> CommandOutcome:
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
            ":model unset is not a session setting; use /model unset for one run"
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
        update = RunOverride(model=replace(model, identity=resolved[0]))
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


def _runnable(app: AppContext, command: str, argument: str) -> CommandOutcome:
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
    if setting.runnable_follows_default:
        return _success(f"Runnable follows agent default: {setting.runnable or 'none'}")
    return _success(f"Runnable set to {setting.runnable or 'none'}")


def _workdir(app: AppContext, command: str, argument: str) -> CommandOutcome:
    """Set or display the current Chat-session workdir."""
    del command
    setting = app.get_setting()
    try:
        values = shlex.split(argument, comments=False, posix=True)
    except ValueError as exc:
        raise ValueError(str(exc)) from exc
    if not values:
        current = setting.workdir or "thread/default"
        return _success(f"Workdir: {current}")
    if len(values) != 1:
        raise ValueError(":workdir requires one path")
    path = values[0]
    if path in {"default", "unset"}:
        updated = replace(setting, workdir=None, workdir_base=None)
        _commit_setting(app, updated)
        return _success("Workdir reset to thread/default")
    if not path or path.startswith(":"):
        raise ValueError(":workdir requires one valid path")
    if "://" in path or path.startswith("/"):
        workdir, base = path, None
    else:
        current = setting.workdir
        if current is not None and "://" not in current and not current.startswith("/"):
            workdir = posixpath.normpath(posixpath.join(current, path))
            base = setting.workdir_base
        else:
            workdir = path
            base = current or setting.workdir_base
    updated = replace(setting, workdir=workdir, workdir_base=base)
    _commit_setting(app, updated)
    return _success(f"Workdir set to {workdir}")


def _setting(app: AppContext, command: str, argument: str) -> CommandOutcome:
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
                details.append("Model cleared: no models available.")
            elif previous_ref is None:
                details.append(f"Model selected: {setting_ref}")
            else:
                details.append(f"Model changed: {previous_ref} -> {setting_ref}")
                details.append("  Previous model is outside allow.models.")
    else:
        setting = _candidate_setting(app, update)
        summary = _limit_summary(setting, update)
        details = []
    _commit_setting(app, setting)
    return _success(summary, *("", *details) if details else ())


def _resources(app: AppContext, command: str, argument: str) -> CommandOutcome:
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
        headers = _with_allowed_header(
            ("MODEL", "PRICE ($/1M)", "EFFORT"), enabled=all_available
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
        return CommandOutcome(
            "result",
            CommandTable(
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
        headers = _with_allowed_header(("TOOL", "DESCRIPTION"), enabled=all_available)
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
        return CommandOutcome(
            "result",
            CommandTable(
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
    headers = _with_allowed_header(
        ("CAP", "SCOPE", "FORM", "DESCRIPTION"), enabled=all_available
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
    return CommandOutcome(
        "result",
        CommandTable(
            summary,
            headers,
            rows,
            shrink_order=(4, 0, 2, 3, 1) if all_available else (3, 0, 1, 2),
        ),
    )


def _runnables(app: AppContext, command: str, _argument: str) -> CommandOutcome:
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
        (f"{label}{' *' if current == f'{kind}:{name}' else ''}",)
        for item in items
        for name in (as_text(item.get("name")) or "-",)
        for label in (
            f"/{kind}:{name}"
            if name in SLASH_COMMAND_NAMES | RUN_OVERRIDE_NAMES
            else name,
        )
    )
    return CommandOutcome(
        "result",
        CommandTable(
            summary,
            (kind.upper(),),
            rows,
            protected_suffixes=(" *",),
        ),
    )


def _output(app: AppContext, _command: str, argument: str) -> CommandOutcome:
    tokens = argument.split()
    if len(tokens) > 1:
        raise ValueError("/output accepts at most one run id")
    run_id = tokens[0] if tokens else None
    result = app.get_client().get_result(
        run_id,
        thread_id=app.get_thread_id(),
    )
    return CommandOutcome("result", CommandRunResult(result))


_MODEL_HELP = CommandText(
    ":model [MODEL] [PARAM=VALUE...]",
    (
        "Set the session model or its parameters.",
        "",
        "Parameters:",
        "  effort=LEVEL|TOKENS|auto",
        "  max_output=POSITIVE_INT|auto",
        "",
        "default selects the session's allowed default model.",
        "auto clears the specified parameter override.",
        "Selecting a model clears unspecified parameter overrides.",
        "",
        "Examples:",
        "  :model openai/gpt-5 effort=high",
        "  :model max_output=8192",
    ),
)
_RUNNABLE_HELP = CommandText(
    ":runnable RUNNABLE",
    (
        "Set the session runnable.",
        "",
        "Shortcuts:",
        "  :agic NAME = :runnable agic:NAME",
        "  :flow NAME = :runnable flow:NAME",
        "",
        "default follows the agent default (only :runnable).",
        "",
        "Examples:",
        "  :flow review",
        "  :runnable default",
    ),
)
_ALLOW_HELP = CommandText(
    ":allow FIELD=QUERY...",
    (
        "Set session resource ceilings.",
        "",
        "Fields:",
        "  models, tools, psyches, skills, services, prompts",
        "",
        "Values:",
        "  QUERY, all, none",
        "",
        "Examples:",
        "  :allow models=openai/*",
        "  :allow tools=shell/* skills=review*",
    ),
)
_LIMIT_HELP = CommandText(
    ":limit FIELD=VALUE...",
    (
        "Set session run limits.",
        "",
        "Fields:",
        "  agic_model_calls, agic_tool_calls, tokens, cost, time",
        "",
        "time is in seconds; none disables a limit.",
        "",
        "Examples:",
        "  :limit tokens=20000",
        "  :limit time=120 cost=1.50",
    ),
)


COMMANDS: tuple[CommandSpec, ...] = (
    CommandSpec(
        "?", "", "Show session settings", _session_help, prefix=":", argument="none"
    ),
    CommandSpec(
        "model",
        "[MODEL] [PARAM=VALUE...]",
        "Set model or parameters",
        _model,
        prefix=":",
        category="session",
        argument="required",
        focused_help=_MODEL_HELP,
    ),
    CommandSpec(
        "runnable",
        "RUNNABLE",
        "Set runnable (shortcuts: :agic, :flow)",
        _runnable,
        prefix=":",
        category="session",
        argument="required",
        focused_help=_RUNNABLE_HELP,
    ),
    CommandSpec(
        "agic",
        "AGIC",
        "Switch the session agic",
        _runnable,
        prefix=":",
        category="session",
        argument="required",
        focused_help=_RUNNABLE_HELP,
    ),
    CommandSpec(
        "flow",
        "FLOW",
        "Switch the session flow",
        _runnable,
        prefix=":",
        category="session",
        argument="required",
        focused_help=_RUNNABLE_HELP,
    ),
    CommandSpec(
        "allow",
        "FIELD=QUERY...",
        "Set resource ceilings",
        _setting,
        prefix=":",
        category="session",
        argument="required",
        focused_help=_ALLOW_HELP,
    ),
    CommandSpec(
        "workdir",
        "[PATH]",
        "Show or set workdir",
        _workdir,
        prefix=":",
        category="session",
    ),
    CommandSpec(
        "limit",
        "FIELD=VALUE...",
        "Set run limits",
        _setting,
        prefix=":",
        category="session",
        argument="required",
        focused_help=_LIMIT_HELP,
    ),
    CommandSpec(
        "models",
        "[-a] [QUERY]",
        "List allowed models",
        _resources,
        category="inspection",
    ),
    CommandSpec(
        "tools",
        "[-a] [QUERY]",
        "List allowed tools",
        _resources,
        category="inspection",
    ),
    CommandSpec(
        "caps",
        "[-a] [QUERY]",
        "List allowed capabilities",
        _resources,
        category="inspection",
    ),
    CommandSpec(
        "agics",
        "",
        "List available agics",
        _runnables,
        category="inspection",
        argument="none",
    ),
    CommandSpec(
        "flows",
        "",
        "List available flows",
        _runnables,
        category="inspection",
        argument="none",
    ),
    CommandSpec(
        "output",
        "[RUN]",
        "Show saved output",
        _output,
        aliases=("show",),
        category="inspection",
    ),
    CommandSpec(
        "help",
        "",
        "Show this help",
        _help,
        aliases=("?",),
        category="other",
        argument="none",
    ),
    CommandSpec(
        "exit",
        "",
        "Exit Chat",
        _exit,
        aliases=("quit",),
        category="other",
        argument="none",
    ),
    CommandSpec(
        "keys",
        "",
        "Show keyboard shortcuts",
        _keys,
        category="other",
        argument="none",
    ),
)
_COMMAND_BY_KEY = {
    (spec.prefix, name): spec for spec in COMMANDS for name in spec.names
}


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


def _help_lines(help_content: CommandHelp, *, width: int | None) -> tuple[str, ...]:
    usage_width = max(
        display_width(f"{row.command} {row.arguments}".rstrip())
        for section in help_content.sections
        for row in section.rows
    )
    lines: list[str] = list(_wrapped_lines(help_content.intro, width=width))
    for section in help_content.sections:
        if lines:
            lines.append("")
        lines.extend(_wrapped_lines((section.title,), width=width))
        for row in section.rows:
            usage = f"{row.command} {row.arguments}".rstrip()
            aliases = f" (alias: {', '.join(row.aliases)})" if row.aliases else ""
            description = f"{row.description}{aliases}"
            prefix = f"  {usage}{' ' * (usage_width - display_width(usage) + 2)}"
            if width is None:
                lines.append(f"{prefix}{description}")
            elif display_width(prefix) + HELP_DESCRIPTION_MIN_WIDTH <= width:
                wrapped = wrap_display(description, width - display_width(prefix))
                lines.extend(
                    (
                        f"{prefix}{wrapped[0]}",
                        *(
                            f"{' ' * display_width(prefix)}{line}"
                            for line in wrapped[1:]
                        ),
                    )
                )
            else:
                if display_width(f"  {usage}") <= width:
                    lines.append(f"  {usage}")
                else:
                    lines.extend(_wrapped_lines((f"  {row.command}",), width=width))
                    if row.arguments:
                        lines.extend(
                            _wrapped_lines((f"    {row.arguments}",), width=width)
                        )
                lines.extend(_wrapped_lines((f"    {description}",), width=width))
    if help_content.footer:
        lines.append("")
        lines.extend(_wrapped_lines(help_content.footer.splitlines(), width=width))
    return tuple(lines)


def _wrapped_lines(
    lines: Sequence[str],
    *,
    width: int | None,
) -> tuple[str, ...]:
    if width is None:
        return tuple(lines)
    result: list[str] = []
    for line in lines:
        if not line.strip():
            result.append("")
            continue
        content = line.lstrip(" ")
        indent_width = min(len(line) - len(content), max(0, width - 1))
        indent = " " * indent_width
        result.extend(
            indent + part
            for part in wrap_display(content, max(1, width - indent_width))
        )
    return tuple(result)


def _bounded_line(line: str, *, width: int | None) -> str:
    return line if width is None else truncate(line, max(1, width))


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
    parts = [model.ref]
    effort = model_reasoning_value(model)
    if effort is not None or effort_applicable is True:
        parts.append(f"effort={effort or 'auto'}")
    if model.max_output is not None:
        parts.append(f"max_output={model.max_output}")
    return f"Model set to {' · '.join(parts)}"


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
    input_width = max((6, *(len(input_value) for input_value, _ in components)))
    output_width = max((6, *(len(output_value) for _, output_value in components)))
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
        price = float(str(value))
    except ValueError:
        return "-"
    if not math.isfinite(price):
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
