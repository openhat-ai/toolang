"""Typer CLI for Toolang agent management."""

from __future__ import annotations

import ast
import asyncio
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
import io
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
import json
from pathlib import Path
import os
import shutil
import subprocess
import sys
import threading
import time
import tomllib
from typing import Annotated, Any, Literal, TYPE_CHECKING, cast
from urllib.parse import urlencode
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

import click
from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import ANSI, to_formatted_text
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, VSplit, Window
from prompt_toolkit.layout.containers import ConditionalContainer
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.styles import Style
from prompt_toolkit.utils import get_cwidth
from rich.console import Console
from rich.markdown import Markdown
import typer
from typer import rich_utils
from typer.core import TyperCommand, TyperGroup

from ... import agents
from ...caps import split_cap_selectors
from ...base.types.message import message_summary, parts_to_data
from ...config.log import LoggingPlan, configure_logging, configure_logging_plan, resolve_agent_logging
from ...execution.db import ExecutionStore, execution_db_path
from ...execution.detail import run_detail_from_record, thread_info_from_record, thread_info_from_runs
from ...execution.labels import child_call_summary, executable_label, flow_op_summary
from ...execution.projection import (
    FlowCallView,
    FlowStageView,
    child_run_ids,
    flow_stage_context,
    output_count,
    project_flow_from_run,
    project_flow_from_step_payloads,
    shape_label,
    stage_calls,
    stage_lanes,
    stage_title_label,
)
from ...execution.records import RunStatus, step_input_items_to_data, step_payload_to_data
from ...execution.stream import event_data
from ...models.resolution import split_model_selectors
from ...tools.registry import split_tool_selectors
from ..chat_history import ChatInputHistoryStore
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
_AGENT_PANEL_COMMAND_ORDER = ("new", "clone", "remove", "list", "info", "run", "start", "stop", "chore", "task")
_THREAD_PANEL_COMMAND_ORDER = ("chat", "cancel", "steer", "rewind", "fork", "inspect", "runs", "threads")
_RUNTIME_PANEL_COMMAND_ORDER = ("model", "tool", "channel", "sandbox")
_THREAD_TARGET_COMMANDS = frozenset({"steer", "cancel", "rewind", "fork"})
_HIDDEN_ALIAS_COMMANDS = frozenset({"send", "attach"})
_CHAT_MAX_INPUT_ROWS = 6
_CHAT_MAX_QUEUE_ROWS = 4
_CHAT_MAX_ACTIVE_RUN_ACTIVITY_ROWS = 12
_CHAT_DIM = "\x1b[2m"
_CHAT_NORMAL_INTENSITY = "\x1b[22m"
_CHAT_RESET = "\x1b[0m"
_CHAT_BOLD = "\x1b[1m"
_CHAT_QUEUE_FG = "#f2f2f2"
_CHAT_QUEUE_BG = "#3a3a3a"
_CHAT_INPUT_FG = "#f5f5f5"
_CHAT_INPUT_BG = "#444444"
_CHAT_STEER_INPUT_FG = "#f5f5f5"
_CHAT_STEER_INPUT_BG = "#3f4a4d"
_CHAT_STATUS_FG = "#f2f2f2"
_CHAT_STATUS_BG = "#5a5a5a"
_CHAT_CURSOR_FG = "#111111"
_CHAT_CURSOR_BG = "#eeeeee"
_CHAT_SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
_CHAT_FLOW_DETAIL_INDENT = "  "
_CHAT_FLOW_STATEMENT_MARKER = "‣"


class _ToolangGroup(TyperGroup):
    def list_commands(self, ctx: click.Context) -> list[str]:
        names = TyperGroup.list_commands(self, ctx)
        agent_names = [name for name in _AGENT_PANEL_COMMAND_ORDER if name in names]
        if agent_names:
            first_agent_index = min(names.index(name) for name in agent_names)
            reordered = [name for name in names if name not in agent_names]
            names = reordered[:first_agent_index] + agent_names + reordered[first_agent_index:]
        thread_names = [name for name in _THREAD_PANEL_COMMAND_ORDER if name in names]
        ordered_thread_group_names = [*thread_names]
        if ordered_thread_group_names:
            reordered = [name for name in names if name not in ordered_thread_group_names]
            runtime_indexes = [reordered.index(name) for name in _RUNTIME_PANEL_COMMAND_ORDER if name in reordered]
            insertion_index = min(runtime_indexes) if runtime_indexes else len(reordered)
            names = reordered[:insertion_index] + ordered_thread_group_names + reordered[insertion_index:]
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
    {"run", "start", "stop", "info", "chat", "send", "attach", "threads", "runs", "inspect", "steer", "cancel", "rewind", "fork"}
)
ROAMING_THREAD_COMMANDS = frozenset({"threads", "runs", "inspect", "steer", "cancel", "rewind", "fork"})
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
    try:
        return package_version("toolang")
    except PackageNotFoundError:
        pyproject_path = Path(__file__).resolve().parents[3] / "pyproject.toml"
        try:
            data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            return "unknown"
        project = data.get("project")
        if not isinstance(project, dict):
            return "unknown"
        version = project.get("version")
        return version if isinstance(version, str) else "unknown"


def _source_state_suffix() -> str:
    source_root = _source_tree_root()
    if source_root is None:
        return ""
    short_sha = _git_output(source_root, "rev-parse", "--short", "HEAD")
    if short_sha is None:
        return ""
    dirty = _git_output(source_root, "status", "--short")
    if dirty is None:
        return f"+{short_sha}"
    dirty_suffix = "*" if dirty else ""
    return f"+{short_sha}{dirty_suffix}"


def _git_output(source_root: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=source_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _source_tree_root() -> Path | None:
    current = Path(__file__).resolve()
    for parent in current.parents:
        if (parent / ".git").exists():
            return parent
    return None


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


def _print_hidden_command_panel(console: Any, name: str, commands: list[click.Command]) -> None:
    if not commands:
        return
    rich_utils._print_commands_panel(
        name=name,
        commands=commands,
        markup_mode="rich",
        console=console,
        cmd_len=max(len(command.name or "") for command in commands),
    )


@app.command("new", help="Create an agent.", no_args_is_help=True, rich_help_panel=AGENT_COMMAND_PANEL)
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


@app.command("clone", help="Clone an agent.", no_args_is_help=True, rich_help_panel=AGENT_COMMAND_PANEL)
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


@app.command("remove", help="Remove an agent.", no_args_is_help=True, rich_help_panel=AGENT_COMMAND_PANEL)
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


@app.command("list", help="Show agents and their status.", rich_help_panel=AGENT_COMMAND_PANEL)
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
        ("Models", _info_models_summary(root, agent_name, runtime_state=runtime_state, running=status.status != "stopped")),
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
    thunk: Annotated[str | None, typer.Option("--thunk", help="Use a thunk for new runs.")] = None,
    flow: Annotated[str | None, typer.Option("--flow", help="Use a flow for new runs.")] = None,
) -> None:
    thread_id = _target_thread_id(ctx, thread) if thread is not None else None
    selectors = _chat_selector_payload(models=models, tools=tools, caps=caps, thunk=thunk, flow=flow)
    _chat_interactive(ctx, thread_id=thread_id, selector_payload=selectors)


@app.command("send", help="Send one message to a thread.", hidden=True, cls=_RequiredPrefixAgentCommand)
def send_command(
    ctx: typer.Context,
    thread: Annotated[str, typer.Argument(help="Thread id.")],
    message: Annotated[str, typer.Argument(help="Message text.")],
    model: Annotated[str | None, typer.Option("--model", help="Model selector.")] = None,
) -> None:
    target = _target_thread_id(ctx, thread)
    payload: dict[str, Any] = {"thread": target, "client": "tui", "message": _message_payload(message)}
    if model is not None:
        payload["model"] = model
    _runtime_stream(ctx, "/api/v1/chat/stream", payload=payload)


@app.command("attach", help="Open chat on a thread.", hidden=True, cls=_RequiredPrefixAgentCommand)
def attach_command(
    ctx: typer.Context,
    thread: Annotated[str, typer.Argument(help="Thread id.")],
) -> None:
    _open_thread_ui(ctx, _target_thread_id(ctx, thread))


@app.command("threads", help="List threads.", cls=_RequiredPrefixAgentCommand, rich_help_panel=THREAD_COMMAND_PANEL)
def threads_command(
    ctx: typer.Context,
    origin: Annotated[str | None, typer.Option("--origin", help="Filter by origin.")] = None,
    channel: Annotated[str | None, typer.Option("--channel", help="Filter by channel.")] = None,
    status: Annotated[str | None, typer.Option("--status", help="Filter by thread status.")] = None,
) -> None:
    query = _query_params(origin=origin, channel=channel, status=status)
    path = "/api/v1/threads" if not query else f"/api/v1/threads?{query}"
    result = _runtime_json_or_offline(
        ctx,
        path,
        lambda: _offline_threads_json(ctx, origin=origin, channel=channel, status=status),
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


@app.command("runs", help="List runs.", cls=_RequiredPrefixAgentCommand, rich_help_panel=THREAD_COMMAND_PANEL)
def runs_command(
    ctx: typer.Context,
    thread: Annotated[str | None, typer.Option("--thread", help="Filter by thread id.")] = None,
    status: Annotated[str | None, typer.Option("--status", help="Filter by run status.")] = None,
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
                _truncate_table_text(item.get("summary") or item.get("input_text"), width=48),
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
                _truncate_table_text(item.get("summary") or item.get("input_text"), width=48),
                _display_run_status(item.get("status")),
                str(item.get("created_at", "")),
            )
            for item in result.get("items", [])
            if isinstance(item, dict)
        ]
        _echo_table(("THREAD", "RUN", "TITLE", "STATUS", "CREATED"), rows)


@app.command("inspect", help="Inspect a thread or run.", no_args_is_help=True, cls=_RequiredPrefixAgentCommand, rich_help_panel=THREAD_COMMAND_PANEL)
def inspect_command(
    ctx: typer.Context,
    target: Annotated[str, typer.Argument(help="Run id or thread id to inspect.")],
    view: Annotated[
        Literal["tree", "steps", "events", "json"],
        typer.Option("--view", help="Inspection view."),
    ] = "tree",
    verbosity: Annotated[int, typer.Option("-v", "--verbose", count=True, help="Expand inspect tree depth.")] = 0,
    limit: Annotated[int, typer.Option("--limit", help="Maximum events or thread runs to read.")] = 100,
) -> None:
    if limit < 1:
        raise click.ClickException("--limit must be at least 1")
    if view == "json":
        typer.echo(json.dumps(_inspect_detail(ctx, target, limit=limit), ensure_ascii=False, indent=2))
        return
    if view == "events":
        _render_inspect_events(ctx, target, limit=limit, verbosity=verbosity)
        return
    detail = _inspect_detail(ctx, target, limit=limit, include_thread=view == "tree")
    if view == "steps":
        _render_inspect_steps(detail)
        return
    _render_inspect_tree(detail, verbosity=verbosity)


@app.command(
    "steer",
    help="Steer an active run.",
    no_args_is_help=True,
    cls=_RequiredPrefixAgentCommand,
    rich_help_panel=THREAD_COMMAND_PANEL,
)
def steer_command(
    ctx: typer.Context,
    run: str = typer.Argument(..., help="Run id to steer. Thread id means its active run."),
    message: str = typer.Argument(..., help="Instruction to steer the run."),
) -> None:
    run_id = _target_run_id(ctx, run)
    _runtime_post(ctx, f"/api/v1/runs/{run_id}/steer", payload={"message": _message_payload(message)})
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
    run: str = typer.Argument(..., help="Run id to cancel. Thread id means its active run."),
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
    chat: Annotated[bool, typer.Option("--chat", help="Open chat on the rewound thread.")] = False,
) -> None:
    run_id = _target_latest_run_id(ctx, point)
    result = _runtime_post(ctx, f"/api/v1/runs/{run_id}/rewind", payload={})
    typer.echo(f"rewound {result.get('thread_id')} before {run_id}")
    if chat:
        thread = result.get("thread_id")
        _open_thread_ui(ctx, str(thread) if isinstance(thread, str) else _target_thread_id(ctx, point))


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
    chat: Annotated[bool, typer.Option("--chat", help="Open chat on the forked thread.")] = False,
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
    host: Annotated[str, typer.Option(help="Bind the agent API to this host.")] = "127.0.0.1",
    port: Annotated[int | None, typer.Option(help="Bind the agent API to this port.")] = None,
    components: Annotated[
        list[str] | None,
        typer.Option("--enable", help="Enable runtime components. Pass CSV or repeat."),
    ] = None,
    inboxes: Annotated[
        list[Path] | None,
        typer.Option("--inbox", help="Watch an inbox directory for file requests. Repeat to watch more than one."),
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
        with agents.resolved_run_target(root, selector, progress=as_progress_sink(progress)) as target:
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
    except (FileExistsError, FileNotFoundError, ValueError, click.ClickException) as exc:
        if not progress_finished:
            progress.finish(details=False)
        if isinstance(exc, click.ClickException):
            raise
        raise click.ClickException(str(exc)) from exc


def _active_run_error(status: agents.AgentStatus) -> str:
    message = f"Agent {status.name} already {status.status}"
    detail = (status.webui_url or status.api_url) if status.status == "running" else None
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
    agent_name = _required_prefix_agent(ctx, command_name=str(ctx.info_name or "runtime"))
    status = agents.get_agent_status(_context_root(ctx), agent_name, ui_base_url=_ui_base_url())
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
        raise click.ClickException(f"runtime request failed: {exc.code} {detail}") from exc
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


def _runtime_post(ctx: typer.Context, path: str, *, payload: dict[str, Any]) -> dict[str, Any]:
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
        raise click.ClickException(f"runtime request failed: {exc.code} {detail}") from exc
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
        steps_by_run = store.list_steps_for_runs(run_ids=tuple(item.run_id for item in ordered_runs))
        commands_by_run = {run.run_id: store.list_commands(run_id=run.run_id) for run in ordered_runs}
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
        return {"items": sorted(filtered, key=lambda item: str(item.get("updated_at", "")), reverse=True)}
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
        steps_by_run = store.list_steps_for_runs(run_ids=tuple(item.run_id for item in runs))
        commands_by_run = {run.run_id: store.list_commands(run_id=run.run_id) for run in runs}
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
        (item.message for item in reversed(detail.output.steps) if item.message is not None),
        None,
    )
    summary = message_summary(last_step_message.parts) if last_step_message is not None else input_text
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


def _inspect_detail(ctx: typer.Context, target: str, *, limit: int, include_thread: bool = True) -> dict[str, Any]:
    if target.startswith("run_"):
        run = _inspect_run_detail(ctx, target)
        info = _mapping(run.get("info"))
        thread_id = _text(info.get("thread_id"))
        thread = _inspect_thread_detail(ctx, thread_id, limit=limit) if include_thread and thread_id else None
        return {"kind": "run", "target": target, "run": run, "thread": thread}
    thread = _inspect_thread_detail(ctx, target, limit=limit)
    return {"kind": "thread", "target": target, "thread": thread}


def _inspect_run_detail(ctx: typer.Context, run_id: str) -> dict[str, Any]:
    return _runtime_json_or_offline(
        ctx,
        f"/api/v1/runs/{run_id}",
        lambda: _offline_run_detail_json(ctx, run_id),
    )


def _inspect_thread_detail(ctx: typer.Context, thread_id: str, *, limit: int) -> dict[str, Any]:
    return _runtime_json_or_offline(
        ctx,
        f"/api/v1/threads/{thread_id}?{urlencode({'limit': str(limit)})}",
        lambda: _offline_thread_detail_json(ctx, thread_id, limit=limit),
    )


def _inspect_events(ctx: typer.Context, target: str, *, limit: int) -> dict[str, Any]:
    if target.startswith("run_"):
        return _runtime_json_or_offline(
            ctx,
            f"/api/v1/runs/{target}/events?{urlencode({'limit': str(limit)})}",
            lambda: _offline_events_json(ctx, domain="run", domain_id=target, limit=limit),
        )
    return _runtime_json_or_offline(
        ctx,
        f"/api/v1/threads/{target}/events?{urlencode({'limit': str(limit)})}",
        lambda: _offline_events_json(ctx, domain="thread", domain_id=target, limit=limit),
    )


