"""Toolang agent-management CLI entry point."""

from __future__ import annotations

from collections.abc import Callable, Sequence
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
from ...common import version as _version
from ..common.context import CliContext, resolve_root
from ..common.lazy import LazyCommand, lazy_typer_command, lazy_typer_group
from ..common.output import echo_error
from ..common.routing import (
    OptionalPrefixAgentListCommand,
    RequiredPrefixAgentCommand,
    RunAgentCommand,
    RuntimeAgentCommand,
    StartAgentCommand,
    explicit_root,
    extract_root_args,
)
from . import routing
from .commands.metadata import QUERY_HELP

_PREFIX_AGENT: ContextVar[str | None] = ContextVar(
    "toolang_cli_prefix_agent", default=None
)
_SELECTED_LAYOUT: ContextVar[AgentLayout | None] = ContextVar(
    "toolang_cli_selected_layout", default=None
)
AGENT_COMMAND_PANEL = "Agent Commands"
CAPS_COMMAND_PANEL = "Cap Commands"
CONTROL_COMMAND_PANEL = "Control Commands"
INSPECTION_COMMAND_PANEL = "Inspection Commands"
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
_CAPS_PANEL_COMMAND_ORDER = ("psyche", "skill", "service", "prompt")
_CONTROL_PANEL_COMMAND_ORDER = (
    "chat",
    "steer",
    "cancel",
    "retry",
    "rerun",
    "rewind",
    "fork",
)
_INSPECTION_PANEL_COMMAND_ORDER = (
    "inspect",
    "caps",
    "tools",
    "models",
    "providers",
    "catalogs",
    "adapters",
    "toolsets",
    "sandboxes",
)
_HIDDEN_COMMAND_ORDER = ("query", "fmt", "parse", "serve", "channel")
_VISIBLE_COMMAND_ORDER = (
    *_AGENT_PANEL_COMMAND_ORDER,
    *_CAPS_PANEL_COMMAND_ORDER,
    *_CONTROL_PANEL_COMMAND_ORDER,
    *_INSPECTION_PANEL_COMMAND_ORDER,
)
_REGISTERED_COMMANDS: dict[str, Callable[[], LazyCommand]] = {}


class _ToolangGroup(TyperGroup):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        commands = dict(kwargs.pop("commands", None) or {})
        commands.update(
            (name, factory()) for name, factory in _REGISTERED_COMMANDS.items()
        )
        super().__init__(*args, commands=commands, **kwargs)

    def list_commands(self, ctx: click.Context) -> list[str]:
        names = TyperGroup.list_commands(self, ctx)
        visible = [name for name in _VISIBLE_COMMAND_ORDER if name in names]
        return [*visible, *(name for name in names if name not in visible)]


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


def _registered_command(name: str, target: str, **kwargs: Any) -> None:
    routing.command_spec(name)
    if name in _REGISTERED_COMMANDS:
        raise RuntimeError(f"top-level command registered more than once: {name}")

    def create() -> LazyCommand:
        return lazy_typer_command(name, target, **kwargs)

    _REGISTERED_COMMANDS[name] = create


def _registered_group(target: str, *, name: str, **kwargs: Any) -> None:
    routing.command_spec(name)
    if name in _REGISTERED_COMMANDS:
        raise RuntimeError(f"top-level command registered more than once: {name}")

    def create() -> LazyCommand:
        return lazy_typer_group(name, target, **kwargs)

    _REGISTERED_COMMANDS[name] = create


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
            callback=_version_callback,
            help="Show current version and exit.",
            is_eager=True,
        ),
    ] = False,
) -> None:
    """Toolang CLI."""

    del version
    try:
        from ...up.logging import configure_logging

        configure_logging(spec=None, environ=os.environ)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    ctx.obj = CliContext(
        root=resolve_root(toolang_root),
        agent=_PREFIX_AGENT.get(),
        layout=_SELECTED_LAYOUT.get(),
    )


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
    hidden_order = {name: index for index, name in enumerate(_HIDDEN_COMMAND_ORDER)}
    hidden_commands = sorted(
        (
            command
            for name, command in group.commands.items()
            if command.hidden and name != "hidden"
        ),
        key=lambda command: hidden_order.get(
            command.name or "", len(_HIDDEN_COMMAND_ORDER)
        ),
    )
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
    "hidden",
    "toolang.cli.toolang.main:hidden_commands",
    help="Show hidden commands.",
    hidden=True,
)


