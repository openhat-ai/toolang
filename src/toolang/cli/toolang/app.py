"""Typer CLI for Toolang agent management."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
import json
from pathlib import Path
import os
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
    child_run_ids,
    flow_stage_context,
    output_count,
    project_flow_from_run,
    shape_label,
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
from . import chat_tui as _chat_tui
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

KeyBindings = _chat_tui.KeyBindings
shutil = _chat_tui.shutil
Style = _chat_tui.Style
_CHAT_DIM = _chat_tui._CHAT_DIM
_CHAT_INPUT_BG = _chat_tui._CHAT_INPUT_BG
_CHAT_INPUT_FG = _chat_tui._CHAT_INPUT_FG
_CHAT_MAX_ACTIVE_RUN_ACTIVITY_ROWS = _chat_tui._CHAT_MAX_ACTIVE_RUN_ACTIVITY_ROWS
_CHAT_STEER_INPUT_BG = _chat_tui._CHAT_STEER_INPUT_BG
_CHAT_STEER_INPUT_FG = _chat_tui._CHAT_STEER_INPUT_FG
_ChatLastRunPanel = _chat_tui._ChatLastRunPanel
_ChatPromptBox = _chat_tui._ChatPromptBox
_ChatQueueItem = _chat_tui._ChatQueueItem
_ChatRun = _chat_tui._ChatRun
_ChatSubmissionQueue = _chat_tui._ChatSubmissionQueue
_ChatToolCall = _chat_tui._ChatToolCall
_chat_active_step_line = _chat_tui._chat_active_step_line
_chat_ansi_style = _chat_tui._chat_ansi_style
_chat_child_completed_step_lines = _chat_tui._chat_child_completed_step_lines
_chat_display_len = _chat_tui._chat_display_len
_chat_executable_list_lines = _chat_tui._chat_executable_list_lines
_chat_flow_stage_detail_lines = _chat_tui._chat_flow_stage_detail_lines
_chat_flow_stage_line = _chat_tui._chat_flow_stage_line
_chat_friendly_error = _chat_tui._chat_friendly_error
_chat_header_lines = _chat_tui._chat_header_lines
_chat_help_lines = _chat_tui._chat_help_lines
_chat_input_block_line = _chat_tui._chat_input_block_line
_chat_local_command = _chat_tui._chat_local_command
_chat_markdown_width = _chat_tui._chat_markdown_width
_chat_model_command_selectors = _chat_tui._chat_model_command_selectors
_chat_model_list_lines = _chat_tui._chat_model_list_lines
_chat_plain_value = _chat_tui._chat_plain_value
_chat_prompt_style = _chat_tui._chat_prompt_style
_chat_record_system_event = _chat_tui._chat_record_system_event
_chat_render_markdown_lines = _chat_tui._chat_render_markdown_lines
_chat_run_lines = _chat_tui._chat_run_lines
_chat_run_state_line = _chat_tui._chat_run_state_line
_chat_scrollback_user_block = _chat_tui._chat_scrollback_user_block
_chat_set_executable_selector = _chat_tui._chat_set_executable_selector
_chat_steer_input_block = _chat_tui._chat_steer_input_block
_chat_tool_message_lines = _chat_tui._chat_tool_message_lines
_chat_ui_palette = _chat_tui._chat_ui_palette
_chat_visible_text = _chat_tui._chat_visible_text
_chat_write_lines = _chat_tui._chat_write_lines


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
    target: Annotated[str, typer.Argument(help="Thread id, run id, or run step path to inspect.")],
    tree: Annotated[bool, typer.Option("--tree", help="Show the step tree.")] = False,
    depth: Annotated[int, typer.Option("--depth", help="Step tree depth.")] = 1,
    limit: Annotated[int, typer.Option("--limit", help="Maximum thread runs to read.")] = 100,
) -> None:
    if limit < 1:
        raise click.ClickException("--limit must be at least 1")
    if depth < 1:
        raise click.ClickException("--depth must be at least 1")
    parsed = _parse_inspect_target(target)
    detail = _inspect_detail(ctx, parsed.identifier, limit=limit, include_thread=parsed.kind == "run")
    _render_inspect(detail, path=parsed.path, tree=tree, depth=depth)


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


@dataclass(frozen=True, slots=True)
class _InspectTarget:
    kind: Literal["thread", "run"]
    identifier: str
    path: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class _InspectStepNode:
    run: Mapping[str, Any]
    step: Mapping[str, Any]
    path: tuple[int, ...]
    children: tuple["_InspectStepNode", ...] = ()


def _parse_inspect_target(target: str) -> _InspectTarget:
    identifier, separator, raw_path = target.partition(":")
    identifier = identifier.strip()
    if not identifier:
        raise click.ClickException("inspect target is required")
    if separator and not identifier.startswith("run_"):
        raise click.ClickException("step paths are only supported for run targets")
    path = _parse_inspect_step_path(raw_path) if separator else ()
    kind: Literal["thread", "run"] = "run" if identifier.startswith("run_") else "thread"
    return _InspectTarget(kind=kind, identifier=identifier, path=path)


def _parse_inspect_step_path(raw_path: str) -> tuple[int, ...]:
    if not raw_path:
        raise click.ClickException("step path is required after ':'")
    pieces = raw_path.split(".")
    path: list[int] = []
    for piece in pieces:
        if not piece.isdecimal():
            raise click.ClickException(f"invalid step path: {raw_path}")
        value = int(piece)
        if value < 1:
            raise click.ClickException(f"invalid step path: {raw_path}")
        path.append(value)
    return tuple(path)


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
    data = {
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
    prompts = _inspect_run_prompt_bodies(store, data)
    if prompts:
        data["prompts"] = prompts
    return data


def _inspect_run_prompt_bodies(store: ExecutionStore, run: Mapping[str, Any]) -> dict[str, str]:
    prompts: dict[str, str] = {}
    for prompt_hash in _inspect_run_prompt_hashes(run):
        body = store.get_prompt(prompt_hash=prompt_hash)
        if body is not None:
            prompts[prompt_hash] = body
    return prompts


def _inspect_run_prompt_hashes(run: Mapping[str, Any]) -> tuple[str, ...]:
    hashes: list[str] = []
    for step in _run_steps(run):
        record = _mapping(step.get("record"))
        payload = _mapping(record.get("payload"))
        for key in ("instruct", "context"):
            value = _text(payload.get(key))
            if value is not None and value not in hashes:
                hashes.append(value)
    return tuple(hashes)


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


def _render_inspect(
    detail: Mapping[str, Any],
    *,
    path: tuple[int, ...],
    tree: bool,
    depth: int,
) -> None:
    if detail.get("kind") == "thread":
        if path:
            raise click.ClickException("step paths are only supported for run targets")
        _render_inspect_thread_timeline(_mapping(detail.get("thread")))
        return

    run = _mapping(detail.get("run"))
    thread = _mapping(detail.get("thread"))
    run_by_id = _inspect_thread_run_map(thread, fallback=run)
    run_id = _text(_mapping(run.get("info")).get("id"))
    display_run = run_by_id.get(run_id, run) if run_id is not None else run
    nodes = _inspect_step_tree(display_run, run_by_id=run_by_id)
    if path:
        node = _inspect_find_step_node(nodes, path)
        if node is None:
            raise click.ClickException(f"step path not found: {'.'.join(str(item) for item in path)}")
        _render_inspect_step_focus(node)
        return

    _render_inspect_run_summary(display_run)
    _render_inspect_step_nodes(nodes, depth=depth if tree else 1)


def _render_inspect_thread_timeline(thread: Mapping[str, Any]) -> None:
    thread_info = _mapping(thread.get("info"))
    runs = [_mapping(item) for item in _list(thread.get("runs"))]
    typer.echo(f"thread {_text(thread_info.get('id')) or '-'}  {_text(thread_info.get('status')) or '-'}")
    if title := _text(thread_info.get("title")):
        typer.echo(f"title   {title}")
    run_count = thread_info.get("run_count")
    if run_count is not None:
        typer.echo(f"runs    {run_count} total")
    typer.echo("runs")
    for run in _inspect_top_level_runs(runs):
        info = _mapping(run.get("info"))
        output = _mapping(run.get("output"))
        run_id = _text(info.get("id")) or "-"
        target = executable_label(
            _text(info.get("executable_kind")) or "run",
            _text(info.get("executable_name")),
            metadata=_mapping(info.get("metadata")),
        )
        status = _display_run_status(output.get("status"))
        elapsed = _inspect_elapsed(_text(info.get("started_at")), _text(info.get("finished_at")))
        pieces = [_inspect_status_mark(status), run_id, target]
        if elapsed:
            pieces.append(elapsed)
        typer.echo(f"  {'  '.join(pieces)}")


def _render_inspect_run_summary(run: Mapping[str, Any]) -> None:
    info = _mapping(run.get("info"))
    output = _mapping(run.get("output"))
    run_id = _text(info.get("id")) or "-"
    status = _display_run_status(output.get("status"))
    target = executable_label(
        _text(info.get("executable_kind")) or "run",
        _text(info.get("executable_name")),
        metadata=_mapping(info.get("metadata")),
    )
    typer.echo(f"run {run_id}  {status or '-'}  {target}")
    if failure := _inspect_failure_summary(run):
        typer.echo("failure")
        typer.echo(f"  {failure}")
    if input_summary := _inspect_run_input_summary(run):
        typer.echo(f"input  {input_summary}")


def _render_inspect_step_nodes(nodes: Sequence[_InspectStepNode], *, depth: int) -> None:
    if not nodes:
        return
    typer.echo("steps")
    for node in nodes:
        _render_inspect_step_node(node, depth=depth, level=0)


def _render_inspect_step_node(node: _InspectStepNode, *, depth: int, level: int) -> None:
    record = _mapping(node.step.get("record"))
    message = _mapping(node.step.get("message"))
    status = _display_run_status(record.get("status"))
    path = _inspect_step_path_label(node.path)
    kind = _text(record.get("kind")) or "step"
    summary = _inspect_step_summary(record, message)
    indent = "  " * (level + 1)
    line = f"{indent}{_inspect_status_mark(status)} {path} {kind}"
    if summary and summary != "-":
        line = f"{line}  {summary}"
    typer.echo(line)
    if level + 1 >= depth:
        return
    for child in node.children:
        _render_inspect_step_node(child, depth=depth, level=level + 1)


def _render_inspect_step_focus(node: _InspectStepNode) -> None:
    record = _mapping(node.step.get("record"))
    message = _mapping(node.step.get("message"))
    run_info = _mapping(node.run.get("info"))
    status = _display_run_status(record.get("status"))
    kind = _text(record.get("kind")) or "step"
    typer.echo(f"step {_inspect_step_path_label(node.path)} {kind}  {status or '-'}")
    if run_id := _text(run_info.get("id")):
        typer.echo(f"run  {run_id}")
    if error := _text(record.get("error")):
        typer.echo("error")
        typer.echo(f"  {error}")
    if not node.children:
        if kind == "model":
            _render_inspect_model_leaf(node)
            return
        if kind == "tool":
            _render_inspect_tool_leaf(node)
            return
        _render_inspect_generic_leaf(node)
        return
    input_items = _list(record.get("input"))
    if input_items:
        typer.echo("input")
        typer.echo(f"  {_inspect_compact_value(input_items)}")
    output = _list(record.get("output"))
    output_text = _message_summary(message)
    if not output_text and kind == "tool":
        output_text = _inspect_tool_output_text(record)
    if not output_text:
        output_text = _inspect_step_summary(record, message) or _parts_summary(output)
    if output_text or output:
        typer.echo("output")
        typer.echo(f"  {output_text or _inspect_compact_value(output)}")
    if node.children:
        typer.echo("children")
        for child in node.children:
            child_record = _mapping(child.step.get("record"))
            child_status = _display_run_status(child_record.get("status"))
            child_kind = _text(child_record.get("kind")) or "step"
            typer.echo(f"  {_inspect_status_mark(child_status)} {_inspect_step_path_label(child.path)} {child_kind}")


def _render_inspect_model_leaf(node: _InspectStepNode) -> None:
    record = _mapping(node.step.get("record"))
    payload = _mapping(record.get("payload"))
    prompts = _mapping(node.run.get("prompts"))
    typer.echo("model")
    _render_inspect_kv("ref", payload.get("model_ref"))
    _render_inspect_kv("provider", payload.get("provider"))
    _render_inspect_kv("model", payload.get("model"))
    _render_inspect_kv("adapter", payload.get("adapter"))
    _render_inspect_kv("base_url", payload.get("base_url"))
    _render_inspect_kv("input_tokens", payload.get("input_tokens"))
    _render_inspect_kv("output_tokens", payload.get("output_tokens"))

    request = _mapping(payload.get("adapter_request"))
    if request:
        _render_inspect_section("adapter_request", request)
    else:
        request = _inspect_reconstructed_model_request(node, prompts=prompts)
        _render_inspect_section("adapter_request", request)

    output = _list(record.get("output"))
    if output:
        _render_inspect_section("output", output)
    if reasoning := _text(payload.get("reasoning_content")):
        _render_inspect_text_section("reasoning_content", reasoning)


def _render_inspect_tool_leaf(node: _InspectStepNode) -> None:
    record = _mapping(node.step.get("record"))
    input_items = _list(record.get("input"))
    if input_items:
        _render_inspect_section("input_refs", input_items)
    outputs = _list(record.get("output"))
    if not outputs:
        return
    for index, part in enumerate(outputs, start=1):
        typed = _mapping(part)
        if typed.get("type") != "tool_result":
            _render_inspect_section(f"output_part_{index}", typed)
            continue
        typer.echo(f"tool_result {index}")
        _render_inspect_kv("tool", typed.get("tool_name") or typed.get("tool_family"))
        if "input" in typed:
            _render_inspect_section("input", typed.get("input"))
        if "output" in typed:
            _render_inspect_section("result", typed.get("output"))
        if "error" in typed:
            _render_inspect_section("error", typed.get("error"))


def _render_inspect_generic_leaf(node: _InspectStepNode) -> None:
    record = _mapping(node.step.get("record"))
    input_items = _list(record.get("input"))
    output = _list(record.get("output"))
    if input_items:
        _render_inspect_section("input", input_items)
    if output:
        _render_inspect_section("output", output)


def _render_inspect_kv(label: str, value: object) -> None:
    if value is None or value == "":
        return
    typer.echo(f"  {label}: {value}")


def _render_inspect_text_section(label: str, text: str) -> None:
    typer.echo(label)
    for line in text.splitlines() or [""]:
        typer.echo(f"  {line}")


def _render_inspect_section(label: str, value: object) -> None:
    if isinstance(value, str):
        _render_inspect_text_section(label, value)
        return
    typer.echo(label)
    for line in _inspect_full_value(value).splitlines():
        typer.echo(f"  {line}")


def _inspect_reconstructed_model_request(
    node: _InspectStepNode,
    *,
    prompts: Mapping[str, Any],
) -> dict[str, Any]:
    record = _mapping(node.step.get("record"))
    payload = _mapping(record.get("payload"))
    return {
        "instructions": _inspect_prompt_body(payload.get("instruct"), prompts=prompts),
        "context": _inspect_prompt_body(payload.get("context"), prompts=prompts),
        "messages": _inspect_messages_from_input_refs(node, _list(record.get("input"))),
        "tools": None,
        "state": None,
    }


def _inspect_prompt_body(value: object, *, prompts: Mapping[str, Any]) -> str | None:
    prompt_hash = _text(value)
    if prompt_hash is None:
        return None
    body = prompts.get(prompt_hash)
    return str(body) if body is not None else prompt_hash


def _inspect_messages_from_input_refs(
    node: _InspectStepNode,
    input_items: Sequence[Any],
    *,
    seen_steps: set[int] | None = None,
) -> list[Mapping[str, Any]]:
    seen = seen_steps or set()
    messages: list[Mapping[str, Any]] = []
    for item in input_items:
        typed = _mapping(item)
        kind = _text(typed.get("kind"))
        if kind == "message":
            message = _mapping(typed.get("message"))
            if message:
                messages.append(message)
            continue
        if kind == "command":
            command_message = _inspect_command_message(node.run, _int_or_none(typed.get("index")) or 0)
            if command_message:
                messages.append(command_message)
            continue
        if kind == "step":
            step_index = _int_or_none(typed.get("index"))
            if step_index is None or step_index in seen:
                continue
            seen.add(step_index)
            step = _inspect_run_step_by_index(node.run, step_index)
            if step is None:
                continue
            step_record = _mapping(step.get("record"))
            messages.extend(_inspect_messages_from_input_refs(node, _list(step_record.get("input")), seen_steps=seen))
            message = _inspect_step_output_message(step, part_index=_int_or_none(typed.get("part")))
            if message:
                messages.append(message)
    return messages


def _inspect_command_message(run: Mapping[str, Any], index: int) -> Mapping[str, Any] | None:
    for item in _list(run.get("inputs")):
        typed = _mapping(item)
        record = _mapping(typed.get("record"))
        if _int_or_none(record.get("index")) == index:
            message = _mapping(typed.get("message"))
            return message or None
    if index == 0:
        message = _mapping(run.get("input"))
        return message or None
    return None


def _inspect_run_step_by_index(run: Mapping[str, Any], step_index: int) -> Mapping[str, Any] | None:
    for step in _run_steps(run):
        record = _mapping(step.get("record"))
        if _int_or_none(record.get("step_index")) == step_index:
            return step
    return None


def _inspect_step_output_message(step: Mapping[str, Any], *, part_index: int | None) -> Mapping[str, Any] | None:
    message = _mapping(step.get("message"))
    if message:
        if part_index is None:
            return message
        parts = _list(message.get("parts"))
        if 0 <= part_index < len(parts):
            return {**message, "parts": [parts[part_index]]}
        return message
    record = _mapping(step.get("record"))
    role = _inspect_step_output_role(_text(record.get("kind")))
    if role is None:
        return None
    parts = _list(record.get("output"))
    if part_index is not None and 0 <= part_index < len(parts):
        parts = [parts[part_index]]
    if not parts:
        return None
    return {"role": role, "parts": parts}


def _inspect_step_output_role(kind: str | None) -> str | None:
    if kind == "model":
        return "assistant"
    if kind == "tool":
        return "tool"
    return None


def _inspect_step_tree(
    run: Mapping[str, Any],
    *,
    run_by_id: Mapping[str, Mapping[str, Any]],
    path_prefix: tuple[int, ...] = (),
) -> tuple[_InspectStepNode, ...]:
    nodes: list[_InspectStepNode] = []
    run_id = _text(_mapping(run.get("info")).get("id"))
    for step in _run_steps(run):
        record = _mapping(step.get("record"))
        step_index = _int_or_none(record.get("step_index"))
        if step_index is None:
            continue
        path = (*path_prefix, step_index)
        children: list[_InspectStepNode] = []
        if run_id is not None:
            for child_run in _inspect_child_runs_for_step(
                run_id,
                step_index,
                step=step,
                run_by_id=run_by_id,
            ):
                children.extend(_inspect_step_tree(child_run, run_by_id=run_by_id, path_prefix=path))
        nodes.append(_InspectStepNode(run=run, step=step, path=path, children=tuple(children)))
    return tuple(nodes)


def _inspect_child_runs_for_step(
    run_id: str,
    step_index: int,
    *,
    step: Mapping[str, Any],
    run_by_id: Mapping[str, Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    child_runs: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    record = _mapping(step.get("record"))
    payload = _mapping(record.get("payload"))
    for child_id in child_run_ids(payload, record):
        child = run_by_id.get(child_id)
        if child is not None and child_id not in seen:
            child_runs.append(child)
            seen.add(child_id)
    for child in run_by_id.values():
        info = _mapping(child.get("info"))
        child_id = _text(info.get("id"))
        if child_id is None or child_id in seen:
            continue
        if _text(info.get("parent_run_id")) != run_id:
            continue
        if _int_or_none(info.get("parent_step_index")) != step_index:
            continue
        child_runs.append(child)
        seen.add(child_id)
    return child_runs


def _inspect_find_step_node(
    nodes: Sequence[_InspectStepNode],
    path: tuple[int, ...],
) -> _InspectStepNode | None:
    for node in nodes:
        if node.path == path:
            return node
        if path[: len(node.path)] == node.path:
            found = _inspect_find_step_node(node.children, path)
            if found is not None:
                return found
    return None


def _inspect_step_path_label(path: Sequence[int]) -> str:
    return ".".join(str(item) for item in path)


def _inspect_status_mark(status: str) -> str:
    if status == "succeeded":
        return "✓"
    if status == "failed":
        return "✗"
    if status == "canceled":
        return "-"
    if status == "running":
        return "…"
    return "·"


def _inspect_elapsed(started_at: str | None, finished_at: str | None) -> str:
    if not started_at or not finished_at:
        return ""
    start = _parse_utc_timestamp(started_at)
    finish = _parse_utc_timestamp(finished_at)
    if start is None or finish is None:
        return ""
    seconds = max((finish - start).total_seconds(), 0)
    if seconds < 1:
        return f"{max(round(seconds * 1000), 1)}ms"
    if seconds < 10:
        return f"{seconds:.1f}s"
    return f"{seconds:.0f}s"


def _inspect_compact_value(value: object) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except TypeError:
        text = str(value)
    return _truncate_table_text(text, width=96)


def _inspect_full_value(value: object) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, indent=2)
    except TypeError:
        return str(value)


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
        text = _message_summary(message) or _parts_summary(record.get("output"))
        requests = "; ".join(line.removeprefix("requested ") for line in _inspect_tool_request_lines(record))
        request_summary = f"requested {requests}" if requests else ""
        return " ".join(item for item in (model, text, request_summary) if item)
    if kind == "tool":
        return _inspect_tool_result_summary(record)
    if kind == "run":
        return child_call_summary(payload)
    if kind in {"step", "parallel", "bind"}:
        return flow_op_summary(payload)
    text = _parts_summary(record.get("output"))
    return text or _text(record.get("error")) or "-"


def _inspect_tool_result_summary(record: Mapping[str, Any]) -> str:
    for part in _list(record.get("output")):
        typed = _mapping(part)
        if typed.get("type") != "tool_result":
            continue
        name = _text(typed.get("tool_name")) or _text(typed.get("tool_family")) or "tool"
        tool_input = _inspect_tool_input_summary(typed.get("input"))
        suffix = f": {tool_input}" if tool_input else ""
        return f"{name}{suffix}"
    return _parts_summary(record.get("output")) or _text(record.get("error")) or "-"


def _inspect_tool_output_text(record: Mapping[str, Any]) -> str:
    messages: list[str] = []
    for part in _list(record.get("output")):
        typed = _mapping(part)
        if typed.get("type") != "tool_result":
            continue
        output = typed.get("output")
        if isinstance(output, str):
            messages.append(output.strip())
        elif output is not None:
            messages.append(_inspect_compact_value(output))
    return _truncate_table_text(" ".join(item for item in messages if item), width=96)


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


def _chat_home_label(ctx: typer.Context) -> str:
    try:
        agent_name = _context_agent(ctx)
        if agent_name is None:
            return "agent home"
        return str(agents.agent_home(_context_root(ctx), agent_name))
    except Exception:
        return "agent home"


def _chat_tui_dependencies() -> _chat_tui.ChatTuiDependencies:
    return _chat_tui.ChatTuiDependencies(
        runtime_json=lambda ctx, path: _runtime_json(ctx, path),
        runtime_post=lambda *args, **kwargs: _runtime_post(*args, **kwargs),
        runtime_consume_stream=lambda *args, **kwargs: _runtime_consume_stream(*args, **kwargs),
        message_payload=lambda text: _message_payload(text),
        input_history_store=lambda ctx: _chat_input_history_store(ctx),
        home_label=lambda ctx: _chat_home_label(ctx),
        write_lines=lambda *args, **kwargs: _chat_write_lines(*args, **kwargs),
    )


class _ChatBottomApp(_chat_tui._ChatBottomApp):
    def __init__(self, ctx: typer.Context, *, thread_id: str | None, selector_payload: dict[str, object]) -> None:
        super().__init__(
            ctx,
            thread_id=thread_id,
            selector_payload=selector_payload,
            deps=_chat_tui_dependencies(),
        )


def _chat_interactive_prompt_toolkit(
    ctx: typer.Context,
    *,
    thread_id: str | None,
    selector_payload: dict[str, object] | None = None,
) -> None:
    _chat_tui._chat_interactive_prompt_toolkit(
        ctx,
        thread_id=thread_id,
        selector_payload=selector_payload,
        deps=_chat_tui_dependencies(),
    )


def _chat_resolve_model_command_labels(ctx: typer.Context, selectors: Sequence[str]) -> tuple[str, ...] | None:
    return _chat_tui._chat_resolve_model_command_labels(ctx, selectors, deps=_chat_tui_dependencies())


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
