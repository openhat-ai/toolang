"""Toolang agent-management CLI entry point."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from copy import copy
from pathlib import Path
import os
import sys
from typing import Annotated, Any

import typer
from rich.text import Text
from typer._click import Context, HelpFormatter as NativeHelpFormatter
from typer._click.core import augment_usage_errors
from typer._click.exceptions import ClickException
from typer.core import TyperGroup

from toolang.common.typer.ui import HelpFormatter, run
from toolang.cli.common.parameters import RootOption
from toolang.common.typer.options import BARE_VALUE, OptionalValue, OptionalValueCommand

from ...common import version as _version
from ..common.context import CliContext, resolve_root
from ..common.help import CliCommand, CliGroup, HelpContext, show_help
from ..common.lazy import LazyCommand, lazy_typer_command, lazy_typer_group
from . import routing

AGENT_COMMAND_PANEL = "Agent commands"
WORK_COMMAND_PANEL = "Work commands"
CAPS_COMMAND_PANEL = "Cap commands"
INSPECTION_COMMAND_PANEL = "Inspection commands"
SCRIPT_COMMAND_PANEL = "Script commands"
PLUGIN_COMMAND_PANEL = "Plugin commands"
RUN_COMMAND_PANEL = "Run commands"
THREAD_COMMAND_PANEL = "Thread commands"
RUNTIME_COMMAND_PANEL = "Runtime commands"
LANGUAGE_COMMAND_PANEL = "Language commands"
_AGENT_PANEL_COMMAND_ORDER = (
    "new",
    "clone",
    "remove",
    "list",
    "info",
    "cd",
)
_RUNTIME_PANEL_COMMAND_ORDER = ("serve", "start", "stop", "hub")
_WORK_PANEL_COMMAND_ORDER = ("chat", "chore", "talk", "convo", "workspace")
_CAPS_PANEL_COMMAND_ORDER = ("psyche", "skill", "service", "prompt")
_RUN_PANEL_COMMAND_ORDER = ("steer", "cancel", "rerun")
_THREAD_PANEL_COMMAND_ORDER = ("fork", "rewind")
_INSPECTION_PANEL_COMMAND_ORDER = (
    "caps",
    "tools",
    "models",
    "providers",
    "inspect",
    "top",
)
_SCRIPT_PANEL_COMMAND_ORDER = ("init", "run")
# `too more` collects the commands the root directory omits, grouped by panel.
_MORE_PANEL_COMMAND_ORDER = (
    (RUN_COMMAND_PANEL, _RUN_PANEL_COMMAND_ORDER),
    (THREAD_COMMAND_PANEL, _THREAD_PANEL_COMMAND_ORDER),
    (PLUGIN_COMMAND_PANEL, ("catalogs", "adapters", "toolsets", "sandboxes")),
    (LANGUAGE_COMMAND_PANEL, ("parse", "highlight", "fmt")),
)
_VISIBLE_COMMAND_ORDER = (
    *_AGENT_PANEL_COMMAND_ORDER,
    *_CAPS_PANEL_COMMAND_ORDER,
    *_RUNTIME_PANEL_COMMAND_ORDER,
    *_WORK_PANEL_COMMAND_ORDER,
    *_INSPECTION_PANEL_COMMAND_ORDER,
    *_SCRIPT_PANEL_COMMAND_ORDER,
)
# Root-hidden run and thread commands still need an order for target help.
_COMMAND_ORDER = (
    *_VISIBLE_COMMAND_ORDER,
    *_RUN_PANEL_COMMAND_ORDER,
    *_THREAD_PANEL_COMMAND_ORDER,
)
_REGISTERED_COMMANDS: dict[str, Callable[[], LazyCommand]] = {}


class _ServeCommand(OptionalValueCommand, CliCommand):
    optional_values = {"dev": OptionalValue(bare_value=".")}


class _ScriptEntryCommand(CliCommand):
    """Show static help when the required Script file is omitted."""

    def parse_args(self, ctx: Context, args: list[str]) -> list[str]:
        if args in ([], ["--"]) and not ctx.resilient_parsing:
            show_help(ctx)
        return super().parse_args(ctx, args)

    def make_context(
        self,
        info_name: str | None,
        args: list[str],
        parent: Context | None = None,
        **extra: Any,
    ) -> Context:
        if args and (args[0] == "--" and len(args) > 1 or not args[0].startswith("-")):
            from .commands.script import script_context

            try:
                header = self.context_class(
                    self, info_name=info_name, parent=parent, **extra
                )
                with augment_usage_errors(header):
                    return script_context(info_name, args, parent=parent, **extra)
            except ClickException:
                if not extra.get("resilient_parsing"):
                    raise
        return super().make_context(info_name, args, parent=parent, **extra)

    def format_help(self, ctx: Context, formatter: NativeHelpFormatter) -> None:
        super().format_help(ctx, formatter)
        formatter.write_paragraph()
        formatter.write_text(
            "Omit run if RUNNABLE is not a Toolang command, e.g. "
            f"{ctx.find_root().command_path} aid.too whats_for."
        )


def script_entry(
    file: Annotated[str, typer.Argument(metavar="FILE", help="Path to a .too file")],
    runnable: Annotated[
        str | None,
        typer.Argument(metavar="RUNNABLE", help="Runnable name", show_default=False),
    ] = None,
    arguments: Annotated[
        list[str] | None,
        typer.Argument(metavar="ARGS", help="Runnable arguments"),
    ] = None,
) -> None:
    """Describe static entry help; the command resolves FILE before invocation."""


class _StartCommand(OptionalValueCommand, CliCommand):
    optional_values = {"dev": OptionalValue(bare_value=".")}


class _ThreadRunCommand(OptionalValueCommand, CliCommand):
    optional_values = {"dev": OptionalValue(bare_value=".")}


class _ChatCommand(_ThreadRunCommand):
    optional_values = {
        "thread": OptionalValue(bare_value=BARE_VALUE, show_bare="latest thread"),
        "dev": OptionalValue(bare_value="."),
    }


class _InspectCommand(CliCommand):
    def write_help_sections(self, ctx: Context, formatter: HelpFormatter) -> None:
        super().write_help_sections(ctx, formatter)
        from .commands.inspect import write_inspect_help

        write_inspect_help(ctx, formatter)


class _TalkCommand(CliCommand):
    def write_help_sections(self, ctx: Context, formatter: HelpFormatter) -> None:
        super().write_help_sections(ctx, formatter)
        prefix = f"{ctx.find_root().info_name} talk"
        with formatter.section("Examples"):
            formatter.write_dl(
                [
                    (f"{ctx.find_root().info_name} convo list", "List conversations"),
                    (f"{prefix} alice", "Open a conversation with an agent"),
                    (f"{prefix} all", "Open the system group"),
                    (f"{prefix} alice,bob", "View a conversation between agents"),
                    (f"{prefix} gc_abcd1234", "Open a conversation by ID"),
                    (f'{prefix} alice "Hello"', "Send a message and exit"),
                ],
                col_max=formatter.width,
            )


class _ToolangHelpFormatter(HelpFormatter):
    def write_description(self, ctx: Context) -> None:
        description = Text(f"{ctx.command.help}.")
        version = _version.toolang_version()
        description.append(f" ({version})", style="dim")
        self.write_text(description)
        self.write_paragraph()

    def write_epilog(self, ctx: Context) -> None:
        super().write_epilog(ctx)
        self.write_paragraph()
        self.write_text(
            f"Run '{ctx.find_root().info_name} more' to see additional commands."
        )


class _ToolangHelpContext(HelpContext):
    formatter_class = _ToolangHelpFormatter


class _ToolangGroup(CliGroup):
    context_class = _ToolangHelpContext

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        commands = dict(kwargs.pop("commands", None) or {})
        commands.update(
            (name, factory()) for name, factory in _REGISTERED_COMMANDS.items()
        )
        super().__init__(*args, commands=commands, **kwargs)

    def resolve_command(self, ctx: Context, args: list[str]):
        return routing.resolve_command(self, ctx, args)

    def list_commands(self, ctx: Context) -> list[str]:
        names = TyperGroup.list_commands(self, ctx)
        ordered = [name for name in _COMMAND_ORDER if name in names]
        return [*ordered, *(name for name in names if name not in ordered)]


def _version_callback(value: bool) -> None:
    if not value:
        return
    typer.echo(_version.displayed_toolang_version())
    raise typer.Exit()


app = typer.Typer(
    cls=_ToolangGroup,
    help="Toolang — a language and runtime for agents and humans",
    add_completion=False,
    invoke_without_command=True,
    no_args_is_help=True,
    pretty_exceptions_enable=False,
    pretty_exceptions_show_locals=False,
)


def _registered_command(name: str, target: str, **kwargs: Any) -> None:
    if name in _REGISTERED_COMMANDS:
        raise RuntimeError(f"top-level command registered more than once: {name}")

    def create() -> LazyCommand:
        return lazy_typer_command(name, target, **kwargs)

    _REGISTERED_COMMANDS[name] = create


def _registered_group(target: str, *, name: str, **kwargs: Any) -> None:
    if name in _REGISTERED_COMMANDS:
        raise RuntimeError(f"top-level command registered more than once: {name}")

    def create() -> LazyCommand:
        return lazy_typer_group(name, target, **kwargs)

    _REGISTERED_COMMANDS[name] = create


@app.callback()
def callback(
    ctx: typer.Context,
    toolang_root: RootOption = None,
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            "-V",
            callback=_version_callback,
            help="Show version and exit",
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
        raise ClickException(str(exc)) from exc
    root = resolve_root(toolang_root)
    ctx.obj = CliContext(root=root)


class _MoreCommandsCommand(CliCommand):
    def format_help(self, ctx: Context, formatter: NativeHelpFormatter) -> None:
        root = typer.main.get_command(app)
        assert isinstance(root, TyperGroup)
        commands = {}
        for panel, names in _MORE_PANEL_COMMAND_ORDER:
            for name in names:
                if name not in root.commands:
                    continue
                command = copy(root.commands[name])
                assert isinstance(command, LazyCommand)
                command.hidden = False
                command.rich_help_panel = panel
                commands[name] = command
        group = CliGroup(name="more", commands=commands)
        assert isinstance(formatter, HelpFormatter)
        formatter.write_commands(Context(group))
        formatter.write_paragraph()
        formatter.write_text(
            f"Run '{ctx.find_root().info_name} COMMAND --help' for details."
        )


def more_commands(ctx: typer.Context) -> None:
    show_help(ctx)


_registered_command(
    "more",
    "toolang.cli.toolang.main:more_commands",
    help="Show additional commands",
    hidden=True,
    cls=_MoreCommandsCommand,
)


_registered_command(
    "new",
    "toolang.cli.toolang.commands.agent:new_agent",
    help="Create an agent",
    no_args_is_help=True,
    rich_help_panel=AGENT_COMMAND_PANEL,
)
_registered_command(
    "clone",
    "toolang.cli.toolang.commands.agent:clone_agent",
    help="Clone an agent",
    no_args_is_help=True,
    rich_help_panel=AGENT_COMMAND_PANEL,
)
_registered_command(
    "remove",
    "toolang.cli.toolang.commands.agent:remove_agent",
    help="Remove an agent",
    no_args_is_help=True,
    rich_help_panel=AGENT_COMMAND_PANEL,
)
_registered_command(
    "list",
    "toolang.cli.toolang.commands.agent:list_agents",
    help="List agents and their status",
    rich_help_panel=AGENT_COMMAND_PANEL,
)
_registered_command(
    "info",
    "toolang.cli.toolang.commands.agent:info_agent",
    help="Show agent information",
    no_args_is_help=True,
    rich_help_panel=AGENT_COMMAND_PANEL,
)
_registered_command(
    "cd",
    "toolang.cli.toolang.commands.cd:cd",
    help="Enter an agent home, or the Toolang root if no agent is specified",
    short_help="Enter an agent home",
    rich_help_panel=AGENT_COMMAND_PANEL,
)
_registered_group(
    "toolang.cli.toolang.commands.workspace:workspace_app",
    name="workspace",
    help="Manage agent workspaces",
    no_args_is_help=True,
    rich_help_panel=WORK_COMMAND_PANEL,
)
_registered_command(
    "serve",
    "toolang.cli.toolang.commands.runtime:run",
    help="Run an agent in the foreground",
    no_args_is_help=True,
    cls=_ServeCommand,
    rich_help_panel=RUNTIME_COMMAND_PANEL,
)
_registered_command(
    "start",
    "toolang.cli.toolang.commands.runtime:start",
    help="Start an agent in the background",
    no_args_is_help=True,
    cls=_StartCommand,
    rich_help_panel=RUNTIME_COMMAND_PANEL,
)
_registered_command(
    "stop",
    "toolang.cli.toolang.commands.runtime:stop",
    help="Stop an agent",
    no_args_is_help=True,
    rich_help_panel=RUNTIME_COMMAND_PANEL,
)
_registered_group(
    "toolang.cli.toolang.commands.chore:chore_app",
    name="chore",
    help="Manage one-off and scheduled tasks",
    no_args_is_help=True,
    rich_help_panel=WORK_COMMAND_PANEL,
)

_registered_command(
    "talk",
    "toolang.cli.toolang.commands.talk:talk_command",
    help="Have conversations with agents",
    cls=_TalkCommand,
    context_settings={"allow_interspersed_args": False},
    rich_help_panel=WORK_COMMAND_PANEL,
)

_registered_group(
    "toolang.cli.toolang.commands.convo:convo_app",
    name="convo",
    help="Manage conversations and rules",
    no_args_is_help=False,
    invoke_without_command=True,
    rich_help_panel=WORK_COMMAND_PANEL,
)


_registered_group(
    "toolang.cli.toolang.commands.hub:hub_app",
    name="hub",
    help="Manage the hub service",
    rich_help_panel=RUNTIME_COMMAND_PANEL,
)


_registered_command(
    "chat",
    "toolang.cli.toolang.commands.chat:chat_command",
    help="Chat directly with an agent",
    cls=_ChatCommand,
    rich_help_panel=WORK_COMMAND_PANEL,
)
_registered_command(
    "inspect",
    "toolang.cli.toolang.commands.inspect:inspect_command",
    help="Inspect agent runs",
    no_args_is_help=True,
    cls=_InspectCommand,
    rich_help_panel=INSPECTION_COMMAND_PANEL,
)
_registered_command(
    "steer",
    "toolang.cli.toolang.commands.thread:steer_command",
    help="Steer an active run",
    no_args_is_help=True,
    hidden=True,
    rich_help_panel=RUN_COMMAND_PANEL,
)
_registered_command(
    "cancel",
    "toolang.cli.toolang.commands.thread:cancel_command",
    help="Cancel an active run",
    no_args_is_help=True,
    hidden=True,
    rich_help_panel=RUN_COMMAND_PANEL,
)

_registered_command(
    "rerun",
    "toolang.cli.toolang.commands.thread:rerun_command",
    help="Rerun an earlier run as a new one",
    no_args_is_help=True,
    cls=_ThreadRunCommand,
    hidden=True,
    rich_help_panel=RUN_COMMAND_PANEL,
)
_registered_command(
    "rewind",
    "toolang.cli.toolang.commands.thread:rewind_command",
    help="Rewind a thread to an earlier run",
    no_args_is_help=True,
    hidden=True,
    rich_help_panel=THREAD_COMMAND_PANEL,
)
_registered_command(
    "fork",
    "toolang.cli.toolang.commands.thread:fork_command",
    help="Fork a thread from an earlier run",
    no_args_is_help=True,
    hidden=True,
    rich_help_panel=THREAD_COMMAND_PANEL,
)

_registered_command(
    "models",
    "toolang.cli.toolang.commands.model_catalog:models_command",
    help="List available models",
    rich_help_panel=INSPECTION_COMMAND_PANEL,
)
_registered_command(
    "top",
    "toolang.cli.toolang.commands.top:top",
    help="Monitor agent runs",
    rich_help_panel=INSPECTION_COMMAND_PANEL,
)
_registered_command(
    "providers",
    "toolang.cli.toolang.commands.model_catalog:providers_command",
    help="List available model providers",
    rich_help_panel=INSPECTION_COMMAND_PANEL,
)
_registered_command(
    "tools",
    "toolang.cli.toolang.commands.plugin:list_tools",
    help="List available tools",
    rich_help_panel=INSPECTION_COMMAND_PANEL,
)
_registered_command(
    "catalogs",
    "toolang.cli.toolang.commands.plugin:list_catalogs",
    help="List installed model catalogs",
    hidden=True,
)
_registered_command(
    "adapters",
    "toolang.cli.toolang.commands.plugin:adapters_command",
    help="List installed model adapters",
    hidden=True,
)
_registered_command(
    "toolsets",
    "toolang.cli.toolang.commands.plugin:list_toolsets",
    help="List installed toolsets",
    hidden=True,
)
_registered_command(
    "sandboxes",
    "toolang.cli.toolang.commands.plugin:list_sandboxes",
    help="List installed sandboxes",
    hidden=True,
)

_registered_group(
    "toolang.cli.toolang.commands.caps:psyche_app",
    name="psyche",
    help="Manage psyche caps",
    no_args_is_help=True,
    rich_help_panel=CAPS_COMMAND_PANEL,
)
_registered_group(
    "toolang.cli.toolang.commands.caps:skill_app",
    name="skill",
    help="Manage skill caps",
    no_args_is_help=True,
    rich_help_panel=CAPS_COMMAND_PANEL,
)
_registered_group(
    "toolang.cli.toolang.commands.caps:service_app",
    name="service",
    help="Manage service caps",
    no_args_is_help=True,
    rich_help_panel=CAPS_COMMAND_PANEL,
)
_registered_group(
    "toolang.cli.toolang.commands.caps:prompt_app",
    name="prompt",
    help="Manage prompt caps",
    no_args_is_help=True,
    rich_help_panel=CAPS_COMMAND_PANEL,
)
_registered_command(
    "caps",
    "toolang.cli.caps.commands:list_caps",
    help="List available caps",
    rich_help_panel=INSPECTION_COMMAND_PANEL,
)


_registered_command(
    "fmt",
    "toolang.cli.toolang.commands.program:fmt",
    help="Format .too source",
    hidden=True,
    no_args_is_help=True,
)
_registered_command(
    "parse",
    "toolang.cli.toolang.commands.program:parse_program",
    help="Parse .too source",
    hidden=True,
    no_args_is_help=True,
)
_registered_command(
    "highlight",
    "toolang.cli.toolang.commands.program:highlight_source",
    help="Highlight .too source",
    hidden=True,
    no_args_is_help=True,
)
_registered_command(
    "_serve",
    "toolang.cli.toolang.commands.runtime:serve",
    help="Run an agent server",
    hidden=True,
    no_args_is_help=True,
)

_registered_command(
    "init",
    "toolang.cli.toolang.commands.init:init_script",
    help="Initialize Toolang in a directory",
    no_args_is_help=True,
    rich_help_panel=SCRIPT_COMMAND_PANEL,
)
_registered_command(
    "run",
    "toolang.cli.toolang.main:script_entry",
    help="Execute a .too file",
    cls=_ScriptEntryCommand,
    context_settings={"allow_interspersed_args": False},
    epilog="Add --help after FILE or RUNNABLE for details.",
    rich_help_panel=SCRIPT_COMMAND_PANEL,
)


def main(argv: Sequence[str] | None = None) -> int:
    args = list(argv) if argv is not None else sys.argv[1:]
    return run(app, args=args, prog_name=_prog_name(sys.argv[0] if sys.argv else ""))


def _prog_name(argv0: str) -> str:
    text = Path(argv0).name.strip()
    return text or "toolang"


if __name__ == "__main__":
    raise SystemExit(main())
