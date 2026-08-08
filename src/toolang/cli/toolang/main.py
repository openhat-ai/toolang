"""Toolang agent-management CLI entry point."""

from __future__ import annotations

from collections.abc import Sequence
from contextvars import ContextVar
from pathlib import Path
import os
import sys
from typing import Annotated, Any

import click
import typer
from typer import rich_utils
from typer.core import TyperGroup

from ...catalog.agent import LocalAgents
from ...common.layout import AgentLayout
from ...up.logging import configure_logging
from ..caps import commands as cap_commands
from ..common.context import CliContext, resolve_root
from ..common import version as _version
from ..common.output import echo_error
from ..common.routing import (
    OptionalPrefixAgentGroup,
    OptionalPrefixAgentListCommand,
    RequiredPrefixAgentCommand,
    RunAgentCommand,
    RuntimeAgentCommand,
    StartAgentCommand,
    explicit_root,
    extract_root_args,
)
from . import routing
from .commands import agent as agent_commands
from .commands import chat as chat_commands
from .commands import plugin as plugin_commands
from .commands import program as program_commands
from .commands import runtime as runtime_commands
from .commands import job as job_commands
from .commands import thread as thread_commands

_PREFIX_AGENT: ContextVar[str | None] = ContextVar(
    "toolang_cli_prefix_agent", default=None
)
_SELECTED_LAYOUT: ContextVar[AgentLayout | None] = ContextVar(
    "toolang_cli_selected_layout", default=None
)
AGENT_COMMAND_PANEL = "Agent Commands"
THREAD_COMMAND_PANEL = "Thread Commands"
RUNTIME_COMMAND_PANEL = "Runtime Commands"
CAPS_COMMAND_PANEL = "Cap Commands"
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
    "steer",
    "cancel",
    "retry",
    "rerun",
    "rewind",
    "fork",
    "inspect",
    "runs",
    "threads",
)
_RUNTIME_PANEL_COMMAND_ORDER = ("model", "tool", "channel", "sandbox")
_REGISTERED_COMMANDS: set[str] = set()


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


def _version_callback(value: bool) -> None:
    if not value:
        return
    typer.echo(f"toolang {_version.toolang_version()}")
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


def _registered_command(name: str, **kwargs: Any) -> Any:
    routing.command_spec(name)
    if name in _REGISTERED_COMMANDS:
        raise RuntimeError(f"top-level command registered more than once: {name}")
    _REGISTERED_COMMANDS.add(name)
    return app.command(name, **kwargs)


def _registered_group(group: typer.Typer, *, name: str, **kwargs: Any) -> None:
    routing.command_spec(name)
    if name in _REGISTERED_COMMANDS:
        raise RuntimeError(f"top-level command registered more than once: {name}")
    _REGISTERED_COMMANDS.add(name)
    app.add_typer(group, name=name, **kwargs)


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
    ctx.obj = CliContext(
        root=resolve_root(toolang_root),
        agent=_PREFIX_AGENT.get(),
        layout=_SELECTED_LAYOUT.get(),
    )


@_registered_command("hidden", help="Show hidden commands.", hidden=True)
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
        if command.hidden and name != "hidden"
    ]
    if not hidden_commands:
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


_registered_command(
    "new",
    help="Create an agent.",
    no_args_is_help=True,
    rich_help_panel=AGENT_COMMAND_PANEL,
)(agent_commands.new_agent)
_registered_command(
    "clone",
    help="Clone an agent.",
    no_args_is_help=True,
    rich_help_panel=AGENT_COMMAND_PANEL,
)(agent_commands.clone_agent)
_registered_command(
    "remove",
    help="Remove an agent.",
    no_args_is_help=True,
    rich_help_panel=AGENT_COMMAND_PANEL,
)(agent_commands.remove_agent)
_registered_command(
    "list",
    help="Show agents and their status.",
    rich_help_panel=AGENT_COMMAND_PANEL,
)(agent_commands.list_agents)
_registered_command(
    "info",
    help="Show agent info.",
    no_args_is_help=True,
    cls=RuntimeAgentCommand,
    rich_help_panel=AGENT_COMMAND_PANEL,
)(agent_commands.info_agent)
_registered_command(
    "run",
    help="Run an agent in the foreground.",
    no_args_is_help=True,
    cls=RunAgentCommand,
    rich_help_panel=AGENT_COMMAND_PANEL,
)(runtime_commands.run)
_registered_command(
    "start",
    help="Start an agent.",
    no_args_is_help=True,
    cls=StartAgentCommand,
    rich_help_panel=AGENT_COMMAND_PANEL,
)(runtime_commands.start)
_registered_command(
    "stop",
    help="Stop an agent.",
    no_args_is_help=True,
    cls=RuntimeAgentCommand,
    rich_help_panel=AGENT_COMMAND_PANEL,
)(runtime_commands.stop)
_registered_group(
    job_commands.chore_app,
    name="chore",
    no_args_is_help=True,
    rich_help_panel=AGENT_COMMAND_PANEL,
)
_registered_group(
    job_commands.task_app,
    name="task",
    no_args_is_help=True,
    rich_help_panel=AGENT_COMMAND_PANEL,
)

