"""Typer CLI for Toolang agent management."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
import sys
import time
from datetime import datetime, timezone
from collections.abc import Callable, Sequence
from typing import Annotated, Literal, cast
from urllib.parse import urlparse

import click
import humanize
from rich import box
from rich.console import Console
from rich.table import Table
from rich.text import Text
import typer
from typer.core import TyperArgument, TyperCommand
from typer import rich_utils as typer_rich_utils

from . import agents, caps, templates, work
from . import up as agent_up
from .config.env import load_runtime_environ
from .config.web import resolve_ui_base_url
from .execution.records import UpdateKind
from .execution.db import ExecutionStore, execution_db_path
from .state.prepared import EntryKind, PreparedEntry, PreparedScope
from .templates import TemplateKind

CapKind = Literal["skill", "psyche", "prompt", "service"]
WorkKind = Literal["task", "chore"]
CapListFilter = Literal["global", "agent"]
_CLI_PREFIX_AGENT: str | None = None
TOP_LEVEL_COMMANDS = frozenset(
    {
        "new",
        "clone",
        "remove",
        "list",
        "info",
        "run",
        "start",
        "stop",
        "task",
        "chore",
        "skill",
        "psyche",
        "service",
        "prompt",
    }
)
POSTFIX_AGENT_COMMANDS = frozenset({"run", "start", "stop", "info"})
PREFIX_AGENT_COMMANDS = frozenset({"run", "start", "stop", "task", "chore", "skill", "psyche", "service", "prompt"})
PREFIX_OPTIONAL_AGENT_COMMANDS = frozenset({"skill", "psyche", "service", "prompt"})
PREFIX_REQUIRED_AGENT_COMMANDS = frozenset({"task", "chore"})

# Typer renders command help text in dim style by default. Keep it at normal
# weight so usage notes remain easy to read in terminal help output.
setattr(typer_rich_utils, "STYLE_HELPTEXT", "")
_TABLE_CONSOLE = Console(highlight=False, width=4096)
_AGENT_AVATAR = templates.load_info_avatar()
_PALETTE_STYLES_TOP, _PALETTE_STYLES_BOTTOM = templates.load_info_palette()
_RAINBOW_STYLES = _PALETTE_STYLES_TOP


class _PrefixAgentCommand(TyperCommand):
    """Render one virtual prefix-agent argument in help output."""

    prefix_agent_metavar = "[AGENT]"
    argument_metavar = "TEXT"
    argument_help = "Apply to this agent instead of global scope."

    def _real_params(self, ctx: click.Context) -> list[click.Parameter]:
        return TyperCommand.get_params(self, ctx)

    def _prefix_agent_argument(self) -> click.Argument:
        return _HelpOnlyTyperArgument(
            param_decls=["agent"],
            metavar=self.argument_metavar,
            required=False,
            default=None,
            expose_value=False,
            help=self.argument_help,
        )

    def get_params(self, ctx: click.Context) -> list[click.Parameter]:
        return [self._prefix_agent_argument(), *self._real_params(ctx)]

    def format_usage(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        command_path = ctx.command_path
        root_name, _, remainder = command_path.partition(" ")
        prefix_path = (
            f"{root_name} {self.prefix_agent_metavar} {remainder}"
            if remainder
            else f"{root_name} {self.prefix_agent_metavar}"
        )
        pieces = [self.options_metavar] if self.options_metavar else []
        for param in self._real_params(ctx):
            pieces.extend(param.get_usage_pieces(ctx))
        formatter.write_usage(prefix_path, " ".join(pieces))


class _OptionalPrefixAgentCommand(_PrefixAgentCommand):
    prefix_agent_metavar = "[AGENT]"


class _RequiredPrefixAgentCommand(_PrefixAgentCommand):
    prefix_agent_metavar = "AGENT"
    argument_help = "Agent name."

    def _prefix_agent_argument(self) -> click.Argument:
        return _HelpOnlyTyperArgument(
            param_decls=["agent"],
            metavar=self.argument_metavar,
            required=True,
            default=None,
            expose_value=False,
            help=self.argument_help,
        )


class _HelpOnlyTyperArgument(TyperArgument):
    """One help-only argument that never participates in parsing."""

    def make_metavar(self, ctx: click.Context | None = None) -> str:
        del ctx
        return self.metavar or "TEXT"

    def add_to_parser(self, parser: object, ctx: click.Context) -> None:
        del parser, ctx

    def handle_parse_result(
        self,
        ctx: click.Context,
        opts: click.core.cabc.Mapping[str, object],
        args: list[str],
    ) -> tuple[None, list[str]]:
        del ctx, opts
        return None, args


class _RuntimeAgentCommand(TyperCommand):
    """Render one required agent argument before the command name in help."""

    usage_agent_metavar = "AGENT"

    def _real_params(self, ctx: click.Context) -> list[click.Parameter]:
        return TyperCommand.get_params(self, ctx)

    def _visible_real_params(self, ctx: click.Context) -> list[click.Parameter]:
        return [param for param in self._real_params(ctx) if not getattr(param, "hidden", False)]

    def _help_agent_argument(self) -> click.Argument:
        return _HelpOnlyTyperArgument(
            param_decls=["agent"],
            metavar="TEXT",
            required=True,
            default=None,
            expose_value=False,
            help="Agent name.",
        )

    def get_params(self, ctx: click.Context) -> list[click.Parameter]:
        return [self._help_agent_argument(), *self._real_params(ctx)]

    def format_usage(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        command_path = ctx.command_path
        root_name, _, remainder = command_path.partition(" ")
        prefix_path = (
            f"{root_name} {self.usage_agent_metavar} {remainder}"
            if remainder
            else f"{root_name} {self.usage_agent_metavar}"
        )
        pieces = [self.options_metavar] if self.options_metavar else []
        for param in self._visible_real_params(ctx):
            pieces.extend(param.get_usage_pieces(ctx))
        formatter.write_usage(prefix_path, " ".join(pieces))


class _OptionalTemplateArgumentCommand(TyperCommand):
    """Render one optional template argument as plain TEXT in help."""

    def _real_params(self, ctx: click.Context) -> list[click.Parameter]:
        return TyperCommand.get_params(self, ctx)

    def _help_template_argument(self) -> click.Argument:
        return _HelpOnlyTyperArgument(
            param_decls=["template"],
            metavar="TEXT",
            required=False,
            default="default",
            expose_value=False,
            help="Template name.",
        )

    def get_params(self, ctx: click.Context) -> list[click.Parameter]:
        return [self._help_template_argument(), *self._real_params(ctx)]

    def format_usage(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        pieces: list[str] = [self.options_metavar] if self.options_metavar else []
        for param in self._real_params(ctx):
            pieces.extend(param.get_usage_pieces(ctx))
        formatter.write_usage(ctx.command_path, " ".join(pieces))


class _OptionalPrefixAgentTemplateCommand(_OptionalPrefixAgentCommand):
    def _help_template_argument(self) -> click.Argument:
        return _HelpOnlyTyperArgument(
            param_decls=["template"],
            metavar="TEXT",
            required=False,
            default="default",
            expose_value=False,
            help="Template name.",
        )

    def get_params(self, ctx: click.Context) -> list[click.Parameter]:
        return [
            self._prefix_agent_argument(),
            self._help_template_argument(),
            *self._real_params(ctx),
        ]

app = typer.Typer(
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
        typer.Option("--root", "-r", help="Root directory for all agents."),
    ] = None,
) -> None:
    """Toolang CLI."""

    ctx.obj = {
        "toolang_root": _toolang_root(toolang_root),
        "agent": _CLI_PREFIX_AGENT,
    }


@app.command("new", help="Create an agent.", no_args_is_help=True)
def new_agent(
    ctx: typer.Context,
    agent: Annotated[str, typer.Argument(help="Agent name")],
    template: Annotated[
        str,
        typer.Option("--template", "-t", help="Template name."),
    ] = "default",
) -> None:
    program_path = _wrap_user_error(
        agents.create_agent,
        _context_root(ctx),
        agent,
        template_name=template,
    )
    _append_agent_update(
        _context_root(ctx),
        agent,
        "created",
        {"path": str(program_path)},
    )
    typer.echo(str(program_path))


@app.command("clone", help="Clone an agent.", no_args_is_help=True)
def clone_agent(
    ctx: typer.Context,
    source: Annotated[str, typer.Argument(help="Agent source selector.")],
    target: Annotated[str | None, typer.Argument(help="New local agent name.")] = None,
) -> None:
    program_path = _wrap_user_error(agents.clone_agent, _context_root(ctx), source, target)
    target_name = program_path.stem
    _append_agent_update(
        _context_root(ctx),
        target_name,
        "created",
        {"path": str(program_path), "source": source},
    )
    typer.echo(str(program_path))


@app.command("remove", help="Remove an agent.", no_args_is_help=True)
def remove_agent(
    ctx: typer.Context,
    agent: Annotated[str, typer.Argument(help="Agent name")],
) -> None:
    _wrap_user_error(agents.remove_agent, _context_root(ctx), agent)
    typer.echo(f"{agent}\tremoved")


@app.command("list", help="Show agents and their status.")
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
            item.api_url or "-",
            item.webui_url or "-",
        )
        for item in items
    ]
    _echo_table(("AGENT", "STATUS", "SANDBOX", "API", "WEBUI"), rows)


@app.command("info", help="Show agent info.", no_args_is_help=True)
def info_agent(
    ctx: typer.Context,
    agent: Annotated[str, typer.Argument(help="Agent name")],
) -> None:
    root = _context_root(ctx)
    status = _wrap_user_error(
        agents.get_agent_status,
        root,
        agent,
        ui_base_url=_ui_base_url(),
    )
    if status is None:
        raise click.ClickException(f"agent not found: {agent}")
    runtime_state = agents.load_runtime_state(root, agent) or {}
    created_at = _created_time(agents.agent_home(root, agent))
    started_at = _runtime_value(runtime_state.get("started_at"))
    updated_at = _runtime_value(runtime_state.get("updated_at"))
    status_value = status.status
    if status.status == "running" and started_at != "-":
        online = _human_uptime_since(started_at)
        if online is not None:
            status_value = f"{status.status} ({online})"
    rows = [
        ("Home", str(agents.agent_home(root, agent))),
        ("Caps", _info_caps_summary(root, agent)),
        ("Jobs", _info_jobs_summary(root, agent)),
        ("Status", status_value),
    ]
    if status.status == "stopped":
        rows.append(("Created", created_at))
        _echo_pairs_table(rows, avatar=_AGENT_AVATAR, title=agent.upper())
        return
    if status.sandbox:
        rows.append(("Sandbox", status.sandbox))
    if status.status == "running":
        loops_text = _runtime_loops(runtime_state)
        if loops_text is not None:
            rows.append(("Loops", loops_text))
    message = _runtime_value(runtime_state.get("message"))
    pid_text = agents.runtime_pid_label(runtime_state)
    if pid_text is not None and status.status != "stopped":
        rows.append(("PID", pid_text))
    if status.api_url:
        rows.append(("API", status.api_url))
    if status.webui_url:
        rows.append(("WebUI", status.webui_url))
    if status.status == "running" and started_at != "-":
        rows.append(("Started", started_at))
        rows.append(("Created", created_at))
    if status.status != "running" and updated_at != "-":
        rows.append(("Updated", updated_at))
    if status.status != "running" and message != "-":
        rows.append(("Message", message))
    _echo_pairs_table(rows, avatar=_AGENT_AVATAR, title=agent.upper())


@app.command(
    "run",
    help="Run an agent in the foreground.",
    no_args_is_help=True,
    cls=_RuntimeAgentCommand,
)
def run_agent(
    ctx: typer.Context,
    agent: str | None = typer.Argument(None, help="Agent selector", hidden=True),
    sandbox: Annotated[
        str | None,
        typer.Option(help="Sandbox to use: none or <driver>[:target]."),
    ] = None,
    model: Annotated[
        str | None,
        typer.Option("--model", help="Default model selector for this activation."),
    ] = None,
    loops: Annotated[
        list[str] | None,
        typer.Option("--loop", help="Runtime loop to enable. Repeat or pass CSV."),
    ] = None,
    port: Annotated[int | None, typer.Option(help="Port to listen on.")] = None,
    host: Annotated[str, typer.Option(help="Host interface to bind.")] = "127.0.0.1",
    dev: Annotated[
        Path | None,
        typer.Option(
            "--dev",
            help="Wheel file, or a directory tree containing wheels, for managed sandbox startup.",
        ),
    ] = None,
    public_host: Annotated[
        str | None,
        typer.Option("--public-host", help="Published host name.", hidden=True),
    ] = None,
    sandbox_child: Annotated[
        bool,
        typer.Option("--sandbox-child", hidden=True),
    ] = False,
) -> None:
    selector = _required_runtime_agent(ctx, agent)
    normalized_loops = _normalize_loop_option(loops)
    root = _context_root(ctx)
    try:
        with agents.materialized_run_target(root, selector) as (run_root, agent_name):
            raise typer.Exit(
                _wrap_user_error(
                    agent_up.up,
                    toolang_root=run_root,
                    agent_name=agent_name,
                    host=host,
                    public_host=public_host,
                    port=port,
                    sandbox=sandbox,
                    model=model,
                    dev=dev,
                    sandbox_child=sandbox_child,
                    loop_names=normalized_loops,
                    environ=_runtime_environ_for_agent(ctx, agent_name, toolang_root=run_root),
                )
            )
    except (FileExistsError, FileNotFoundError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc


@app.command(
    "start",
    help="Start an agent.",
    no_args_is_help=True,
    cls=_RuntimeAgentCommand,
)
def start_agent(
    ctx: typer.Context,
    agent: str | None = typer.Argument(None, help="Agent name", hidden=True),
    sandbox: Annotated[
        str | None,
        typer.Option(help="Sandbox to use: none or <driver>[:target]."),
    ] = None,
    model: Annotated[
        str | None,
        typer.Option("--model", help="Default model selector for this activation."),
    ] = None,
    loops: Annotated[
        list[str] | None,
        typer.Option("--loop", help="Runtime loop to enable. Repeat or pass CSV."),
    ] = None,
    port: Annotated[int | None, typer.Option(help="Port to listen on.")] = None,
    host: Annotated[str, typer.Option(help="Host interface to bind.")] = "127.0.0.1",
    dev: Annotated[
        Path | None,
        typer.Option(
            "--dev",
            help="Wheel file, or a directory tree containing wheels, for managed sandbox startup.",
        ),
    ] = None,
    public_host: Annotated[
        str | None,
        typer.Option("--public-host", help="Published host name.", hidden=True),
    ] = None,
) -> None:
    selector = _required_runtime_agent(ctx, agent)
    parsed_selector = _wrap_user_error(agents.parse_agent_selector, selector)
    if parsed_selector.form != "name":
        raise click.ClickException("start only supports local agent names; clone the remote source first")
    agent_name = parsed_selector.name or ""
    root = _context_root(ctx)
    normalized_loops = _normalize_loop_option(loops)
    existing = agents.get_agent_status(root, agent_name, ui_base_url=_ui_base_url())
    if existing is not None and existing.status in {"running", "preparing", "starting"}:
        raise click.ClickException(f"agent is already active: {agent_name}")

    environ = _runtime_environ_for_agent(ctx, agent_name)
    environ["TOOLANG_ROOT"] = str(root)
    startup = _wrap_user_error(
        agent_up.resolve_startup,
        toolang_root=root,
        agent_name=agent_name,
        host=host,
        public_host=public_host,
        port=port,
        sandbox=sandbox,
        model=model,
        dev=dev,
        loop_names=normalized_loops,
        environ=environ,
    )
    log_path = agents.agent_runtime_log_path(root, agent_name)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    launched_at = time.time()
    command = [
        sys.executable,
        "-m",
        "toolang.cli",
        *agent_up.build_run_argv(startup),
    ]
    with log_path.open("ab") as stream:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=stream,
            stderr=stream,
            env=environ,
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
        raise click.ClickException(f"agent start timed out: {agent_name} (see {log_path})")
    if status.status == "failed":
        raise click.ClickException(f"{agent_name}\tfailed\t{status.endpoint or '-'}\t{log_path}")
    typer.echo(_format_runtime_row(status))


@app.command(
    "stop",
    help="Stop an agent.",
    no_args_is_help=True,
    cls=_RuntimeAgentCommand,
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
    runtime_state = agents.load_runtime_state(_context_root(ctx), agent_name)
    if runtime_state is None:
        raise click.ClickException(f"Agent is not running: {agent_name}")

    sandbox_plugin = None
    sandbox = runtime_state.get("sandbox")
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
        _context_root(ctx),
        agent_name,
        sandbox_plugin=sandbox_plugin,
        force=force,
    )
    typer.echo(f"{agent_name}\tstopped" if stopped else f"{agent_name}\talready-stopped")


def register_cap_commands() -> None:
    cap_titles: dict[CapKind, str] = {
        "psyche": "Psyche",
        "skill": "Skill",
        "service": "Service",
        "prompt": "Prompt",
    }
    cap_group_help: dict[CapKind, str] = {
        "psyche": "Manage psyches.",
        "skill": "Manage skills.",
        "service": "Manage services.",
        "prompt": "Manage prompts.",
    }
    cap_list_help: dict[CapKind, str] = {
        "psyche": "List available psyches.",
        "skill": "List available skills.",
        "service": "List available services.",
        "prompt": "List available prompts.",
    }

    @dataclass(frozen=True, slots=True)
    class CapCommandSpec:
        name: str
        help: Callable[[CapKind], str]
        factory: Callable[[CapKind, str], Callable[..., None]]
        no_args_is_help: bool = False

    command_specs: tuple[CapCommandSpec, ...] = (
        CapCommandSpec(
            name="list",
            help=lambda kind: cap_list_help[kind],
            factory=_make_cap_list_command,
        ),
        CapCommandSpec(
            name="new",
            help=lambda kind: f"Create a local {kind}.",
            factory=_make_new_cap_command,
            no_args_is_help=True,
        ),
        CapCommandSpec(
            name="edit",
            help=lambda kind: f"Edit a local {kind}.",
            factory=_make_edit_cap_command,
            no_args_is_help=True,
        ),
        CapCommandSpec(
            name="add",
            help=lambda kind: f"Add a remote {kind}.",
            factory=_make_add_cap_command,
            no_args_is_help=True,
        ),
        CapCommandSpec(
            name="remove",
            help=lambda kind: f"Remove a {kind}.",
            factory=_make_remove_cap_command,
            no_args_is_help=True,
        ),
        CapCommandSpec(
            name="templates",
            help=lambda kind: f"List {kind} templates.",
            factory=lambda kind, title: _make_template_list_command(kind, title=title),
        ),
        CapCommandSpec(
            name="template",
            help=lambda kind: f"Show a {kind} template.",
            factory=lambda kind, title: _make_template_show_command(kind, title=title),
        ),
    )

    for kind in cap_titles:
        title = cap_titles[kind]
        cap_app = typer.Typer(
            help=cap_group_help[kind],
            add_completion=False,
            no_args_is_help=True,
            pretty_exceptions_enable=False,
            pretty_exceptions_show_locals=False,
        )
        for spec in command_specs:
            cap_app.command(
                spec.name,
                help=spec.help(kind),
                no_args_is_help=spec.no_args_is_help,
                cls=(
                    _OptionalPrefixAgentTemplateCommand
                    if spec.name == "template"
                    else _OptionalPrefixAgentCommand
                ),
            )(spec.factory(kind, title))
        app.add_typer(cap_app, name=kind, no_args_is_help=True)


def _make_cap_list_command(kind: CapKind, title: str) -> Callable[..., None]:
    def list_caps(
        ctx: typer.Context,
        filter_scope: Annotated[
            CapListFilter | None,
            typer.Option("--filter", help="Filter by scope: global or agent."),
        ] = None,
    ) -> None:
        selected_agent = _context_agent(ctx)
        agent_name = selected_agent or "default"
        effective_scope = _cap_list_scope(ctx, filter_scope)
        entries = caps.list_entries(
            _context_root(ctx),
            agent_name,
            scope=None if effective_scope == "all" else effective_scope,
            kinds={cast(EntryKind, kind)},
        )
        if not entries:
            typer.echo(f"No {kind}s found.")
            return
        rows = [
            (
                entry.name,
                _entry_ref(entry),
                _entry_scope(entry, agent_name=agent_name),
                _entry_location(_context_root(ctx), entry, agent_name=agent_name),
            )
            for entry in entries
        ]
        rows.sort(key=lambda item: (0 if item[2] == "global" else 1, item[0], item[1], item[3]))
        _echo_table((title.upper(), "REF", "SCOPE", "LOCATION"), rows)

    return list_caps


def _make_new_cap_command(kind: CapKind, title: str) -> Callable[..., None]:
    def new_cap(
        ctx: typer.Context,
        name: str = typer.Argument(..., help=f"{title} name"),
        template: Annotated[
            str,
            typer.Option("--template", "-t", help="Template name."),
        ] = "default",
    ) -> None:
        scope, agent_name = _target_scope(ctx)
        selected_agent = _context_agent(ctx)
        text = click.edit(
            templates.render_template(kind, template, name=name, agent_name=agent_name),
            extension=".md",
            require_save=True,
        )
        if text is None:
            raise typer.Exit()
        path = _wrap_user_error(
            caps.put_local_entry_text,
            _context_root(ctx),
            agent_name,
            scope=scope,
            kind=cast(EntryKind, kind),
            name=name,
            text=text,
        )
        if selected_agent:
            _append_cap_update(_context_root(ctx), selected_agent, kind=kind, name=name, scope=scope)
        typer.echo(str(path))

    return new_cap


def _make_edit_cap_command(kind: CapKind, title: str) -> Callable[..., None]:
    def edit_cap(
        ctx: typer.Context,
        name: str = typer.Argument(..., help=f"{title} name"),
    ) -> None:
        scope, agent_name = _target_scope(ctx)
        selected_agent = _context_agent(ctx)
        text = _wrap_user_error(
            caps.load_local_entry_text,
            _context_root(ctx),
            agent_name,
            scope=scope,
            kind=cast(EntryKind, kind),
            name=name,
        )
        updated_text = click.edit(
            text,
            extension=".md",
            require_save=True,
        )
        if updated_text is None:
            raise typer.Exit()
        path = _wrap_user_error(
            caps.put_local_entry_text,
            _context_root(ctx),
            agent_name,
            scope=scope,
            kind=cast(EntryKind, kind),
            name=name,
            text=updated_text,
        )
        if selected_agent:
            _append_cap_update(_context_root(ctx), selected_agent, kind=kind, name=name, scope=scope)
        typer.echo(str(path))

    return edit_cap


def _make_add_cap_command(kind: CapKind, title: str) -> Callable[..., None]:
    def add_cap(
        ctx: typer.Context,
        ref: str = typer.Argument(..., help=f"{title} ref"),
    ) -> None:
        scope, agent_name = _target_scope(ctx)
        selected_agent = _context_agent(ctx)
        path = _wrap_user_error(
            caps.add_remote_entry,
            _context_root(ctx),
            agent_name,
            scope=scope,
            kind=cast(EntryKind, kind),
            ref=ref,
        )
        if selected_agent:
            _append_cap_update(
                _context_root(ctx),
                selected_agent,
                kind=kind,
                name=caps.remote_entry_name(cast(EntryKind, kind), ref),
                scope=scope,
            )
        typer.echo(str(path))

    return add_cap


def _make_remove_cap_command(kind: CapKind, title: str) -> Callable[..., None]:
    def remove_cap(
        ctx: typer.Context,
        name: str = typer.Argument(..., help=f"{title} name"),
    ) -> None:
        scope, agent_name = _target_scope(ctx)
        selected_agent = _context_agent(ctx)
        entry = _named_entry(
            _context_root(ctx),
            agent_name,
            scope=scope,
            kind=cast(EntryKind, kind),
            name=name,
        )
        if entry.source.form == "remote":
            removed = _wrap_user_error(
                caps.remove_remote_entry,
                _context_root(ctx),
                agent_name,
                scope=scope,
                kind=cast(EntryKind, kind),
                name=name,
            )
            if not removed:
                raise click.ClickException(f"{kind} not found: {name}")
            if selected_agent:
                _append_cap_update(_context_root(ctx), selected_agent, kind=kind, name=name, scope=scope)
            typer.echo(f"Removed {kind} {name} from {entry.ref}")
            return

        deleted_path = _context_root(ctx) / entry.path
        if entry.shape == "dir":
            deleted_path = deleted_path.parent
        removed = _wrap_user_error(
            caps.remove_local_entry,
            _context_root(ctx),
            agent_name,
            scope=scope,
            kind=cast(EntryKind, kind),
            name=name,
        )
        if not removed:
            raise click.ClickException(f"{kind} not found: {name}")
        if selected_agent:
            _append_cap_update(_context_root(ctx), selected_agent, kind=kind, name=name, scope=scope)
        typer.echo(f"Removed {kind} {name} from {deleted_path}")

    return remove_cap


def _make_template_list_command(kind: TemplateKind, *, title: str) -> Callable[..., None]:
    del title

    def list_templates() -> None:
        specs = templates.list_templates(kind)
        if not specs:
            typer.echo(f"No {kind} templates found.")
            return
        rows = [(item.name, item.description or "-") for item in specs]
        _echo_table(("TEMPLATE", "DESCRIPTION"), rows)

    return list_templates


def _make_template_show_command(kind: TemplateKind, *, title: str) -> Callable[..., None]:
    del title

    def show_template(
        template: Annotated[str, typer.Argument(help="Template name", hidden=True)] = "default",
    ) -> None:
        _echo_block(templates.load_template(kind, template).raw_text.rstrip("\n"))

    return show_template


def _target_scope(ctx: typer.Context) -> tuple[PreparedScope, str]:
    agent_name = _context_agent(ctx)
    if agent_name:
        return "agent", agent_name
    return "global", "default"


def _cap_list_scope(
    ctx: typer.Context,
    filter_scope: CapListFilter | None,
) -> PreparedScope | Literal["all"]:
    selected_agent = _context_agent(ctx)
    if filter_scope is None:
        return "all" if selected_agent else "global"
    if filter_scope == "agent" and not selected_agent:
        raise click.ClickException("an agent prefix is required when --filter is agent")
    return filter_scope


def _entry_scope(entry: PreparedEntry, *, agent_name: str) -> PreparedScope:
    prefix = f"agents/{agent_name}/"
    if entry.path.startswith(prefix) or entry.source.path.startswith(prefix):
        return "agent"
    return "global"


def _entry_ref(entry: PreparedEntry) -> str:
    if entry.source.form == "local":
        return entry.name
    return _remote_ref_shorthand(entry.kind, entry.ref)


def _remote_ref_shorthand(kind: EntryKind, ref: str) -> str:
    parsed = urlparse(ref)
    if parsed.scheme != "github":
        return ref
    path = parsed.path.strip("/")
    owner = parsed.netloc.strip()
    if not owner or not path:
        return ref
    parts = path.split("/")
    if kind == "skill" and len(parts) >= 3 and parts[-2] == "skills":
        return f"{owner}/{parts[-1]}"
    if kind == "service" and len(parts) >= 3 and parts[-2] == "services":
        return f"{owner}/{Path(parts[-1]).stem}"
    if kind == "prompt" and len(parts) >= 3 and parts[-2] == "prompts":
        return f"{owner}/{Path(parts[-1]).stem}"
    if kind == "psyche" and len(parts) >= 3 and parts[-2] == "psyches":
        return f"{owner}/{Path(parts[-1]).stem}"
    return ref


def _entry_location(toolang_root: Path, entry: PreparedEntry, *, agent_name: str) -> str:
    if entry.source.form == "remote":
        return entry.ref
    location = toolang_root / entry.path
    if entry.shape == "dir":
        location = location.parent
    return str(location)


def _info_caps_summary(toolang_root: Path, agent_name: str) -> str:
    counts = {
        "skills": len(caps.list_entries(toolang_root, agent_name, scope=None, kinds={"skill"})),
        "psyches": len(caps.list_entries(toolang_root, agent_name, scope=None, kinds={"psyche"})),
        "services": len(caps.list_entries(toolang_root, agent_name, scope=None, kinds={"service"})),
        "prompts": len(caps.list_entries(toolang_root, agent_name, scope=None, kinds={"prompt"})),
    }
    singular = {
        "skills": "skill",
        "psyches": "psyche",
        "services": "service",
        "prompts": "prompt",
    }
    parts = [
        f"{count} {singular[label] if count == 1 else label}"
        for label, count in counts.items()
        if count
    ]
    return ", ".join(parts) if parts else "0"


def _info_jobs_summary(toolang_root: Path, agent_name: str) -> str:
    chore_count = len(work.list_chores(toolang_root, agent_name))
    task_count = len(work.list_tasks(toolang_root, agent_name))
    parts = []
    if chore_count:
        parts.append(f"{chore_count} {'chore' if chore_count == 1 else 'chores'}")
    if task_count:
        parts.append(f"{task_count} {'task' if task_count == 1 else 'tasks'}")
    return ", ".join(parts) if parts else "0"


def _named_entry(
    toolang_root: Path,
    agent_name: str,
    *,
    scope: PreparedScope,
    kind: EntryKind,
    name: str,
    source_form: Literal["local", "remote"] | None = None,
) -> PreparedEntry:
    entries = caps.list_entries(
        toolang_root,
        agent_name,
        scope=scope,
        kinds={kind},
    )
    for entry in entries:
        if entry.name != name:
            continue
        if source_form is not None and entry.source.form != source_form:
            continue
        return entry
    qualifier = f"{source_form} " if source_form is not None else ""
    raise click.ClickException(f"{qualifier}{kind} not found: {name}")


def _append_agent_update(
    toolang_root: Path,
    agent_name: str,
    update_kind: UpdateKind,
    payload: dict[str, object] | None = None,
) -> None:
    store = ExecutionStore(execution_db_path(toolang_root, agent_name))
    try:
        store.append_update(kind=update_kind, payload=payload or {})
    finally:
        store.close()


def _append_cap_update(
    toolang_root: Path,
    agent_name: str,
    *,
    kind: CapKind,
    name: str,
    scope: PreparedScope,
) -> None:
    update_kind = cast(UpdateKind, f"{kind}_changed")
    _append_agent_update(
        toolang_root,
        agent_name,
        update_kind,
        {
            "name": name,
            "scope": scope,
        },
    )


def _append_work_update(
    toolang_root: Path,
    agent_name: str,
    *,
    kind: WorkKind,
    name: str,
    path: Path,
) -> None:
    update_kind = cast(UpdateKind, f"{kind}_changed")
    _append_agent_update(
        toolang_root,
        agent_name,
        update_kind,
        {
            "name": name,
            "path": str(path),
        },
    )


def _context_root(ctx: typer.Context) -> Path:
    state = cast(dict[str, Path | str | None], ctx.obj)
    root = state["toolang_root"]
    if not isinstance(root, Path):
        raise TypeError("missing toolang root")
    return root


def _context_agent(ctx: typer.Context) -> str | None:
    state = cast(dict[str, Path | str | None], ctx.obj)
    agent = state.get("agent")
    return agent if isinstance(agent, str) else None


def _required_prefix_agent(ctx: typer.Context, *, command_name: str) -> str:
    agent = _context_agent(ctx)
    if isinstance(agent, str) and agent:
        return agent
    del command_name
    typer.echo(ctx.get_help())
    raise typer.Exit()


def _required_runtime_agent(ctx: typer.Context, agent: str | None) -> str:
    if agent:
        return agent
    typer.echo(ctx.get_help())
    raise typer.Exit()


def _wrap_user_error(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except (FileExistsError, FileNotFoundError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc


def _toolang_root(explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit
    return Path(os.environ.get("TOOLANG_ROOT", str(Path(os.path.expanduser("~/.toolang")))))


def _ui_base_url() -> str:
    return resolve_ui_base_url(_toolang_root(None), environ=os.environ)


def _runtime_environ_for_agent(
    ctx: typer.Context,
    agent_name: str,
    *,
    toolang_root: Path | None = None,
) -> dict[str, str]:
    root = toolang_root or _context_root(ctx)
    return load_runtime_environ(root, agent_name, base_environ=os.environ)


def _make_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> Table:
    table = Table(box=box.SIMPLE_HEAVY, header_style="", show_lines=False)
    for header in headers:
        table.add_column(header, no_wrap=True)
    for row in rows:
        table.add_row(*row)
    return table


def _echo_block(text: str) -> None:
    typer.echo()
    typer.echo(text)
    typer.echo()


def _echo_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> None:
    typer.echo()
    _TABLE_CONSOLE.print(_make_table(headers, rows))
    typer.echo()


def _echo_pairs_table(
    rows: Sequence[tuple[str, str]],
    *,
    avatar: str | None = None,
    title: str | None = None,
) -> None:
    table = Table(
        box=None,
        header_style="",
        show_header=False,
        show_lines=False,
        pad_edge=False,
        collapse_padding=True,
    )
    table.add_column("FIELD", no_wrap=True, style="bold bright_cyan")
    table.add_column("VALUE", no_wrap=False, style="white")
    for key, value in rows:
        table.add_row(Text(key), _styled_info_value(key, value))
    typer.echo()
    if avatar is None:
        if title is None:
            _TABLE_CONSOLE.print(table)
        else:
            _TABLE_CONSOLE.print(_info_title_block(title))
            _TABLE_CONSOLE.print(table)
    else:
        layout = Table.grid(padding=(0, 4))
        layout.add_column(no_wrap=True, ratio=0)
        layout.add_column(no_wrap=False, ratio=1)
        right = Table.grid(padding=(0, 0))
        right.add_column(no_wrap=False)
        avatar_text = _rainbow_avatar_text(avatar)
        if title is not None:
            right.add_row(_info_title_block(title))
            avatar_text = _rainbow_avatar_text("\n" + avatar)
        right.add_row(table)
        right.add_row(Text(""))
        right.add_row(_palette_block())
        layout.add_row(
            avatar_text,
            right,
        )
        _TABLE_CONSOLE.print(layout)
    typer.echo()


def _styled_info_value(key: str, value: str) -> Text:
    del key
    return Text(value)


def _rainbow_avatar_text(avatar: str) -> Text:
    lines = avatar.splitlines()
    text = Text()
    for row, line in enumerate(lines):
        for column, char in enumerate(line):
            if char == " ":
                text.append(char)
                continue
            style_index = (column + (row * 2)) % len(_RAINBOW_STYLES)
            text.append(char, style=_RAINBOW_STYLES[style_index])
        text.append("\n")
    if text.plain.endswith("\n"):
        text = text[:-1]
    return text


def _info_title_block(title: str) -> Table:
    block = Table.grid(padding=(0, 0))
    block.add_column(no_wrap=False)
    block.add_row(Text(title, style="bold bright_cyan"))
    block.add_row(Text("-" * len(title), style="bright_black"))
    return block


def _palette_block() -> Text:
    palette = Text()
    for style in _PALETTE_STYLES_TOP:
        palette.append("██", style=style)
    palette.append("\n")
    for style in _PALETTE_STYLES_BOTTOM:
        palette.append("██", style=style)
    return palette


def _runtime_value(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        return "-"
    return value


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _runtime_loops(runtime_state: dict[str, object]) -> str | None:
    raw = runtime_state.get("loops")
    if not isinstance(raw, list):
        return None
    values = [str(item).strip() for item in raw if str(item).strip()]
    if not values:
        return None
    return ", ".join(values)


def _created_time(path: Path) -> str:
    stat = path.stat()
    timestamp = getattr(stat, "st_birthtime", None)
    if timestamp is None:
        timestamp = stat.st_mtime
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _human_uptime_since(timestamp_text: str) -> str | None:
    started = _parse_utc_timestamp(timestamp_text)
    if started is None:
        return None
    delta = _utc_now() - started
    total_seconds = max(int(delta.total_seconds()), 0)
    return f"up {humanize.naturaldelta(total_seconds)}"


def _parse_utc_timestamp(value: str) -> datetime | None:
    try:
        if value.endswith("Z"):
            return datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _format_runtime_row(status: agents.AgentStatus) -> str:
    return f"{status.name}\t{status.status}\t{status.api_url or '-'}\t{status.webui_url or '-'}"


def _normalize_loop_option(loops: list[str] | None) -> list[str] | None:
    if loops is None:
        return None
    normalized: list[str] = []
    for item in loops:
        for value in item.split(","):
            loop_name = value.strip()
            if loop_name:
                normalized.append(loop_name)
    return normalized


def _wait_for_started_status(
    *,
    root: Path,
    agent_name: str,
    process: subprocess.Popen[bytes],
    launched_at: float,
    timeout_sec: float,
) -> agents.AgentStatus | None:
    deadline = time.monotonic() + timeout_sec
    state_path = agents.agent_runtime_state_path(root, agent_name)
    while time.monotonic() < deadline:
        if state_path.is_file() and state_path.stat().st_mtime >= launched_at - 0.01:
            status = agents.get_agent_status(root, agent_name, ui_base_url=_ui_base_url())
            if status is not None and status.status in {"running", "failed"}:
                return status
        if process.poll() is not None:
            break
        time.sleep(0.1)
    if state_path.is_file() and state_path.stat().st_mtime >= launched_at - 0.01:
        return agents.get_agent_status(root, agent_name, ui_base_url=_ui_base_url())
    return None


def register_work_commands() -> None:
    work_titles: dict[WorkKind, str] = {
        "chore": "Chore",
        "task": "Task",
    }
    work_group_help: dict[WorkKind, str] = {
        "chore": "Manage chores.",
        "task": "Manage tasks.",
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
            name="remove",
            help=lambda kind: f"Remove a {kind}.",
            factory=_make_remove_work_command,
            cls=_RequiredPrefixAgentCommand,
            no_args_is_help=True,
        ),
        WorkCommandSpec(
            name="templates",
            help=lambda kind: f"List {kind} templates.",
            factory=lambda kind, title: _make_template_list_command(kind, title=title),
        ),
        WorkCommandSpec(
            name="template",
            help=lambda kind: f"Show a {kind} template.",
            factory=lambda kind, title: _make_template_show_command(kind, title=title),
            cls=_OptionalTemplateArgumentCommand,
        ),
    )

    for kind in work_titles:
        title = work_titles[kind]
        work_app = typer.Typer(
            help=work_group_help[kind],
            add_completion=False,
            no_args_is_help=True,
            pretty_exceptions_enable=False,
            pretty_exceptions_show_locals=False,
        )
        for spec in command_specs:
            work_app.command(
                spec.name,
                help=spec.help(kind),
                cls=spec.cls,
                no_args_is_help=spec.no_args_is_help,
            )(spec.factory(kind, title))
        app.add_typer(work_app, name=kind, no_args_is_help=True)


def _make_work_list_command(kind: WorkKind, title: str) -> Callable[..., None]:
    def list_work(ctx: typer.Context) -> None:
        agent_name = _required_prefix_agent(ctx, command_name=kind)
        if kind == "task":
            entries = work.list_tasks(_context_root(ctx), agent_name)
            if not entries:
                typer.echo("No tasks found.")
                return
            rows = [
                (
                    entry.name,
                    entry.document.status,
                    entry.document.requester,
                    "yes" if entry.document.paused else "-",
                    str(entry.path),
                )
                for entry in entries
            ]
            _echo_table((title.upper(), "STATUS", "REQUESTER", "PAUSED", "LOCATION"), rows)
            return
        entries = work.list_chores(_context_root(ctx), agent_name)
        if not entries:
            typer.echo("No chores found.")
            return
        rows = [
            (
                entry.name,
                (entry.document.title or "-").strip() or "-",
                entry.document.rrule,
                "yes" if entry.document.paused else "-",
                str(entry.path),
            )
            for entry in entries
        ]
        _echo_table((title.upper(), "TITLE", "RRULE", "PAUSED", "LOCATION"), rows)

    return list_work


def _make_new_work_command(kind: WorkKind, title: str) -> Callable[..., None]:
    def new_work(
        ctx: typer.Context,
        name: str = typer.Argument(..., help=f"{title} name"),
        template: Annotated[
            str,
            typer.Option("--template", "-t", help="Template name."),
        ] = "default",
    ) -> None:
        agent_name = _required_prefix_agent(ctx, command_name=kind)
        text = click.edit(
            templates.render_template(kind, template, name=name, agent_name=agent_name),
            extension=".md",
            require_save=True,
        )
        if text is None:
            raise typer.Exit()
        path = _wrap_user_error(
            work.put_task_text if kind == "task" else work.put_chore_text,
            _context_root(ctx),
            agent_name,
            name,
            text,
        )
        _append_work_update(_context_root(ctx), agent_name, kind=kind, name=name, path=path)
        typer.echo(str(path))

    return new_work


def _make_edit_work_command(kind: WorkKind, title: str) -> Callable[..., None]:
    def edit_work(
        ctx: typer.Context,
        name: str = typer.Argument(..., help=f"{title} name"),
    ) -> None:
        agent_name = _required_prefix_agent(ctx, command_name=kind)
        text = _wrap_user_error(
            work.load_task_text if kind == "task" else work.load_chore_text,
            _context_root(ctx),
            agent_name,
            name,
        )
        updated_text = click.edit(
            text,
            extension=".md",
            require_save=True,
        )
        if updated_text is None:
            raise typer.Exit()
        path = _wrap_user_error(
            work.put_task_text if kind == "task" else work.put_chore_text,
            _context_root(ctx),
            agent_name,
            name,
            updated_text,
        )
        _append_work_update(_context_root(ctx), agent_name, kind=kind, name=name, path=path)
        typer.echo(str(path))

    return edit_work


def _make_remove_work_command(kind: WorkKind, title: str) -> Callable[..., None]:
    def remove_work(
        ctx: typer.Context,
        name: str = typer.Argument(..., help=f"{title} name"),
    ) -> None:
        agent_name = _required_prefix_agent(ctx, command_name=kind)
        path = (
            work.task_path(_context_root(ctx), agent_name, name)
            if kind == "task"
            else work.chore_path(_context_root(ctx), agent_name, name)
        )
        removed = _wrap_user_error(
            work.remove_task if kind == "task" else work.remove_chore,
            _context_root(ctx),
            agent_name,
            name,
        )
        if not removed:
            raise click.ClickException(f"{kind} not found: {name}")
        _append_work_update(_context_root(ctx), agent_name, kind=kind, name=name, path=path)
        typer.echo(f"Removed {kind} {name} from {path}")

    return remove_work


register_cap_commands()
register_work_commands()


def main(argv: Sequence[str] | None = None) -> int:
    global _CLI_PREFIX_AGENT
    args, prefix_agent = _normalize_cli_args(list(argv) if argv is not None else sys.argv[1:])
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
    option_name = "--root"
    if token == option_name:
        if index + 1 >= len(argv):
            return ([token], 1)
        return ([token, argv[index + 1]], 2)
    prefix = f"{option_name}="
    if token.startswith(prefix):
        return ([option_name, token.removeprefix(prefix)], 1)
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
