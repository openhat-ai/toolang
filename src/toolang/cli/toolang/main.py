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

from ...common.layout import AgentLayout
from ...up.logging import configure_logging
from ..caps import commands as cap_commands
from ..common.context import CliContext, resolve_root
from ..common import version as _version
from ..common.routing import (
    OptionalPrefixAgentGroup,
    OptionalPrefixAgentListCommand,
    RequiredPrefixAgentCommand,
    RunAgentCommand,
    RuntimeAgentCommand,
    StartAgentCommand,
)
from . import routing
from .commands import agent as agent_commands
from .commands import chat as chat_commands
from .commands import plugin as plugin_commands
from .commands import program as program_commands
from .commands import runtime as runtime_commands
from .commands import script as script_commands
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
    "cancel",
    "steer",
    "rewind",
    "fork",
    "inspect",
    "runs",
    "threads",
)
_RUNTIME_PANEL_COMMAND_ORDER = ("model", "tool", "channel", "sandbox")
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


app.command(
    "new",
    help="Create an agent.",
    no_args_is_help=True,
    rich_help_panel=AGENT_COMMAND_PANEL,
)(agent_commands.new_agent)
app.command(
    "clone",
    help="Clone an agent.",
    no_args_is_help=True,
    rich_help_panel=AGENT_COMMAND_PANEL,
)(agent_commands.clone_agent)
app.command(
    "remove",
    help="Remove an agent.",
    no_args_is_help=True,
    rich_help_panel=AGENT_COMMAND_PANEL,
)(agent_commands.remove_agent)
app.command(
    "list",
    help="Show agents and their status.",
    rich_help_panel=AGENT_COMMAND_PANEL,
)(agent_commands.list_agents)
app.command(
    "info",
    help="Show agent info.",
    no_args_is_help=True,
    cls=RuntimeAgentCommand,
    rich_help_panel=AGENT_COMMAND_PANEL,
)(agent_commands.info_agent)
app.command(
    "run",
    help="Run an agent in the foreground.",
    no_args_is_help=True,
    cls=RunAgentCommand,
    rich_help_panel=AGENT_COMMAND_PANEL,
)(runtime_commands.run)
app.command(
    "start",
    help="Start an agent.",
    no_args_is_help=True,
    cls=StartAgentCommand,
    rich_help_panel=AGENT_COMMAND_PANEL,
)(runtime_commands.start)
app.command(
    "stop",
    help="Stop an agent.",
    no_args_is_help=True,
    cls=RuntimeAgentCommand,
    rich_help_panel=AGENT_COMMAND_PANEL,
)(runtime_commands.stop)
app.add_typer(
    job_commands.chore_app,
    name="chore",
    no_args_is_help=True,
    rich_help_panel=AGENT_COMMAND_PANEL,
)
app.add_typer(
    job_commands.task_app,
    name="task",
    no_args_is_help=True,
    rich_help_panel=AGENT_COMMAND_PANEL,
)

app.command(
    "chat",
    help="Open a terminal chat session.",
    cls=RequiredPrefixAgentCommand,
    rich_help_panel=THREAD_COMMAND_PANEL,
)(chat_commands.chat_command)
app.command(
    "send",
    help="Send one message to a thread.",
    hidden=True,
    cls=RequiredPrefixAgentCommand,
)(chat_commands.send_command)
app.command(
    "attach",
    help="Open chat on a thread.",
    hidden=True,
    cls=RequiredPrefixAgentCommand,
)(chat_commands.attach_command)
app.command(
    "threads",
    help="List threads.",
    cls=RequiredPrefixAgentCommand,
    rich_help_panel=THREAD_COMMAND_PANEL,
)(thread_commands.threads_command)
app.command(
    "runs",
    help="List runs.",
    cls=RequiredPrefixAgentCommand,
    rich_help_panel=THREAD_COMMAND_PANEL,
)(thread_commands.runs_command)
app.command(
    "inspect",
    help="Inspect a thread or run.",
    no_args_is_help=True,
    cls=RequiredPrefixAgentCommand,
    rich_help_panel=THREAD_COMMAND_PANEL,
)(thread_commands.inspect_command)
app.command(
    "steer",
    help="Steer an active run.",
    no_args_is_help=True,
    cls=RequiredPrefixAgentCommand,
    rich_help_panel=THREAD_COMMAND_PANEL,
)(thread_commands.steer_command)
app.command(
    "cancel",
    help="Cancel an active run.",
    no_args_is_help=True,
    cls=RequiredPrefixAgentCommand,
    rich_help_panel=THREAD_COMMAND_PANEL,
)(thread_commands.cancel_command)
app.command(
    "rewind",
    help="Rewind a thread to an earlier point.",
    no_args_is_help=True,
    cls=RequiredPrefixAgentCommand,
    rich_help_panel=THREAD_COMMAND_PANEL,
)(thread_commands.rewind_command)
app.command(
    "fork",
    help="Fork a thread from a branch point.",
    no_args_is_help=True,
    cls=RequiredPrefixAgentCommand,
    rich_help_panel=THREAD_COMMAND_PANEL,
)(thread_commands.fork_command)

