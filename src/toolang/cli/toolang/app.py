"""Typer CLI for Toolang agent management."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from importlib.metadata import version as package_version
import json
from pathlib import Path
import os
import shutil as shutil
import subprocess
import sys
import threading
import time
from typing import Annotated, Any, Literal, TYPE_CHECKING, cast
from urllib.parse import urlencode
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

import click
from prompt_toolkit.key_binding import KeyBindings as KeyBindings
from prompt_toolkit.styles import Style as Style
import typer
from typer import rich_utils
from typer.core import TyperCommand, TyperGroup

from ... import agents
from ...caps import split_cap_selectors
from ...base.types.message import message_summary
from ...config.log import (
    LoggingPlan,
    configure_logging,
    configure_logging_plan,
    resolve_agent_logging,
)
from ...execution.db import ExecutionStore, execution_db_path
from ...execution.detail import (
    run_detail_from_record,
    thread_info_from_record,
    thread_info_from_runs,
)
from ...execution.records import RunStatus
from ...execution.events import TraceEvent, trace_event_from_data
from ...models.resolution import split_model_selectors
from ...tools.registry import split_tool_selectors
from ..utils import (
    _PrefixAgentWorkGroup,
    _RequiredPrefixAgentCommand,
    _RunAgentCommand,
    _RuntimeAgentCommand,
    _StartAgentCommand,
    _agent_avatar,
    _append_agent_update,
    _context_agent,
    _context_root,
    _created_time,
    _echo_pairs_table,
    _echo_table,
    _normalize_component_option,
    _parse_utc_timestamp,
    _required_prefix_agent,
    _required_runtime_agent,
    _runtime_environ_for_agent,
    _runtime_value,
    _toolang_root,
    _ui_base_url,
    _wait_for_started_status,
    _wrap_user_error,
)
from .caps import CAP_KINDS, register_caps_commands
from .chat import slashes as chat_slashes
from .chat.base import friendly_error as chat_friendly_error
from .chat.history import ChatInputHistoryStore
from .chat.tui import ChatTuiApp
from . import inspect as _inspect_cli
from . import version as _version
from .fmt import register_fmt_command
from .parse import register_parse_command

if TYPE_CHECKING:
    from ... import caps as cap_store
    from ... import templates
    from ... import up as agent_up
    from ...execution.records import UpdateKind
    from ...state.prepared import PreparedLock, PreparedState
    from .. import invoke as cli_invoke
    from ..progress import CliProgress


class _LazyModule:
    """Import a module only when one of its attributes is used."""

    def __init__(self, module_name: str) -> None:
        self._module_name = module_name
        self._module: Any | None = None

    def _load(self) -> Any:
        if self._module is None:
            import importlib

            self._module = importlib.import_module(self._module_name)
        return self._module

    def __getattr__(self, name: str) -> Any:
        return getattr(self._load(), name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name.startswith("_"):
            object.__setattr__(self, name, value)
            return
        setattr(self._load(), name, value)

    def __delattr__(self, name: str) -> None:
        if name.startswith("_"):
            object.__delattr__(self, name)
            return
        delattr(self._load(), name)


if not TYPE_CHECKING:
    agent_up = _LazyModule("toolang.up")
    cli_invoke = _LazyModule("toolang.cli.invoke")
    cap_store = _LazyModule("toolang.caps")
    templates = _LazyModule("toolang.templates")

WorkKind = Literal["task", "chore"]
_CLI_PREFIX_AGENT: str | None = None
AGENT_COMMAND_PANEL = "Agent Commands"
THREAD_COMMAND_PANEL = "Thread Commands"
RUNTIME_COMMAND_PANEL = "Runtime Commands"
CAPS_COMMAND_PANEL = "Cap Commands"
TOP_LEVEL_COMMANDS = frozenset(
    {
        "new",
        "clone",
        "remove",
        "list",
        "info",
        "hidden",
        "fmt",
        "parse",
        "model",
        "tool",
        "channel",
        "sandbox",
        "chat",
        "send",
        "attach",
        "threads",
        "runs",
        "inspect",
        "steer",
        "cancel",
        "rewind",
        "fork",
        "run",
        "start",
        "stop",
        "caps",
        *CAP_KINDS,
        "task",
        "chore",
    }
)

_CAPS_PANEL_COMMAND_ORDER = ("psyche", "skill", "service", "prompt", "caps")
_AGENT_PANEL_COMMAND_ORDER = (
    "new",
    "clone",
    "remove",
    "list",
    "info",
    "run",
    "start",
    "stop",
    "chore",
    "task",
)
_THREAD_PANEL_COMMAND_ORDER = (
    "chat",
    "cancel",
    "steer",
    "rewind",
    "fork",
    "inspect",
    "runs",
    "threads",
)
_RUNTIME_PANEL_COMMAND_ORDER = ("model", "tool", "channel", "sandbox")
_THREAD_TARGET_COMMANDS = frozenset({"steer", "cancel", "rewind", "fork"})
_HIDDEN_ALIAS_COMMANDS = frozenset({"send", "attach"})


class _ToolangGroup(TyperGroup):
    def list_commands(self, ctx: click.Context) -> list[str]:
        names = TyperGroup.list_commands(self, ctx)
        agent_names = [name for name in _AGENT_PANEL_COMMAND_ORDER if name in names]
        if agent_names:
            first_agent_index = min(names.index(name) for name in agent_names)
            reordered = [name for name in names if name not in agent_names]
            names = (
                reordered[:first_agent_index]
                + agent_names
                + reordered[first_agent_index:]
            )
        thread_names = [name for name in _THREAD_PANEL_COMMAND_ORDER if name in names]
        ordered_thread_group_names = [*thread_names]
        if ordered_thread_group_names:
            reordered = [
                name for name in names if name not in ordered_thread_group_names
            ]
            runtime_indexes = [
                reordered.index(name)
                for name in _RUNTIME_PANEL_COMMAND_ORDER
                if name in reordered
            ]
            insertion_index = (
                min(runtime_indexes) if runtime_indexes else len(reordered)
            )
            names = (
                reordered[:insertion_index]
                + ordered_thread_group_names
                + reordered[insertion_index:]
            )
        cap_names = [name for name in _CAPS_PANEL_COMMAND_ORDER if name in names]
        if len(cap_names) < 2:
            return names
        first_cap_index = min(names.index(name) for name in cap_names)
        reordered = [name for name in names if name not in cap_names]
        return reordered[:first_cap_index] + cap_names + reordered[first_cap_index:]


@dataclass(frozen=True, slots=True)
class _RuntimeStartup:
    """Resolved and prepared inputs for one runtime launch."""

    target: agents.MaterializedRunTarget
    startup: agent_up.StartupSpec
    environ: dict[str, str]
    log_plan: LoggingPlan
    prepared_state: PreparedState


@dataclass(frozen=True, slots=True)
class _RoamingFileRuntimeOptions:
    """Parsed foreground runtime options for a local script inbox."""

    inboxes: tuple[Path, ...]
    models: tuple[str, ...]
    tools: tuple[str, ...] | None
    caps: tuple[str, ...]
    components: tuple[str, ...]
    host: str
    endpoint_host: str | None
    port: int | None
    sandbox: str
    dev: Path | None


POSTFIX_AGENT_COMMANDS = frozenset(
    {
        "run",
        "start",
        "stop",
        "info",
        "chat",
        "send",
        "attach",
        "threads",
        "runs",
        "inspect",
        "steer",
        "cancel",
        "rewind",
        "fork",
    }
)
ROAMING_THREAD_COMMANDS = frozenset(
    {"threads", "runs", "inspect", "steer", "cancel", "rewind", "fork"}
)
PREFIX_AGENT_COMMANDS = frozenset(
    {
        "run",
        "start",
        "stop",
        "caps",
        "chat",
        "send",
        "attach",
        "threads",
        "runs",
        "inspect",
        "steer",
        "cancel",
        "rewind",
        "fork",
        *CAP_KINDS,
        "task",
        "chore",
    }
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _human_uptime_since(timestamp_text: str) -> str | None:
    started = _parse_utc_timestamp(timestamp_text)
    if started is None:
        return None
    delta = _utc_now() - started
    total_seconds = max(int(delta.total_seconds()), 0)
    import humanize

    return f"up {humanize.naturaldelta(total_seconds)}"


def _toolang_version() -> str:
    return f"{_base_toolang_version()}{_source_state_suffix()}"


def _base_toolang_version() -> str:
    original = _version.package_version
    _version.package_version = package_version
    try:
        return _version.base_toolang_version()
    finally:
        _version.package_version = original


def _source_state_suffix() -> str:
    return _version.source_state_suffix()


def _git_output(source_root: Path, *args: str) -> str | None:
    return _version.git_output(source_root, *args)


def _source_tree_root() -> Path | None:
    return _version.source_tree_root()


def _version_callback(value: bool) -> None:
    if not value:
        return
    typer.echo(f"toolang {_toolang_version()}")
    raise typer.Exit()


app = typer.Typer(
    cls=_ToolangGroup,
    help="Run and manage Toolang agents.",
    add_completion=False,
    invoke_without_command=True,
    no_args_is_help=True,
    pretty_exceptions_enable=False,
    pretty_exceptions_show_locals=False,
)


@app.callback()
def callback(
    ctx: typer.Context,
    toolang_root: Annotated[
        Path | None,
        typer.Option("--root", "-r", help="Use a custom Toolang root."),
    ] = None,
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            "-V",
            callback=_version_callback,
            help="Show current version and exit.",
            is_eager=True,
        ),
    ] = False,
) -> None:
    """Toolang CLI."""

    del version
    try:
        configure_logging(spec=None, environ=os.environ)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    ctx.obj = {
        "toolang_root": _toolang_root(toolang_root),
        "agent": _CLI_PREFIX_AGENT,
    }


@app.command("hidden", help="Show hidden commands.", hidden=True)
def hidden_commands(ctx: typer.Context) -> None:
    console = rich_utils._get_rich_console()
    console.print(
        rich_utils.Padding(rich_utils.highlighter(ctx.get_usage()), 1),
        style=rich_utils.STYLE_USAGE_COMMAND,
    )
    group = typer.main.get_command(app)
    if not isinstance(group, click.Group):
        typer.echo("No hidden commands.")
        return
    hidden_commands = [
        command
        for name, command in group.commands.items()
        if command.hidden and name not in {"hidden", *_HIDDEN_ALIAS_COMMANDS}
    ]
    alias_commands = [
        command
        for name, command in group.commands.items()
        if command.hidden and name in _HIDDEN_ALIAS_COMMANDS
    ]
    if not hidden_commands and not alias_commands:
        typer.echo("No hidden commands.")
        return
    command_name = ctx.command_path.split()[0] if ctx.command_path else "toolang"
    console.print(
        rich_utils.Padding(
            (
                "Show commands hidden from the main help.\n\n"
                f"Run with: {command_name} COMMAND [OPTIONS]"
            ),
            (0, 1, 1, 1),
        )
    )
    _print_hidden_command_panel(console, "Advanced Commands", hidden_commands)
    _print_hidden_command_panel(console, "Alias Commands", alias_commands)


def _print_hidden_command_panel(
    console: Any, name: str, commands: list[click.Command]
) -> None:
    if not commands:
        return
    rich_utils._print_commands_panel(
        name=name,
        commands=commands,
        markup_mode="rich",
        console=console,
        cmd_len=max(len(command.name or "") for command in commands),
    )


@app.command(
    "new",
    help="Create an agent.",
    no_args_is_help=True,
    rich_help_panel=AGENT_COMMAND_PANEL,
)
def new_agent(
    ctx: typer.Context,
    agent: Annotated[str, typer.Argument(help="Agent name")],
    template: Annotated[
        str,
        typer.Option("--template", "-t", help="Template name."),
    ] = "default",
) -> None:
    try:
        program_path = agents.create_agent(
            _context_root(ctx),
            agent,
            template_name=template,
        )
    except FileExistsError as exc:
        raise click.ClickException(f"Agent {agent} already exists") from exc
    _append_agent_update(
        _context_root(ctx),
        agent,
        "created",
        {"path": str(program_path)},
    )
    typer.echo(f"Created agent {agent}: {program_path}")


@app.command(
    "clone",
    help="Clone an agent.",
    no_args_is_help=True,
    rich_help_panel=AGENT_COMMAND_PANEL,
)
def clone_agent(
    ctx: typer.Context,
    source: Annotated[str, typer.Argument(help="Agent source selector.")],
    target: Annotated[str | None, typer.Argument(help="New local agent name.")] = None,
) -> None:
    try:
        program_path = agents.clone_agent(_context_root(ctx), source, target)
    except FileExistsError as exc:
        target_name = target or Path(source).stem
        raise click.ClickException(f"Agent {target_name} already exists") from exc
    except FileNotFoundError as exc:
        raise click.ClickException(f"Agent {source} not found") from exc
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    target_name = program_path.parent.name
    _append_agent_update(
        _context_root(ctx),
        target_name,
        "created",
        {"path": str(program_path), "source": source},
    )
    typer.echo(f"Cloned agent {target_name}: {program_path}")


@app.command(
    "remove",
    help="Remove an agent.",
    no_args_is_help=True,
    rich_help_panel=AGENT_COMMAND_PANEL,
)
def remove_agent(
    ctx: typer.Context,
    agent: Annotated[str, typer.Argument(help="Agent name")],
) -> None:
    root = _context_root(ctx)
    try:
        agents.remove_agent(root, agent)
    except FileNotFoundError as exc:
        raise click.ClickException(f"Agent {agent} not found") from exc
    except ValueError as exc:
        status = agents.get_agent_status(root, agent, ui_base_url=_ui_base_url())
        if status is not None and status.status in {"running", "preparing", "starting"}:
            raise click.ClickException(_active_run_error(status)) from exc
        raise click.ClickException(f"Agent {agent} already running") from exc
    typer.echo(f"Removed agent {agent}")


@app.command(
    "list", help="Show agents and their status.", rich_help_panel=AGENT_COMMAND_PANEL
)
def list_agents(ctx: typer.Context) -> None:
    items = agents.list_agent_statuses(
        _context_root(ctx),
        ui_base_url=_ui_base_url(),
    )
    if not items:
        typer.echo("No agents found.")
        return
    rows = [
        (
            item.name,
            item.status,
            item.sandbox if item.status == "running" and item.sandbox else "-",
            str(item.port) if item.port is not None else "-",
            item.webui_url or "-",
        )
        for item in items
    ]
    _echo_table(("AGENT", "STATUS", "SANDBOX", "PORT", "WEBUI"), rows)


@app.command(
    "info",
    help="Show agent info.",
    no_args_is_help=True,
    cls=_RuntimeAgentCommand,
    rich_help_panel=AGENT_COMMAND_PANEL,
)
def info_agent(
    ctx: typer.Context,
    agent: str | None = typer.Argument(None, help="Agent name", hidden=True),
) -> None:
    agent_name = _required_runtime_agent(ctx, agent)
    root = _context_root(ctx)
    status = _wrap_user_error(
        agents.get_agent_status,
        root,
        agent_name,
        ui_base_url=_ui_base_url(),
    )
    if status is None:
        raise click.ClickException(f"Agent {agent_name} not found")
    runtime_state = agents.load_runtime_state(root, agent_name) or {}
    created_at = _created_time(agents.agent_home(root, agent_name))
    started_at = _runtime_value(runtime_state.get("started_at"))
    updated_at = _runtime_value(runtime_state.get("updated_at"))
    status_value = status.status
    if status.status == "running" and started_at != "-":
        online = _human_uptime_since(started_at)
        if online is not None:
            status_value = f"{status.status} ({online})"
    rows = [
        ("Home", str(agents.agent_home(root, agent_name))),
        ("Caps", _info_caps_summary(root, agent_name)),
        ("Jobs", _info_jobs_summary(root, agent_name)),
        ("Tools", _info_tools_summary(root, agent_name)),
        (
            "Models",
            _info_models_summary(
                root,
                agent_name,
                runtime_state=runtime_state,
                running=status.status != "stopped",
            ),
        ),
        ("Status", status_value),
    ]
    if status.status == "stopped":
        rows.append(("Created", created_at))
        _echo_pairs_table(rows, avatar=_agent_avatar(), title=agent_name.upper())
        return
    if status.sandbox:
        rows.append(("Sandbox", status.sandbox))
    message = _runtime_value(runtime_state.get("message"))
    pid_text = agents.runtime_pid_label(runtime_state)
    if pid_text is not None and status.status != "stopped":
        rows.append(("PID", pid_text))
    if status.endpoint:
        rows.append(("API", status.endpoint))
    if status.webui_url:
        rows.append(("WebUI", status.webui_url))
    if status.status == "running" and started_at != "-":
        rows.append(("Started", started_at))
    if status.status != "running" and updated_at != "-":
        rows.append(("Updated", updated_at))
    if status.status != "running" and message != "-":
        rows.append(("Message", message))
    _echo_pairs_table(rows, avatar=_agent_avatar(), title=agent_name.upper())


@app.command(
    "chat",
    help="Open a terminal chat session.",
    cls=_RequiredPrefixAgentCommand,
    rich_help_panel=THREAD_COMMAND_PANEL,
)
def chat_command(
    ctx: typer.Context,
    thread: Annotated[
        str | None,
        typer.Argument(
            help="Thread id to continue. Run id also accepted. Omit to start a new one.",
            metavar="THREAD",
        ),
    ] = None,
    models: Annotated[
        list[str] | None,
        typer.Option(
            "--models",
            help="Limit available models. Pass CSV or repeat.",
        ),
    ] = None,
    tools: Annotated[
        list[str] | None,
        typer.Option(
            "--tools",
            help="Allow selected tools. Pass CSV or repeat.",
        ),
    ] = None,
    caps: Annotated[
        list[str] | None,
        typer.Option(
            "--caps",
            help="Allow selected caps. Pass CSV or repeat.",
        ),
    ] = None,
    thunk: Annotated[
        str | None, typer.Option("--thunk", help="Use a thunk for new runs.")
    ] = None,
    flow: Annotated[
        str | None, typer.Option("--flow", help="Use a flow for new runs.")
    ] = None,
) -> None:
    thread_id = _target_thread_id(ctx, thread) if thread is not None else None
    selectors = _chat_selector_payload(
        models=models, tools=tools, caps=caps, thunk=thunk, flow=flow
    )
    _chat_interactive(ctx, thread_id=thread_id, selector_payload=selectors)


@app.command(
    "send",
    help="Send one message to a thread.",
    hidden=True,
    cls=_RequiredPrefixAgentCommand,
)
def send_command(
    ctx: typer.Context,
    thread: Annotated[str, typer.Argument(help="Thread id.")],
    message: Annotated[str, typer.Argument(help="Message text.")],
    model: Annotated[
        str | None, typer.Option("--model", help="Model selector.")
    ] = None,
) -> None:
    target = _target_thread_id(ctx, thread)
    payload: dict[str, Any] = {
        "thread": target,
        "client": "tui",
        "message": _message_payload(message),
    }
    if model is not None:
        payload["model"] = model
    _runtime_stream(ctx, "/api/v1/chat/stream", payload=payload)


@app.command(
    "attach",
    help="Open chat on a thread.",
    hidden=True,
    cls=_RequiredPrefixAgentCommand,
)
def attach_command(
    ctx: typer.Context,
    thread: Annotated[str, typer.Argument(help="Thread id.")],
) -> None:
    _open_thread_ui(ctx, _target_thread_id(ctx, thread))


@app.command(
    "threads",
    help="List threads.",
    cls=_RequiredPrefixAgentCommand,
    rich_help_panel=THREAD_COMMAND_PANEL,
)
def threads_command(
    ctx: typer.Context,
    origin: Annotated[
        str | None, typer.Option("--origin", help="Filter by origin.")
    ] = None,
    channel: Annotated[
        str | None, typer.Option("--channel", help="Filter by channel.")
    ] = None,
    status: Annotated[
        str | None, typer.Option("--status", help="Filter by thread status.")
    ] = None,
) -> None:
    query = _query_params(origin=origin, channel=channel, status=status)
    path = "/api/v1/threads" if not query else f"/api/v1/threads?{query}"
    result = _runtime_json_or_offline(
        ctx,
        path,
        lambda: _offline_threads_json(
            ctx, origin=origin, channel=channel, status=status
        ),
    )
    rows = [
        (
            str(item.get("id", "")),
            _truncate_table_text(item.get("title"), width=48),
            str(item.get("run_count", "")),
            str(item.get("status", "")),
            str(item.get("updated_at", "")),
        )
        for item in result.get("items", [])
        if isinstance(item, dict)
    ]
    _echo_table(("THREAD", "TITLE", "RUNS", "STATUS", "UPDATED"), rows)


@app.command(
    "runs",
    help="List runs.",
    cls=_RequiredPrefixAgentCommand,
    rich_help_panel=THREAD_COMMAND_PANEL,
)
def runs_command(
    ctx: typer.Context,
    thread: Annotated[
        str | None, typer.Option("--thread", help="Filter by thread id.")
    ] = None,
    status: Annotated[
        str | None, typer.Option("--status", help="Filter by run status.")
    ] = None,
) -> None:
    query: list[tuple[str, str]] = []
    if thread is not None:
        query.append(("thread_id", thread))
    if status is not None:
        query.append(("status", _api_run_status(status)))
    path = "/api/v1/runs" if not query else f"/api/v1/runs?{urlencode(query)}"
    result = _runtime_json_or_offline(
        ctx,
        path,
        lambda: _offline_runs_json(ctx, thread=thread, status=status),
    )
    if thread is not None:
        rows = [
            (
                str(item.get("id", "")),
                _truncate_table_text(
                    item.get("summary") or item.get("input_text"), width=48
                ),
                _display_run_status(item.get("status")),
                str(item.get("created_at", "")),
            )
            for item in result.get("items", [])
            if isinstance(item, dict)
        ]
        _echo_table(("RUN", "TITLE", "STATUS", "CREATED"), rows)
    else:
        rows = [
            (
                str(item.get("thread_id", "")),
                str(item.get("id", "")),
                _truncate_table_text(
                    item.get("summary") or item.get("input_text"), width=48
                ),
                _display_run_status(item.get("status")),
                str(item.get("created_at", "")),
            )
            for item in result.get("items", [])
            if isinstance(item, dict)
        ]
        _echo_table(("THREAD", "RUN", "TITLE", "STATUS", "CREATED"), rows)


@app.command(
    "inspect",
    help="Inspect a thread or run.",
    no_args_is_help=True,
    cls=_RequiredPrefixAgentCommand,
    rich_help_panel=THREAD_COMMAND_PANEL,
)
def inspect_command(
    ctx: typer.Context,
    target: Annotated[
        str, typer.Argument(help="Thread id, run id, or run step path to inspect.")
    ],
    limit: Annotated[
        int, typer.Option("--limit", help="Maximum thread runs to read.")
    ] = 100,
    json_view: Annotated[
        bool, typer.Option("--json", help="Render preprocessed inspect data as JSON.")
    ] = False,
) -> None:
    _inspect_cli.inspect_command(ctx, target, limit=limit, json_view=json_view)


@app.command(
    "steer",
    help="Steer an active run.",
    no_args_is_help=True,
    cls=_RequiredPrefixAgentCommand,
    rich_help_panel=THREAD_COMMAND_PANEL,
)
def steer_command(
    ctx: typer.Context,
    run: str = typer.Argument(
        ..., help="Run id to steer. Thread id means its active run."
    ),
    message: str = typer.Argument(..., help="Instruction to steer the run."),
) -> None:
    run_id = _target_run_id(ctx, run)
    _runtime_post(
        ctx,
        f"/api/v1/runs/{run_id}/steer",
        payload={"message": _message_payload(message)},
    )
    typer.echo(f"steered {run_id}")


@app.command(
    "cancel",
    help="Cancel an active run.",
    no_args_is_help=True,
    cls=_RequiredPrefixAgentCommand,
    rich_help_panel=THREAD_COMMAND_PANEL,
)
def cancel_command(
    ctx: typer.Context,
    run: str = typer.Argument(
        ..., help="Run id to cancel. Thread id means its active run."
    ),
) -> None:
    run_id = _target_run_id(ctx, run)
    _runtime_post(ctx, f"/api/v1/runs/{run_id}/cancel", payload={})
    typer.echo(f"canceled {run_id}")


@app.command(
    "rewind",
    help="Rewind a thread to an earlier point.",
    no_args_is_help=True,
    cls=_RequiredPrefixAgentCommand,
    rich_help_panel=THREAD_COMMAND_PANEL,
)
def rewind_command(
    ctx: typer.Context,
    point: str = typer.Argument(
        ...,
        help="Run id to rewind before. Thread id means rewind before its latest run.",
    ),
    chat: Annotated[
        bool, typer.Option("--chat", help="Open chat on the rewound thread.")
    ] = False,
) -> None:
    run_id = _target_latest_run_id(ctx, point)
    result = _runtime_post(ctx, f"/api/v1/runs/{run_id}/rewind", payload={})
    typer.echo(f"rewound {result.get('thread_id')} before {run_id}")
    if chat:
        thread = result.get("thread_id")
        _open_thread_ui(
            ctx,
            str(thread) if isinstance(thread, str) else _target_thread_id(ctx, point),
        )


@app.command(
    "fork",
    help="Fork a thread from a branch point.",
    no_args_is_help=True,
    cls=_RequiredPrefixAgentCommand,
    rich_help_panel=THREAD_COMMAND_PANEL,
)
def fork_command(
    ctx: typer.Context,
    point: str = typer.Argument(
        ...,
        help="Run id to fork before. Thread id means fork after its latest run.",
    ),
    chat: Annotated[
        bool, typer.Option("--chat", help="Open chat on the forked thread.")
    ] = False,
) -> None:
    run_id, include_anchor = _fork_anchor_run(ctx, point)
    payload: dict[str, object] = {}
    if include_anchor:
        payload["include_anchor"] = True
    result = _runtime_post(ctx, f"/api/v1/runs/{run_id}/fork", payload=payload)
    boundary = "through" if include_anchor else "before"
    typer.echo(f"forked {result.get('thread_id')} {boundary} {run_id}")
    if chat:
        thread = result.get("thread_id")
        if isinstance(thread, str):
            _open_thread_ui(ctx, thread)


@app.command(
    "run",
    help="Run an agent in the foreground.",
    no_args_is_help=True,
    cls=_RunAgentCommand,
    rich_help_panel=AGENT_COMMAND_PANEL,
)
def run_agent(
    ctx: typer.Context,
    agent: str | None = typer.Argument(
        None,
        help="Existing local agent name, remote agent ref, or URL.",
        hidden=True,
    ),
    sandbox: Annotated[
        str,
        typer.Option(help="Run the agent in a sandbox."),
    ] = "none",
    models: Annotated[
        list[str] | None,
        typer.Option(
            "--models",
            help="Limit available models. Pass CSV or repeat.",
        ),
    ] = None,
    tools: Annotated[
        list[str] | None,
        typer.Option(
            "--tools",
            help="Allow selected tools. Pass CSV or repeat.",
        ),
    ] = None,
    caps: Annotated[
        list[str] | None,
        typer.Option(
            "--caps",
            help="Allow selected caps. Pass CSV or repeat.",
        ),
    ] = None,
    host: Annotated[
        str, typer.Option(help="Bind the agent API to this host.")
    ] = "127.0.0.1",
    port: Annotated[
        int | None, typer.Option(help="Bind the agent API to this port.")
    ] = None,
    components: Annotated[
        list[str] | None,
        typer.Option("--enable", help="Enable runtime components. Pass CSV or repeat."),
    ] = None,
    inboxes: Annotated[
        list[Path] | None,
        typer.Option(
            "--inbox",
            help="Watch an inbox directory for file requests. Repeat to watch more than one.",
        ),
    ] = None,
    dev: Annotated[
        Path | None,
        typer.Option(
            "--dev",
            help="Use wheels from this file or directory when starting a sandbox.",
        ),
    ] = None,
    endpoint_host: Annotated[
        str | None,
        typer.Option("--endpoint-host", help="Endpoint host name.", hidden=True),
    ] = None,
    sandbox_child: Annotated[
        bool,
        typer.Option("--sandbox-child", hidden=True),
    ] = False,
) -> None:
    from ..progress import as_progress_sink, make_cli_progress

    selector = _required_runtime_agent(ctx, agent)
    normalized_components = _normalize_component_option(components)
    root = _context_root(ctx)
    progress = make_cli_progress()
    progress_finished = False
    try:
        with agents.resolved_run_target(
            root, selector, progress=as_progress_sink(progress)
        ) as target:
            launch = _resolve_runtime_startup(
                ctx,
                target,
                sandbox=sandbox,
                models=models,
                tools=tools,
                caps=caps,
                components=normalized_components,
                inboxes=inboxes,
                port=port,
                host=host,
                endpoint_host=endpoint_host,
                dev=dev,
                background=False,
                progress=progress,
            )
            progress.finish(details=False)
            progress_finished = True
            raise typer.Exit(
                _wrap_user_error(
                    agent_up.start_runtime,
                    launch.startup,
                    environ=launch.environ,
                    sandbox_child=sandbox_child,
                    progress=None,
                    prepared_state=launch.prepared_state,
                )
            )
    except KeyboardInterrupt:
        if not progress_finished:
            progress.interrupt()
        raise typer.Exit(130) from None
    except (
        FileExistsError,
        FileNotFoundError,
        ValueError,
        click.ClickException,
    ) as exc:
        if not progress_finished:
            progress.finish(details=False)
        if isinstance(exc, click.ClickException):
            raise
        raise click.ClickException(str(exc)) from exc


def _active_run_error(status: agents.AgentStatus) -> str:
    message = f"Agent {status.name} already {status.status}"
    detail = (
        (status.webui_url or status.api_url) if status.status == "running" else None
    )
    if detail:
        return f"{message}: {detail}"
    return message


def _truncate_table_text(value: object, *, width: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= width:
        return text
    if width <= 3:
        return text[:width]
    return f"{text[: width - 3].rstrip()}..."


def _runtime_base_url(ctx: typer.Context) -> str:
    agent_name = _required_prefix_agent(
        ctx, command_name=str(ctx.info_name or "runtime")
    )
    status = agents.get_agent_status(
        _context_root(ctx), agent_name, ui_base_url=_ui_base_url()
    )
    if status is None or status.status != "running" or status.endpoint is None:
        raise click.ClickException(f"agent is not running: {agent_name}")
    return status.endpoint.rstrip("/")


def _runtime_json(ctx: typer.Context, path: str) -> dict[str, Any]:
    url = f"{_runtime_base_url(ctx)}{path}"
    try:
        with urlopen(url, timeout=30) as response:
            return cast(dict[str, Any], json.loads(response.read().decode("utf-8")))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise click.ClickException(
            f"runtime request failed: {exc.code} {detail}"
        ) from exc
    except URLError as exc:
        raise click.ClickException(f"runtime request failed: {exc.reason}") from exc


def _runtime_json_or_offline(
    ctx: typer.Context,
    path: str,
    offline: Callable[[], dict[str, Any] | None],
) -> dict[str, Any]:
    try:
        return _runtime_json(ctx, path)
    except click.ClickException as exc:
        result = offline()
        if result is None:
            raise exc
        return result


def _runtime_post(
    ctx: typer.Context, path: str, *, payload: dict[str, Any]
) -> dict[str, Any]:
    url = f"{_runtime_base_url(ctx)}{path}"
    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=60) as response:
            return cast(dict[str, Any], json.loads(response.read().decode("utf-8")))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise click.ClickException(
            f"runtime request failed: {exc.code} {detail}"
        ) from exc
    except URLError as exc:
        raise click.ClickException(f"runtime request failed: {exc.reason}") from exc


def _offline_threads_json(
    ctx: typer.Context,
    *,
    origin: str | None,
    channel: str | None,
    status: str | None,
) -> dict[str, Any] | None:
    store = _open_offline_execution_store(ctx)
    if store is None:
        return {"items": []}
    try:
        runs = store.list_runs(limit=None)
        thread_ids = sorted({run.thread_id for run in runs})
        ordered_runs = [
            run
            for thread_id in thread_ids
            for run in store.list_thread_runs_chronological(thread_id=thread_id)
        ]
        steps_by_run = store.list_steps_for_runs(
            run_ids=tuple(item.run_id for item in ordered_runs)
        )
        commands_by_run = {
            run.run_id: store.list_commands(run_id=run.run_id) for run in ordered_runs
        }
        grouped_runs: dict[str, list[Any]] = {}
        for run in ordered_runs:
            grouped_runs.setdefault(run.thread_id, []).append(run)
        thread_records = {item.thread_id: item for item in store.list_threads()}
        items: list[dict[str, Any]] = []
        for thread_id, thread_runs in grouped_runs.items():
            info = thread_info_from_runs(
                thread_id,
                thread_runs,
                commands_by_run=commands_by_run,
                steps_by_run=steps_by_run,
                thread=thread_records.get(thread_id),
            )
            items.append(asdict(info))
        for thread_id, thread in thread_records.items():
            if thread_id not in grouped_runs:
                items.append(asdict(thread_info_from_record(thread)))
        filtered = [
            item
            for item in items
            if (origin is None or item.get("origin") == origin)
            and (channel is None or item.get("channel") == channel)
            and (status is None or item.get("status") == status)
        ]
        return {
            "items": sorted(
                filtered, key=lambda item: str(item.get("updated_at", "")), reverse=True
            )
        }
    finally:
        store.close()


def _offline_runs_json(
    ctx: typer.Context,
    *,
    thread: str | None,
    status: str | None,
) -> dict[str, Any] | None:
    store = _open_offline_execution_store(ctx)
    if store is None:
        return {"items": []}
    try:
        run_status = _run_status_or_none(status)
        runs = store.list_runs(limit=50, thread_id=thread, status=run_status)
        steps_by_run = store.list_steps_for_runs(
            run_ids=tuple(item.run_id for item in runs)
        )
        commands_by_run = {
            run.run_id: store.list_commands(run_id=run.run_id) for run in runs
        }
        return {
            "items": [
                _offline_run_item(
                    run,
                    inputs=commands_by_run.get(run.run_id, ()),
                    steps=steps_by_run.get(run.run_id, ()),
                )
                for run in runs
            ]
        }
    finally:
        store.close()


def _offline_run_item(run, *, inputs: Sequence, steps: Sequence) -> dict[str, Any]:
    detail = run_detail_from_record(run, inputs=inputs, steps=steps)
    input_text = message_summary(detail.input.parts) if detail.input is not None else ""
    last_step_message = next(
        (
            item.message
            for item in reversed(detail.output.steps)
            if item.message is not None
        ),
        None,
    )
    summary = (
        message_summary(last_step_message.parts)
        if last_step_message is not None
        else input_text
    )
    if run.status == "failed" and run.error and (not summary or summary == input_text):
        summary = run.error
    return {
        "id": run.run_id,
        "origin": run.origin,
        "thread_id": run.thread_id,
        "input_text": input_text,
        "summary": summary,
        "status": run.status,
        "error": run.error,
        "created_at": run.created_at,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "updated_at": run.finished_at or run.started_at,
    }


def _message_summary(message: Mapping[str, Any]) -> str:
    return _parts_summary(message.get("parts"))


def _parts_summary(parts: object) -> str:
    if not isinstance(parts, list):
        return ""
    texts: list[str] = []
    for part in parts:
        if not isinstance(part, Mapping):
            continue
        typed_part = cast(Mapping[str, object], part)
        if typed_part.get("type") == "text":
            texts.append(str(typed_part.get("text") or ""))
    return _truncate_table_text("".join(texts).strip(), width=72)


def _mapping(value: object) -> Mapping[str, Any]:
    return cast(Mapping[str, Any], value) if isinstance(value, Mapping) else {}


def _list(value: object) -> list[Any]:
    return value if isinstance(value, list) else []


def _text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None


def _int_or_none(value: object) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    if value is None:
        return None
    return None


def _open_offline_execution_store(ctx: typer.Context) -> ExecutionStore | None:
    agent_name = _required_prefix_agent(
        ctx, command_name=str(ctx.info_name or "runtime")
    )
    path = execution_db_path(_context_root(ctx), agent_name)
    if not path.exists():
        return None
    return ExecutionStore(path)


def _run_status_or_none(status: str | None) -> RunStatus | None:
    if status is None:
        return None
    normalized = _api_run_status(status)
    if normalized in {"running", "finished", "failed", "canceled"}:
        return cast(RunStatus, normalized)
    raise click.ClickException(f"unknown run status: {status}")


def _runtime_stream(ctx: typer.Context, path: str, *, payload: dict[str, Any]) -> None:
    url = f"{_runtime_base_url(ctx)}{path}"
    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=60) as response:
            for raw_line in response:
                line = raw_line.decode("utf-8", errors="replace").rstrip()
                if line:
                    typer.echo(line)
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise click.ClickException(
            f"runtime request failed: {exc.code} {detail}"
        ) from exc
    except URLError as exc:
        raise click.ClickException(f"runtime request failed: {exc.reason}") from exc


def _runtime_get_stream(ctx: typer.Context, path: str) -> None:
    url = f"{_runtime_base_url(ctx)}{path}"
    try:
        with urlopen(url, timeout=60) as response:
            for raw_line in response:
                line = raw_line.decode("utf-8", errors="replace").rstrip()
                if line:
                    typer.echo(line)
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise click.ClickException(
            f"runtime request failed: {exc.code} {detail}"
        ) from exc
    except URLError as exc:
        raise click.ClickException(f"runtime request failed: {exc.reason}") from exc


def _runtime_consume_stream(
    ctx: typer.Context,
    path: str,
    *,
    payload: dict[str, Any],
    event_handler: Callable[[dict[str, Any]], None] | None = None,
) -> None:
    url = f"{_runtime_base_url(ctx)}{path}"
    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    stop_event = threading.Event()
    try:
        with urlopen(request, timeout=None) as response:
            if event_handler is None:
                for _raw_line in response:
                    pass
                return
            for event in _iter_sse_events(response, stop_event=stop_event):
                event_handler(event)
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise click.ClickException(
            f"runtime request failed: {exc.code} {detail}"
        ) from exc
    except URLError as exc:
        raise click.ClickException(f"runtime request failed: {exc.reason}") from exc


def _stream_result_run(ctx: typer.Context, result: dict[str, Any]) -> None:
    run_id = result.get("run_id")
    if isinstance(run_id, str) and run_id:
        _runtime_get_stream(ctx, f"/api/v1/runs/{run_id}/stream")


def _message_payload(text: str) -> dict[str, object]:
    return {
        "role": "user",
        "parts": [{"type": "text", "text": text}],
    }


def _chat_selector_payload(
    *,
    models: list[str] | None,
    tools: list[str] | None,
    caps: list[str] | None,
    thunk: str | None = None,
    flow: str | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {}
    if thunk is not None and flow is not None:
        raise click.ClickException("--thunk and --flow cannot be used together")
    model_selectors = tuple(dict.fromkeys(split_model_selectors(tuple(models or ()))))
    if model_selectors:
        payload["models"] = list(model_selectors)
    if tools is not None:
        tool_selectors = tuple(dict.fromkeys(split_tool_selectors(tuple(tools))))
        payload["tools"] = list(tool_selectors)
    cap_selectors = tuple(dict.fromkeys(split_cap_selectors(tuple(caps or ()))))
    if cap_selectors:
        payload["caps"] = list(cap_selectors)
    if thunk is not None:
        payload["thunk"] = thunk
    if flow is not None:
        payload["flow"] = flow
    return payload


def _query_params(**items: str | None) -> str:
    return urlencode(
        [(key, value) for key, value in items.items() if value is not None]
    )


def _api_run_status(status: str) -> str:
    return "finished" if status == "succeeded" else status


def _display_run_status(status: object) -> str:
    text = str(status or "")
    return "succeeded" if text == "finished" else text


def _open_thread_ui(
    ctx: typer.Context,
    thread_id: str | None,
    *,
    selector_payload: dict[str, object] | None = None,
) -> None:
    _chat_interactive(ctx, thread_id=thread_id, selector_payload=selector_payload)


def _chat_interactive(
    ctx: typer.Context,
    *,
    thread_id: str | None,
    selector_payload: dict[str, object] | None = None,
) -> None:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        _chat_interactive_scripted(
            ctx, thread_id=thread_id, selector_payload=selector_payload
        )
        return
    _chat_interactive_prompt_toolkit(
        ctx, thread_id=thread_id, selector_payload=selector_payload
    )


def _chat_input_history_store(ctx: typer.Context) -> ChatInputHistoryStore | None:
    try:
        agent = _context_agent(ctx)
        root = _context_root(ctx)
    except (AttributeError, KeyError, TypeError):
        return None
    if not agent:
        return None
    return ChatInputHistoryStore(
        agents.agent_room(root, agent) / "chat-input-history.jsonl"
    )


def _chat_home_label(ctx: typer.Context) -> str:
    try:
        agent_name = _context_agent(ctx)
        if agent_name is None:
            return "agent home"
        return str(agents.agent_home(_context_root(ctx), agent_name))
    except Exception:
        return "agent home"


class _RuntimeChatClient:
    def __init__(self, ctx: typer.Context) -> None:
        self.ctx = ctx

    def list_models(self) -> Mapping[str, Any]:
        return _runtime_json(self.ctx, "/api/v1/chat/models")

    def list_executables(self, kind: str) -> Mapping[str, Any]:
        return _runtime_json(self.ctx, f"/api/v1/chat/{kind}s")

    def create_thread(self) -> str:
        result = _runtime_post(self.ctx, "/api/v1/threads", payload={"client": "tui"})
        thread_id = result.get("thread_id")
        if not isinstance(thread_id, str):
            raise click.ClickException("runtime did not return a thread id")
        return thread_id

    def start_run(
        self,
        thread_id: str,
        message: str,
        selects: Mapping[str, object],
        on_event: Callable[[TraceEvent], None],
        on_error: Callable[[str], None],
    ) -> None:
        try:
            _runtime_consume_stream(
                self.ctx,
                "/api/v1/chat/stream",
                payload={
                    "thread": thread_id,
                    "client": "tui",
                    "request_id": f"term_{uuid4().hex}",
                    "message": _message_payload(message),
                    **selects,
                },
                event_handler=lambda event: on_event(trace_event_from_data(event)),
            )
        except click.ClickException as exc:
            on_error(exc.message)
        except Exception as exc:
            on_error(f"{type(exc).__name__}: {exc}")

    def stop_run(
        self,
        run_id: str,
        on_event: Callable[[TraceEvent], None],
        on_error: Callable[[str], None],
    ) -> None:
        try:
            result = _runtime_post(
                self.ctx,
                f"/api/v1/runs/{run_id}/cancel",
                payload={"request_id": f"req_{uuid4().hex}"},
            )
            if event := _chat_command_trace_event(result.get("input")):
                on_event(event)
        except click.ClickException as exc:
            on_error(exc.message)
        except Exception as exc:
            on_error(f"{type(exc).__name__}: {exc}")

    def steer_run(
        self,
        run_id: str,
        message: str,
        on_event: Callable[[TraceEvent], None],
        on_error: Callable[[str], None],
    ) -> None:
        try:
            result = _runtime_post(
                self.ctx,
                f"/api/v1/runs/{run_id}/steer",
                payload={
                    "request_id": f"req_{uuid4().hex}",
                    "message": _message_payload(message),
                },
            )
            if event := _chat_command_trace_event(result.get("input")):
                on_event(event)
        except click.ClickException as exc:
            on_error(exc.message)
        except Exception as exc:
            on_error(f"{type(exc).__name__}: {exc}")


def _chat_command_trace_event(payload: object) -> TraceEvent | None:
    if not isinstance(payload, Mapping):
        return None
    event_payload = cast(Mapping[str, object], payload)
    event_type = event_payload.get("type")
    if not isinstance(event_type, str):
        return None
    return trace_event_from_data({"type": event_type, "payload": event_payload})


def _chat_interactive_prompt_toolkit(
    ctx: typer.Context,
    *,
    thread_id: str | None,
    selector_payload: dict[str, object] | None = None,
) -> None:
    ChatTuiApp.run(
        thread_id=thread_id,
        selects=dict(selector_payload or {}),
        home=_chat_home_label(ctx),
        input_history=_chat_input_history_store(ctx),
        client=_RuntimeChatClient(ctx),
    )


def _chat_resolve_model_command_labels(
    ctx: typer.Context, selectors: Sequence[str]
) -> tuple[str, ...] | None:
    try:
        payload = _runtime_json(ctx, "/api/v1/chat/models")
    except click.ClickException:
        return None
    return chat_slashes._chat_resolve_model_command_labels(payload, selectors)


def _chat_interactive_scripted(
    ctx: typer.Context,
    *,
    thread_id: str | None,
    selector_payload: dict[str, object] | None = None,
) -> None:
    selectors = dict(selector_payload or {})
    local_streaming = threading.Event()
    local_request_ids: set[str] = set()
    listener: _ThreadEventListener | None = None

    def ensure_thread_id() -> str:
        nonlocal listener, thread_id
        if thread_id is None:
            result = _runtime_post(ctx, "/api/v1/threads", payload={"client": "tui"})
            created = result.get("thread_id")
            if not isinstance(created, str):
                raise click.ClickException("runtime did not return a thread id")
            thread_id = created
            typer.echo(f"thread {thread_id}")
        if listener is None:
            listener = _start_thread_event_listener(
                ctx,
                thread_id,
                local_streaming=local_streaming,
                local_request_ids=local_request_ids,
            )
        return thread_id

    if thread_id is not None:
        typer.echo(f"thread {thread_id}")
        ensure_thread_id()
    try:
        while True:
            try:
                text = input("> ")
            except EOFError:
                return
            except KeyboardInterrupt:
                typer.echo()
                return
            if text.strip() in {"/exit", "/quit"}:
                return
            if not text.strip():
                continue
            if _chat_handle_scripted_command(ctx, text, selectors):
                continue
            active_thread_id = ensure_thread_id()
            request_id = f"term_{uuid4().hex}"
            local_request_ids.add(request_id)
            payload: dict[str, Any] = {
                "thread": active_thread_id,
                "client": "tui",
                "request_id": request_id,
                "message": _message_payload(text),
                **selectors,
            }
            local_streaming.set()
            try:
                _runtime_consume_stream(ctx, "/api/v1/chat/stream", payload=payload)
            finally:
                local_streaming.clear()
                local_request_ids.discard(request_id)
    finally:
        if listener is not None:
            listener.stop()


def _chat_handle_scripted_command(
    ctx: typer.Context, message: str, selector_payload: dict[str, object]
) -> bool:
    parsed = chat_slashes._chat_local_command(message)
    if parsed is None:
        return False
    command, argument = parsed
    if command in {"help", "?"}:
        for line in chat_slashes._chat_help_lines():
            typer.echo(line)
        return True
    if command in {"thunk", "flow"}:
        return _chat_handle_scripted_executable_command(
            ctx, command, argument, selector_payload
        )
    if command not in {"model", "models"}:
        typer.echo(f"Unknown command: /{command}")
        return True
    if argument:
        selectors = chat_slashes._chat_model_command_selectors(argument)
        if not selectors:
            typer.echo("/model requires a selector")
            return True
        labels = _chat_resolve_model_command_labels(ctx, selectors)
        if labels is None:
            typer.echo(f"Model selector matched no models: {', '.join(selectors)}")
            return True
        selector_payload["models"] = list(selectors)
        typer.echo(f"model: {', '.join(labels)}")
        return True
    try:
        payload = _runtime_json(ctx, "/api/v1/chat/models")
    except click.ClickException as exc:
        typer.echo(chat_friendly_error(exc.message))
        return True
    typer.echo("available models")
    for line in chat_slashes._chat_model_list_lines(payload):
        typer.echo(line)
    return True


def _chat_handle_scripted_executable_command(
    ctx: typer.Context,
    command: str,
    argument: str,
    selector_payload: dict[str, object],
) -> bool:
    if argument:
        chat_slashes._chat_set_executable_selector(
            selector_payload, kind=command, name=argument
        )
        typer.echo(f"{command}: {argument}")
        return True
    try:
        payload = _runtime_json(ctx, f"/api/v1/chat/{command}s")
    except click.ClickException as exc:
        typer.echo(chat_friendly_error(exc.message))
        return True
    selected = _text(selector_payload.get(command))
    typer.echo(f"available {command}s")
    for line in chat_slashes._chat_executable_list_lines(payload, selected=selected):
        typer.echo(line)
    return True


class _ThreadEventListener:
    def __init__(self, stop_event: threading.Event) -> None:
        self._stop_event = stop_event

    def stop(self) -> None:
        self._stop_event.set()


def _start_thread_event_listener(
    ctx: typer.Context,
    thread_id: str,
    *,
    local_streaming: threading.Event | None = None,
    local_request_ids: set[str] | None = None,
    redraw_prompt: bool = True,
    event_handler: Callable[[dict[str, Any]], None] | None = None,
) -> _ThreadEventListener:
    stop_event = threading.Event()
    thread = threading.Thread(
        target=_run_thread_event_listener_from_cursor,
        args=(
            ctx,
            thread_id,
            stop_event,
            local_streaming,
            local_request_ids,
            redraw_prompt,
            event_handler,
        ),
        daemon=True,
    )
    thread.start()
    return _ThreadEventListener(stop_event)


def _thread_event_cursor(ctx: typer.Context, thread_id: str) -> int | None:
    detail = _runtime_json(ctx, f"/api/v1/threads/{thread_id}")
    cursor = detail.get("event_cursor")
    if isinstance(cursor, int):
        return cursor
    return None


def _run_thread_event_listener_from_cursor(
    ctx: typer.Context,
    thread_id: str,
    stop_event: threading.Event,
    local_streaming: threading.Event | None,
    local_request_ids: set[str] | None,
    redraw_prompt: bool,
    event_handler: Callable[[dict[str, Any]], None] | None,
) -> None:
    try:
        after = _thread_event_cursor(ctx, thread_id)
    except click.ClickException:
        return
    if stop_event.is_set():
        return
    _run_thread_event_listener(
        ctx,
        thread_id,
        after,
        stop_event,
        local_streaming,
        local_request_ids,
        redraw_prompt,
        event_handler,
    )


def _run_thread_event_listener(
    ctx: typer.Context,
    thread_id: str,
    after: int | None,
    stop_event: threading.Event,
    local_streaming: threading.Event | None,
    local_request_ids: set[str] | None,
    redraw_prompt: bool,
    event_handler: Callable[[dict[str, Any]], None] | None,
) -> None:
    renderer = _ThreadEventRenderer(
        redraw_prompt=redraw_prompt,
        local_streaming=local_streaming,
        local_request_ids=local_request_ids,
    )
    path = f"/api/v1/threads/{thread_id}/stream"
    if after is not None:
        path = f"{path}?{urlencode([('after', str(after))])}"
    url = f"{_runtime_base_url(ctx)}{path}"
    try:
        with urlopen(url, timeout=None) as response:
            for event in _iter_sse_events(response, stop_event=stop_event):
                if stop_event.is_set():
                    return
                if event_handler is not None:
                    event_handler(event)
                else:
                    renderer.render(event)
    except Exception:
        if not stop_event.is_set():
            typer.echo("thread event stream closed", err=True)


def _iter_sse_events(
    response, *, stop_event: threading.Event
) -> Iterator[dict[str, Any]]:
    data_lines: list[str] = []
    for raw_line in response:
        if stop_event.is_set():
            break
        line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
        if line == "":
            if data_lines:
                data = "\n".join(data_lines)
                data_lines = []
                try:
                    event = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict):
                    yield cast(dict[str, Any], event)
            continue
        if line.startswith("data:"):
            data_lines.append(line.removeprefix("data:").strip())


class _ThreadEventRenderer:
    def __init__(
        self,
        *,
        redraw_prompt: bool = False,
        local_streaming: threading.Event | None = None,
        local_request_ids: set[str] | None = None,
    ) -> None:
        self._assistant_open = False
        self._redraw_prompt = redraw_prompt
        self._local_streaming = local_streaming
        self._local_request_ids = local_request_ids
        self._local_run_ids: set[str] = set()
        self._text_delta_runs: set[str] = set()

    def render(self, event: dict[str, Any]) -> None:
        event_type = str(event.get("type") or event.get("event_type") or "")
        payload = event.get("payload")
        if not isinstance(payload, dict):
            return
        if event_type == "run_starting":
            self._render_run_starting(payload)
        elif event_type == "part_delta":
            self._render_part_delta(payload)
        elif event_type == "step_end":
            self._render_step_end(payload)
        elif event_type in {"part_end", "run_end"}:
            self._close_assistant(
                redraw_prompt=event_type == "run_end",
                run_id=str(payload.get("run_id") or "") or None,
            )

    def _render_run_starting(self, payload: dict[str, Any]) -> None:
        self._remember_local_run(payload)
        text = _event_message_text(payload.get("input"))
        if not text:
            return
        self._close_assistant(
            redraw_prompt=False, run_id=str(payload.get("run_id") or "") or None
        )
        typer.echo(f"\nuser: {text}")

    def _render_part_delta(self, payload: dict[str, Any]) -> None:
        delta = payload.get("delta")
        if not isinstance(delta, dict) or delta.get("type") != "text":
            return
        text = str(delta.get("text") or "")
        if not text:
            return
        run_id = payload.get("run_id")
        if isinstance(run_id, str):
            self._text_delta_runs.add(run_id)
        if not self._assistant_open:
            typer.echo("assistant: ", nl=False)
            self._assistant_open = True
        typer.echo(text, nl=False)

    def _render_step_end(self, payload: dict[str, Any]) -> None:
        if payload.get("kind") != "model":
            return
        run_id = payload.get("run_id")
        if isinstance(run_id, str) and run_id in self._text_delta_runs:
            return
        text = _event_parts_text(payload.get("output"))
        if not text:
            return
        if not self._assistant_open:
            typer.echo("assistant: ", nl=False)
            self._assistant_open = True
        typer.echo(text, nl=False)

    def _close_assistant(self, *, redraw_prompt: bool, run_id: str | None) -> None:
        if self._assistant_open:
            typer.echo()
            self._assistant_open = False
        local_run = run_id is not None and run_id in self._local_run_ids
        if (
            redraw_prompt
            and self._redraw_prompt
            and not self._local_run_active(run_id=run_id)
        ):
            typer.echo("> ", nl=False)
        if redraw_prompt and local_run and run_id is not None:
            self._local_run_ids.discard(run_id)

    def _remember_local_run(self, payload: dict[str, Any]) -> None:
        request_id = payload.get("request_id")
        run_id = payload.get("run_id")
        if not isinstance(request_id, str) or not isinstance(run_id, str):
            return
        if (
            self._local_request_ids is not None
            and request_id in self._local_request_ids
        ):
            self._local_run_ids.add(run_id)

    def _local_run_active(self, *, run_id: str | None) -> bool:
        if run_id is not None and run_id in self._local_run_ids:
            return True
        if self._local_streaming is not None and self._local_streaming.is_set():
            return True
        return False


def _event_message_text(message: object) -> str:
    if not isinstance(message, Mapping):
        return ""
    typed_message = cast(Mapping[str, object], message)
    parts = typed_message.get("parts")
    if not isinstance(parts, list):
        return ""
    texts: list[str] = []
    for part in parts:
        if not isinstance(part, Mapping):
            continue
        typed_part = cast(Mapping[str, object], part)
        if typed_part.get("type") == "text":
            texts.append(str(typed_part.get("text") or ""))
    return "".join(texts).strip()


def _event_parts_text(parts: object) -> str:
    if not isinstance(parts, list):
        return ""
    texts: list[str] = []
    for part in parts:
        if not isinstance(part, Mapping):
            continue
        typed_part = cast(Mapping[str, object], part)
        if typed_part.get("type") == "text":
            texts.append(str(typed_part.get("text") or ""))
    return "".join(texts).strip()


def _target_thread_id(ctx: typer.Context, target: str | None) -> str | None:
    if target is None:
        return None
    if target.startswith("run_"):
        try:
            detail = _runtime_json(ctx, f"/api/v1/runs/{target}")
        except click.ClickException:
            thread_id = _offline_thread_id_for_run(ctx, target)
            if thread_id is not None:
                return thread_id
            raise
        info = detail.get("info")
        if isinstance(info, dict) and isinstance(info.get("thread_id"), str):
            return str(info["thread_id"])
        thread_id = detail.get("thread_id")
        if isinstance(thread_id, str):
            return thread_id
        raise click.ClickException(f"run has no thread: {target}")
    return target


def _target_run_id(ctx: typer.Context, target: str) -> str:
    if target.startswith("run_"):
        return target
    try:
        detail = _runtime_json(ctx, f"/api/v1/threads/{target}")
    except click.ClickException:
        run_id = _offline_active_run_id(ctx, target)
        if run_id is not None:
            return run_id
        raise
    info = detail.get("info")
    if not isinstance(info, dict):
        raise click.ClickException(f"thread not found: {target}")
    active = info.get("active_run")
    if not isinstance(active, dict) or not isinstance(active.get("id"), str):
        raise click.ClickException(f"thread has no active run: {target}")
    return str(active["id"])


def _target_latest_run_id(ctx: typer.Context, target: str) -> str:
    if target.startswith("run_"):
        return target
    try:
        detail = _runtime_json(ctx, f"/api/v1/threads/{target}")
    except click.ClickException:
        run_id = _offline_latest_run_id(ctx, target)
        if run_id is not None:
            return run_id
        raise
    info = detail.get("info")
    if not isinstance(info, dict):
        raise click.ClickException(f"thread not found: {target}")
    latest = info.get("latest_run")
    if not isinstance(latest, dict) or not isinstance(latest.get("id"), str):
        raise click.ClickException(f"thread has no runs: {target}")
    return str(latest["id"])


def _fork_anchor_run(ctx: typer.Context, target: str) -> tuple[str, bool]:
    if target.startswith("run_"):
        return target, False
    return _target_latest_run_id(ctx, target), True


def _offline_thread_id_for_run(ctx: typer.Context, run_id: str) -> str | None:
    store = _open_offline_execution_store(ctx)
    if store is None:
        return None
    try:
        run = store.get_run(run_id=run_id)
        return run.thread_id if run is not None else None
    finally:
        store.close()


def _offline_active_run_id(ctx: typer.Context, thread_id: str) -> str | None:
    store = _open_offline_execution_store(ctx)
    if store is None:
        return None
    try:
        runs = store.list_runs(thread_id=thread_id, status="running", limit=1)
        return runs[0].run_id if runs else None
    finally:
        store.close()


def _offline_latest_run_id(ctx: typer.Context, thread_id: str) -> str | None:
    store = _open_offline_execution_store(ctx)
    if store is None:
        return None
    try:
        runs = store.list_runs(thread_id=thread_id, limit=1)
        return runs[0].run_id if runs else None
    finally:
        store.close()


def _resolve_runtime_startup(
    ctx: typer.Context,
    target: agents.MaterializedRunTarget,
    *,
    sandbox: str | None,
    models: list[str] | None,
    tools: list[str] | None,
    caps: list[str] | None,
    components: list[str] | None,
    inboxes: list[Path] | None,
    port: int | None,
    host: str,
    endpoint_host: str | None,
    dev: Path | None,
    background: bool,
    progress: CliProgress | None,
) -> _RuntimeStartup:
    from ..progress import as_progress_sink

    run_root = target.toolang_root
    agent_name = target.agent_name
    if (
        target.kind == "resident"
        and not agents.agent_home(run_root, agent_name).is_dir()
    ):
        raise click.ClickException(f"Agent {agent_name} not found")
    existing = agents.get_agent_status(run_root, agent_name, ui_base_url=_ui_base_url())
    if existing is not None and existing.status in {"running", "preparing", "starting"}:
        raise click.ClickException(_active_run_error(existing))
    environ = _runtime_environ_for_agent(ctx, agent_name, toolang_root=run_root)
    environ["TOOLANG_ROOT"] = str(run_root)
    log_plan = resolve_agent_logging(
        mode="start" if background else "run",
        environ=environ,
        agent_log_path=agents.agent_runtime_log_path(run_root, agent_name),
    )
    if not background:
        configure_logging_plan(log_plan)
    startup = _wrap_user_error(
        agent_up.resolve_startup,
        toolang_root=run_root,
        agent_name=agent_name,
        host=host,
        endpoint_host=endpoint_host,
        port=port,
        sandbox=sandbox,
        models=models,
        tools=tools,
        caps=caps,
        file_inboxes=inboxes,
        dev=dev,
        component_names=components,
        log_spec=log_plan.spec,
        temporary_port=target.kind == "visiting" and port is None,
        environ=log_plan.environ,
    )
    prepared_state = _wrap_user_error(
        agent_up.prepare_agent,
        toolang_root=run_root,
        agent_name=agent_name,
        progress=as_progress_sink(progress),
    )
    return _RuntimeStartup(
        target=target,
        startup=startup,
        environ=log_plan.environ,
        log_plan=log_plan,
        prepared_state=prepared_state,
    )


@app.command(
    "start",
    help="Start an agent.",
    no_args_is_help=True,
    cls=_StartAgentCommand,
    rich_help_panel=AGENT_COMMAND_PANEL,
)
def start_agent(
    ctx: typer.Context,
    agent: str | None = typer.Argument(
        None, help="Existing local agent name.", hidden=True
    ),
    sandbox: Annotated[
        str,
        typer.Option(help="Run the agent in a sandbox."),
    ] = "none",
    models: Annotated[
        list[str] | None,
        typer.Option(
            "--models",
            help="Limit available models. Pass CSV or repeat.",
        ),
    ] = None,
    tools: Annotated[
        list[str] | None,
        typer.Option(
            "--tools",
            help="Allow selected tools. Pass CSV or repeat.",
        ),
    ] = None,
    caps: Annotated[
        list[str] | None,
        typer.Option(
            "--caps",
            help="Allow selected caps. Pass CSV or repeat.",
        ),
    ] = None,
    host: Annotated[
        str, typer.Option(help="Bind the agent API to this host.")
    ] = "127.0.0.1",
    port: Annotated[
        int | None, typer.Option(help="Bind the agent API to this port.")
    ] = None,
    components: Annotated[
        list[str] | None,
        typer.Option("--enable", help="Enable runtime components. Pass CSV or repeat."),
    ] = None,
    inboxes: Annotated[
        list[Path] | None,
        typer.Option(
            "--inbox",
            help="Watch an inbox directory for file requests. Repeat to watch more than one.",
        ),
    ] = None,
    dev: Annotated[
        Path | None,
        typer.Option(
            "--dev",
            help="Use wheels from this file or directory when starting a sandbox.",
        ),
    ] = None,
    endpoint_host: Annotated[
        str | None,
        typer.Option("--endpoint-host", help="Endpoint host name.", hidden=True),
    ] = None,
) -> None:
    from ..progress import as_progress_sink, make_cli_progress

    selector = _required_runtime_agent(ctx, agent)
    parsed_selector = _wrap_user_error(agents.parse_agent_selector, selector)
    if parsed_selector.form != "name":
        raise click.ClickException(
            "start only supports local agent names; clone the remote source first"
        )
    root = _context_root(ctx)
    normalized_components = _normalize_component_option(components)
    progress = make_cli_progress()
    try:
        with agents.resolved_run_target(
            root, selector, progress=as_progress_sink(progress)
        ) as target:
            launch = _resolve_runtime_startup(
                ctx,
                target,
                sandbox=sandbox,
                models=models,
                tools=tools,
                caps=caps,
                components=normalized_components,
                inboxes=inboxes,
                port=port,
                host=host,
                endpoint_host=endpoint_host,
                dev=dev,
                background=True,
                progress=progress,
            )
    except KeyboardInterrupt:
        progress.interrupt()
        raise typer.Exit(130) from None
    except click.ClickException:
        progress.finish(details=False)
        raise
    agent_name = launch.target.agent_name
    root = launch.target.toolang_root
    progress.finish(details=False)
    if launch.log_plan.path is None:
        raise click.ClickException("agent log path was not resolved")
    log_path = launch.log_plan.path
    log_path.parent.mkdir(parents=True, exist_ok=True)
    launched_at = time.time()
    command = [
        sys.executable,
        "-m",
        "toolang.cli.toolang",
        *agent_up.build_run_argv(launch.startup),
    ]
    with log_path.open("ab") as stream:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=stream,
            stderr=stream,
            env=launch.environ,
            cwd=str(Path.cwd()),
            start_new_session=True,
            close_fds=True,
        )
    status = _wait_for_started_status(
        root=root,
        agent_name=agent_name,
        process=process,
        launched_at=launched_at,
        timeout_sec=30.0,
    )
    if status is None:
        if process.poll() is not None:
            raise click.ClickException(
                f"Agent {agent_name} failed to start: {log_path}"
            )
        raise click.ClickException(f"Agent {agent_name} start timed out: {log_path}")
    if status.status == "failed":
        raise click.ClickException(f"Agent {agent_name} failed to start: {log_path}")
    typer.echo(
        f"Started agent {agent_name}: {status.webui_url or status.api_url or status.endpoint or '-'}"
    )


@app.command(
    "stop",
    help="Stop an agent.",
    no_args_is_help=True,
    cls=_RuntimeAgentCommand,
    rich_help_panel=AGENT_COMMAND_PANEL,
)
def stop_agent(
    ctx: typer.Context,
    agent: str | None = typer.Argument(None, help="Agent name", hidden=True),
    force: Annotated[
        bool,
        typer.Option(help="Force-stop when graceful shutdown does not complete."),
    ] = False,
) -> None:
    agent_name = _required_runtime_agent(ctx, agent)
    root = _context_root(ctx)
    runtime_state = agents.load_runtime_state(root, agent_name)
    runtime_pids = (
        ()
        if runtime_state is not None
        else agents.agent_runtime_process_pids(root, agent_name)
    )
    if runtime_state is None and not runtime_pids:
        raise click.ClickException(f"Agent {agent_name} not running")

    sandbox_plugin = None
    sandbox = runtime_state.get("sandbox") if runtime_state is not None else None
    if isinstance(sandbox, dict):
        sandbox_data = {str(key): value for key, value in sandbox.items()}
        selector = sandbox_data.get("selector")
        if not isinstance(selector, dict):
            raise click.ClickException(f"Sandbox state is invalid for agent: {agent}")
        selector_data = {str(key): value for key, value in selector.items()}
        driver = selector_data.get("driver")
        if not isinstance(driver, str) or not driver.strip():
            raise click.ClickException(
                f"Sandbox driver is missing for agent: {agent_name}"
            )
        sandbox_plugin = agent_up.create_sandbox_plugin(driver.strip(), config={})

    stopped = _wrap_user_error(
        agents.stop_agent,
        root,
        agent_name,
        sandbox_plugin=sandbox_plugin,
        force=force,
    )
    typer.echo(
        f"Stopped agent {agent_name}" if stopped else f"Agent {agent_name} not running"
    )


def _info_caps_summary(toolang_root: Path, agent_name: str) -> str:
    counts = _prepared_info_cap_counts(toolang_root, agent_name)
    if counts is None:
        counts = _prepare_info_cap_counts(toolang_root, agent_name)
    singular = {
        "psyches": "psyche",
        "skills": "skill",
        "services": "service",
        "prompts": "prompt",
    }
    return ", ".join(
        f"{count} {singular[label] if count == 1 else label}"
        for label, count in counts.items()
    )


def _prepared_info_cap_counts(
    toolang_root: Path, agent_name: str
) -> dict[str, int] | None:
    from ...state.prepared import load_private_lock, load_shared_lock

    try:
        shared_lock = load_shared_lock(toolang_root)
        private_lock = load_private_lock(toolang_root, agent_name)
    except (FileNotFoundError, OSError, TypeError, ValueError, KeyError):
        return None
    return _prepared_lock_info_cap_counts(shared_lock, private_lock)


def _prepared_lock_info_cap_counts(
    shared_lock: "PreparedLock",
    private_lock: "PreparedLock",
) -> dict[str, int]:
    counts = {
        "psyches": 0,
        "skills": 0,
        "services": 0,
        "prompts": 0,
    }
    for entry in cap_store.effective_cap_entries(shared_lock, private_lock):
        if entry.kind == "psyche":
            counts["psyches"] += 1
        elif entry.kind == "skill":
            counts["skills"] += 1
        elif entry.kind == "service":
            counts["services"] += 1
        elif entry.kind == "prompt":
            counts["prompts"] += 1
    return counts


def _prepare_info_cap_counts(toolang_root: Path, agent_name: str) -> dict[str, int]:
    from ..progress import as_progress_sink, make_cli_progress

    progress = make_cli_progress(show_materialize_summary=True)
    try:
        prepared = _wrap_user_error(
            agent_up.prepare_agent,
            toolang_root=toolang_root,
            agent_name=agent_name,
            progress=as_progress_sink(progress),
        )
        progress.set_prepare_total(
            len(
                cap_store.effective_cap_entries(
                    prepared.shared_lock, prepared.private_lock
                )
            )
        )
        return _prepared_lock_info_cap_counts(
            prepared.shared_lock, prepared.private_lock
        )
    finally:
        progress.finish(details=False)


def _info_jobs_summary(toolang_root: Path, agent_name: str) -> str:
    from ... import work

    chore_count = len(work.list_chores(toolang_root, agent_name))
    task_count = len(work.list_tasks(toolang_root, agent_name))
    return (
        f"{chore_count} {'chore' if chore_count == 1 else 'chores'}, "
        f"{task_count} {'task' if task_count == 1 else 'tasks'}"
    )


def _info_models_summary(
    toolang_root: Path,
    agent_name: str,
    *,
    runtime_state: dict[str, object],
    running: bool,
) -> str:
    if running:
        raw_models = runtime_state.get("models")
        if isinstance(raw_models, list):
            selectors = tuple(
                str(item).strip()
                for item in raw_models
                if isinstance(item, str) and item.strip()
            )
            if selectors:
                return _model_count_summary(
                    toolang_root,
                    agent_name,
                    environ=dict(os.environ),
                    selectors=selectors,
                )
    selectors = agent_up.load_default_models(toolang_root, agent_name)
    return _model_count_summary(
        toolang_root,
        agent_name,
        environ=dict(os.environ),
        selectors=selectors,
    )


def _info_tools_summary(toolang_root: Path, agent_name: str) -> str:
    rows = _tool_rows(toolang_root, dict(os.environ), agent_name=agent_name)
    set_count = len({namespace for namespace, _tool, _description in rows})
    return f"{len(rows)} {'tool' if len(rows) == 1 else 'tools'}, {set_count} {'set' if set_count == 1 else 'sets'}"


def _model_count_summary(
    toolang_root: Path,
    agent_name: str,
    *,
    environ: dict[str, str],
    selectors: Sequence[str] = (),
) -> str:
    rows = _model_rows(
        toolang_root, environ, agent_name=agent_name, model_selectors=selectors
    )
    provider_count = len({provider for _model, provider, _details in rows})
    return (
        f"{len(rows)} {'model' if len(rows) == 1 else 'models'}, "
        f"{provider_count} {'provider' if provider_count == 1 else 'providers'}"
    )


model_app = typer.Typer(
    help="Inspect available models.",
    add_completion=False,
    no_args_is_help=True,
    pretty_exceptions_enable=False,
    pretty_exceptions_show_locals=False,
)


tool_app = typer.Typer(
    help="Inspect available tools.",
    add_completion=False,
    no_args_is_help=True,
    pretty_exceptions_enable=False,
    pretty_exceptions_show_locals=False,
)


channel_app = typer.Typer(
    help="Inspect available channels.",
    add_completion=False,
    no_args_is_help=True,
    pretty_exceptions_enable=False,
    pretty_exceptions_show_locals=False,
)


sandbox_app = typer.Typer(
    help="Inspect available sandboxes.",
    add_completion=False,
    no_args_is_help=True,
    pretty_exceptions_enable=False,
    pretty_exceptions_show_locals=False,
)


@model_app.command("list", help="List available models.")
def list_models(
    filter_: Annotated[
        list[str] | None,
        typer.Option(
            "--filter",
            "-f",
            "--select",
            help="Filter models with selector-list syntax. Pass CSV or repeat.",
        ),
    ] = None,
    refresh: Annotated[
        bool, typer.Option("--refresh", help="Refresh cached provider model lists.")
    ] = False,
) -> None:
    from ...models.errors import NO_AVAILABLE_MODELS_MESSAGE
    from ...models.resolution import split_model_selectors

    environ = dict(os.environ)
    root = _toolang_root(None)
    selectors = split_model_selectors(tuple(filter_ or ()))
    rows = _model_rows(root, environ, model_selectors=selectors, refresh=refresh)
    if not rows:
        if selectors and _model_rows(root, environ, refresh=refresh):
            typer.echo("No matched models.")
            typer.echo("Try: toolang model list --filter <selector>")
            typer.echo("Alias: toolang model list --select <selector>")
        else:
            typer.echo(NO_AVAILABLE_MODELS_MESSAGE)
        return
    _echo_table(("MODEL", "PROVIDER", "PROFILE"), rows)
    typer.echo()
    provider_count = len({provider for _model, provider, _details in rows})
    typer.echo(
        f" {len(rows)} {'model' if len(rows) == 1 else 'models'}, {provider_count} {'provider' if provider_count == 1 else 'providers'}"
    )


@model_app.command("providers", help="Show configured model providers.")
def list_model_providers() -> None:
    environ = dict(os.environ)
    root = _toolang_root(None)
    rows = _model_provider_rows(root, environ)
    if not rows:
        typer.echo("No model providers found.")
        return
    _echo_table(("PROVIDER", "MODELS", "CONFIG"), rows)


@model_app.command("adapters", help="List available model API adapters.")
def list_model_adapters() -> None:
    from ...models.views import available_model_adapters

    rows = [(name,) for name in available_model_adapters()]
    _echo_table(("ADAPTER",), rows)


@tool_app.command("list", help="List available tools.")
def list_tools(
    filter_: Annotated[
        list[str] | None,
        typer.Option(
            "--filter",
            "-f",
            "--select",
            help="Filter tools with selector-list syntax. Pass CSV or repeat.",
        ),
    ] = None,
) -> None:
    from ...tools.registry import split_tool_selectors

    environ = dict(os.environ)
    root = _toolang_root(None)
    selectors = split_tool_selectors(tuple(filter_ or ()))
    rows = _tool_rows(root, environ, tool_selectors=selectors)
    if not rows:
        if selectors and _tool_rows(root, environ):
            typer.echo("No matched tools.")
            typer.echo("Try: toolang tool list --filter <selector>")
            typer.echo("Alias: toolang tool list --select <selector>")
        else:
            typer.echo("No tools found.")
        return
    _echo_table(("SET", "TOOL", "DESCRIPTION"), rows)
    typer.echo()
    toolset_count = len({namespace for namespace, _tool, _description in rows})
    typer.echo(
        f" {len(rows)} {'tool' if len(rows) == 1 else 'tools'}, {toolset_count} {'toolset' if toolset_count == 1 else 'toolsets'}"
    )


@channel_app.command("list", help="List installed channels.")
def list_channels() -> None:
    rows = _plugin_info_rows("toolang.channel")
    if not rows:
        typer.echo("No channels found.")
        return
    _echo_table(("CHANNEL", "SOURCE"), rows)


@sandbox_app.command("list", help="List installed sandboxes.")
def list_sandboxes() -> None:
    rows = _plugin_info_rows("toolang.sandbox")
    if not rows:
        typer.echo("No sandboxes found.")
        return
    _echo_table(("SANDBOX", "SOURCE"), rows)


def _model_rows(
    root: Path,
    environ: dict[str, str],
    *,
    agent_name: str = "",
    model_selectors: Sequence[str] = (),
    refresh: bool = False,
) -> list[tuple[str, str, str]]:
    from ...models.config import load_model_aliases
    from ...models.views import model_list_rows

    providers = agent_up.load_model_providers(root, agent_name)
    aliases = load_model_aliases(root, agent_name)
    return model_list_rows(
        providers=providers,
        aliases=aliases,
        environ=environ,
        selectors=model_selectors,
        cache_dir=_model_cache_dir(root, agent_name),
        refresh=refresh,
    )


def _model_provider_rows(
    root: Path, environ: dict[str, str]
) -> list[tuple[str, str, str]]:
    from ...models.config import load_model_aliases, load_model_provider_configs
    from ...models.views import model_provider_rows

    provider_configs = load_model_provider_configs(root, "")
    providers = agent_up.load_model_providers(root, "")
    aliases = load_model_aliases(root, "")
    return model_provider_rows(
        providers=providers,
        aliases=aliases,
        provider_configs=provider_configs,
        environ=environ,
        cache_dir=_model_cache_dir(root, ""),
    )


def _model_cache_dir(root: Path, agent_name: str) -> Path:
    del agent_name
    return root / ".runtime" / "model-cache"


def _tool_rows(
    root: Path,
    environ: dict[str, str],
    *,
    agent_name: str = "",
    tool_selectors: Sequence[str] = (),
) -> list[tuple[str, str, str]]:
    from ...config.plugins import load_tool_plugin_config
    from ...tools.views import tool_list_rows

    config = load_tool_plugin_config(root, agent_name, environ=environ)
    tools = agent_up.load_tool_plugins(config=config)
    sources = _plugin_source_by_name("toolang.tool")
    return tool_list_rows(
        tools=tools,
        plugin_sources=sources,
        selectors=tool_selectors,
    )


def _plugin_info_rows(group: str) -> list[tuple[str, str]]:
    return [
        (info.name, info.source) for info in agent_up.list_plugin_infos(group=group)
    ]


def _plugin_source_by_name(group: str) -> dict[str, str]:
    return {info.name: info.source for info in agent_up.list_plugin_infos(group=group)}


def _append_work_update(
    toolang_root: Path,
    agent_name: str,
    *,
    kind: WorkKind,
    job_id: str,
    path: Path,
) -> None:
    update_kind = cast("UpdateKind", f"{kind}_changed")
    _append_agent_update(
        toolang_root,
        agent_name,
        update_kind,
        {
            "id": job_id,
            "path": str(path),
        },
    )


def register_work_commands() -> None:
    work_titles: dict[WorkKind, str] = {
        "chore": "Chore",
        "task": "Task",
    }
    work_group_help: dict[WorkKind, str] = {
        "chore": "Manage agent chores.",
        "task": "Manage agent tasks.",
    }

    @dataclass(frozen=True, slots=True)
    class WorkCommandSpec:
        name: str
        help: Callable[[WorkKind], str]
        factory: Callable[[WorkKind, str], Callable[..., None]]
        cls: type[TyperCommand] | None = None
        no_args_is_help: bool = False

    command_specs: tuple[WorkCommandSpec, ...] = (
        WorkCommandSpec(
            name="list",
            help=lambda kind: f"List {kind}s.",
            factory=_make_work_list_command,
            cls=_RequiredPrefixAgentCommand,
        ),
        WorkCommandSpec(
            name="new",
            help=lambda kind: f"Create a {kind}.",
            factory=_make_new_work_command,
            cls=_RequiredPrefixAgentCommand,
        ),
        WorkCommandSpec(
            name="clone",
            help=lambda kind: f"Clone a {kind}.",
            factory=_make_clone_work_command,
            cls=_RequiredPrefixAgentCommand,
            no_args_is_help=True,
        ),
        WorkCommandSpec(
            name="edit",
            help=lambda kind: f"Edit a {kind}.",
            factory=_make_edit_work_command,
            cls=_RequiredPrefixAgentCommand,
            no_args_is_help=True,
        ),
        WorkCommandSpec(
            name="delete",
            help=lambda kind: f"Delete a {kind}.",
            factory=_make_delete_work_command,
            cls=_RequiredPrefixAgentCommand,
            no_args_is_help=True,
        ),
        WorkCommandSpec(
            name="draft",
            help=lambda kind: f"Move a {kind} to drafts.",
            factory=_make_draft_work_command,
            cls=_RequiredPrefixAgentCommand,
            no_args_is_help=True,
        ),
        WorkCommandSpec(
            name="ready",
            help=lambda kind: f"Move a {kind} to ready.",
            factory=_make_ready_work_command,
            cls=_RequiredPrefixAgentCommand,
            no_args_is_help=True,
        ),
        WorkCommandSpec(
            name="archive",
            help=lambda kind: f"Move a {kind} to archive.",
            factory=_make_archive_work_command,
            cls=_RequiredPrefixAgentCommand,
            no_args_is_help=True,
        ),
        WorkCommandSpec(
            name="cancel",
            help=lambda kind: f"Cancel a {kind}.",
            factory=_make_cancel_work_command,
            cls=_RequiredPrefixAgentCommand,
            no_args_is_help=True,
        ),
        WorkCommandSpec(
            name="reopen",
            help=lambda kind: f"Reopen a {kind}.",
            factory=_make_reopen_work_command,
            cls=_RequiredPrefixAgentCommand,
            no_args_is_help=True,
        ),
        WorkCommandSpec(
            name="run",
            help=lambda kind: (
                "Trigger a chore run now." if kind == "chore" else f"Run a {kind}."
            ),
            factory=_make_run_work_command,
            cls=_RequiredPrefixAgentCommand,
            no_args_is_help=True,
        ),
    )

    for kind in work_titles:
        title = work_titles[kind]
        work_app = typer.Typer(
            cls=_PrefixAgentWorkGroup,
            help=work_group_help[kind],
            add_completion=False,
            no_args_is_help=True,
            pretty_exceptions_enable=False,
            pretty_exceptions_show_locals=False,
        )
        for spec in command_specs:
            if kind == "task" and spec.name == "run":
                continue
            if kind == "chore" and spec.name == "reopen":
                continue
            work_app.command(
                spec.name,
                help=spec.help(kind),
                cls=spec.cls,
                no_args_is_help=spec.no_args_is_help,
            )(spec.factory(kind, title))
        app.add_typer(
            work_app,
            name=kind,
            no_args_is_help=True,
            rich_help_panel=AGENT_COMMAND_PANEL,
        )


def _make_work_list_command(kind: WorkKind, title: str) -> Callable[..., None]:
    def list_work(
        ctx: typer.Context,
        drafts: Annotated[
            bool,
            typer.Option("--drafts", help="List draft items."),
        ] = False,
        archived: Annotated[
            bool,
            typer.Option("--archived", help="List archived items."),
        ] = False,
        all_items: Annotated[
            bool,
            typer.Option("--all", help="List ready, draft, and archived items."),
        ] = False,
    ) -> None:
        from ... import work

        agent_name = _required_prefix_agent(ctx, command_name=kind)
        root = _context_root(ctx)
        lifecycles: tuple[work.JobLifecycle, ...]
        if all_items:
            lifecycles = ("ready", "draft", "archived")
        elif drafts:
            lifecycles = ("draft",)
        elif archived:
            lifecycles = ("archived",)
        else:
            lifecycles = ("ready",)
        if kind == "task":
            entries = tuple(
                entry
                for lifecycle in lifecycles
                for entry in work.list_tasks(root, agent_name, lifecycle=lifecycle)
            )
            if not entries:
                typer.echo("No tasks found.")
                return
            rows = [
                (
                    entry.document.task_id(),
                    entry.document.display_title(
                        fallback_name=entry.document.task_id()
                    ),
                    entry.lifecycle,
                    _work_location(root, agent_name, entry.path),
                )
                for entry in entries
            ]
            _echo_table(("ID", title.upper(), "LIFECYCLE", "LOCATION"), rows)
            return
        entries = tuple(
            entry
            for lifecycle in lifecycles
            for entry in work.list_chores(root, agent_name, lifecycle=lifecycle)
        )
        if not entries:
            typer.echo("No chores found.")
            return
        rows = [
            (
                entry.document.chore_id(),
                entry.document.display_title(fallback_name=entry.document.chore_id()),
                entry.lifecycle,
                entry.document.schedule,
                _work_location(root, agent_name, entry.path),
            )
            for entry in entries
        ]
        _echo_table(("ID", title.upper(), "LIFECYCLE", "SCHEDULE", "LOCATION"), rows)

    return list_work


def _make_new_work_command(kind: WorkKind, title: str) -> Callable[..., None]:
    def new_work(
        ctx: typer.Context,
        draft: Annotated[
            bool,
            typer.Option("--draft", help="Create the item in drafts."),
        ] = False,
    ) -> None:
        from ... import work

        agent_name = _required_prefix_agent(ctx, command_name=kind)
        text = click.edit(
            templates.render_template(kind, "default", agent_name=agent_name),
            extension=".md",
            require_save=True,
        )
        if text is None:
            raise typer.Exit()
        path = _wrap_user_error(
            work.create_task_text if kind == "task" else work.create_chore_text,
            _context_root(ctx),
            agent_name,
            text,
            lifecycle="draft" if draft else "ready",
        )
        job_id = path.stem
        if not draft:
            _reconcile_work_jobs(_context_root(ctx), agent_name, kind=kind)
        _append_work_update(
            _context_root(ctx), agent_name, kind=kind, job_id=job_id, path=path
        )
        typer.echo(f"{kind} {job_id} created\t{path}")

    return new_work


def _make_clone_work_command(kind: WorkKind, title: str) -> Callable[..., None]:
    def clone_work(
        ctx: typer.Context,
        id: str = typer.Argument(..., help=f"{title} id", metavar="ID"),
    ) -> None:
        from ... import work

        agent_name = _required_prefix_agent(ctx, command_name=kind)
        path = _wrap_user_error(
            work.clone_task if kind == "task" else work.clone_chore,
            _context_root(ctx),
            agent_name,
            id,
        )
        job_id = path.stem
        _reconcile_work_jobs(_context_root(ctx), agent_name, kind=kind)
        _append_work_update(
            _context_root(ctx), agent_name, kind=kind, job_id=job_id, path=path
        )
        typer.echo(f"{kind} {job_id} cloned\t{path}")

    return clone_work


def _make_edit_work_command(kind: WorkKind, title: str) -> Callable[..., None]:
    def edit_work(
        ctx: typer.Context,
        id: str = typer.Argument(..., help=f"{title} id", metavar="ID"),
    ) -> None:
        from ... import work

        job_id = id
        agent_name = _required_prefix_agent(ctx, command_name=kind)
        text = _wrap_user_error(
            work.load_task_text if kind == "task" else work.load_chore_text,
            _context_root(ctx),
            agent_name,
            job_id,
        )
        updated_text = click.edit(
            text,
            extension=".md",
            require_save=True,
        )
        if updated_text is None:
            raise typer.Exit()
        path = _wrap_user_error(
            work.update_task_text if kind == "task" else work.update_chore_text,
            _context_root(ctx),
            agent_name,
            job_id,
            updated_text,
        )
        _reconcile_work_jobs(_context_root(ctx), agent_name, kind=kind)
        _append_work_update(
            _context_root(ctx), agent_name, kind=kind, job_id=job_id, path=path
        )
        typer.echo(str(path))

    return edit_work


def _make_draft_work_command(kind: WorkKind, title: str) -> Callable[..., None]:
    def draft_work(
        ctx: typer.Context,
        id: str = typer.Argument(..., help=f"{title} id", metavar="ID"),
    ) -> None:
        from ... import work

        job_id = id
        agent_name = _required_prefix_agent(ctx, command_name=kind)
        path = _wrap_user_error(
            work.draft_task if kind == "task" else work.draft_chore,
            _context_root(ctx),
            agent_name,
            job_id,
        )
        if path is None:
            raise click.ClickException(f"{kind} not found: {job_id}")
        _reconcile_work_jobs(_context_root(ctx), agent_name, kind=kind)
        _append_work_update(
            _context_root(ctx), agent_name, kind=kind, job_id=job_id, path=path
        )
        typer.echo(f"{kind} {job_id} drafted\t{path}")

    return draft_work


def _make_ready_work_command(kind: WorkKind, title: str) -> Callable[..., None]:
    def ready_work(
        ctx: typer.Context,
        id: str = typer.Argument(..., help=f"{title} id", metavar="ID"),
    ) -> None:
        from ... import work

        job_id = id
        agent_name = _required_prefix_agent(ctx, command_name=kind)
        path = _wrap_user_error(
            work.ready_task if kind == "task" else work.ready_chore,
            _context_root(ctx),
            agent_name,
            job_id,
        )
        if path is None:
            raise click.ClickException(f"{kind} not found: {job_id}")
        _reconcile_work_jobs(_context_root(ctx), agent_name, kind=kind)
        _append_work_update(
            _context_root(ctx), agent_name, kind=kind, job_id=job_id, path=path
        )
        typer.echo(f"{kind} {job_id} ready\t{path}")

    return ready_work


def _make_archive_work_command(kind: WorkKind, title: str) -> Callable[..., None]:
    def archive_work(
        ctx: typer.Context,
        id: str = typer.Argument(..., help=f"{title} id", metavar="ID"),
    ) -> None:
        from ... import work

        job_id = id
        agent_name = _required_prefix_agent(ctx, command_name=kind)
        path = _wrap_user_error(
            work.archive_task if kind == "task" else work.archive_chore,
            _context_root(ctx),
            agent_name,
            job_id,
        )
        if path is None:
            raise click.ClickException(f"{kind} not found: {job_id}")
        _reconcile_work_jobs(_context_root(ctx), agent_name, kind=kind)
        _append_work_update(
            _context_root(ctx), agent_name, kind=kind, job_id=job_id, path=path
        )
        typer.echo(f"{kind} {job_id} archived\t{path}")

    return archive_work


def _make_reopen_work_command(kind: WorkKind, title: str) -> Callable[..., None]:
    def reopen_work(
        ctx: typer.Context,
        id: str = typer.Argument(..., help=f"{title} id", metavar="ID"),
    ) -> None:
        if kind != "task":
            raise click.ClickException("reopen is only supported for tasks")
        from ... import jobs

        agent_name = _required_prefix_agent(ctx, command_name=kind)
        root = _context_root(ctx)
        store = jobs.open_job_store(root, agent_name)
        try:
            record = _wrap_user_error(
                store.reopen_task,
                toolang_root=root,
                agent_name=agent_name,
                task_id=id,
            )
        finally:
            store.close()
        typer.echo(f"task {record.job_id} reopened\t{record.status}")

    return reopen_work


def _make_run_work_command(kind: WorkKind, title: str) -> Callable[..., None]:
    def run_work(
        ctx: typer.Context,
        id: str = typer.Argument(..., help=f"{title} id", metavar="ID"),
    ) -> None:
        if kind != "chore":
            raise click.ClickException("run is only supported for chores")
        _runtime_post(ctx, f"/api/v1/chores/{id}/run", payload={})
        typer.echo(f"chore {id} manual run requested")

    return run_work


def _make_cancel_work_command(kind: WorkKind, title: str) -> Callable[..., None]:
    def cancel_work(
        ctx: typer.Context,
        id: str = typer.Argument(..., help=f"{title} id", metavar="ID"),
    ) -> None:
        from ... import jobs

        agent_name = _required_prefix_agent(ctx, command_name=kind)
        root = _context_root(ctx)
        store = jobs.open_job_store(root, agent_name)
        try:
            store.reconcile(toolang_root=root, agent_name=agent_name, kind=kind)
            record = store.get(job_id=id, kind=kind)
            if record is None:
                raise click.ClickException(f"{kind} not found: {id}")
            if record.status == "running" and record.last_run_id is not None:
                _runtime_post(
                    ctx, f"/api/v1/runs/{record.last_run_id}/cancel", payload={}
                )
                typer.echo(f"{kind} {id} cancel requested\t{record.last_run_id}")
                return
            if kind == "task" and record.status == "todo":
                updated = store.cancel_pending_task(task_id=id)
                typer.echo(f"task {id} canceled\t{updated.status}")
                return
            raise click.ClickException(
                f"{kind} cannot be canceled from status: {record.status}"
            )
        finally:
            store.close()

    return cancel_work


def _make_delete_work_command(kind: WorkKind, title: str) -> Callable[..., None]:
    def delete_work(
        ctx: typer.Context,
        id: str = typer.Argument(..., help=f"{title} id", metavar="ID"),
    ) -> None:
        from ... import work

        job_id = id
        agent_name = _required_prefix_agent(ctx, command_name=kind)
        active_entry = (
            work.find_task(_context_root(ctx), agent_name, job_id, lifecycle=None)
            if kind == "task"
            else work.find_chore(_context_root(ctx), agent_name, job_id, lifecycle=None)
        )
        if active_entry is not None and active_entry.lifecycle != "archived":
            raise click.ClickException(
                f"{kind} is not archived: {job_id}; archive it before deleting"
            )
        entry = (
            work.find_archived_task(_context_root(ctx), agent_name, job_id)
            if kind == "task"
            else work.find_archived_chore(_context_root(ctx), agent_name, job_id)
        )
        if entry is None:
            raise click.ClickException(f"archived {kind} not found: {job_id}")
        removed = _wrap_user_error(
            work.remove_archived_task if kind == "task" else work.remove_archived_chore,
            _context_root(ctx),
            agent_name,
            job_id,
        )
        if not removed:
            raise click.ClickException(f"archived {kind} not found: {job_id}")
        path = entry.path
        _reconcile_work_jobs(_context_root(ctx), agent_name, kind=kind)
        _append_work_update(
            _context_root(ctx), agent_name, kind=kind, job_id=job_id, path=path
        )
        typer.echo(f"{kind} {job_id} deleted")

    return delete_work


def _reconcile_work_jobs(
    toolang_root: Path, agent_name: str, *, kind: WorkKind
) -> None:
    from ... import jobs

    store = jobs.open_job_store(toolang_root, agent_name)
    try:
        store.reconcile(toolang_root=toolang_root, agent_name=agent_name, kind=kind)
    finally:
        store.close()


def _work_location(toolang_root: Path, agent_name: str, path: Path) -> str:
    try:
        return str(path.relative_to(agents.agent_home(toolang_root, agent_name)))
    except ValueError:
        return str(path)


app.add_typer(
    model_app, name="model", no_args_is_help=True, rich_help_panel=RUNTIME_COMMAND_PANEL
)
app.add_typer(
    tool_app, name="tool", no_args_is_help=True, rich_help_panel=RUNTIME_COMMAND_PANEL
)
app.add_typer(
    channel_app,
    name="channel",
    no_args_is_help=True,
    rich_help_panel=RUNTIME_COMMAND_PANEL,
)
app.add_typer(
    sandbox_app,
    name="sandbox",
    no_args_is_help=True,
    rich_help_panel=RUNTIME_COMMAND_PANEL,
)
register_fmt_command(app)
register_parse_command(app)
register_caps_commands(app, rich_help_panel=CAPS_COMMAND_PANEL)
register_work_commands()


def main(argv: Sequence[str] | None = None) -> int:
    global _CLI_PREFIX_AGENT
    raw_args = list(argv) if argv is not None else sys.argv[1:]
    global_args, body = _extract_global_args(raw_args)
    if body:
        roaming_source = _roaming_source_path(body[0])
        if roaming_source is not None:
            try:
                configure_logging(spec=None, environ={})
            except ValueError as exc:
                typer.echo(f"toolang error: {exc}", err=True)
                return 1
            if _is_roaming_thread_command(body):
                return _run_roaming_thread_command(
                    global_args,
                    body,
                    prog_name=_prog_name(sys.argv[0] if sys.argv else ""),
                )
            if _is_roaming_file_runtime_request(body):
                return _run_roaming_file_runtime(global_args, body)
            return cli_invoke.handle_roaming_invoke(
                global_args,
                body,
                prog_name=_prog_name(sys.argv[0] if sys.argv else ""),
            )
    args, prefix_agent = _normalize_cli_args(raw_args)
    previous_prefix_agent = _CLI_PREFIX_AGENT
    _CLI_PREFIX_AGENT = prefix_agent
    try:
        app(
            args=args,
            prog_name=_prog_name(sys.argv[0] if sys.argv else ""),
            standalone_mode=True,
        )
    except click.exceptions.Exit as exc:
        return exc.exit_code
    except (FileExistsError, FileNotFoundError, ValueError) as exc:
        typer.echo(f"toolang error: {exc}", err=True)
        return 1
    finally:
        _CLI_PREFIX_AGENT = previous_prefix_agent
    return 0


def _is_roaming_file_runtime_request(body: list[str]) -> bool:
    rest = body[1:]
    if not rest or not rest[0].startswith("-"):
        return False
    return any(token == "--inbox" or token.startswith("--inbox=") for token in rest)


def _is_roaming_thread_command(body: list[str]) -> bool:
    return len(body) >= 2 and body[1] in ROAMING_THREAD_COMMANDS


def _run_roaming_thread_command(
    global_args: list[str], body: list[str], *, prog_name: str
) -> int:
    global _CLI_PREFIX_AGENT
    if global_args:
        typer.echo(
            "toolang error: too <path>.too does not support global CLI options",
            err=True,
        )
        return 1
    source_path = _roaming_source_path(body[0])
    if source_path is None:
        typer.echo(f"toolang error: agent program not found: {body[0]}", err=True)
        return 1
    try:
        toolang_root, agent_name = agents.materialize_roaming_program(source_path)
    except (FileExistsError, FileNotFoundError, ValueError) as exc:
        typer.echo(f"toolang error: {exc}", err=True)
        return 1
    previous_prefix_agent = _CLI_PREFIX_AGENT
    _CLI_PREFIX_AGENT = agent_name
    try:
        app(
            args=["--root", str(toolang_root), *body[1:]],
            prog_name=prog_name,
            standalone_mode=True,
        )
    except click.exceptions.Exit as exc:
        return exc.exit_code
    except SystemExit as exc:
        code = exc.code
        return code if isinstance(code, int) else 1
    except (FileExistsError, FileNotFoundError, ValueError) as exc:
        typer.echo(f"toolang error: {exc}", err=True)
        return 1
    finally:
        _CLI_PREFIX_AGENT = previous_prefix_agent
    return 0


def _run_roaming_file_runtime(global_args: list[str], body: list[str]) -> int:
    if global_args:
        typer.echo(
            "toolang error: too <path>.too does not support global CLI options",
            err=True,
        )
        return 1
    source_path = _roaming_source_path(body[0])
    if source_path is None:
        typer.echo(f"toolang error: agent program not found: {body[0]}", err=True)
        return 1
    try:
        options = _parse_roaming_file_runtime_options(body[1:])
        toolang_root, agent_name = agents.materialize_roaming_program(source_path)
        existing = agents.get_agent_status(
            toolang_root, agent_name, ui_base_url=_ui_base_url()
        )
        if existing is not None and existing.status in {
            "running",
            "preparing",
            "starting",
        }:
            raise click.ClickException(_active_run_error(existing))
        from ...config.env import load_runtime_environ

        environ = load_runtime_environ(
            toolang_root, agent_name, base_environ=os.environ
        )
        environ["TOOLANG_ROOT"] = str(toolang_root)
        log_plan = resolve_agent_logging(
            mode="run",
            environ=environ,
            agent_log_path=agents.agent_runtime_log_path(toolang_root, agent_name),
        )
        configure_logging_plan(log_plan)
        startup = _wrap_user_error(
            agent_up.resolve_startup,
            toolang_root=toolang_root,
            agent_name=agent_name,
            host=options.host,
            endpoint_host=options.endpoint_host,
            port=options.port,
            sandbox=options.sandbox,
            models=options.models,
            tools=options.tools,
            caps=options.caps,
            file_inboxes=options.inboxes,
            dev=options.dev,
            component_names=options.components,
            log_spec=log_plan.spec,
            temporary_port=options.port is None,
            environ=log_plan.environ,
        )
        prepared_state = _wrap_user_error(
            agent_up.prepare_agent,
            toolang_root=toolang_root,
            agent_name=agent_name,
        )
        return _wrap_user_error(
            agent_up.start_runtime,
            startup,
            environ=log_plan.environ,
            prepared_state=prepared_state,
        )
    except KeyboardInterrupt:
        return 130
    except (
        FileExistsError,
        FileNotFoundError,
        ValueError,
        click.ClickException,
    ) as exc:
        message = exc.message if isinstance(exc, click.ClickException) else str(exc)
        typer.echo(f"toolang error: {message}", err=True)
        return 1


def _parse_roaming_file_runtime_options(argv: list[str]) -> _RoamingFileRuntimeOptions:
    from ...caps import split_cap_selectors
    from ...models.resolution import split_model_selectors
    from ...tools.registry import split_tool_selectors

    inboxes: list[Path] = []
    models: list[str] = []
    tools: list[str] | None = None
    caps: list[str] = []
    components: list[str] = ["runner.file", "trigger.file", "trigger.watch"]
    host = "127.0.0.1"
    endpoint_host: str | None = None
    port: int | None = None
    sandbox = "none"
    dev: Path | None = None
    index = 0
    while index < len(argv):
        token = argv[index]
        if token.startswith("--inbox="):
            value = token.partition("=")[2].strip()
            if not value:
                raise click.ClickException("--inbox requires a value")
            inboxes.append(Path(value))
            index += 1
            continue
        if token == "--inbox":
            value = _next_option_value(argv, index, "--inbox")
            inboxes.append(Path(value))
            index += 2
            continue
        if token.startswith("--models="):
            value = token.partition("=")[2].strip()
            if not value:
                raise click.ClickException("--models requires a value")
            models.extend(split_model_selectors((value,)))
            index += 1
            continue
        if token == "--models":
            value = _next_option_value(argv, index, "--models")
            models.extend(split_model_selectors((value,)))
            index += 2
            continue
        if token.startswith("--tools="):
            value = token.partition("=")[2].strip()
            if not value:
                raise click.ClickException("--tools requires a value")
            if tools is None:
                tools = []
            tools.extend(split_tool_selectors((value,)))
            index += 1
            continue
        if token == "--tools":
            value = _next_option_value(argv, index, "--tools")
            if tools is None:
                tools = []
            tools.extend(split_tool_selectors((value,)))
            index += 2
            continue
        if token.startswith("--caps="):
            value = token.partition("=")[2].strip()
            if not value:
                raise click.ClickException("--caps requires a value")
            caps.extend(split_cap_selectors((value,)))
            index += 1
            continue
        if token == "--caps":
            value = _next_option_value(argv, index, "--caps")
            caps.extend(split_cap_selectors((value,)))
            index += 2
            continue
        if token.startswith("--enable="):
            value = token.partition("=")[2].strip()
            if not value:
                raise click.ClickException("--enable requires a value")
            components.extend(_normalize_component_option([value]) or [])
            index += 1
            continue
        if token == "--enable":
            value = _next_option_value(argv, index, "--enable")
            components.extend(_normalize_component_option([value]) or [])
            index += 2
            continue
        if token.startswith("--host="):
            host = token.partition("=")[2].strip()
            if not host:
                raise click.ClickException("--host requires a value")
            index += 1
            continue
        if token == "--host":
            host = _next_option_value(argv, index, "--host")
            index += 2
            continue
        if token.startswith("--endpoint-host="):
            endpoint_host = token.partition("=")[2].strip() or None
            index += 1
            continue
        if token == "--endpoint-host":
            endpoint_host = _next_option_value(argv, index, "--endpoint-host")
            index += 2
            continue
        if token.startswith("--port="):
            port = _parse_port_value(token.partition("=")[2])
            index += 1
            continue
        if token == "--port":
            port = _parse_port_value(_next_option_value(argv, index, "--port"))
            index += 2
            continue
        if token.startswith("--sandbox="):
            sandbox = token.partition("=")[2].strip()
            if not sandbox:
                raise click.ClickException("--sandbox requires a value")
            index += 1
            continue
        if token == "--sandbox":
            sandbox = _next_option_value(argv, index, "--sandbox")
            index += 2
            continue
        if token.startswith("--dev="):
            value = token.partition("=")[2].strip()
            if not value:
                raise click.ClickException("--dev requires a value")
            dev = Path(value)
            index += 1
            continue
        if token == "--dev":
            dev = Path(_next_option_value(argv, index, "--dev"))
            index += 2
            continue
        if token in {"--help", "-h"}:
            raise click.ClickException(
                "file request runtime usage: toolang SCRIPT --inbox PATH [--inbox PATH...]"
            )
        if token.startswith("-"):
            raise click.ClickException(f"unknown Toolang runtime option: {token}")
        raise click.ClickException(
            f"unexpected thunk argument for file request runtime: {token}"
        )
    if not inboxes:
        raise click.ClickException("--inbox is required")
    normalized_components = list(dict.fromkeys(components))
    return _RoamingFileRuntimeOptions(
        inboxes=tuple(inboxes),
        models=tuple(dict.fromkeys(models)),
        tools=None if tools is None else tuple(dict.fromkeys(tools)),
        caps=tuple(dict.fromkeys(caps)),
        components=tuple(normalized_components),
        host=host,
        endpoint_host=endpoint_host,
        port=port,
        sandbox=sandbox,
        dev=dev,
    )


def _next_option_value(argv: list[str], index: int, option_name: str) -> str:
    if index + 1 >= len(argv):
        raise click.ClickException(f"{option_name} requires a value")
    value = argv[index + 1].strip()
    if not value:
        raise click.ClickException(f"{option_name} requires a value")
    return value


def _parse_port_value(raw: str) -> int:
    try:
        return int(raw)
    except ValueError as exc:
        raise click.ClickException("--port expects an integer") from exc


def _roaming_source_path(token: str) -> Path | None:
    text = token.strip()
    if not text or text.startswith("-"):
        return None
    candidate = Path(text).expanduser()
    try:
        resolved = candidate.resolve()
    except OSError:
        return None
    if not resolved.is_file() or resolved.suffix != ".too":
        return None
    return resolved


def _prog_name(argv0: str) -> str:
    text = Path(argv0).name.strip()
    return text or "toolang"


def _normalize_cli_args(argv: list[str]) -> tuple[list[str], str | None]:
    global_args, body = _extract_global_args(argv)
    rewritten_body, prefix_agent = _rewrite_agent_shortcuts(body)
    return [*global_args, *rewritten_body], prefix_agent


def _extract_global_args(argv: list[str]) -> tuple[list[str], list[str]]:
    global_args: list[str] = []
    body: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        consumed = _consume_global_arg(token, argv, index)
        if consumed is None:
            body.append(token)
            index += 1
            continue
        extracted, step = consumed
        global_args.extend(extracted)
        index += step
    return global_args, body


def _consume_global_arg(
    token: str, argv: list[str], index: int
) -> tuple[list[str], int] | None:
    if token == "--root":
        if index + 1 >= len(argv):
            return ([token], 1)
        return ([token, argv[index + 1]], 2)
    if token.startswith("--root="):
        return (["--root", token.removeprefix("--root=")], 1)
    return None


def _rewrite_agent_shortcuts(body: list[str]) -> tuple[list[str], str | None]:
    if not body:
        return body, None
    if (
        len(body) == 2
        and _looks_like_agent_name(body[0])
        and body[1] in _THREAD_TARGET_COMMANDS
    ):
        return [body[1], "--help"], body[0]
    if (
        len(body) >= 2
        and _looks_like_agent_name(body[0])
        and body[1] in POSTFIX_AGENT_COMMANDS
    ):
        return [body[1], body[0], *body[2:]], None
    if (
        len(body) >= 2
        and _looks_like_agent_name(body[0])
        and body[1] in PREFIX_AGENT_COMMANDS
    ):
        return [body[1], *body[2:]], body[0]
    return body, None


def _looks_like_agent_name(token: str) -> bool:
    return bool(token) and not token.startswith("-") and token not in TOP_LEVEL_COMMANDS


if __name__ == "__main__":
    raise SystemExit(main())
