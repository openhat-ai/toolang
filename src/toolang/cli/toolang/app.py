"""Typer CLI for Toolang agent management."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
import json
from pathlib import Path
import os
import subprocess
import sys
import time
import tomllib
from typing import Annotated, Any, Literal, TYPE_CHECKING, cast
from urllib.parse import urlencode
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import click
import typer
from typer.core import TyperCommand, TyperGroup

from ... import agents
from ...config.log import LoggingPlan, configure_logging, configure_logging_plan, resolve_agent_logging
from ..utils import (
    _PrefixAgentWorkGroup,
    _RequiredPrefixAgentCommand,
    _RunAgentCommand,
    _RuntimeAgentCommand,
    _StartAgentCommand,
    _agent_avatar,
    _append_agent_update,
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
CAPS_COMMAND_PANEL = "Caps Commands"
TOP_LEVEL_COMMANDS = frozenset(
    {
        "new",
        "clone",
        "remove",
        "list",
        "info",
        "fmt",
        "model",
        "tool",
        "channel",
        "sandbox",
        "chat",
        "send",
        "attach",
        "threads",
        "runs",
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
_THREAD_PANEL_COMMAND_ORDER = ("chat", "steer", "cancel", "rewind", "fork", "runs", "threads")
_RUNTIME_PANEL_COMMAND_ORDER = ("model", "tool", "channel", "sandbox")


class _ToolangGroup(TyperGroup):
    def list_commands(self, ctx: click.Context) -> list[str]:
        names = TyperGroup.list_commands(self, ctx)
        thread_names = [name for name in _THREAD_PANEL_COMMAND_ORDER if name in names]
        if thread_names:
            reordered = [name for name in names if name not in thread_names]
            runtime_indexes = [reordered.index(name) for name in _RUNTIME_PANEL_COMMAND_ORDER if name in reordered]
            insertion_index = min(runtime_indexes) if runtime_indexes else len(reordered)
            names = reordered[:insertion_index] + thread_names + reordered[insertion_index:]
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


POSTFIX_AGENT_COMMANDS = frozenset(
    {"run", "start", "stop", "info", "chat", "send", "attach", "threads", "runs", "steer", "cancel", "rewind", "fork"}
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
    help="Start, continue, or open a thread.",
    cls=_RequiredPrefixAgentCommand,
    rich_help_panel=THREAD_COMMAND_PANEL,
)
def chat_command(
    ctx: typer.Context,
    message: Annotated[str | None, typer.Argument(help="Message text.")] = None,
    thread: Annotated[str | None, typer.Option("--thread", help="Thread or run id.")] = None,
    tui: Annotated[bool, typer.Option("--tui", help="Open the terminal UI.")] = False,
    model: Annotated[str | None, typer.Option("--model", help="Model selector.")] = None,
) -> None:
    thread_id = _target_thread_id(ctx, thread) if thread is not None else None
    if message is None:
        if thread_id is None:
            result = _runtime_post(ctx, "/api/v1/threads", payload={"client": "tui"})
            created = result.get("thread_id")
            if not isinstance(created, str):
                raise click.ClickException("runtime did not return a thread id")
            thread_id = created
        if tui:
            _open_thread_ui(ctx, thread_id)
            return
        _chat_interactive(ctx, thread_id=thread_id, model=model)
        return
    payload: dict[str, Any] = {"thread": thread_id, "client": "tui", "message": _message_payload(message)}
    if model is not None:
        payload["model"] = model
    if tui:
        result = _runtime_post(ctx, "/api/v1/chat", payload=payload)
        thread = result.get("thread_id")
        if isinstance(thread, str):
            _open_thread_ui(ctx, thread)
        typer.echo(json.dumps(result, ensure_ascii=False, indent=2))
        return
    _runtime_stream(ctx, "/api/v1/chat/stream", payload=payload)


@app.command("send", hidden=True, cls=_RequiredPrefixAgentCommand)
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


@app.command("attach", hidden=True, cls=_RequiredPrefixAgentCommand)
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
    result = _runtime_json(ctx, path)
    rows = [
        (
            str(item.get("id", "")),
            _truncate_table_text(item.get("title"), width=48),
            str(item.get("run_count", "")),
            str(item.get("origin", "")),
            str(item.get("channel", "")),
            str(item.get("status", "")),
            str(item.get("updated_at", "")),
        )
        for item in result.get("items", [])
        if isinstance(item, dict)
    ]
    _echo_table(("THREAD", "TITLE", "RUNS", "ORIGIN", "CHANNEL", "STATUS", "UPDATED"), rows)


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
    result = _runtime_json(ctx, path)
    rows = [
        (
            str(item.get("id", "")),
            _truncate_table_text(item.get("summary") or item.get("input_text"), width=48),
            str(item.get("thread_id", "")),
            str(item.get("origin", "")),
            _display_run_status(item.get("status")),
            str(item.get("created_at", "")),
        )
        for item in result.get("items", [])
        if isinstance(item, dict)
    ]
    _echo_table(("RUN", "TITLE", "THREAD", "ORIGIN", "STATUS", "CREATED"), rows)


@app.command("steer", help="Guide an active run.", cls=_RequiredPrefixAgentCommand, rich_help_panel=THREAD_COMMAND_PANEL)
def steer_command(
    ctx: typer.Context,
    target: str = typer.Argument(..., help="Run id or thread id."),
    message: str = typer.Argument(..., help="Steering message."),
    tui: Annotated[bool, typer.Option("--tui", help="Open the terminal UI.")] = False,
) -> None:
    run_id = _target_run_id(ctx, target)
    _runtime_post(ctx, f"/api/v1/runs/{run_id}/steer", payload={"message": _message_payload(message)})
    if tui:
        _open_thread_ui(ctx, _target_thread_id(ctx, target))
    typer.echo(f"steered {run_id}")


@app.command("cancel", help="Cancel an active run.", cls=_RequiredPrefixAgentCommand, rich_help_panel=THREAD_COMMAND_PANEL)
def cancel_command(
    ctx: typer.Context,
    target: str = typer.Argument(..., help="Run id or thread id."),
    tui: Annotated[bool, typer.Option("--tui", help="Open the terminal UI.")] = False,
) -> None:
    run_id = _target_run_id(ctx, target)
    _runtime_post(ctx, f"/api/v1/runs/{run_id}/cancel", payload={})
    if tui:
        _open_thread_ui(ctx, _target_thread_id(ctx, target))
    typer.echo(f"canceled {run_id}")


@app.command("rewind", help="Rewind a thread from a run.", cls=_RequiredPrefixAgentCommand, rich_help_panel=THREAD_COMMAND_PANEL)
def rewind_command(
    ctx: typer.Context,
    target: str = typer.Argument(..., help="Run id or thread id."),
    message: Annotated[str | None, typer.Argument(help="Replacement message.")] = None,
    tui: Annotated[bool, typer.Option("--tui", help="Open the terminal UI.")] = False,
) -> None:
    run_id = _target_latest_run_id(ctx, target)
    payload = {"message": _message_payload(message)} if message is not None else {}
    result = _runtime_post(ctx, f"/api/v1/runs/{run_id}/rewind", payload=payload)
    if tui:
        thread = result.get("thread_id")
        _open_thread_ui(ctx, str(thread) if isinstance(thread, str) else _target_thread_id(ctx, target))
    typer.echo(f"rewound {result.get('thread_id')}\t{result.get('run_id')}")


@app.command("fork", help="Fork a thread from a run.", cls=_RequiredPrefixAgentCommand, rich_help_panel=THREAD_COMMAND_PANEL)
def fork_command(
    ctx: typer.Context,
    target: str = typer.Argument(..., help="Run id or thread id."),
    message: Annotated[str | None, typer.Argument(help="Fork message.")] = None,
    tui: Annotated[bool, typer.Option("--tui", help="Open the terminal UI.")] = False,
) -> None:
    run_id = _target_latest_run_id(ctx, target)
    payload = {"message": _message_payload(message)} if message is not None else {}
    result = _runtime_post(ctx, f"/api/v1/runs/{run_id}/fork", payload=payload)
    if tui:
        thread = result.get("thread_id")
        if isinstance(thread, str):
            _open_thread_ui(ctx, thread)
    typer.echo(f"forked {result.get('thread_id')}\t{result.get('run_id')}")


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


def _message_payload(text: str) -> dict[str, object]:
    return {
        "role": "user",
        "parts": [{"type": "text", "text": text}],
    }


def _query_params(**items: str | None) -> str:
    return urlencode([(key, value) for key, value in items.items() if value is not None])


def _api_run_status(status: str) -> str:
    return "finished" if status == "succeeded" else status


def _display_run_status(status: object) -> str:
    text = str(status or "")
    return "succeeded" if text == "finished" else text


def _open_thread_ui(ctx: typer.Context, thread_id: str | None) -> None:
    if thread_id is None:
        raise click.ClickException("terminal UI requires an existing thread; pass a message to start a terminal chat")
    _chat_interactive(ctx, thread_id=thread_id, model=None)


def _chat_interactive(ctx: typer.Context, *, thread_id: str, model: str | None) -> None:
    typer.echo(f"thread {thread_id}")
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
        payload: dict[str, Any] = {"thread": thread_id, "client": "tui", "message": _message_payload(text)}
        if model is not None:
            payload["model"] = model
        _runtime_stream(ctx, "/api/v1/chat/stream", payload=payload)


def _target_thread_id(ctx: typer.Context, target: str | None) -> str | None:
    if target is None:
        return None
    if target.startswith("run_"):
        detail = _runtime_json(ctx, f"/api/v1/runs/{target}")
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
    detail = _runtime_json(ctx, f"/api/v1/threads/{target}")
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
    detail = _runtime_json(ctx, f"/api/v1/threads/{target}")
    info = detail.get("info")
    if not isinstance(info, dict):
        raise click.ClickException(f"thread not found: {target}")
    latest = info.get("latest_run")
    if not isinstance(latest, dict) or not isinstance(latest.get("id"), str):
        raise click.ClickException(f"thread has no runs: {target}")
    return str(latest["id"])


def _resolve_runtime_startup(
    ctx: typer.Context,
    target: agents.MaterializedRunTarget,
    *,
    sandbox: str | None,
    models: list[str] | None,
    tools: list[str] | None,
    caps: list[str] | None,
    components: list[str] | None,
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
) -> None:
    from ...models.errors import NO_AVAILABLE_MODELS_MESSAGE
    from ...models.resolution import split_model_selectors

    environ = dict(os.environ)
    root = _toolang_root(None)
    selectors = split_model_selectors(tuple(filter_ or ()))
    rows = _model_rows(root, environ, model_selectors=selectors)
    if not rows:
        if selectors and _model_rows(root, environ):
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
    )


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