def _offline_run_detail_json(ctx: typer.Context, run_id: str) -> dict[str, Any] | None:
    store = _open_offline_execution_store(ctx)
    if store is None:
        return None
    try:
        run = store.get_run(run_id=run_id)
        if run is None:
            raise click.ClickException(f"run not found: {run_id}")
        return _run_detail_json(store, run)
    finally:
        store.close()


def _offline_thread_detail_json(ctx: typer.Context, thread_id: str, *, limit: int) -> dict[str, Any] | None:
    store = _open_offline_execution_store(ctx)
    if store is None:
        return None
    try:
        runs = store.list_thread_runs_chronological(thread_id=thread_id, limit=limit)
        thread_record = store.get_thread(thread_id=thread_id)
        if not runs and thread_record is None:
            raise click.ClickException(f"thread not found: {thread_id}")
        if runs:
            all_runs = store.list_thread_runs_chronological(thread_id=thread_id, limit=None)
            steps_by_run = store.list_steps_for_runs(run_ids=tuple(item.run_id for item in all_runs))
            commands_by_run = {run.run_id: store.list_commands(run_id=run.run_id) for run in all_runs}
            info = thread_info_from_runs(
                thread_id,
                all_runs,
                commands_by_run=commands_by_run,
                steps_by_run=steps_by_run,
                thread=thread_record,
            )
        else:
            info = thread_info_from_record(cast(Any, thread_record))
        return {
            "info": asdict(info),
            "runs": [_run_detail_json(store, run) for run in runs],
            "event_cursor": store.latest_event_cursor(domain="thread", domain_id=thread_id),
        }
    finally:
        store.close()


def _offline_events_json(ctx: typer.Context, *, domain: Literal["run", "thread"], domain_id: str, limit: int) -> dict[str, Any] | None:
    store = _open_offline_execution_store(ctx)
    if store is None:
        return None
    try:
        return {
            "cursor": store.latest_event_cursor(domain=domain, domain_id=domain_id),
            "items": [event_data(item) for item in store.list_events(domain=domain, domain_id=domain_id, limit=limit)],
        }
    finally:
        store.close()


def _run_detail_json(store: ExecutionStore, run: Any) -> dict[str, Any]:
    detail = run_detail_from_record(
        run,
        inputs=store.list_commands(run_id=run.run_id),
        steps=store.list_steps(run_id=run.run_id),
    )
    return {
        "info": asdict(detail.info),
        "input": detail.input.to_data() if detail.input is not None else None,
        "inputs": [
            {
                "record": asdict(item.record),
                "message": item.message.to_data() if item.message is not None else None,
            }
            for item in detail.inputs
        ],
        "output": {
            "status": detail.output.status,
            "error": detail.output.error,
            "steps": [
                {
                    "record": _step_record_json(item.record),
                    "message": item.message.to_data() if item.message is not None else None,
                }
                for item in detail.output.steps
            ],
        },
    }


def _step_record_json(step: Any) -> dict[str, Any]:
    return {
        "run_id": step.run_id,
        "step_index": step.step_index,
        "kind": step.kind,
        "status": step.status,
        "input": step_input_items_to_data(step.input),
        "output": parts_to_data(step.output),
        "payload": step_payload_to_data(step.payload),
        "error": step.error,
        "started_at": step.started_at,
        "finished_at": step.finished_at,
    }


def _render_inspect_tree(detail: Mapping[str, Any], *, verbosity: int = 0) -> None:
    if detail.get("kind") == "run":
        _render_inspect_run_focus(detail, verbosity=verbosity)
        return
    thread = _mapping(detail.get("thread"))
    _render_inspect_thread_summary(thread)


def _render_inspect_thread_summary(thread: Mapping[str, Any]) -> None:
    thread_info = _mapping(thread.get("info"))
    runs = [_mapping(item) for item in _list(thread.get("runs"))]
    top_runs = _inspect_top_level_runs(runs)
    typer.echo(f"thread {_text(thread_info.get('id')) or '-'}")
    if title := _text(thread_info.get("title")):
        typer.echo(f"title {title}")
    if status := _text(thread_info.get("status")):
        typer.echo(f"status {status}")
    if origin := _text(thread_info.get("origin")):
        typer.echo(f"origin {origin}")
    run_count = thread_info.get("run_count")
    if run_count is not None:
        top_count = len(top_runs)
        suffix = f", {top_count} top-level" if top_count != run_count else ""
        typer.echo(f"runs {run_count} total{suffix}")
    latest = _mapping(thread_info.get("latest_run"))
    latest_id = _text(latest.get("id"))
    if latest_id:
        latest_status = _display_run_status(latest.get("status"))
        typer.echo(f"latest {latest_id}{f' {latest_status}' if latest_status else ''}")
    if top_runs:
        typer.echo("")
    for index, run in enumerate(top_runs, start=1):
        _render_inspect_thread_run_summary(index, run)