_registered_command(
    "new",
    "toolang.cli.toolang.commands.agent:new_agent",
    help="Create an agent.",
    no_args_is_help=True,
    rich_help_panel=AGENT_COMMAND_PANEL,
)
_registered_command(
    "clone",
    "toolang.cli.toolang.commands.agent:clone_agent",
    help="Clone an agent.",
    no_args_is_help=True,
    rich_help_panel=AGENT_COMMAND_PANEL,
)
_registered_command(
    "remove",
    "toolang.cli.toolang.commands.agent:remove_agent",
    help="Remove an agent.",
    no_args_is_help=True,
    rich_help_panel=AGENT_COMMAND_PANEL,
)
_registered_command(
    "list",
    "toolang.cli.toolang.commands.agent:list_agents",
    help="Show agents and their status.",
    rich_help_panel=AGENT_COMMAND_PANEL,
)
_registered_command(
    "info",
    "toolang.cli.toolang.commands.agent:info_agent",
    help="Show agent info.",
    no_args_is_help=True,
    cls=RuntimeAgentCommand,
    rich_help_panel=AGENT_COMMAND_PANEL,
)
_registered_command(
    "run",
    "toolang.cli.toolang.commands.runtime:run",
    help="Run an agent in the foreground.",
    no_args_is_help=True,
    cls=RunAgentCommand,
    rich_help_panel=AGENT_COMMAND_PANEL,
)
_registered_command(
    "start",
    "toolang.cli.toolang.commands.runtime:start",
    help="Start an agent.",
    no_args_is_help=True,
    cls=StartAgentCommand,
    rich_help_panel=AGENT_COMMAND_PANEL,
)
_registered_command(
    "stop",
    "toolang.cli.toolang.commands.runtime:stop",
    help="Stop an agent.",
    no_args_is_help=True,
    cls=RuntimeAgentCommand,
    rich_help_panel=AGENT_COMMAND_PANEL,
)
_registered_group(
    "toolang.cli.toolang.commands.job:chore_app",
    name="chore",
    help="Manage agent chores.",
    no_args_is_help=True,
    rich_help_panel=AGENT_COMMAND_PANEL,
)
_registered_group(
    "toolang.cli.toolang.commands.job:task_app",
    name="task",
    help="Manage agent tasks.",
    no_args_is_help=True,
    rich_help_panel=AGENT_COMMAND_PANEL,
)

_registered_command(
    "chat",
    "toolang.cli.toolang.commands.chat:chat_command",
    help="Start an interactive TUI.",
    cls=RequiredPrefixAgentCommand,
    rich_help_panel=CONTROL_COMMAND_PANEL,
)
_registered_command(
    "inspect",
    "toolang.cli.toolang.commands.inspect:inspect_command",
    help="Inspect execution subjects.",
    no_args_is_help=True,
    cls=RequiredPrefixAgentCommand,
    rich_help_panel=INSPECTION_COMMAND_PANEL,
)
_registered_command(
    "steer",
    "toolang.cli.toolang.commands.thread:steer_command",
    help="Steer an active run.",
    no_args_is_help=True,
    cls=RequiredPrefixAgentCommand,
    rich_help_panel=CONTROL_COMMAND_PANEL,
)
_registered_command(
    "cancel",
    "toolang.cli.toolang.commands.thread:cancel_command",
    help="Cancel an active run.",
    no_args_is_help=True,
    cls=RequiredPrefixAgentCommand,
    rich_help_panel=CONTROL_COMMAND_PANEL,
)
_registered_command(
    "retry",
    "toolang.cli.toolang.commands.thread:retry_command",
    help="Retry a run from a failed step.",
    no_args_is_help=True,
    cls=RequiredPrefixAgentCommand,
    rich_help_panel=CONTROL_COMMAND_PANEL,
)
_registered_command(
    "rerun",
    "toolang.cli.toolang.commands.thread:rerun_command",
    help="Rerun an earlier run as a new one.",
    no_args_is_help=True,
    cls=RequiredPrefixAgentCommand,
    rich_help_panel=CONTROL_COMMAND_PANEL,
)
_registered_command(
    "rewind",
    "toolang.cli.toolang.commands.thread:rewind_command",
    help="Rewind a thread to an earlier run.",
    no_args_is_help=True,
    cls=RequiredPrefixAgentCommand,
    rich_help_panel=CONTROL_COMMAND_PANEL,
)
_registered_command(
    "fork",
    "toolang.cli.toolang.commands.thread:fork_command",
    help="Fork a thread from an earlier run.",
    no_args_is_help=True,
    cls=RequiredPrefixAgentCommand,
    rich_help_panel=CONTROL_COMMAND_PANEL,
)