app.add_typer(
    plugin_commands.model_app,
    name="model",
    no_args_is_help=True,
    rich_help_panel=RUNTIME_COMMAND_PANEL,
)
app.add_typer(
    plugin_commands.tool_app,
    name="tool",
    no_args_is_help=True,
    rich_help_panel=RUNTIME_COMMAND_PANEL,
)
app.add_typer(
    plugin_commands.channel_app,
    name="channel",
    no_args_is_help=True,
    rich_help_panel=RUNTIME_COMMAND_PANEL,
)
app.add_typer(
    plugin_commands.sandbox_app,
    name="sandbox",
    no_args_is_help=True,
    rich_help_panel=RUNTIME_COMMAND_PANEL,
)

_cap_apps = cap_commands.create_cap_apps(group_cls=OptionalPrefixAgentGroup)
app.add_typer(
    _cap_apps["psyche"],
    name="psyche",
    no_args_is_help=True,
    rich_help_panel=CAPS_COMMAND_PANEL,
)
app.add_typer(
    _cap_apps["skill"],
    name="skill",
    no_args_is_help=True,
    rich_help_panel=CAPS_COMMAND_PANEL,
)
app.add_typer(
    _cap_apps["service"],
    name="service",
    no_args_is_help=True,
    rich_help_panel=CAPS_COMMAND_PANEL,
)
app.add_typer(
    _cap_apps["prompt"],
    name="prompt",
    no_args_is_help=True,
    rich_help_panel=CAPS_COMMAND_PANEL,
)
app.command(
    "caps",
    help="Inspect available caps.",
    cls=OptionalPrefixAgentListCommand,
    rich_help_panel=CAPS_COMMAND_PANEL,
)(cap_commands.list_caps)

app.command(
    "fmt",
    help="Format .too files.",
    hidden=True,
    no_args_is_help=True,
)(program_commands.fmt)
app.command(
    "parse",
    help="Parse a .too file and print its AST.",
    hidden=True,
    no_args_is_help=True,
)(program_commands.parse_program)
app.command(
    "script",
    help="Show local Toolang script usage.",
    hidden=True,
    cls=script_commands.ScriptHelpCommand,
    context_settings={"allow_interspersed_args": False},
)(script_commands.script_command)
app.command(
    "serve",
    help="Run an AgentServer process.",
    hidden=True,
    no_args_is_help=True,
)(runtime_commands.serve)


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
            catch_system_exit=True,
            layout=layout,
        ),
    )
    if routed is not None:
        return routed
    args, prefix_agent = routing.normalize(raw_args)
    return _run_app(args, prefix_agent, prog_name=prog_name)


def _run_app(
    args: list[str],
    prefix_agent: str | None,
    *,
    prog_name: str,
    catch_system_exit: bool = False,
    layout: AgentLayout | None = None,
) -> int:
    agent_token = _PREFIX_AGENT.set(prefix_agent)
    layout_token = _SELECTED_LAYOUT.set(layout)
    try:
        app(
            args=args,
            prog_name=prog_name,
            standalone_mode=True,
        )
    except click.exceptions.Exit as exc:
        return exc.exit_code
    except SystemExit as exc:
        if not catch_system_exit:
            raise
        return exc.code if isinstance(exc.code, int) else 1
    except (FileExistsError, FileNotFoundError, ValueError) as exc:
        typer.echo(f"toolang error: {exc}", err=True)
        return 1
    finally:
        _SELECTED_LAYOUT.reset(layout_token)
        _PREFIX_AGENT.reset(agent_token)
    return 0


def _prog_name(argv0: str) -> str:
    text = Path(argv0).name.strip()
    return text or "toolang"


if __name__ == "__main__":
    raise SystemExit(main())