_registered_command(
    "chat",
    help="Open or continue a terminal chat.",
    cls=RequiredPrefixAgentCommand,
    rich_help_panel=THREAD_COMMAND_PANEL,
)(chat_commands.chat_command)
_registered_command(
    "threads",
    help="List threads.",
    cls=RequiredPrefixAgentCommand,
    rich_help_panel=THREAD_COMMAND_PANEL,
)(thread_commands.threads_command)
_registered_command(
    "runs",
    help="List runs.",
    cls=RequiredPrefixAgentCommand,
    rich_help_panel=THREAD_COMMAND_PANEL,
)(thread_commands.runs_command)
_registered_command(
    "inspect",
    help="Inspect a thread or run.",
    no_args_is_help=True,
    cls=RequiredPrefixAgentCommand,
    rich_help_panel=THREAD_COMMAND_PANEL,
)(thread_commands.inspect_command)
_registered_command(
    "steer",
    help="Steer an active run.",
    no_args_is_help=True,
    cls=RequiredPrefixAgentCommand,
    rich_help_panel=THREAD_COMMAND_PANEL,
)(thread_commands.steer_command)
_registered_command(
    "cancel",
    help="Cancel an active run.",
    no_args_is_help=True,
    cls=RequiredPrefixAgentCommand,
    rich_help_panel=THREAD_COMMAND_PANEL,
)(thread_commands.cancel_command)
_registered_command(
    "retry",
    help="Retry a run from a failed step.",
    no_args_is_help=True,
    cls=RequiredPrefixAgentCommand,
    rich_help_panel=THREAD_COMMAND_PANEL,
)(thread_commands.retry_command)
_registered_command(
    "rerun",
    help="Rerun a prior invocation.",
    no_args_is_help=True,
    cls=RequiredPrefixAgentCommand,
    rich_help_panel=THREAD_COMMAND_PANEL,
)(thread_commands.rerun_command)
_registered_command(
    "rewind",
    help="Rewind a thread to an earlier run.",
    no_args_is_help=True,
    cls=RequiredPrefixAgentCommand,
    rich_help_panel=THREAD_COMMAND_PANEL,
)(thread_commands.rewind_command)
_registered_command(
    "fork",
    help="Fork a thread from an earlier run.",
    no_args_is_help=True,
    cls=RequiredPrefixAgentCommand,
    rich_help_panel=THREAD_COMMAND_PANEL,
)(thread_commands.fork_command)

_registered_group(
    plugin_commands.model_app,
    name="model",
    no_args_is_help=True,
    rich_help_panel=RUNTIME_COMMAND_PANEL,
)
_registered_group(
    plugin_commands.tool_app,
    name="tool",
    no_args_is_help=True,
    rich_help_panel=RUNTIME_COMMAND_PANEL,
)
_registered_group(
    plugin_commands.channel_app,
    name="channel",
    no_args_is_help=True,
    rich_help_panel=RUNTIME_COMMAND_PANEL,
)
_registered_group(
    plugin_commands.sandbox_app,
    name="sandbox",
    no_args_is_help=True,
    rich_help_panel=RUNTIME_COMMAND_PANEL,
)