_registered_command(
    "models",
    "toolang.cli.toolang.commands.model_catalog:models_command",
    help="List models.",
    rich_help_panel=INSPECTION_COMMAND_PANEL,
)
_registered_command(
    "providers",
    "toolang.cli.toolang.commands.model_catalog:providers_command",
    help="List model providers.",
    rich_help_panel=INSPECTION_COMMAND_PANEL,
)
_registered_group(
    "toolang.cli.toolang.commands.plugin:channel_app",
    name="channel",
    help="List available channels.",
    no_args_is_help=True,
    hidden=True,
)
_registered_command(
    "tools",
    "toolang.cli.toolang.commands.plugin:list_tools",
    help="List tools.",
    rich_help_panel=INSPECTION_COMMAND_PANEL,
)
_registered_command(
    "catalogs",
    "toolang.cli.toolang.commands.plugin:list_catalogs",
    help="List installed model catalogs.",
    rich_help_panel=INSPECTION_COMMAND_PANEL,
)
_registered_command(
    "adapters",
    "toolang.cli.toolang.commands.model_catalog:adapters_command",
    help="List installed model adapters.",
    rich_help_panel=INSPECTION_COMMAND_PANEL,
)
_registered_command(
    "toolsets",
    "toolang.cli.toolang.commands.plugin:list_toolsets",
    help="List installed toolsets.",
    rich_help_panel=INSPECTION_COMMAND_PANEL,
)
_registered_command(
    "sandboxes",
    "toolang.cli.toolang.commands.plugin:list_sandboxes",
    help="List installed sandboxes.",
    rich_help_panel=INSPECTION_COMMAND_PANEL,
)

_registered_group(
    "toolang.cli.toolang.commands.caps:psyche_app",
    name="psyche",
    help="Manage psyche caps.",
    no_args_is_help=True,
    rich_help_panel=CAPS_COMMAND_PANEL,
)
_registered_group(
    "toolang.cli.toolang.commands.caps:skill_app",
    name="skill",
    help="Manage skill caps.",
    no_args_is_help=True,
    rich_help_panel=CAPS_COMMAND_PANEL,
)
_registered_group(
    "toolang.cli.toolang.commands.caps:service_app",
    name="service",
    help="Manage service caps.",
    no_args_is_help=True,
    rich_help_panel=CAPS_COMMAND_PANEL,
)
_registered_group(
    "toolang.cli.toolang.commands.caps:prompt_app",
    name="prompt",
    help="Manage prompt caps.",
    no_args_is_help=True,
    rich_help_panel=CAPS_COMMAND_PANEL,
)
_registered_command(
    "caps",
    "toolang.cli.caps.commands:list_caps",
    help="List caps.",
    cls=OptionalPrefixAgentListCommand,
    rich_help_panel=INSPECTION_COMMAND_PANEL,
)

_registered_command(
    "query",
    "toolang.cli.toolang.commands.query:query_command",
    help=QUERY_HELP,
    hidden=True,
)
_registered_command(
    "fmt",
    "toolang.cli.toolang.commands.program:fmt",
    help="Format .too files.",
    hidden=True,
    no_args_is_help=True,
)
_registered_command(
    "parse",
    "toolang.cli.toolang.commands.program:parse_program",
    help="Parse a .too file and print its AST.",
    hidden=True,
    no_args_is_help=True,
)
_registered_command(
    "serve",
    "toolang.cli.toolang.commands.runtime:serve",
    help="Run an AgentServer process.",
    hidden=True,
    no_args_is_help=True,
)

routing.validate_command_registration(set(_REGISTERED_COMMANDS))


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