def _inspect_top_level_runs(runs: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    roots = [run for run in runs if _text(_mapping(run.get("info")).get("parent_run_id")) is None]
    return roots or list(runs)


def _render_inspect_thread_run_summary(index: int, run: Mapping[str, Any]) -> None:
    info = _mapping(run.get("info"))
    output = _mapping(run.get("output"))
    label = _run_tree_label(info, output)
    typer.echo(f"{index}. {label}")
    if created_at := _text(info.get("created_at")):
        timing = created_at
        if finished_at := _text(info.get("finished_at")):
            timing = f"{timing} -> {finished_at}"
        typer.echo(f"   time {timing}")
    input_summary = _inspect_run_input_summary(run)
    if input_summary:
        typer.echo(f"   input {input_summary}")
    failure = _inspect_failure_summary(run)
    if failure:
        typer.echo(f"   failure {failure}")


def _render_inspect_run_focus(detail: Mapping[str, Any], *, verbosity: int) -> None:
    run = _mapping(detail.get("run"))
    info = _mapping(run.get("info"))
    output = _mapping(run.get("output"))
    thread = _mapping(detail.get("thread"))
    thread_info = _mapping(thread.get("info"))
    thread_id = _text(info.get("thread_id")) or _text(thread_info.get("id")) or "-"
    run_id = _text(info.get("id")) or "-"
    kind = _text(info.get("executable_kind")) or "run"
    target = executable_label(kind, _text(info.get("executable_name")), metadata=_mapping(info.get("metadata")))
    status = _display_run_status(output.get("status"))

    typer.echo(f"thread {thread_id}")
    typer.echo(f"run {run_id}")
    typer.echo(f"type {target}")
    typer.echo(f"status {status}")
    parent = _text(info.get("parent_run_id"))
    root = _text(info.get("root_run_id"))
    if root and root != run_id:
        typer.echo(f"root {root}")
    if parent:
        parent_step = info.get("parent_step_index")
        suffix = f" step {parent_step}" if parent_step is not None else ""
        typer.echo(f"parent {parent}{suffix}")
    input_summary = _inspect_run_input_summary(run)
    if input_summary:
        typer.echo(f"input {input_summary}")
    failure = _inspect_failure_summary(run)
    if failure:
        typer.echo(f"failure {failure}")
    run_by_id = _inspect_thread_run_map(thread, fallback=run)
    display_run = run_by_id.get(run_id, run)
    if kind == "flow":
        _render_flow_tree_node(display_run, run_by_id=run_by_id, verbosity=verbosity)
        return
    _render_inspect_run_steps_tree(display_run, verbosity=verbosity)


def _render_flow_tree_node(
    run: Mapping[str, Any],
    *,
    run_by_id: Mapping[str, Mapping[str, Any]],
    verbosity: int,
) -> None:
    info = _mapping(run.get("info"))
    output = _mapping(run.get("output"))
    typer.echo(f"- {_run_tree_label(info, output)}")
    stages, calls = project_flow_from_run(run, run_by_id=run_by_id)
    for stage in stages:
        typer.echo(_chat_flow_stage_line(stage, calls))
        for line in _chat_flow_stage_detail_lines(stage, calls, child_run_for=lambda call: call.run):
            typer.echo(line, color=True)
        typer.echo()


def _render_inspect_call(call: FlowCallView, *, indent: str, include_steps: bool) -> None:
    prefix = "✓" if call.status in {"succeeded", "done"} else "✗" if call.status == "failed" else "…"
    typer.echo(f"{indent}{prefix} {call.label} · {call.run_id} {call.status}")
    failure = _inspect_failure_summary(call.run or {})
    if failure:
        typer.echo(f"{indent}  failure {failure}")
    if not include_steps or call.run is None:
        return
    for step in _run_steps(call.run):
        record = _mapping(step.get("record"))
        typer.echo(f"{indent}  - {_tree_step_label(record, _mapping(step.get('message')))}")


def _render_inspect_run_steps_tree(run: Mapping[str, Any], *, verbosity: int) -> None:
    del verbosity
    for step in _run_steps(run):
        record = _mapping(step.get("record"))
        for line in _chat_child_completed_step_lines(record, run=None, indent=""):
            typer.echo(line, color=True)


def _inspect_thread_run_map(thread: Mapping[str, Any], *, fallback: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    runs = [_mapping(item) for item in _list(thread.get("runs"))]
    if not runs:
        runs = [fallback]
    result: dict[str, Mapping[str, Any]] = {}
    for run in runs:
        run_id = _text(_mapping(run.get("info")).get("id"))
        if run_id is not None:
            result[run_id] = run
    return result


def _inspect_run_input_summary(run: Mapping[str, Any]) -> str:
    input_message = _mapping(run.get("input"))
    return _message_summary(input_message)


def _inspect_failure_summary(run: Mapping[str, Any]) -> str:
    output = _mapping(run.get("output"))
    failure = _mapping(output.get("failure"))
    reason = _text(failure.get("reason")) or _text(output.get("error"))
    if not reason:
        return ""
    step_index = failure.get("step_index")
    step_kind = _text(failure.get("step_kind"))
    if step_index is not None and step_kind:
        return f"{reason} (step {step_index} {step_kind})"
    if step_index is not None:
        return f"{reason} (step {step_index})"
    return reason


def _inspect_step_detail_lines(record: Mapping[str, Any], message: Mapping[str, Any]) -> list[str]:
    lines: list[str] = []
    message_text = _message_summary(message)
    if message_text:
        lines.append(f"message {message_text}")
    tool_requests = _inspect_tool_request_lines(record)
    lines.extend(tool_requests)
    error = _text(record.get("error"))
    if error:
        lines.append(f"error {error}")
    return lines


def _inspect_tool_request_lines(record: Mapping[str, Any]) -> list[str]:
    lines: list[str] = []
    for part in _list(record.get("output")):
        typed = _mapping(part)
        if typed.get("type") != "tool_call":
            continue
        name = _text(typed.get("tool_name")) or _text(typed.get("tool_family")) or "tool"
        tool_input = typed.get("input")
        input_summary = _inspect_tool_input_summary(tool_input)
        suffix = f": {input_summary}" if input_summary else ""
        lines.append(f"requested {name}{suffix}")
    return lines


def _inspect_tool_input_summary(tool_input: object) -> str:
    if not isinstance(tool_input, Mapping) or not tool_input:
        return ""
    return ", ".join(f"{key}={_chat_plain_value(value)}" for key, value in tool_input.items())


def _render_run_tree_node(
    run: Mapping[str, Any],
    *,
    children: Mapping[str | None, list[Mapping[str, Any]]],
    children_by_step: Mapping[tuple[str, int], list[Mapping[str, Any]]],
    depth: int,
) -> None:
    info = _mapping(run.get("info"))
    output = _mapping(run.get("output"))
    run_id = _text(info.get("id")) or "-"
    indent = "  " * depth
    typer.echo(f"{indent}- {_run_tree_label(info, output)}")
    attached_child_ids: set[str] = set()
    for step in _run_steps(run):
        record = _mapping(step.get("record"))
        step_index = _int_or_none(record.get("step_index"))
        typer.echo(f"{'  ' * (depth + 1)}- {_tree_step_label(record, _mapping(step.get('message')))}")
        if step_index is None:
            continue
        for child in children_by_step.get((run_id, step_index), []):
            child_info = _mapping(child.get("info"))
            child_id = _text(child_info.get("id"))
            if child_id is not None:
                attached_child_ids.add(child_id)
            _render_run_tree_node(child, children=children, children_by_step=children_by_step, depth=depth + 2)
    for child in children.get(run_id, []):
        child_id = _text(_mapping(child.get("info")).get("id"))
        if child_id in attached_child_ids:
            continue
        _render_run_tree_node(child, children=children, children_by_step=children_by_step, depth=depth + 1)


def _run_tree_label(info: Mapping[str, Any], output: Mapping[str, Any]) -> str:
    pieces = [_text(info.get("id")) or "-"]
    pieces.append(
        executable_label(
            _text(info.get("executable_kind")) or "run",
            _text(info.get("executable_name")),
            metadata=_mapping(info.get("metadata")),
        )
    )
    pieces.append(_display_run_status(output.get("status")))
    call = _text(info.get("call_kind"))
    if call and call != "root":
        pieces.append(f"call={call}")
    parent_step = info.get("parent_step_index")
    if parent_step is not None:
        pieces.append(f"step={parent_step}")
    return " ".join(str(item) for item in pieces if item)


def _tree_step_label(record: Mapping[str, Any], message: Mapping[str, Any]) -> str:
    step_index = record.get("step_index")
    kind = _text(record.get("kind")) or "step"
    status = _text(record.get("status"))
    summary = _inspect_step_summary(record, message)
    pieces = [f"step {step_index}" if step_index is not None else "step", kind]
    if status:
        pieces.append(status)
    if summary and summary != "-":
        pieces.append(f"- {summary}")
    return " ".join(pieces)


def _render_inspect_steps(detail: Mapping[str, Any]) -> None:
    runs = _inspect_detail_runs(detail)
    rows: list[tuple[str, ...]] = []
    include_run = len(runs) > 1
    for run in runs:
        info = _mapping(run.get("info"))
        for step in _run_steps(run):
            record = _mapping(step.get("record"))
            row = (
                _text(info.get("id")) or "-",
                str(record.get("step_index", "")),
                _text(record.get("kind")) or "-",
                _text(record.get("status")) or "-",
                _inspect_step_summary(record, _mapping(step.get("message"))),
            )
            rows.append(row if include_run else row[1:])
    if include_run:
        _echo_table(("RUN", "STEP", "KIND", "STATUS", "SUMMARY"), rows)
    else:
        _echo_table(("STEP", "KIND", "STATUS", "SUMMARY"), rows)


def _render_inspect_events(ctx: typer.Context, target: str, *, limit: int, verbosity: int = 0) -> None:
    result = _inspect_events(ctx, target, limit=limit)
    rows = []
    for item in _list(result.get("items")):
        if not isinstance(item, Mapping):
            continue
        event_type = str(item.get("type", ""))
        payload = _mapping(item.get("payload"))
        if _hide_inspect_event(event_type, payload, verbosity=verbosity):
            continue
        rows.append(
            (
                str(item.get("cursor", "")),
                event_type,
                str(item.get("at", "")),
                _inspect_event_summary(event_type, payload, verbosity=verbosity),
            )
        )
    _echo_table(("#", "EVENT", "AT", "DETAIL"), rows)


def _inspect_detail_runs(detail: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    if detail.get("kind") == "run":
        return [_mapping(detail.get("run"))]
    thread = _mapping(detail.get("thread"))
    return [_mapping(item) for item in _list(thread.get("runs"))]


def _run_steps(run: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    output = _mapping(run.get("output"))
    return [_mapping(item) for item in _list(output.get("steps"))]


def _inspect_step_summary(record: Mapping[str, Any], message: Mapping[str, Any]) -> str:
    payload = _mapping(record.get("payload"))
    kind = _text(record.get("kind"))
    if kind == "model":
        model = _text(payload.get("model_ref")) or _text(payload.get("model"))
        text = _message_summary(message)
        requests = "; ".join(line.removeprefix("requested ") for line in _inspect_tool_request_lines(record))
        request_summary = f"requested {requests}" if requests else ""
        return " ".join(item for item in (model, text, request_summary) if item)
    if kind == "run":
        return child_call_summary(payload)
    if kind in {"step", "parallel", "bind"}:
        return flow_op_summary(payload)
    text = _parts_summary(record.get("output"))
    return text or _text(record.get("error")) or "-"


def _hide_inspect_event(event_type: str, payload: Mapping[str, Any], *, verbosity: int) -> bool:
    del event_type, payload, verbosity
    return False


def _inspect_event_summary(event_type: str, payload: Mapping[str, Any], *, verbosity: int = 0) -> str:
    if event_type in {"run_queued", "run_waiting"}:
        return _format_kv(
            (
                ("run", _text(payload.get("run_id"))),
                ("thread", _text(payload.get("thread_id"))),
                ("group", _text(payload.get("group"))),
                ("target", _event_executable_label(payload)),
                ("waiting", _text(payload.get("waiting_for"))),
                ("position", payload.get("position")),
            )
        )
    if event_type == "run_start":
        return _format_kv(
            (
                ("run", _text(payload.get("run_id"))),
                ("kind", _text(payload.get("executable_kind"))),
                ("target", _event_executable_label(payload)),
                ("call", _text(payload.get("call_kind"))),
                ("parent", _text(payload.get("parent_run_id"))),
                ("step", payload.get("parent_step_index")),
            )
        )
    if event_type == "run_command":
        text = _message_summary(_mapping(payload.get("message")))
        return _format_kv((("kind", _input_action_label(_text(payload.get("kind")))), ("text", text)))
    if event_type == "step_start":
        return _inspect_event_step_summary(payload, status=None, verbosity=verbosity)
    if event_type == "part_start":
        return _format_kv((("part", _event_part_label(payload)),))
    if event_type == "part_delta":
        delta = _mapping(payload.get("delta"))
        text = _text(delta.get("text"))
        return _format_kv((("part", _event_part_label(payload)), ("text", _truncate_table_text(text, width=72))))
    if event_type == "part_end":
        part = _mapping(payload.get("part"))
        text = _parts_summary([part])
        return _format_kv((("part", _event_part_label(payload, part=part)), ("text", text)))
    if event_type == "step_end":
        status = _text(payload.get("status")) or "finished"
        error = _text(payload.get("error"))
        return _inspect_event_step_summary(payload, status=status, verbosity=verbosity, error=error)
    if event_type == "run_end":
        status = _display_run_status(payload.get("status"))
        error = _text(payload.get("error"))
        return _format_kv((("run", _text(payload.get("run_id"))), ("status", status), ("error", error)))
    message = _mapping(payload.get("message"))
    if message:
        return _format_kv((("text", _message_summary(message)),))
    if payload.get("delta") is not None:
        return _format_kv((("delta", json.dumps(payload.get("delta"), ensure_ascii=False, separators=(",", ":"))),))
    return _format_kv(
        (
            ("run", _text(payload.get("run_id"))),
            ("thread", _text(payload.get("thread_id"))),
            ("kind", _text(payload.get("kind"))),
            ("status", _text(payload.get("status"))),
        )
    )


def _inspect_event_step_summary(
    payload: Mapping[str, Any],
    *,
    status: str | None,
    verbosity: int,
    error: str | None = None,
) -> str:
    kind = _text(payload.get("kind")) or "step"
    step_index = payload.get("step_index")
    step_payload = _mapping(payload.get("payload"))
    if not step_payload:
        step_payload = _mapping(payload.get("metadata"))
    if not step_payload:
        return _format_kv(
            (
                ("step", step_index),
                ("kind", kind),
                ("status", _event_status_value(status)),
                ("error", error),
            )
        )
    if kind in {"step", "parallel", "bind"}:
        op = _text(step_payload.get("op"))
        stage_fields = _event_stage_fields(step_payload)
        output = _event_output_shape(payload)
        return _format_kv(
            (
                ("step", step_index),
                ("kind", kind),
                ("op", _event_flow_phase(op)),
                *stage_fields,
                ("shape", output),
                ("status", _event_status_value(status)),
                ("error", error),
            )
        )
    if kind == "run":
        return _format_kv(
            (
                ("step", step_index),
                ("kind", kind),
                *_event_child_call_fields(step_payload),
                ("status", _event_status_value(status)),
                ("error", error),
            )
        )
    if kind == "model":
        model = _text(step_payload.get("model_ref")) or _text(step_payload.get("model"))
        text = _parts_summary(payload.get("output"))
        return _format_kv(
            (
                ("step", step_index),
                ("kind", kind),
                ("model", model),
                ("output", text),
                ("status", _event_status_value(status)),
                ("error", error),
            )
        )
    return _format_kv(
        (
            ("step", step_index),
            ("kind", kind),
            ("status", _event_status_value(status)),
            ("error", error),
        )
    )


def _event_stage_fields(payload: Mapping[str, Any]) -> tuple[tuple[str, object], ...]:
    ctx = flow_stage_context(payload)
    index = _int_or_none(ctx.get("stage_index"))
    kind = _text(ctx.get("stage_kind")) or "stage"
    title = _text(ctx.get("stage_title")) or _text(ctx.get("stage_doc")) or _text(ctx.get("stage_target"))
    return (
        ("stage", index + 1 if index is not None else None),
        ("stage_kind", kind),
        ("title", _truncate_table_text(title, width=56) if title else None),
    )


def _event_flow_phase(op: str | None) -> str:
    if op is None:
        return "op"
    if op.startswith("prepare_"):
        return "prepare"
    if op == "set_current":
        return "done"
    return op


def _event_output_shape(payload: Mapping[str, Any]) -> str | None:
    step_payload = _mapping(payload.get("payload"))
    preview = step_payload.get("output_preview")
    if preview is None:
        return None
    return shape_label(preview, fallback_count=output_count(payload))


def _event_child_call_fields(payload: Mapping[str, Any]) -> tuple[tuple[str, object], ...]:
    target = executable_label(
        _text(payload.get("target_kind")) or "run",
        _text(payload.get("target")),
        metadata=_mapping(payload.get("metadata")),
    ).replace(":", " ", 1)
    ctx = flow_stage_context(payload)
    lane_index = _int_or_none(ctx.get("lane_index"))
    parallelism = _int_or_none(ctx.get("parallelism"))
    item_index = _int_or_none(ctx.get("item_index"))
    item_count = _int_or_none(ctx.get("item_count"))
    children = ", ".join(child_run_ids(payload, {}))
    return (
        ("stage", (_int_or_none(ctx.get("stage_index")) or 0) + 1 if ctx.get("stage_index") is not None else None),
        ("target", target),
        ("item", f"{item_index + 1}/{item_count}" if item_index is not None and item_count else item_index + 1 if item_index is not None else None),
        ("lane", f"{lane_index + 1}/{parallelism}" if lane_index is not None and parallelism and parallelism > 1 else None),
        ("child", children),
    )


def _event_status_value(status: str | None) -> str | None:
    if status is None:
        return None
    if status == "finished":
        return None
    return status


def _format_kv(items: Sequence[tuple[str, object]]) -> str:
    parts = []
    for key, value in items:
        if value is None or value == "":
            continue
        parts.append(f"{key}: {_format_kv_value(value)}")
    return ", ".join(parts) if parts else "-"


def _format_kv_value(value: object) -> str:
    if isinstance(value, str):
        if not value:
            return value
        if any(char in value for char in (",", "{", "}", "[", "]")):
            return json.dumps(value, ensure_ascii=False)
        return value
    return str(value)


def _event_executable_label(payload: Mapping[str, Any]) -> str:
    kind = _text(payload.get("executable_kind")) or "run"
    name = _text(payload.get("executable_name"))
    return executable_label(kind, name, metadata=_mapping(payload.get("metadata")))


def _event_step_label(payload: Mapping[str, Any]) -> str:
    step_index = payload.get("step_index")
    kind = _text(payload.get("kind"))
    pieces = ["step", str(step_index) if step_index is not None else "?"]
    if kind:
        pieces.append(kind)
    return " ".join(pieces)


def _event_part_label(payload: Mapping[str, Any], *, part: Mapping[str, Any] | None = None) -> str:
    step_index = payload.get("step_index")
    part_index = payload.get("part_index")
    kind = _text(payload.get("kind"))
    if part is not None:
        kind = _text(part.get("type")) or kind
    pieces = ["step", str(step_index) if step_index is not None else "?"]
    if kind:
        pieces.append(kind)
    pieces.extend(["part", str(part_index) if part_index is not None else "?"])
    return " ".join(pieces)


def _input_action_label(action: str | None) -> str:
    if action == "start":
        return "input"
    if action == "steer":
        return "steer"
    if action == "stop":
        return "stop"
    return action or "input"


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
    agent_name = _required_prefix_agent(ctx, command_name=str(ctx.info_name or "runtime"))
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
        raise click.ClickException(f"runtime request failed: {exc.code} {detail}") from exc
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
        raise click.ClickException(f"runtime request failed: {exc.code} {detail}") from exc
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
        raise click.ClickException(f"runtime request failed: {exc.code} {detail}") from exc
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
    return urlencode([(key, value) for key, value in items.items() if value is not None])


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
        _chat_interactive_scripted(ctx, thread_id=thread_id, selector_payload=selector_payload)
        return
    _chat_interactive_prompt_toolkit(ctx, thread_id=thread_id, selector_payload=selector_payload)


def _chat_input_history_store(ctx: typer.Context) -> ChatInputHistoryStore | None:
    try:
        agent = _context_agent(ctx)
        root = _context_root(ctx)
    except (AttributeError, KeyError, TypeError):
        return None
    if not agent:
        return None
    return ChatInputHistoryStore(agents.agent_room(root, agent) / "chat-input-history.jsonl")


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


@dataclass(frozen=True, slots=True)
class _ChatUIEvent:
    type: str
    value: str | dict[str, Any] | None = None


@dataclass(slots=True)
class _ChatStep:
    index: int
    kind: str
    label: str
    payload: dict[str, Any] = field(default_factory=dict)
    frame: int = 0


@dataclass(frozen=True, slots=True)
class _ChatToolCall:
    name: str
    input: dict[str, Any]


@dataclass(slots=True)
class _ChatQueueItem:
    kind: Literal["run", "steer"]
    text: str


@dataclass(slots=True)
class _ChatRun:
    run_id: str
    message: str
    status: str
    executable_kind: str = "thunk"
    executable_name: str | None = None
    accept_child_trace: bool = False
    queue_state: str | None = None
    waiting_for: str | None = None
    queue_position: int | None = None
    cancel_requested: bool = False
    cancel_sent_run_id: str | None = None
    started: bool = False
    steps: dict[int, _ChatStep] = field(default_factory=dict)
    completed_steps: dict[int, dict[str, Any]] = field(default_factory=dict)
    tool_calls_by_part: dict[tuple[int, int], _ChatToolCall] = field(default_factory=dict)
    commands: dict[int, dict[str, Any]] = field(default_factory=dict)
    timeline: list[tuple[Literal["step", "command"], int]] = field(default_factory=list)
    child_runs: dict[str, _ChatRun] = field(default_factory=dict)

    def start_step(self, payload: dict[str, Any]) -> None:
        index = _chat_step_index(payload)
        self.remember_timeline("step", index)
        stored_payload = dict(payload)
        if stored_payload.get("kind") in {"step", "parallel", "bind", "run"} and "payload" not in stored_payload:
            stored_payload["payload"] = dict(_mapping(stored_payload.get("metadata")))
        self.steps[index] = _ChatStep(
            index,
            str(stored_payload.get("kind") or "unknown"),
            _chat_step_label(stored_payload, self),
            stored_payload,
        )

    def complete_step(self, payload: dict[str, Any]) -> None:
        index = _chat_step_index(payload)
        self.remember_timeline("step", index)
        completed_payload = dict(payload)
        active_step = self.steps.get(index)
        if active_step is not None and "input" not in completed_payload and "input" in active_step.payload:
            completed_payload["input"] = active_step.payload["input"]
        self.completed_steps[index] = completed_payload
        self.steps.pop(index, None)

    def record_part(self, payload: dict[str, Any]) -> None:
        part = _mapping(payload.get("part"))
        if part.get("type") != "tool_call":
            return
        tool_name = _text(part.get("tool_name")) or _text(part.get("tool_family"))
        if tool_name is None:
            return
        step_index = _chat_step_index(payload)
        part_index = _chat_part_index(payload)
        tool_input = part.get("input")
        self.tool_calls_by_part[(step_index, part_index)] = _ChatToolCall(
            name=tool_name,
            input=dict(tool_input) if isinstance(tool_input, Mapping) else {},
        )

    def record_command(self, payload: Mapping[str, Any]) -> None:
        index = _chat_command_index(payload)
        self.remember_timeline("command", index)
        self.commands[index] = dict(payload)

    def remember_timeline(self, kind: Literal["step", "command"], index: int) -> None:
        item = (kind, index)
        if item not in self.timeline:
            self.timeline.append(item)

    def update_queue(self, event_type: str, payload: Mapping[str, Any]) -> None:
        if run_id := _text(payload.get("run_id")):
            self.run_id = run_id
        if self.cancel_requested:
            self.status = "canceling"
            self.queue_state = None
            self.waiting_for = None
            self.queue_position = None
            return
        self.status = "waiting" if event_type == "run_waiting" else "queued"
        self.queue_state = self.status
        self.waiting_for = _text(payload.get("waiting_for"))
        self.queue_position = _int_or_none(payload.get("position"))

    def mark_running(self) -> None:
        self.started = True
        self.status = "canceling" if self.cancel_requested else "running"
        self.queue_state = None
        self.waiting_for = None
        self.queue_position = None

    def request_cancel(self) -> None:
        self.cancel_requested = True
        self.status = "canceling"
        self.queue_state = None
        self.waiting_for = None
        self.queue_position = None

    def clear_cancel_request(self) -> None:
        self.cancel_requested = False
        if self.status == "canceling":
            self.status = "running" if self.started else "submitting"

    def start_child_run(self, payload: Mapping[str, Any]) -> None:
        run_id = _text(payload.get("run_id"))
        if run_id is None:
            return
        child = self.child_runs.get(run_id)
        if child is None:
            child = _ChatRun(
                run_id=run_id,
                message=_event_message_text(payload.get("input")),
                status="running",
                executable_kind=_text(payload.get("executable_kind")) or "thunk",
                executable_name=_text(payload.get("executable_name")),
                accept_child_trace=True,
            )
            self.child_runs[run_id] = child
        else:
            child.status = "running"
            child.executable_kind = _text(payload.get("executable_kind")) or child.executable_kind
            child.executable_name = _text(payload.get("executable_name")) or child.executable_name

    def child_run(self, run_id: str | None) -> _ChatRun | None:
        if run_id is None:
            return None
        return self.child_runs.get(run_id)

    def tick(self) -> None:
        for step in self.steps.values():
            step.frame += 1

    def step_indexes(self) -> list[int]:
        return sorted(set(self.steps) | set(self.completed_steps))


class _ChatLastRunPanel:
    def __init__(self, get_run: Callable[[], _ChatRun | None]) -> None:
        self.get_run = get_run
        self.user_view = FormattedTextControl(self.render_user)
        self.activity_view = FormattedTextControl(self.render_activity)

    def container(self) -> ConditionalContainer:
        return ConditionalContainer(
            HSplit(
                [
                    Window(
                        self.user_view,
                        height=self.user_rows,
                        wrap_lines=False,
                        always_hide_cursor=True,
                        style="class:input",
                        char=" ",
                    ),
                    Window(
                        self.activity_view,
                        height=self.activity_rows,
                        wrap_lines=False,
                        always_hide_cursor=True,
                        style="class:last-run",
                    ),
                ],
                height=self.height_dimension,
                window_too_small=Window(style="class:last-run", always_hide_cursor=True),
            ),
            filter=Condition(lambda: bool(self.lines())),
        )

    def render_user(self) -> ANSI:
        return ANSI("\n".join(self.user_lines()))

    def render_activity(self) -> list[tuple[str, str]]:
        return _chat_activity_formatted_text(self.activity_lines())

    def lines(self) -> list[str]:
        return [*self.user_lines(), *self.activity_lines()]

    def user_lines(self) -> list[str]:
        run = self.get_run()
        if run is None:
            return []
        return _chat_panel_user_block(run)

    def activity_lines(self) -> list[str]:
        run = self.get_run()
        if run is None:
            return []
        lines = _chat_run_activity_lines(run, self.step_line)
        return ["", *_chat_tail_activity_lines(lines, max_lines=_CHAT_MAX_ACTIVE_RUN_ACTIVITY_ROWS), ""]

    def step_line(self, run: _ChatRun, index: int) -> str:
        if index in run.completed_steps:
            return _chat_completed_step_line(run.completed_steps[index], run=run)
        return _chat_active_step_line(run.steps[index])

    def rows(self) -> int:
        return len(self.lines())

    def height_dimension(self) -> Dimension:
        return Dimension(min=0, preferred=self.rows(), weight=1)

    def user_rows(self) -> int:
        return len(self.user_lines())

    def activity_rows(self) -> int:
        return len(self.activity_lines())


class _ChatSubmissionQueue:
    def __init__(self, get_items: Callable[[], list[_ChatQueueItem]]) -> None:
        self.get_items = get_items
        self.view = FormattedTextControl(self.render)

    def container(self) -> ConditionalContainer:
        return ConditionalContainer(
            VSplit(
                [
                    Window(width=2),
                    Window(
                        self.view,
                        height=self.rows,
                        wrap_lines=False,
                        always_hide_cursor=True,
                        style="class:queue",
                        char=" ",
                    ),
                    Window(width=2),
                ],
                height=self.height_dimension,
            ),
            filter=Condition(lambda: bool(self.get_items())),
        )

    def render(self) -> ANSI:
        return ANSI("\n".join(self.lines()))

    def lines(self) -> list[str]:
        items = self.get_items()
        indexed = list(enumerate(items, 1))
        shown = indexed[:_CHAT_MAX_QUEUE_ROWS]
        hidden = len(items) - len(shown)
        summary = "  queued for submission:"
        if hidden:
            summary += f" ({hidden} more not shown)"
        return [summary, *[f"  [{index}] {_chat_summarize(item.text)}" for index, item in shown]]

    def rows(self) -> int:
        return len(self.lines()) if self.get_items() else 0

    def height_dimension(self) -> Dimension:
        return Dimension(min=0, preferred=self.rows(), weight=1)


class _ChatPromptBox:
    def __init__(
        self,
        emit: Callable[[_ChatUIEvent], None],
        invalidate: Callable[[], None],
        status_label: str,
        *,
        history_store: ChatInputHistoryStore | None = None,
    ) -> None:
        self.emit = emit
        self.invalidate = invalidate
        self.status_label = status_label
        self.history = InMemoryHistory()
        self.history_store = history_store
        for entry in history_store.load() if history_store is not None else ():
            self.history.append_string(entry)
        self.buffer = Buffer(multiline=True, history=self.history)
        self.error_message = ""
        self.history_index: int | None = None
        self.history_draft = ""
        self.status = FormattedTextControl(self.render_status)
        self.buffer.on_text_changed += self.handle_text_changed

    def container(self) -> HSplit:
        return HSplit(
            [
                Window(height=1, style="class:input", always_hide_cursor=True, char=" "),
                VSplit(
                    [
                        Window(FormattedTextControl(ANSI(f"{_CHAT_DIM}> {_CHAT_NORMAL_INTENSITY}")), width=2, style="class:input", char=" "),
                        Window(
                            BufferControl(buffer=self.buffer),
                            height=self.input_rows,
                            wrap_lines=True,
                            style="class:input",
                            char=" ",
                        ),
                    ],
                    height=self.input_rows,
                    style="class:input",
                ),
                Window(height=1, style="class:input", always_hide_cursor=True, char=" "),
                Window(self.status, height=1, style="class:status", always_hide_cursor=True, char=" "),
            ],
            height=self.height_dimension,
            window_too_small=self.compact_container(),
        )

    def compact_container(self) -> VSplit:
        return VSplit(
            [
                Window(FormattedTextControl(ANSI(f"{_CHAT_DIM}> {_CHAT_NORMAL_INTENSITY}")), width=2, style="class:input", char=" "),
                Window(
                    BufferControl(buffer=self.buffer),
                    height=1,
                    wrap_lines=False,
                    style="class:input",
                    char=" ",
                ),
            ],
            height=1,
            style="class:input",
        )

    def render_status(self) -> list[tuple[str, str]]:
        if self.error_message:
            return [("class:status.error", f"  ! {self.error_message}  ")]
        segments = _chat_status_segments(self.status_label)
        if segments:
            style, text = segments[0]
            segments[0] = (style, f"  {text}")
        return [
            *segments,
            ("class:status.text", "  ^c cancel  ^d exit  ^j newline  ↑↓ history"),
            ("class:status.text", "  "),
        ]

    def bind(self, keys: KeyBindings) -> None:
        @keys.add("enter")
        def submit(_event: Any) -> None:
            message = self.buffer.text.strip()
            if not message:
                return
            self.record_history(message)
            self.buffer.text = ""
            self.history_index = None
            self.history_draft = ""
            self.emit(_ChatUIEvent("submit", message))
            self.invalidate()

        @keys.add("c-c")
        def interrupt(_event: Any) -> None:
            self.emit(_ChatUIEvent("interrupt"))

        @keys.add("c-d")
        def eof(_event: Any) -> None:
            self.emit(_ChatUIEvent("eof"))

        @keys.add("c-q")
        def quit_app(_event: Any) -> None:
            self.emit(_ChatUIEvent("quit"))

        @keys.add("c-l")
        def clear_screen(_event: Any) -> None:
            self.emit(_ChatUIEvent("clear"))

        @keys.add("c-j")
        @keys.add("escape", "enter")
        def insert_newline(_event: Any) -> None:
            self.insert_newline()

        @keys.add("escape", "escape")
        def cancel_run(_event: Any) -> None:
            self.emit(_ChatUIEvent("cancel"))

        @keys.add("up")
        @keys.add("c-p")
        def previous_history(_event: Any) -> None:
            self.previous_history()

        @keys.add("down")
        @keys.add("c-n")
        def next_history(_event: Any) -> None:
            self.next_history()

        try:
            keys.add("s-enter")(lambda _event: self.insert_newline())
        except ValueError:
            pass

    def insert_newline(self) -> None:
        self.buffer.insert_text("\n")
        self.invalidate()

    def has_input(self) -> bool:
        return bool(self.buffer.text)

    def clear_input(self) -> None:
        if not self.buffer.text:
            return
        self.buffer.text = ""
        self.history_index = None
        self.history_draft = ""
        self.invalidate()

    def record_history(self, message: str) -> None:
        entries = self.history_entries()
        if not entries or entries[-1] != message:
            self.history.append_string(message)
            if self.history_store is not None:
                try:
                    self.history_store.append(message)
                except OSError:
                    pass

    def previous_history(self) -> None:
        if self.buffer.document.cursor_position_row > 0:
            self.buffer.cursor_up()
            return
        entries = self.history_entries()
        if not entries:
            return
        if self.history_index is None:
            self.history_draft = self.buffer.text
            self.history_index = len(entries) - 1
        else:
            self.history_index = max(0, self.history_index - 1)
        self.replace_input(entries[self.history_index])

    def next_history(self) -> None:
        if self.buffer.document.cursor_position_row < self.buffer.document.line_count - 1:
            self.buffer.cursor_down()
            return
        if self.history_index is None:
            return
        entries = self.history_entries()
        if self.history_index < len(entries) - 1:
            self.history_index += 1
            self.replace_input(entries[self.history_index])
        else:
            self.history_index = None
            self.replace_input(self.history_draft)
            self.history_draft = ""

    def history_entries(self) -> list[str]:
        return list(self.history.get_strings())

    def replace_input(self, text: str) -> None:
        self.buffer.text = text
        self.buffer.cursor_position = len(text)
        self.invalidate()

    def handle_text_changed(self, _buffer: Buffer) -> None:
        if self.error_message:
            self.error_message = ""
        if self.history_index is not None:
            entries = self.history_entries()
            if self.buffer.text != entries[self.history_index]:
                self.history_index = None
                self.history_draft = ""

    def set_error(self, message: str) -> None:
        self.error_message = message
        self.invalidate()

    def clear_error(self) -> None:
        if self.error_message:
            self.error_message = ""
            self.invalidate()

    def input_rows(self) -> int:
        return min(_CHAT_MAX_INPUT_ROWS, max(1, self.buffer.document.line_count))

    def rows(self) -> int:
        return self.input_rows() + 3

    def height_dimension(self) -> Dimension:
        return Dimension(min=1, preferred=self.rows(), weight=8)


class _ChatBottomApp:
    """Use terminal scrollback for history and prompt-toolkit for the bottom UI."""

    def __init__(self, ctx: typer.Context, *, thread_id: str | None, selector_payload: dict[str, object]) -> None:
        self.ctx = ctx
        self.thread_id = thread_id
        self.selector_payload = selector_payload
        self.events: asyncio.Queue[_ChatUIEvent] = asyncio.Queue()
        self.pending: list[_ChatQueueItem] = []
        self.active_run: _ChatRun | None = None
        self.local_streaming = threading.Event()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.dispatcher: asyncio.Task[None] | None = None
        self.ticker: asyncio.Task[None] | None = None
        self.stream_step_index: int | None = None
        self.stream_text_parts: list[str] = []
        self.stream_tool_steps: dict[str, int] = {}
        self.model_label = _chat_resolved_model_label(ctx, self.selector_payload)
        self.home_label = _chat_home_label(ctx)

        self.last_run_panel = _ChatLastRunPanel(lambda: self.active_run)
        self.queue_panel = _ChatSubmissionQueue(lambda: self.pending)
        self.prompt = _ChatPromptBox(
            self.emit,
            self.invalidate,
            self.status_label(),
            history_store=_chat_input_history_store(ctx),
        )
        self.app = Application(
            layout=self.build_layout(),
            key_bindings=self.build_keys(),
            style=self.build_style(),
            full_screen=False,
            erase_when_done=True,
            mouse_support=False,
        )

    def status_label(self) -> str:
        model_label = self.model_label
        executable_label = _chat_executable_status_label(self.selector_payload)
        if executable_label:
            return f"{model_label}  {executable_label}"
        return model_label

    def build_layout(self) -> Layout:
        root = HSplit(
            [
                self.last_run_panel.container(),
                self.queue_panel.container(),
                self.prompt.container(),
            ],
            height=self.bottom_dimension,
            window_too_small=self.prompt.compact_container(),
        )
        return Layout(root, focused_element=self.prompt.buffer)

    def build_keys(self) -> KeyBindings:
        keys = KeyBindings()
        self.prompt.bind(keys)
        return keys

    def emit(self, event: _ChatUIEvent) -> None:
        self.events.put_nowait(event)

    def emit_from_thread(self, event: _ChatUIEvent) -> None:
        if self.loop is None:
            return
        self.loop.call_soon_threadsafe(self.events.put_nowait, event)

    def invalidate(self) -> None:
        if hasattr(self, "app"):
            self.app.invalidate()

    def build_style(self) -> Style:
        return Style.from_dict(_chat_ui_palette())

    def bottom_rows(self) -> int:
        return self.last_run_panel.rows() + self.queue_panel.rows() + self.prompt.rows()

    def bottom_dimension(self) -> Dimension:
        return Dimension(min=1, preferred=self.bottom_rows(), weight=1)

    async def run(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.print_header()
        self.dispatcher = asyncio.create_task(self.dispatch_events())
        self.ticker = asyncio.create_task(self.emit_ticks())
        try:
            with patch_stdout(raw=True):
                await self.app.run_async()
        finally:
            self.events.put_nowait(_ChatUIEvent("quit"))
            await self.stop_tasks()

    async def stop_tasks(self) -> None:
        if self.ticker and not self.ticker.done():
            self.ticker.cancel()
        if self.dispatcher and not self.dispatcher.done():
            await self.dispatcher

    async def emit_ticks(self) -> None:
        while True:
            await asyncio.sleep(0.12)
            if self.active_run and self.active_run.steps:
                self.events.put_nowait(_ChatUIEvent("tick"))

    async def dispatch_events(self) -> None:
        while True:
            event = await self.events.get()
            try:
                if event.type == "submit":
                    self.handle_submit(str(event.value))
                elif event.type == "runtime" and isinstance(event.value, dict):
                    self.handle_runtime_event(event.value)
                elif event.type == "error":
                    self.handle_error(str(event.value or "runtime request failed"))
                elif event.type == "cancel_error":
                    self.handle_cancel_error(str(event.value or "cancel request failed"))
                elif event.type == "tick":
                    self.handle_tick()
                elif event.type == "interrupt":
                    self.handle_interrupt()
                elif event.type == "eof":
                    self.handle_eof()
                elif event.type == "cancel":
                    self.handle_cancel()
                elif event.type == "clear":
                    self.handle_clear()
                elif event.type == "quit":
                    self.handle_quit()
                    return
            finally:
                self.events.task_done()

    def handle_submit(self, message: str) -> None:
        self.prompt.clear_error()
        if self.handle_local_command(message):
            self.app.invalidate()
            return
        if self.has_active_run():
            self.pending.append(_ChatQueueItem(kind="run", text=message))
        else:
            self.start_run(message)
        self.app.invalidate()

    def handle_clear(self) -> None:
        if self.has_active_run():
            self.prompt.set_error("Cannot clear while a run is active.")
            return
        self.prompt.clear_error()
        self.app.renderer.clear()
        self.app.invalidate()

    def handle_interrupt(self) -> None:
        if self.prompt.has_input():
            self.prompt.clear_input()
            return
        if self.has_active_run():
            self.handle_cancel()
            return
        self.prompt.clear_error()
        self.app.invalidate()

    def handle_eof(self) -> None:
        if not self.prompt.has_input() and not self.has_active_run():
            self.handle_quit()
            return
        self.app.invalidate()

    def handle_cancel(self) -> None:
        if not self.has_active_run():
            return
        if self.active_run is None or self.active_run.cancel_requested:
            return
        self.prompt.clear_error()
        self.active_run.request_cancel()
        self.maybe_send_cancel_request()
        self.app.invalidate()

    def handle_quit(self) -> None:
        if self.app.is_running:
            self.app.exit()

    def handle_tick(self) -> None:
        if self.active_run and self.active_run.steps:
            self.active_run.tick()
            self.app.invalidate()

    def handle_error(self, message: str) -> None:
        friendly = _chat_friendly_error(message)
        if self.active_run:
            self.active_run.status = "error"
            _chat_record_system_event(self.active_run, f"error: {friendly}", clear_active=True)
            self.print_run(self.active_run)
        else:
            self.prompt.set_error(friendly)
        self.active_run = None
        self.start_next_run()
        self.app.invalidate()

    def handle_cancel_error(self, message: str) -> None:
        friendly = _chat_friendly_error(message)
        if self.active_run is None:
            self.prompt.set_error(friendly)
            self.app.invalidate()
            return
        self.active_run.clear_cancel_request()
        _chat_record_system_event(self.active_run, f"error: cancel failed: {friendly}", clear_active=False)
        self.app.invalidate()

    def maybe_send_cancel_request(self) -> None:
        run = self.active_run
        if run is None or not run.cancel_requested or not run.started or not run.run_id:
            return
        if run.cancel_sent_run_id == run.run_id:
            return
        run.cancel_sent_run_id = run.run_id
        self.send_cancel_request(run.run_id)

    def send_cancel_request(self, run_id: str) -> None:
        def consume() -> None:
            try:
                _runtime_post(
                    self.ctx,
                    f"/api/v1/runs/{run_id}/cancel",
                    payload={},
                )
            except click.ClickException as exc:
                self.emit_from_thread(_ChatUIEvent("cancel_error", exc.message))
            except Exception as exc:  # pragma: no cover - defensive cross-thread reporting
                self.emit_from_thread(_ChatUIEvent("cancel_error", f"{type(exc).__name__}: {exc}"))

        threading.Thread(target=consume, daemon=True).start()

    def handle_runtime_event(self, event: dict[str, Any]) -> None:
        event_type = str(event.get("type") or event.get("event_type") or "")
        if self.handle_chat_stream_event(event_type, event):
            self.app.invalidate()
            return
        payload = event.get("payload")
        if not isinstance(payload, dict):
            return
        if self.handle_child_trace_event(event_type, payload):
            self.app.invalidate()
            return
        if self.should_ignore_trace_event(event_type, payload):
            return
        if event_type in {"run_queued", "run_waiting"}:
            self.handle_queue_event(event_type, payload)
        elif event_type == "run_command":
            self.handle_run_command(payload)
        elif event_type == "run_start":
            self.handle_run_start(payload)
        elif event_type == "step_start" and self.active_run:
            self.active_run.start_step(payload)
        elif event_type == "part_end" and self.active_run:
            self.active_run.record_part(payload)
        elif event_type == "step_end" and self.active_run:
            self.active_run.complete_step(payload)
        elif event_type == "run_end":
            self.finish_run(payload)
        self.app.invalidate()

    def should_ignore_trace_event(self, event_type: str, payload: Mapping[str, Any]) -> bool:
        run_id = _text(payload.get("run_id"))
        if event_type in {"run_queued", "run_waiting"}:
            return self.active_run is not None and bool(self.active_run.run_id) and run_id is not None and run_id != self.active_run.run_id
        if event_type == "run_command":
            return self.active_run is not None and bool(self.active_run.run_id) and run_id != self.active_run.run_id
        if event_type == "run_start":
            parent_run_id = _text(payload.get("parent_run_id"))
            call_kind = _text(payload.get("call_kind")) or "top"
            if parent_run_id or call_kind != "top":
                return True
        if event_type in {"run_start", "step_start", "part_end", "step_end", "run_end"}:
            return self.active_run is not None and bool(self.active_run.run_id) and run_id != self.active_run.run_id
        return False

    def handle_queue_event(self, event_type: str, payload: Mapping[str, Any]) -> None:
        if self.active_run is None:
            self.active_run = _ChatRun(
                run_id=_text(payload.get("run_id")) or "",
                message="",
                status="queued",
                accept_child_trace=True,
            )
        self.active_run.update_queue(event_type, payload)

    def handle_child_trace_event(self, event_type: str, payload: Mapping[str, Any]) -> bool:
        if self.active_run is None or not self.active_run.accept_child_trace:
            return False
        run_id = _text(payload.get("run_id"))
        if event_type == "run_start":
            parent_run_id = _text(payload.get("parent_run_id"))
            root_run_id = _text(payload.get("root_run_id"))
            if parent_run_id == self.active_run.run_id or root_run_id == self.active_run.run_id or parent_run_id in self.active_run.child_runs:
                self.active_run.start_child_run(payload)
                return True
            return False
        child = self.active_run.child_run(run_id)
        if child is None:
            return False
        if event_type == "step_start":
            child.start_step(dict(payload))
            return True
        if event_type == "part_end":
            child.record_part(dict(payload))
            return True
        if event_type == "step_end":
            child.complete_step(dict(payload))
            return True
        if event_type == "run_end":
            child.status = _display_run_status(payload.get("status")) or "completed"
            error = _text(payload.get("error"))
            if child.status in {"failed", "error", "canceled", "cancelled"}:
                message = _chat_stopped_run_message(child.status, error)
                if error:
                    message = f"error: {message}"
                _chat_record_system_event(child, message, clear_active=True)
            return True
        return False

    def handle_chat_stream_event(self, event_type: str, event: Mapping[str, Any]) -> bool:
        if event_type == "start":
            self.handle_chat_stream_start(event)
            return True
        if event_type == "message-metadata":
            self.handle_chat_stream_metadata(event)
            return True
        if event_type in {"start-step", "text-start"}:
            self.start_chat_stream_step()
            return True
        if event_type == "text-delta":
            self.append_chat_stream_text(event)
            return True
        if event_type == "finish-step":
            self.complete_chat_stream_step()
            return True
        if event_type == "tool-input-available":
            self.record_chat_stream_tool_request(event)
            return True
        if event_type == "tool-output-available":
            self.record_chat_stream_tool_result(event)
            return True
        if event_type == "error":
            self.handle_error(_text(event.get("errorText")) or _text(event.get("error")) or "runtime request failed")
            return True
        if event_type == "finish":
            if self.active_run is None:
                return True
            self.complete_chat_stream_step()
            status = "canceled" if self.active_run.cancel_requested else "finished"
            self.finish_run(
                {
                    "run_id": self.active_run.run_id if self.active_run is not None else "",
                    "status": status,
                }
            )
            return True
        return event_type == "text-end"

    def handle_chat_stream_start(self, event: Mapping[str, Any]) -> None:
        metadata = _mapping(event.get("messageMetadata"))
        run_id = _text(metadata.get("run_id")) or _text(event.get("messageId")) or ""
        thread_id = _text(metadata.get("thread_id"))
        if thread_id:
            self.thread_id = thread_id
        if self.active_run is None:
            self.active_run = _ChatRun(run_id=run_id, message="", status="running", accept_child_trace=True)
            self.active_run.mark_running()
            self.maybe_send_cancel_request()
            return
        if run_id:
            self.active_run.run_id = run_id
        self.active_run.mark_running()
        self.maybe_send_cancel_request()

    def handle_chat_stream_metadata(self, event: Mapping[str, Any]) -> None:
        metadata = _mapping(event.get("messageMetadata"))
        thread_id = _text(metadata.get("thread_id"))
        if thread_id:
            self.thread_id = thread_id
        run_id = _text(metadata.get("run_id"))
        if run_id and self.active_run is not None:
            self.active_run.run_id = run_id
            self.maybe_send_cancel_request()

    def start_chat_stream_step(self) -> None:
        if self.active_run is None:
            return
        if self.stream_step_index is not None:
            return
        index = self.next_chat_stream_step_index()
        self.stream_step_index = index
        self.stream_text_parts = []
        self.active_run.start_step({"step_index": index, "kind": "model"})

    def append_chat_stream_text(self, event: Mapping[str, Any]) -> None:
        if self.active_run is None:
            return
        self.start_chat_stream_step()
        delta = _text(event.get("delta"))
        if delta:
            self.stream_text_parts.append(delta)

    def complete_chat_stream_step(self) -> None:
        if self.active_run is None or self.stream_step_index is None:
            return
        index = self.stream_step_index
        text = "".join(self.stream_text_parts)
        output: list[dict[str, object]] = []
        if text:
            output.append({"type": "text", "text": text})
        self.active_run.complete_step(
            {
                "run_id": self.active_run.run_id,
                "step_index": index,
                "kind": "model",
                "output": output,
            }
        )
        self.stream_step_index = None
        self.stream_text_parts = []

    def record_chat_stream_tool_request(self, event: Mapping[str, Any]) -> None:
        if self.active_run is None:
            return
        self.complete_chat_stream_step()
        index = self.next_chat_stream_step_index()
        tool_call_id = _text(event.get("toolCallId")) or f"tool_{index}"
        tool_name = _text(event.get("toolName")) or "tool"
        tool_input = event.get("input")
        part = {
            "type": "tool_call",
            "tool_call_id": tool_call_id,
            "tool_name": tool_name,
            "input": dict(tool_input) if isinstance(tool_input, Mapping) else {},
        }
        self.active_run.record_part({"step_index": index, "part_index": 0, "part": part})
        self.stream_tool_steps[tool_call_id] = index
        self.active_run.start_step(
            {
                "run_id": self.active_run.run_id,
                "step_index": index,
                "kind": "tool",
                "input": [part],
            }
        )

    def record_chat_stream_tool_result(self, event: Mapping[str, Any]) -> None:
        if self.active_run is None:
            return
        self.complete_chat_stream_step()
        tool_call_id = _text(event.get("toolCallId"))
        index = self.stream_tool_steps.pop(tool_call_id, None) if tool_call_id is not None else None
        if index is None:
            index = self.next_chat_stream_step_index()
        tool_name = _text(event.get("toolName")) or "tool"
        input_part = None
        active_step = self.active_run.steps.get(index)
        if active_step is not None:
            for item in _list(active_step.payload.get("input")):
                if isinstance(item, Mapping):
                    input_part = dict(item)
                    break
        if input_part is None:
            input_part = {"tool_name": tool_name, "input": {}}
        self.active_run.complete_step(
            {
                "run_id": self.active_run.run_id,
                "step_index": index,
                "kind": "tool",
                "input": [input_part],
                "output": [
                    {
                        "type": "tool_result",
                        "tool_call_id": tool_call_id or "",
                        "tool_name": tool_name,
                        "output": event.get("output"),
                    }
                ],
            }
        )

    def next_chat_stream_step_index(self) -> int:
        if self.active_run is None:
            return 1
        return max(self.active_run.step_indexes(), default=0) + 1

    def handle_run_command(self, payload: dict[str, Any]) -> None:
        kind = payload.get("kind")
        if kind == "steer":
            if self.active_run is not None:
                self.active_run.record_command(payload)
            return
        if kind == "stop":
            if self.active_run is not None:
                self.active_run.record_command(payload)
                self.active_run.request_cancel()
            return
        if kind != "start":
            return
        run_id = str(payload.get("run_id") or "")
        message = _event_message_text(payload.get("message"))
        if not message:
            message = self.active_run.message if self.active_run is not None else ""
        if self.active_run and (not self.active_run.run_id or self.active_run.run_id == run_id):
            self.active_run.run_id = run_id
            self.active_run.message = message
            self.active_run.mark_running()
            self.maybe_send_cancel_request()
            return
        self.active_run = _ChatRun(run_id=run_id, message=message, status="running")
        self.active_run.mark_running()
        self.maybe_send_cancel_request()

    def handle_run_start(self, payload: dict[str, Any]) -> None:
        run_id = str(payload.get("run_id") or "")
        if self.active_run and (not self.active_run.run_id or self.active_run.run_id == run_id):
            self.active_run.run_id = run_id
            self.active_run.mark_running()
            self.maybe_send_cancel_request()
            return
        message = _event_message_text(payload.get("input"))
        self.active_run = _ChatRun(run_id=run_id, message=message, status="running")
        self.active_run.mark_running()
        self.maybe_send_cancel_request()

    def finish_run(self, payload: dict[str, Any]) -> None:
        completed_run = self.active_run
        if completed_run is not None:
            completed_run.status = _display_run_status(payload.get("status")) or "completed"
            error = _text(payload.get("error"))
            if completed_run.status in {"failed", "error", "canceled", "cancelled"}:
                message = _chat_stopped_run_message(completed_run.status, error)
                if error:
                    message = f"error: {message}"
                _chat_record_system_event(completed_run, message, clear_active=True)
        self.active_run = None
        self.local_streaming.clear()
        self.prompt.clear_error()
        if completed_run is not None:
            self.print_run(completed_run)
        self.start_next_run()
        self.app.invalidate()

    def start_next_run(self) -> None:
        if self.pending:
            item = self.pending.pop(0)
            self.start_run(item.text)

    def handle_local_command(self, message: str) -> bool:
        parsed = _chat_local_command(message)
        if parsed is None:
            return False
        command, argument = parsed
        if command in {"exit", "quit"}:
            self.handle_quit()
            return True
        if command in {"help", "?"}:
            _chat_write_lines(_chat_local_command_lines(message, _chat_help_lines()))
            return True
        if command in {"queue", "q"}:
            return self.handle_queue_command(argument, message)
        if command in {"thunk", "flow"}:
            return self.handle_executable_command(command, argument, message)
        if command not in {"model", "models"}:
            self.prompt.set_error(f"Unknown command: /{command}")
            return True
        if argument:
            if self.has_active_run():
                self.prompt.set_error("Cannot change model while a run is active.")
                return True
            selectors = _chat_model_command_selectors(argument)
            if not selectors:
                self.prompt.set_error("/model requires a selector.")
                return True
            labels = _chat_resolve_model_command_labels(self.ctx, selectors)
            if labels is None:
                self.prompt.set_error(f"Model selector matched no models: {', '.join(selectors)}")
                return True
            self.selector_payload["models"] = list(selectors)
            self.model_label = ", ".join(labels)
            self.prompt.status_label = self.status_label()
            _chat_write_lines(_chat_local_command_lines(message, [f"model: {self.model_label}"]))
            return True
        try:
            payload = _runtime_json(self.ctx, "/api/v1/chat/models")
        except click.ClickException as exc:
            self.prompt.set_error(_chat_friendly_error(exc.message))
            return True
        _chat_write_lines(_chat_local_command_lines(message, ["available models", *_chat_model_list_lines(payload)]))
        return True

    def handle_queue_command(self, argument: str, message: str) -> bool:
        tokens = argument.split()
        if not tokens:
            _chat_write_lines(_chat_local_command_lines(message, _chat_queue_help_lines()))
            return True
        action = tokens[0].lower()
        if action in {"clear", "c"}:
            self.pending.clear()
            return True
        if action not in {"steer", "s", "delete", "d", "edit", "e"}:
            self.prompt.set_error(f"Unknown queue command: {tokens[0]}")
            return True
        if len(tokens) < 2:
            self.prompt.set_error(f"/queue {tokens[0]} requires an item number.")
            return True
        index = _chat_queue_command_index(tokens[1], len(self.pending))
        if index is None:
            self.prompt.set_error(f"Queue item not found: {tokens[1]}")
            return True
        item = self.pending[index]
        if action in {"delete", "d"}:
            self.pending.pop(index)
            return True
        if action in {"edit", "e"}:
            self.pending.pop(index)
            self.prompt.replace_input(item.text)
            return True
        run = self.active_run
        if run is None or not run.run_id:
            self.prompt.set_error("No active run to steer.")
            return True
        try:
            _runtime_post(
                self.ctx,
                f"/api/v1/runs/{run.run_id}/steer",
                payload={"message": _message_payload(item.text)},
            )
        except click.ClickException as exc:
            self.prompt.set_error(_chat_friendly_error(exc.message))
            return True
        self.pending.pop(index)
        return True

    def handle_executable_command(self, command: str, argument: str, message: str) -> bool:
        if argument:
            if self.has_active_run():
                self.prompt.set_error(f"Cannot change {command} while a run is active.")
                return True
            _chat_set_executable_selector(self.selector_payload, kind=command, name=argument)
            self.prompt.status_label = self.status_label()
            _chat_write_lines(_chat_local_command_lines(message, [f"{command}: {argument}"]))
            return True
        try:
            payload = _runtime_json(self.ctx, f"/api/v1/chat/{command}s")
        except click.ClickException as exc:
            self.prompt.set_error(_chat_friendly_error(exc.message))
            return True
        selected = _text(self.selector_payload.get(command))
        _chat_write_lines(
            _chat_local_command_lines(
                message,
                [f"available {command}s", *_chat_executable_list_lines(payload, selected=selected)],
            )
        )
        return True

    def start_run(self, message: str) -> None:
        self.active_run = _ChatRun(run_id="", message=message, status="submitting", accept_child_trace=True)
        try:
            thread_id = self.ensure_thread_id()
        except click.ClickException as exc:
            self.handle_error(exc.message)
            return
        request_id = f"term_{uuid4().hex}"
        payload: dict[str, Any] = {
            "thread": thread_id,
            "client": "tui",
            "request_id": request_id,
            "message": _message_payload(message),
            **self.selector_payload,
        }

        def consume() -> None:
            self.local_streaming.set()
            try:
                _runtime_consume_stream(
                    self.ctx,
                    "/api/v1/chat/stream",
                    payload=payload,
                    event_handler=lambda event: self.emit_from_thread(_ChatUIEvent("runtime", event)),
                )
            except click.ClickException as exc:
                self.emit_from_thread(_ChatUIEvent("error", exc.message))
            except Exception as exc:
                self.emit_from_thread(_ChatUIEvent("error", f"{type(exc).__name__}: {exc}"))
            finally:
                self.local_streaming.clear()

        threading.Thread(target=consume, daemon=True).start()

    def ensure_thread_id(self) -> str:
        if self.thread_id is None:
            result = _runtime_post(self.ctx, "/api/v1/threads", payload={"client": "tui"})
            created = result.get("thread_id")
            if not isinstance(created, str):
                raise click.ClickException("runtime did not return a thread id")
            self.thread_id = created
        return self.thread_id

    def has_active_run(self) -> bool:
        return self.active_run is not None or self.local_streaming.is_set()

    def print_header(self) -> None:
        _chat_write_lines(_chat_header_lines(self.status_label(), home_label=self.home_label), hide_cursor=False)

    def print_run(self, run: _ChatRun) -> None:
        lines = _chat_run_lines(run, include_steps=True)
        _chat_write_lines(lines)


def _chat_interactive_prompt_toolkit(
    ctx: typer.Context,
    *,
    thread_id: str | None,
    selector_payload: dict[str, object] | None = None,
) -> None:
    asyncio.run(_ChatBottomApp(ctx, thread_id=thread_id, selector_payload=dict(selector_payload or {})).run())


def _chat_step_index(payload: Mapping[str, Any]) -> int:
    value = payload.get("step_index")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            pass
    return 0


def _chat_command_index(payload: Mapping[str, Any]) -> int:
    ref = _mapping(payload.get("ref"))
    for value in (ref.get("index"), payload.get("index")):
        index = _int_or_none(value)
        if index is not None:
            return index
    return 0


def _chat_part_index(payload: Mapping[str, Any]) -> int:
    value = payload.get("part_index")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            pass
    return 0


def _chat_step_label(payload: Mapping[str, Any], run: _ChatRun | None = None) -> str:
    kind = str(payload.get("kind") or "")
    step_payload = _mapping(payload.get("payload"))
    if kind == "model":
        return "thinking..."
    if kind == "tool":
        return f"running {_chat_tool_call_display(_chat_tool_call(payload, run=run))}"
    if kind == "run":
        target = executable_label(
            _text(step_payload.get("target_kind")) or "run",
            _text(step_payload.get("target")),
            metadata=_mapping(step_payload.get("metadata")),
        ).replace(":", " ", 1)
        return f"running {target}"
    if kind in {"step", "parallel", "bind"}:
        op = _text(step_payload.get("op")) or "flow"
        return f"running {op}"
    if kind == "system":
        return _text(step_payload.get("message")) or _text(step_payload.get("op")) or kind
    return "running"


def _chat_active_step_line(step: _ChatStep) -> str:
    line = f"{_chat_marker_for(step.kind)} {step.label}"
    if step.kind in {"tool", "step", "parallel", "bind", "system"}:
        return _chat_dim(line)
    return line


def _chat_completed_step_line(payload: Mapping[str, Any], *, run: _ChatRun | None = None) -> str:
    kind = str(payload.get("kind") or "")
    step_payload = _mapping(payload.get("payload"))
    marker = _chat_marker_for(kind)
    if kind == "model":
        text = _event_parts_text(payload.get("output"))
        if text:
            return f"{marker} assistant message"
        requests = _chat_model_tool_request_summary(payload, run=run)
        if requests:
            return f"{marker} requested {requests}"
        model = _text(step_payload.get("model_ref")) or _text(step_payload.get("model"))
        return f"{marker} model returned no message{f' ({model})' if model else ''}"
    if kind == "tool":
        tool = _chat_tool_call(payload, run=run)
        detail = _chat_tool_call_display(tool)
        error = _text(payload.get("error"))
        if error:
            return _chat_dim(f"{marker} ran {detail} failed: {_chat_summarize(error, width=120)}")
        return _chat_dim(f"{marker} ran {detail}")
    if kind == "run":
        target = executable_label(
            _text(step_payload.get("target_kind")) or "run",
            _text(step_payload.get("target")),
            metadata=_mapping(step_payload.get("metadata")),
        ).replace(":", " ", 1)
        return _chat_dim(f"{marker} ran {target}")
    if kind in {"step", "parallel", "bind"}:
        op = _text(step_payload.get("op")) or "flow"
        return _chat_dim(f"{marker} ran {op}")
    if kind in {"system", "error"}:
        return _chat_system_line(payload)
    return _chat_dim(f"{marker} ran {kind or 'step'}")


def _chat_record_system_event(run: _ChatRun, message: str, *, clear_active: bool) -> None:
    if clear_active:
        run.steps.clear()
    index = max(run.step_indexes(), default=0) + 1
    kind = "error" if message.startswith("error:") else "system"
    if kind == "error":
        message = message.removeprefix("error:").strip()
    run.completed_steps[index] = {
        "kind": kind,
        "step_index": index,
        "payload": {"message": message},
    }


def _chat_stopped_run_message(status: str, error: str | None) -> str:
    if error:
        return _chat_friendly_error(error)
    if status in {"canceled", "cancelled"}:
        return "canceled"
    if status in {"failed", "error"}:
        return "failed"
    return status


def _chat_friendly_error(message: str) -> str:
    text = message.strip()
    if text.startswith("runtime request failed:"):
        text = text.removeprefix("runtime request failed:").strip()
    extracted = _chat_extract_error_message(text)
    if extracted:
        return extracted
    return text


def _chat_extract_error_message(text: str) -> str | None:
    candidates = [text]
    if " - " in text:
        candidates.append(text.split(" - ", 1)[1].strip())
    for candidate in candidates:
        parsed = _chat_parse_error_payload(candidate)
        if parsed is None:
            continue
        error = parsed.get("error")
        if isinstance(error, Mapping):
            message = _text(error.get("message"))
            if message is not None:
                return message
        message = _text(parsed.get("message"))
        if message is not None:
            return message
    return None


def _chat_parse_error_payload(text: str) -> Mapping[str, Any] | None:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        try:
            parsed = ast.literal_eval(text)
        except (SyntaxError, ValueError):
            return None
    return cast(Mapping[str, Any], parsed) if isinstance(parsed, Mapping) else None


def _chat_marker_for(kind: str | None) -> str:
    return {
        "model": "•",
        "tool": "›",
        "run": "›",
        "step": "─",
        "parallel": "⋯",
        "bind": "→",
        "system": "◇",
        "error": "!",
    }.get(kind or "", "·")


def _chat_tool_name(payload: Mapping[str, Any], *, run: _ChatRun | None = None) -> str:
    return _chat_tool_call(payload, run=run).name


def _chat_tool_call(payload: Mapping[str, Any], *, run: _ChatRun | None = None) -> _ChatToolCall:
    output_tool: _ChatToolCall | None = None
    for part in _list(payload.get("output")):
        if not isinstance(part, Mapping):
            continue
        name = _text(part.get("tool_name")) or _text(part.get("tool_family"))
        if name is not None:
            tool_input = part.get("input")
            output_tool = _ChatToolCall(name=name, input=dict(tool_input) if isinstance(tool_input, Mapping) else {})
            if output_tool.input:
                return output_tool
    for part in _list(payload.get("input")):
        if not isinstance(part, Mapping):
            continue
        name = _text(part.get("tool_name")) or _text(part.get("tool_family"))
        if name is not None:
            tool_input = part.get("input")
            return _ChatToolCall(name=name, input=dict(tool_input) if isinstance(tool_input, Mapping) else {})
        if run is None:
            continue
        ref_step = _int_or_none(part.get("step_index"))
        ref_part = _int_or_none(part.get("part_index"))
        if ref_step is not None and ref_part is not None:
            tool = run.tool_calls_by_part.get((ref_step, ref_part))
            if tool is not None:
                return tool
    if output_tool is not None:
        return output_tool
    step_payload = _mapping(payload.get("payload"))
    name = _text(step_payload.get("tool_name")) or _text(step_payload.get("tool")) or _text(step_payload.get("name"))
    tool_input = step_payload.get("input")
    return _ChatToolCall(name=name or "tool", input=dict(tool_input) if isinstance(tool_input, Mapping) else {})


def _chat_tool_call_display(tool: _ChatToolCall) -> str:
    input_summary = _chat_tool_input_summary(tool.input)
    if input_summary:
        return f"{tool.name}: {input_summary}"
    return tool.name


def _chat_model_tool_request_summary(payload: Mapping[str, Any], *, run: _ChatRun | None = None) -> str:
    tools: list[str] = []
    for part in _list(payload.get("output")):
        if not isinstance(part, Mapping) or part.get("type") != "tool_call":
            continue
        name = _text(part.get("tool_name")) or _text(part.get("tool_family")) or "tool"
        tool_input = part.get("input")
        tool = _ChatToolCall(name=name, input=dict(tool_input) if isinstance(tool_input, Mapping) else {})
        tools.append(_chat_tool_call_display(tool))
    if not tools and run is not None:
        step_index = _chat_step_index(payload)
        tools.extend(
            _chat_tool_call_display(tool)
            for (tool_step_index, _part_index), tool in sorted(run.tool_calls_by_part.items())
            if tool_step_index == step_index
        )
    return "; ".join(tools)


def _chat_tool_input_summary(tool_input: Mapping[str, Any]) -> str:
    if not tool_input:
        return ""
    for key in ("command", "cmd", "query", "path", "url", "prompt", "text"):
        value = tool_input.get(key)
        if value is not None:
            return _chat_plain_value(value)
    if len(tool_input) == 1:
        value = next(iter(tool_input.values()))
        return _chat_plain_value(value)
    pieces = [f"{key}={_chat_plain_value(value)}" for key, value in tool_input.items()]
    return ", ".join(pieces)


def _chat_plain_value(value: object) -> str:
    if isinstance(value, str):
        return _chat_summarize(value, width=160)
    if isinstance(value, (int, float, bool)) or value is None:
        return str(value)
    return _chat_summarize(json.dumps(value, ensure_ascii=False, separators=(",", ":")), width=160)


def _chat_system_line(payload: Mapping[str, Any]) -> str:
    kind = str(payload.get("kind") or "system")
    step_payload = _mapping(payload.get("payload"))
    message = (
        _text(payload.get("error"))
        or _text(step_payload.get("message"))
        or _text(step_payload.get("op"))
        or _text(step_payload.get("status"))
        or "runtime event"
    )
    marker = _chat_marker_for(kind)
    line = f"{marker} {message}"
    if kind in {"error", "system"}:
        return line
    return _chat_dim(line)


def _chat_dim(text: str) -> str:
    return f"{_CHAT_DIM}{text}{_CHAT_NORMAL_INTENSITY}"


def _chat_panel_user_block(run: _ChatRun) -> list[str]:
    return _chat_input_bar_lines(
        marker=">",
        text=run.message,
        footer=_chat_run_id_footer(run),
        fg=_CHAT_INPUT_FG,
        bg=_CHAT_INPUT_BG,
        ansi=False,
    )


def _chat_scrollback_user_block(run: _ChatRun) -> list[str]:
    return _chat_input_bar_lines(
        marker=">",
        text=run.message,
        footer=_chat_run_id_footer(run),
        fg=_CHAT_INPUT_FG,
        bg=_CHAT_INPUT_BG,
        ansi=True,
    )


def _chat_input_bar_lines(
    *,
    marker: str,
    text: str,
    footer: str = "",
    fg: str,
    bg: str,
    ansi: bool,
    outer_blank: bool = False,
) -> list[str]:
    lines: list[str] = []
    if outer_blank:
        lines.append("")
    lines.append(_chat_input_bar_line("", fg=fg, bg=bg, ansi=ansi))
    lines.extend(
        _chat_input_bar_line(_chat_input_bar_message_line(marker, index, line), fg=fg, bg=bg, ansi=ansi)
        for index, line in enumerate(text.splitlines() or [""])
    )
    lines.append(_chat_input_bar_line(footer, fg=fg, bg=bg, ansi=ansi))
    if outer_blank:
        lines.append("")
    return lines


def _chat_input_bar_message_line(marker: str, index: int, line: str) -> str:
    if index == 0:
        return f"{_CHAT_DIM}{marker}{_CHAT_NORMAL_INTENSITY} {line}"
    return f"  {line}"


def _chat_user_message_line(index: int, line: str) -> str:
    return _chat_input_bar_message_line(">", index, line)


def _chat_run_id_footer(run: _ChatRun) -> str:
    return f"{_CHAT_DIM}  {run.run_id}{_CHAT_NORMAL_INTENSITY}" if run.run_id else ""


def _chat_input_block_line(content: str) -> str:
    return _chat_input_bar_line(content, fg=_CHAT_INPUT_FG, bg=_CHAT_INPUT_BG, ansi=True)


def _chat_input_bar_line(content: str, *, fg: str, bg: str, ansi: bool) -> str:
    if not ansi:
        return content
    return f"{_chat_ansi_style(fg, bg)}{_chat_pad_visible(content, _chat_terminal_width())}{_CHAT_RESET}"


def _chat_pad_visible(content: str, width: int) -> str:
    return content + " " * max(0, width - _chat_display_len(content))


def _chat_terminal_width(default: int = 100) -> int:
    return shutil.get_terminal_size((default, 24)).columns


def _chat_ui_palette() -> dict[str, str]:
    return {
        "": "",
        "last-run": "",
        "queue": _chat_prompt_style(_CHAT_QUEUE_FG, _CHAT_QUEUE_BG),
        "input": _chat_prompt_style(_CHAT_INPUT_FG, _CHAT_INPUT_BG),
        "steer-input": _chat_prompt_style(_CHAT_STEER_INPUT_FG, _CHAT_STEER_INPUT_BG),
        "cursor": _chat_prompt_style(_CHAT_CURSOR_FG, _CHAT_CURSOR_BG),
        "input.cursor": _chat_prompt_style(_CHAT_CURSOR_FG, _CHAT_CURSOR_BG),
        "status": _chat_prompt_style(_CHAT_STATUS_FG, _CHAT_STATUS_BG),
        "status.model": "fg:#ffd866",
        "status.thunk": "fg:#8fd7ff",
        "status.flow": "fg:#d7b3ff",
        "status.text": "fg:ansigray",
        "status.error": "fg:ansired",
    }


def _chat_prompt_style(fg: str, bg: str) -> str:
    return f"fg:{fg} bg:{bg}"


def _chat_activity_formatted_text(lines: Sequence[str]) -> list[tuple[str, str]]:
    steer_prefix = _chat_ansi_style(_CHAT_STEER_INPUT_FG, _CHAT_STEER_INPUT_BG)
    fragments: list[tuple[str, str]] = []
    for index, line in enumerate(lines):
        if line.startswith(steer_prefix):
            fragments.append(("class:steer-input", _chat_visible_text(line)))
        else:
            fragments.extend(cast(list[tuple[str, str]], to_formatted_text(ANSI(line))))
        if index < len(lines) - 1:
            fragments.append(("", "\n"))
    return fragments


def _chat_ansi_style(fg: str, bg: str) -> str:
    if fg.startswith("#") or bg.startswith("#"):
        return f"\x1b[{_chat_sgr_color(fg, foreground=True)};{_chat_sgr_color(bg, foreground=False)}m"
    foreground = {
        "ansiblack": "30",
        "ansired": "31",
        "ansigreen": "32",
        "ansiyellow": "33",
        "ansiblue": "34",
        "ansimagenta": "35",
        "ansicyan": "36",
        "ansiwhite": "37",
        "ansibrightblack": "90",
        "ansibrightred": "91",
        "ansibrightgreen": "92",
        "ansibrightyellow": "93",
        "ansibrightblue": "94",
        "ansibrightmagenta": "95",
        "ansibrightcyan": "96",
        "ansibrightwhite": "97",
    }
    background = {
        "ansiblack": "40",
        "ansired": "41",
        "ansigreen": "42",
        "ansiyellow": "43",
        "ansiblue": "44",
        "ansimagenta": "45",
        "ansicyan": "46",
        "ansiwhite": "47",
        "ansibrightblack": "100",
        "ansibrightred": "101",
        "ansibrightgreen": "102",
        "ansibrightyellow": "103",
        "ansibrightblue": "104",
        "ansibrightmagenta": "105",
        "ansibrightcyan": "106",
        "ansibrightwhite": "107",
    }
    return f"\x1b[{foreground[fg]};{background[bg]}m"


def _chat_sgr_color(color: str, *, foreground: bool) -> str:
    if not color.startswith("#") or len(color) != 7:
        raise ValueError(f"unsupported color: {color}")
    red = int(color[1:3], 16)
    green = int(color[3:5], 16)
    blue = int(color[5:7], 16)
    prefix = "38" if foreground else "48"
    return f"{prefix};2;{red};{green};{blue}"


def _chat_run_lines(run: _ChatRun, *, include_steps: bool) -> list[str]:
    lines = [*_chat_scrollback_user_block(run), ""]
    if include_steps:
        lines.extend(_chat_run_activity_lines(run, _chat_completed_line_for))
    while lines and lines[-1] == "":
        lines.pop()
    lines.append("")
    return lines


def _chat_tail_activity_lines(lines: Sequence[str], *, max_lines: int) -> list[str]:
    if len(lines) <= max_lines:
        return list(lines)
    if max_lines <= 0:
        return []
    if max_lines == 1:
        return [_chat_dim(f"... {len(lines)} earlier lines")]
    tail_count = max_lines - 1
    hidden = len(lines) - tail_count
    return [_chat_dim(f"... {hidden} earlier lines"), *lines[-tail_count:]]


def _chat_completed_line_for(run: _ChatRun, index: int) -> str:
    if index in run.completed_steps:
        return _chat_completed_step_line(run.completed_steps[index], run=run)
    return _chat_active_step_line(run.steps[index])


def _chat_run_activity_lines(run: _ChatRun, step_renderer: Callable[[_ChatRun, int], str]) -> list[str]:
    state_line = _chat_run_state_line(run)
    queue_line = _chat_queue_activity_line(run)
    if queue_line:
        lines = [queue_line]
        if state_line:
            lines.append(state_line)
        lines.extend(_chat_terminal_event_lines(run, step_renderer))
        return lines
    flow_lines = _chat_flow_stage_lines(run)
    if flow_lines:
        lines = []
        if state_line and not _chat_state_line_after_activity(run):
            lines.append(state_line)
        lines.extend(flow_lines)
        lines.extend(_chat_timeline_command_lines(run))
        lines.extend(_chat_terminal_event_lines(run, step_renderer))
        if state_line and _chat_state_line_after_activity(run):
            lines.append(state_line)
        return lines
    lines: list[str] = []
    if state_line and not _chat_state_line_after_activity(run):
        lines.append(state_line)
    timeline = run.timeline or [("step", index) for index in run.step_indexes()]
    rendered_steps: set[int] = set()
    for position, (kind, index) in enumerate(timeline):
        if kind == "command":
            command = run.commands.get(index)
            if command is not None:
                lines.extend(_chat_command_activity_lines(run, command, position))
            continue
        if index in rendered_steps:
            continue
        step_lines = _chat_step_activity_lines(run, index, step_renderer)
        if step_lines:
            lines.extend(step_lines)
            rendered_steps.add(index)
    for index in run.step_indexes():
        if index not in rendered_steps:
            lines.extend(_chat_step_activity_lines(run, index, step_renderer))
    if state_line and _chat_state_line_after_activity(run):
        lines.append(state_line)
    return lines


def _chat_state_line_after_activity(run: _ChatRun) -> bool:
    status = _chat_run_display_status(run.status)
    return status in {"succeeded", "finished", "completed", "done", "failed", "error", "canceled", "cancelled"}


def _chat_step_activity_lines(
    run: _ChatRun,
    index: int,
    step_renderer: Callable[[_ChatRun, int], str],
) -> list[str]:
    if index in run.steps:
        return [step_renderer(run, index)]
    payload = run.completed_steps.get(index)
    if payload is None:
        return []
    if payload.get("kind") == "model":
        text = _event_parts_text(payload.get("output"))
        if text:
            return _chat_message_lines(_chat_marker_for("model"), text)
        if _chat_model_tool_requests_have_results(run, index):
            return []
    return [step_renderer(run, index)]


def _chat_timeline_command_lines(run: _ChatRun) -> list[str]:
    lines: list[str] = []
    for position, (kind, index) in enumerate(run.timeline):
        if kind != "command":
            continue
        command = run.commands.get(index)
        if command is None:
            continue
        lines.extend(_chat_command_activity_lines(run, command, position))
    return lines


def _chat_command_activity_lines(run: _ChatRun, command: Mapping[str, Any], timeline_position: int) -> list[str]:
    if command.get("kind") == "steer":
        return _chat_steer_input_block(command, waiting=_chat_command_is_waiting(run, timeline_position))
    if command.get("kind") == "stop":
        return [_chat_dim("◇ cancel requested")]
    return []


def _chat_command_is_waiting(run: _ChatRun, timeline_position: int) -> bool:
    if not run.steps:
        return False
    return not any(kind == "step" for kind, _index in run.timeline[timeline_position + 1 :])


def _chat_steer_input_block(command: Mapping[str, Any], *, waiting: bool) -> list[str]:
    message = _event_message_text(command.get("message"))
    footer = _chat_dim("  pending for next step") if waiting else ""
    return _chat_input_bar_lines(
        marker="+",
        text=message,
        footer=footer,
        fg=_CHAT_STEER_INPUT_FG,
        bg=_CHAT_STEER_INPUT_BG,
        ansi=True,
        outer_blank=True,
    )


def _chat_terminal_event_lines(run: _ChatRun, step_renderer: Callable[[_ChatRun, int], str]) -> list[str]:
    lines: list[str] = []
    for index in run.step_indexes():
        payload = run.completed_steps.get(index)
        if payload is None:
            continue
        if payload.get("kind") not in {"error", "system"}:
            continue
        lines.append(step_renderer(run, index))
    return lines


def _chat_model_tool_requests_have_results(run: _ChatRun, model_step_index: int) -> bool:
    model_payload = run.completed_steps.get(model_step_index)
    if model_payload is None or not _chat_model_tool_request_summary(model_payload, run=run):
        return False
    for payload in run.completed_steps.values():
        if payload.get("kind") != "tool":
            continue
        for item in _list(payload.get("input")):
            if not isinstance(item, Mapping):
                continue
            if _int_or_none(item.get("step_index")) == model_step_index:
                return True
        if payload.get("step_index") == model_step_index:
            return True
    return False


def _chat_run_state_line(run: _ChatRun) -> str:
    run_id = run.run_id or "run"
    status = _chat_run_display_status(run.status)
    if not status:
        return ""
    if status in {"queued", "waiting", "submitting"}:
        return ""
    if status == "running":
        return f"◇ running {run_id}"
    if status == "canceling":
        return _chat_dim(f"◇ canceling {run_id}")
    if status in {"succeeded", "finished", "completed", "done"}:
        return f"◇ stopped {run_id}: succeeded"
    if status in {"failed", "error"}:
        return f"◇ stopped {run_id}: failed"
    if status in {"canceled", "cancelled"}:
        return _chat_dim(f"◇ stopped {run_id}: canceled")
    return f"◇ stopped {run_id}: {status}"


def _chat_run_display_status(status: str) -> str:
    if status == "finished":
        return "succeeded"
    return status.strip().lower()


def _chat_queue_activity_line(run: _ChatRun) -> str:
    if run.queue_state == "waiting":
        reason = run.waiting_for or "queue"
        run_id = f" {run.run_id}" if run.run_id else ""
        return f"◇ waiting{run_id} for {reason}"
    if run.queue_state == "queued":
        suffix = f" · position {run.queue_position}" if run.queue_position else ""
        run_id = f" {run.run_id}" if run.run_id else ""
        return f"◇ queued{run_id}{suffix}"
    if run.status == "submitting":
        return "◇ submitting"
    return ""


def _chat_flow_stage_lines(run: _ChatRun) -> list[str]:
    stages, calls = _chat_flow_projection(run)
    if not stages:
        return []
    lines: list[str] = []
    for stage in stages:
        lines.append(_chat_flow_stage_line(stage, calls))
        lines.extend(_chat_flow_stage_detail_lines(stage, calls, child_run_for=lambda call: run.child_run(call.run_id)))
        lines.append("")
    return lines


def _chat_flow_projection(run: _ChatRun) -> tuple[list[FlowStageView], dict[str, FlowCallView]]:
    steps: list[Mapping[str, Any]] = []
    for step in run.step_indexes():
        payload = run.completed_steps.get(step)
        if payload is not None:
            steps.append(payload)
            continue
        active = run.steps.get(step)
        if active is None or active.kind not in {"step", "parallel", "bind", "run"}:
            continue
        active_payload = dict(active.payload)
        if "payload" not in active_payload:
            active_payload["payload"] = dict(_mapping(active_payload.get("metadata")))
        steps.append(active_payload)
    return project_flow_from_step_payloads(steps)


def _chat_flow_stage_line(stage: FlowStageView, calls: Mapping[str, FlowCallView]) -> str:
    pieces = [stage_title_label(stage)]
    tail = _chat_flow_stage_tail(stage, calls)
    if tail:
        pieces.append(tail)
    return " · ".join(pieces)


def _chat_flow_stage_tail(stage: FlowStageView, calls: Mapping[str, FlowCallView]) -> str:
    stage_call_items = stage_calls(stage, calls)
    total = len(stage_call_items)
    failed = sum(1 for call in stage_call_items if call.status == "failed")
    done = sum(1 for call in stage_call_items if call.status in {"succeeded", "done", "failed", "canceled"})
    parts: list[str] = []
    if stage.output_shape:
        parts.append(f"{stage.input_shape or '?'} -> {stage.output_shape or '?'}")
    elif stage.item_total is not None and total:
        parts.append(f"{done}/{stage.item_total} calls")
    elif total:
        parts.append(f"{done}/{total} calls")
    if failed:
        parts.append(f"{failed} failed")
    if stage.parallelism and stage.parallelism > 1:
        parts.append(f"{stage.parallelism} lanes")
    return " · ".join(parts)


def _chat_flow_stage_detail_lines(
    stage: FlowStageView,
    calls: Mapping[str, FlowCallView],
    *,
    child_run_for: Callable[[FlowCallView], object | None],
) -> list[str]:
    stage_call_items = stage_calls(stage, calls)
    if not stage_call_items:
        return []
    if stage.parallelism and stage.parallelism > 1:
        lines: list[str] = []
        lanes = stage_lanes(stage_call_items)
        for lane_index in range(stage.parallelism):
            lane_calls = lanes.get(lane_index, [])
            if not lane_calls:
                continue
            done = sum(1 for call in lane_calls if call.status in {"succeeded", "done", "failed", "canceled"})
            lines.append(
                _chat_dim(
                    f"{_CHAT_FLOW_DETAIL_INDENT}{_CHAT_FLOW_STATEMENT_MARKER} "
                    f"lane {lane_index + 1}/{stage.parallelism} · {done}/{len(lane_calls)} calls"
                )
            )
            for call in lane_calls:
                lines.extend(_chat_flow_call_lines(call, child_run_for(call), indent=_CHAT_FLOW_DETAIL_INDENT))
        return lines
    lines = []
    for call in stage_call_items:
        lines.extend(_chat_flow_call_lines(call, child_run_for(call), indent=_CHAT_FLOW_DETAIL_INDENT))
    return lines


def _chat_flow_call_lines(call: FlowCallView, child_run: object | None, *, indent: str) -> list[str]:
    header = _chat_dim(f"{indent}{_CHAT_FLOW_STATEMENT_MARKER} {_chat_flow_call_label(call, child_run)}")
    lines = [header]
    step_lines = _chat_child_run_step_lines(child_run, indent=indent)
    lines.extend(step_lines)
    return lines


def _chat_flow_call_label(call: FlowCallView, child_run: object | None) -> str:
    if isinstance(child_run, _ChatRun):
        status = child_run.status or call.status
        target = (
            executable_label(child_run.executable_kind, child_run.executable_name).replace(":", " ", 1)
            if child_run.executable_name
            else call.label
        )
        return f"{call.label if call.item_index is not None else target} · {call.run_id} {status}"
    if isinstance(child_run, Mapping):
        child_mapping = cast(Mapping[str, Any], child_run)
        info = _mapping(child_mapping.get("info"))
        output = _mapping(child_mapping.get("output"))
        status = _display_run_status(output.get("status")) or call.status
        name = _text(info.get("executable_name"))
        target = (
            executable_label(
                _text(info.get("executable_kind")) or "run",
                name,
                metadata=_mapping(info.get("metadata")),
            ).replace(":", " ", 1)
            if name
            else call.label
        )
        return f"{call.label if call.item_index is not None else target} · {call.run_id} {status}"
    return f"{call.label} · {call.run_id} {call.status}"


def _chat_child_run_step_lines(child_run: object | None, *, indent: str) -> list[str]:
    if isinstance(child_run, _ChatRun):
        return _chat_child_chat_run_step_lines(child_run, indent=indent)
    if isinstance(child_run, Mapping):
        return _chat_child_mapping_run_step_lines(cast(Mapping[str, Any], child_run), indent=indent)
    return []


def _chat_child_chat_run_step_lines(run: _ChatRun, *, indent: str) -> list[str]:
    lines: list[str] = []
    if run.executable_kind == "flow":
        for line in _chat_flow_stage_lines(run):
            lines.append(f"{indent}{line}" if line else "")
        return lines
    for index in run.step_indexes():
        if index in run.steps:
            lines.append(f"{indent}{_chat_active_step_line(run.steps[index])}")
            continue
        payload = run.completed_steps[index]
        lines.extend(_chat_child_completed_step_lines(payload, run=run, indent=indent))
    return lines


def _chat_child_mapping_run_step_lines(run: Mapping[str, Any], *, indent: str) -> list[str]:
    info = _mapping(run.get("info"))
    if _text(info.get("executable_kind")) == "flow":
        stages, calls = project_flow_from_run(run)
        lines: list[str] = []
        for stage in stages:
            lines.append(f"{indent}{_chat_flow_stage_line(stage, calls)}")
            lines.append("")
        return lines
    lines: list[str] = []
    for step in _run_steps(run):
        record = _mapping(step.get("record"))
        lines.extend(_chat_child_completed_step_lines(record, run=None, indent=indent))
    return lines


def _chat_child_completed_step_lines(
    payload: Mapping[str, Any],
    *,
    run: _ChatRun | None,
    indent: str,
) -> list[str]:
    kind = _text(payload.get("kind"))
    if kind == "model":
        text = _event_parts_text(payload.get("output"))
        lines: list[str] = []
        if text:
            lines.extend(f"{indent}{line}" for line in _chat_message_lines(_chat_marker_for("model"), text))
        requests = _chat_model_tool_request_summary(payload, run=run)
        if requests:
            lines.append(f"{indent}{_chat_marker_for('model')} requested {requests}")
        if lines:
            return lines
    line = f"{indent}{_chat_completed_step_line(payload, run=run)}"
    if kind != "tool":
        return [line]
    return [line, *_chat_tool_message_lines(payload, indent=indent)]


def _chat_tool_message_lines(payload: Mapping[str, Any], *, indent: str) -> list[str]:
    text = _chat_tool_message_text(payload)
    if not text:
        return []
    prefix = f"{indent}  "
    width = max(8, _chat_markdown_width() - _chat_display_len(prefix))
    message = _chat_truncate_display(" ".join(_chat_visible_text(text).split()), width=width)
    return [_chat_dim(f"{prefix}{message}")]


def _chat_tool_message_text(payload: Mapping[str, Any]) -> str:
    messages: list[str] = []
    for part in _list(payload.get("output")):
        if not isinstance(part, Mapping) or part.get("type") != "tool_result":
            continue
        output = part.get("output")
        if isinstance(output, str):
            messages.append(output.strip())
            continue
        if isinstance(output, Mapping):
            stdout = _text(output.get("stdout"))
            stderr = _text(output.get("stderr"))
            if stdout:
                messages.append(stdout)
            if stderr:
                messages.append(stderr)
            if stdout or stderr:
                continue
        if output is not None:
            messages.append(_chat_plain_value(output))
    return "\n".join(item for item in messages if item).strip()


def _chat_assistant_lines(run: _ChatRun) -> list[str]:
    lines: list[str] = []
    for index in run.step_indexes():
        payload = run.completed_steps.get(index)
        if payload is None or payload.get("kind") != "model":
            continue
        text = _event_parts_text(payload.get("output"))
        if not text:
            continue
        lines.extend(_chat_message_lines(_chat_marker_for("model"), text))
    return lines


def _chat_message_lines(marker: str, text: str) -> list[str]:
    source_lines = _chat_render_markdown_lines(text)
    source_lines = source_lines or [""]
    lines = [f"{marker} {source_lines[0]}"]
    lines.extend(f"  {line}" for line in source_lines[1:])
    return lines


def _chat_handle_scripted_command(ctx: typer.Context, message: str, selector_payload: dict[str, object]) -> bool:
    parsed = _chat_local_command(message)
    if parsed is None:
        return False
    command, argument = parsed
    if command in {"help", "?"}:
        for line in _chat_help_lines():
            typer.echo(line)
        return True
    if command in {"thunk", "flow"}:
        return _chat_handle_scripted_executable_command(ctx, command, argument, selector_payload)
    if command not in {"model", "models"}:
        typer.echo(f"Unknown command: /{command}")
        return True
    if argument:
        selectors = _chat_model_command_selectors(argument)
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
        typer.echo(_chat_friendly_error(exc.message))
        return True
    typer.echo("available models")
    for line in _chat_model_list_lines(payload):
        typer.echo(line)
    return True


def _chat_handle_scripted_executable_command(
    ctx: typer.Context,
    command: str,
    argument: str,
    selector_payload: dict[str, object],
) -> bool:
    if argument:
        _chat_set_executable_selector(selector_payload, kind=command, name=argument)
        typer.echo(f"{command}: {argument}")
        return True
    try:
        payload = _runtime_json(ctx, f"/api/v1/chat/{command}s")
    except click.ClickException as exc:
        typer.echo(_chat_friendly_error(exc.message))
        return True
    selected = _text(selector_payload.get(command))
    typer.echo(f"available {command}s")
    for line in _chat_executable_list_lines(payload, selected=selected):
        typer.echo(line)
    return True


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


def _chat_resolved_model_label(ctx: typer.Context, selector_payload: Mapping[str, object]) -> str:
    requested = _chat_requested_model_selectors(selector_payload)
    fallback = _chat_initial_model_label(selector_payload)
    try:
        payload = _runtime_json(ctx, "/api/v1/chat/models")
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


def _chat_resolve_model_command_labels(ctx: typer.Context, selectors: Sequence[str]) -> tuple[str, ...] | None:
    try:
        payload = _runtime_json(ctx, "/api/v1/chat/models")
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


def _chat_home_label(ctx: typer.Context) -> str:
    try:
        agent_name = _context_agent(ctx)
        if agent_name is None:
            return "agent home"
        return str(agents.agent_home(_context_root(ctx), agent_name))
    except Exception:
        return "agent home"


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


def _chat_local_command_lines(message: str, body: Sequence[str]) -> list[str]:
    return [
        *_chat_scrollback_user_message_block(message),
        "",
        *_chat_system_block_lines(body),
        "",
    ]


def _chat_scrollback_user_message_block(message: str) -> list[str]:
    lines = [_chat_input_block_line("")]
    lines.extend(
        _chat_input_block_line(_chat_user_message_line(index, line))
        for index, line in enumerate(message.splitlines() or [""])
    )
    lines.append(_chat_input_block_line(""))
    return lines


def _chat_system_block_lines(body: Sequence[str]) -> list[str]:
    if not body:
        return []
    first, *rest = body
    return [f"{_chat_marker_for('system')} {first}", *[f"  {line}" for line in rest]]


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


def _chat_render_markdown_lines(text: str) -> list[str]:
    stream = io.StringIO()
    section_titles = _chat_markdown_section_titles(text)
    try:
        console = Console(
            file=stream,
            force_terminal=True,
            color_system="standard",
            width=_chat_markdown_width(),
            soft_wrap=False,
        )
        console.print(Markdown(text), width=_chat_markdown_width(), end="")
    except Exception:
        return text.splitlines()
    rendered = stream.getvalue().rstrip("\n")
    return _chat_compact_markdown_lines(rendered.splitlines(), section_titles=section_titles)


def _chat_compact_markdown_lines(lines: Sequence[str], *, section_titles: set[str]) -> list[str]:
    compact: list[str] = []
    normalized_lines = [line.rstrip() for line in lines]
    for index, normalized in enumerate(normalized_lines):
        visible = _chat_visible_text(normalized)
        if not visible.strip():
            if _chat_should_keep_markdown_blank(normalized_lines, index, section_titles=section_titles):
                if compact and compact[-1] != "":
                    compact.append("")
            continue
        compact.append(normalized)
    return compact


def _chat_should_keep_markdown_blank(lines: Sequence[str], index: int, *, section_titles: set[str]) -> bool:
    previous = _chat_previous_visible_line(lines, index)
    next_line = _chat_next_visible_line(lines, index)
    return _chat_is_section_title(previous, section_titles) or _chat_is_section_title(next_line, section_titles)


def _chat_previous_visible_line(lines: Sequence[str], index: int) -> str | None:
    for candidate in reversed(lines[:index]):
        visible = _chat_visible_text(candidate).strip()
        if visible:
            return visible
    return None


def _chat_next_visible_line(lines: Sequence[str], index: int) -> str | None:
    for candidate in lines[index + 1 :]:
        visible = _chat_visible_text(candidate).strip()
        if visible:
            return visible
    return None


def _chat_is_section_title(line: str | None, section_titles: set[str]) -> bool:
    return line is not None and line in section_titles


def _chat_markdown_section_titles(text: str) -> set[str]:
    titles: set[str] = set()
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("#"):
            continue
        prefix, _, title = stripped.partition(" ")
        if 1 <= len(prefix) <= 6 and set(prefix) == {"#"} and title.strip():
            titles.add(title.strip())
    return titles


def _chat_markdown_width() -> int:
    return min(100, max(40, _chat_terminal_width() - 4))


def _chat_visible_text(text: str) -> str:
    visible: list[str] = []
    in_escape = False
    for char in text:
        if char == "\x1b":
            in_escape = True
        elif in_escape and char == "m":
            in_escape = False
        elif not in_escape:
            visible.append(char)
    return "".join(visible)


def _chat_header_lines(model_label: str, *, home_label: str) -> list[str]:
    content = [
        (
            f"{_CHAT_DIM}T··⅃ "
            f"{_CHAT_NORMAL_INTENSITY}{_CHAT_BOLD}Toolang{_CHAT_NORMAL_INTENSITY} "
            f"{_CHAT_DIM}(v{_toolang_version()}){_CHAT_NORMAL_INTENSITY}"
        ),
        "",
        f"model: {model_label}",
        f"home:  {home_label}",
    ]
    width = max(_chat_display_len(line) for line in content) + 2
    top = f"{_CHAT_DIM}╭{'─' * width}╮{_CHAT_NORMAL_INTENSITY}"
    bottom = f"{_CHAT_DIM}╰{'─' * width}╯{_CHAT_NORMAL_INTENSITY}"
    body = [
        f"{_CHAT_DIM}│{_CHAT_NORMAL_INTENSITY} {line}{' ' * (width - 1 - _chat_display_len(line))}{_CHAT_DIM}│{_CHAT_NORMAL_INTENSITY}"
        for line in content
    ]
    return [top, *body, bottom, " "]


def _chat_display_len(text: str) -> int:
    in_escape = False
    visible: list[str] = []
    for char in text:
        if char == "\x1b":
            in_escape = True
        elif in_escape and char == "m":
            in_escape = False
        elif not in_escape:
            visible.append(char)
    return get_cwidth("".join(visible))


def _chat_write_lines(lines: list[str], *, hide_cursor: bool = True) -> None:
    if hide_cursor:
        sys.stdout.write("\x1b[?25l")
    try:
        sys.stdout.write("\n".join(lines) + "\n")
        sys.stdout.flush()
    finally:
        if hide_cursor:
            sys.stdout.write("\x1b[?25h")
            sys.stdout.flush()


def _chat_summarize(message: str, *, width: int = 72) -> str:
    text = " ".join(message.split())
    if len(text) <= width:
        return text
    return f"{text[: width - 3].rstrip()}..."


def _chat_truncate_display(text: str, *, width: int) -> str:
    if width <= 0 or _chat_display_len(text) <= width:
        return text
    ellipsis = "..."
    if width <= len(ellipsis):
        return ellipsis[:width]
    limit = width - len(ellipsis)
    pieces: list[str] = []
    used = 0
    for char in text:
        char_width = get_cwidth(char)
        if used + char_width > limit:
            break
        pieces.append(char)
        used += char_width
    return f"{''.join(pieces).rstrip()}{ellipsis}"


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
        args=(ctx, thread_id, stop_event, local_streaming, local_request_ids, redraw_prompt, event_handler),
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


def _iter_sse_events(response, *, stop_event: threading.Event) -> Iterator[dict[str, Any]]:
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
        if event_type == "run_command":
            self._render_run_command(payload)
        elif event_type == "part_delta":
            self._render_part_delta(payload)
        elif event_type == "step_end":
            self._render_step_end(payload)
        elif event_type in {"part_end", "run_end"}:
            self._close_assistant(
                redraw_prompt=event_type == "run_end",
                run_id=str(payload.get("run_id") or "") or None,
            )

    def _render_run_command(self, payload: dict[str, Any]) -> None:
        if payload.get("kind") != "start":
            return
        self._remember_local_run(payload)
        text = _event_message_text(payload.get("message"))
        if not text:
            return
        self._close_assistant(redraw_prompt=False, run_id=str(payload.get("run_id") or "") or None)
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
        if redraw_prompt and self._redraw_prompt and not self._local_run_active(run_id=run_id):
            typer.echo("> ", nl=False)
        if redraw_prompt and local_run and run_id is not None:
            self._local_run_ids.discard(run_id)

    def _remember_local_run(self, payload: dict[str, Any]) -> None:
        request_id = payload.get("request_id")
        run_id = payload.get("run_id")
        if not isinstance(request_id, str) or not isinstance(run_id, str):
            return
        if self._local_request_ids is not None and request_id in self._local_request_ids:
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
    if target.kind == "resident" and not agents.agent_home(run_root, agent_name).is_dir():
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
    agent: str | None = typer.Argument(None, help="Existing local agent name.", hidden=True),
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
    host: Annotated[str, typer.Option(help="Bind the agent API to this host.")] = "127.0.0.1",
    port: Annotated[int | None, typer.Option(help="Bind the agent API to this port.")] = None,
    components: Annotated[
        list[str] | None,
        typer.Option("--enable", help="Enable runtime components. Pass CSV or repeat."),
    ] = None,
    inboxes: Annotated[
        list[Path] | None,
        typer.Option("--inbox", help="Watch an inbox directory for file requests. Repeat to watch more than one."),
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
        raise click.ClickException("start only supports local agent names; clone the remote source first")
    root = _context_root(ctx)
    normalized_components = _normalize_component_option(components)
    progress = make_cli_progress()
    try:
        with agents.resolved_run_target(root, selector, progress=as_progress_sink(progress)) as target:
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
            raise click.ClickException(f"Agent {agent_name} failed to start: {log_path}")
        raise click.ClickException(f"Agent {agent_name} start timed out: {log_path}")
    if status.status == "failed":
        raise click.ClickException(f"Agent {agent_name} failed to start: {log_path}")
    typer.echo(f"Started agent {agent_name}: {status.webui_url or status.api_url or status.endpoint or '-'}")


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
    runtime_pids = () if runtime_state is not None else agents.agent_runtime_process_pids(root, agent_name)
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
            raise click.ClickException(f"Sandbox driver is missing for agent: {agent_name}")
        sandbox_plugin = agent_up.create_sandbox_plugin(driver.strip(), config={})

    stopped = _wrap_user_error(
        agents.stop_agent,
        root,
        agent_name,
        sandbox_plugin=sandbox_plugin,
        force=force,
    )
    typer.echo(f"Stopped agent {agent_name}" if stopped else f"Agent {agent_name} not running")


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


def _prepared_info_cap_counts(toolang_root: Path, agent_name: str) -> dict[str, int] | None:
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
            len(cap_store.effective_cap_entries(prepared.shared_lock, prepared.private_lock))
        )
        return _prepared_lock_info_cap_counts(prepared.shared_lock, prepared.private_lock)
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
    rows = _model_rows(toolang_root, environ, agent_name=agent_name, model_selectors=selectors)
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
    refresh: Annotated[bool, typer.Option("--refresh", help="Refresh cached provider model lists.")] = False,
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
    typer.echo(f" {len(rows)} {'model' if len(rows) == 1 else 'models'}, {provider_count} {'provider' if provider_count == 1 else 'providers'}")


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
    typer.echo(f" {len(rows)} {'tool' if len(rows) == 1 else 'tools'}, {toolset_count} {'toolset' if toolset_count == 1 else 'toolsets'}")


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


def _model_provider_rows(root: Path, environ: dict[str, str]) -> list[tuple[str, str, str]]:
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
    return [(info.name, info.source) for info in agent_up.list_plugin_infos(group=group)]


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
            help=lambda kind: "Trigger a chore run now." if kind == "chore" else f"Run a {kind}.",
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
        app.add_typer(work_app, name=kind, no_args_is_help=True, rich_help_panel=AGENT_COMMAND_PANEL)


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
                    entry.document.display_title(fallback_name=entry.document.task_id()),
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
        _append_work_update(_context_root(ctx), agent_name, kind=kind, job_id=job_id, path=path)
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
        _append_work_update(_context_root(ctx), agent_name, kind=kind, job_id=job_id, path=path)
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
        _append_work_update(_context_root(ctx), agent_name, kind=kind, job_id=job_id, path=path)
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
        _append_work_update(_context_root(ctx), agent_name, kind=kind, job_id=job_id, path=path)
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
        _append_work_update(_context_root(ctx), agent_name, kind=kind, job_id=job_id, path=path)
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
        _append_work_update(_context_root(ctx), agent_name, kind=kind, job_id=job_id, path=path)
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
                _runtime_post(ctx, f"/api/v1/runs/{record.last_run_id}/cancel", payload={})
                typer.echo(f"{kind} {id} cancel requested\t{record.last_run_id}")
                return
            if kind == "task" and record.status == "todo":
                updated = store.cancel_pending_task(task_id=id)
                typer.echo(f"task {id} canceled\t{updated.status}")
                return
            raise click.ClickException(f"{kind} cannot be canceled from status: {record.status}")
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
            raise click.ClickException(f"{kind} is not archived: {job_id}; archive it before deleting")
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
        _append_work_update(_context_root(ctx), agent_name, kind=kind, job_id=job_id, path=path)
        typer.echo(f"{kind} {job_id} deleted")

    return delete_work


def _reconcile_work_jobs(toolang_root: Path, agent_name: str, *, kind: WorkKind) -> None:
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

app.add_typer(model_app, name="model", no_args_is_help=True, rich_help_panel=RUNTIME_COMMAND_PANEL)
app.add_typer(tool_app, name="tool", no_args_is_help=True, rich_help_panel=RUNTIME_COMMAND_PANEL)
app.add_typer(channel_app, name="channel", no_args_is_help=True, rich_help_panel=RUNTIME_COMMAND_PANEL)
app.add_typer(sandbox_app, name="sandbox", no_args_is_help=True, rich_help_panel=RUNTIME_COMMAND_PANEL)
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


def _run_roaming_thread_command(global_args: list[str], body: list[str], *, prog_name: str) -> int:
    global _CLI_PREFIX_AGENT
    if global_args:
        typer.echo("toolang error: too <path>.too does not support global CLI options", err=True)
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
        typer.echo("toolang error: too <path>.too does not support global CLI options", err=True)
        return 1
    source_path = _roaming_source_path(body[0])
    if source_path is None:
        typer.echo(f"toolang error: agent program not found: {body[0]}", err=True)
        return 1
    try:
        options = _parse_roaming_file_runtime_options(body[1:])
        toolang_root, agent_name = agents.materialize_roaming_program(source_path)
        existing = agents.get_agent_status(toolang_root, agent_name, ui_base_url=_ui_base_url())
        if existing is not None and existing.status in {"running", "preparing", "starting"}:
            raise click.ClickException(_active_run_error(existing))
        from ...config.env import load_runtime_environ

        environ = load_runtime_environ(toolang_root, agent_name, base_environ=os.environ)
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
    except (FileExistsError, FileNotFoundError, ValueError, click.ClickException) as exc:
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
            raise click.ClickException("file request runtime usage: toolang SCRIPT --inbox PATH [--inbox PATH...]")
        if token.startswith("-"):
            raise click.ClickException(f"unknown Toolang runtime option: {token}")
        raise click.ClickException(f"unexpected thunk argument for file request runtime: {token}")
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


def _consume_global_arg(token: str, argv: list[str], index: int) -> tuple[list[str], int] | None:
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