_cap_apps = cap_commands.create_cap_apps(group_cls=OptionalPrefixAgentGroup)
_registered_group(
    _cap_apps["psyche"],
    name="psyche",
    no_args_is_help=True,
    rich_help_panel=CAPS_COMMAND_PANEL,
)
_registered_group(
    _cap_apps["skill"],
    name="skill",
    no_args_is_help=True,
    rich_help_panel=CAPS_COMMAND_PANEL,
)
_registered_group(
    _cap_apps["service"],
    name="service",
    no_args_is_help=True,
    rich_help_panel=CAPS_COMMAND_PANEL,
)
_registered_group(
    _cap_apps["prompt"],
    name="prompt",
    no_args_is_help=True,
    rich_help_panel=CAPS_COMMAND_PANEL,
)
_registered_command(
    "caps",
    help="Inspect available caps.",
    cls=OptionalPrefixAgentListCommand,
    rich_help_panel=CAPS_COMMAND_PANEL,
)(cap_commands.list_caps)

_registered_command(
    "fmt",
    help="Format .too files.",
    hidden=True,
    no_args_is_help=True,
)(program_commands.fmt)
_registered_command(
    "parse",
    help="Parse a .too file and print its AST.",
    hidden=True,
    no_args_is_help=True,
)(program_commands.parse_program)
_registered_command(
    "serve",
    help="Run an AgentServer process.",
    hidden=True,
    no_args_is_help=True,
)(runtime_commands.serve)

routing.validate_command_registration(_REGISTERED_COMMANDS)


def main(argv: Sequence[str] | None = None) -> int:
    raw_args = list(argv) if argv is not None else sys.argv[1:]
    prog_name = _prog_name(sys.argv[0] if sys.argv else "")
    routed = routing.dispatch_roaming(
        raw_args,
        prog_name=prog_name,
        run_app=lambda args, layout: _run_app(
            args,
            layout.name,
            prog_name=prog_name,
            layout=layout,
        ),
    )
    if routed is not None:
        return routed
    target_help = routing.select_target_help(
        raw_args,
        residents=_routing_residents(raw_args),
    )
    if target_help is not None:
        return _run_target_help(target_help, prog_name=prog_name)
    routed = routing.dispatch_visiting(
        raw_args,
        run_app=lambda args, layout: _run_app(
            args,
            layout.name,
            prog_name=prog_name,
            layout=layout,
        ),
    )
    if routed is not None:
        return routed
    try:
        args, prefix_agent = routing.normalize(raw_args)
    except routing.RoutingError as exc:
        echo_error(str(exc))
        return 2
    return _run_app(args, prefix_agent, prog_name=prog_name)


def _run_target_help(
    target: routing.TargetHelp,
    *,
    prog_name: str,
) -> int:
    root_command = typer.main.get_command(app)
    if not isinstance(root_command, click.Group):
        raise TypeError("Toolang CLI root must be a command group")
    commands = {
        name: command
        for name, command in root_command.commands.items()
        if not command.hidden
        and routing.command_spec(name).accepts("before", target.placement)
    }
    group = _ToolangGroup(
        name=target.selector,
        commands=commands,
        help=f"Commands for {target.placement} agent {target.label}.",
        no_args_is_help=True,
        rich_markup_mode="rich",
    )
    try:
        group.main(
            args=["--help"],
            prog_name=f"{prog_name} {target.selector}",
            standalone_mode=False,
        )
    except click.exceptions.Exit as exc:
        return exc.exit_code
    return 0


def _run_app(
    args: list[str],
    prefix_agent: str | None,
    *,
    prog_name: str,
    layout: AgentLayout | None = None,
) -> int:
    agent_token = _PREFIX_AGENT.set(prefix_agent)
    layout_token = _SELECTED_LAYOUT.set(layout)
    try:
        result = app(
            args=args,
            prog_name=prog_name,
            standalone_mode=False,
        )
    except click.exceptions.Exit as exc:
        return exc.exit_code
    except click.ClickException as exc:
        if exc.__class__.__name__ != "NoArgsIsHelpError":
            echo_error(exc)
        return exc.exit_code
    except (FileExistsError, FileNotFoundError, ValueError) as exc:
        echo_error(str(exc))
        return 1
    finally:
        _SELECTED_LAYOUT.reset(layout_token)
        _PREFIX_AGENT.reset(agent_token)
    return result if isinstance(result, int) else 0


def _prog_name(argv0: str) -> str:
    text = Path(argv0).name.strip()
    return text or "toolang"


def _routing_residents(argv: Sequence[str]) -> frozenset[str]:
    root_args, _body = extract_root_args(argv)
    root = resolve_root(explicit_root(root_args))
    return frozenset(LocalAgents(root / "agents").list())


if __name__ == "__main__":
    raise SystemExit(main())
