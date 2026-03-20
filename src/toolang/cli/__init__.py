from __future__ import annotations

import sys
from typing import Annotated

import click
import typer
from dotenv import load_dotenv
from typer.core import TyperGroup

from .agents import register_agent_commands
from .caps import register_cap_commands
from .helpers import register_helper_commands
from .runtime import register_runtime_commands
from .support import _toolang_version
from toolang.errors import ToolangError

__all__ = ["app", "main"]


def _version_callback(value: bool | None) -> None:
    if value:
        typer.echo(f"toolang {_toolang_version()}")
        raise typer.Exit()


def _show_hidden_callback(ctx: click.Context, _param: click.Parameter, value: bool) -> bool:
    if value:
        ctx.meta["show_hidden_commands"] = True
    return value


def _show_hidden_commands(ctx: click.Context) -> bool:
    if ctx.meta.get("show_hidden_commands") is True:
        return True
    value = ctx.params.get("show_hidden")
    return isinstance(value, bool) and value


class ToolangGroup(TyperGroup):
    def format_help(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        if not _show_hidden_commands(ctx):
            return super().format_help(ctx, formatter)

        hidden_commands: list[click.Command] = []
        for command_name in self.list_commands(ctx):
            command = self.get_command(ctx, command_name)
            if command is None or not command.hidden:
                continue
            hidden_commands.append(command)

        for command in hidden_commands:
            command.hidden = False
        try:
            return super().format_help(ctx, formatter)
        finally:
            for command in hidden_commands:
                command.hidden = True


app = typer.Typer(
    cls=ToolangGroup,
    help="Toolang CLI",
    add_completion=False,
    invoke_without_command=True,
    pretty_exceptions_enable=False,
    pretty_exceptions_show_locals=False,
)


@app.callback()
def callback(
    ctx: typer.Context,
    version: Annotated[
        bool | None,
        typer.Option(
            "--version",
            help="Show the version and exit.",
            callback=_version_callback,
            is_eager=True,
        ),
    ] = None,
    show_hidden: Annotated[
        bool,
        typer.Option(
            "--hidden",
            help="Show hidden commands in help output.",
            callback=_show_hidden_callback,
            is_eager=True,
        ),
    ] = False,
) -> None:
    """Toolang CLI."""
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())
        raise typer.Exit()


register_helper_commands(app)
register_agent_commands(app)
register_runtime_commands(app)
register_cap_commands(app)


def _reorder_help_entries() -> None:
    command_order = {
        "new": 0,
        "clone": 1,
        "remove": 2,
        "list": 3,
        "sync": 4,
        "invoke": 5,
        "serve": 6,
        "start": 7,
        "home": 100,
        "source": 101,
        "room": 102,
        "init": 103,
    }
    group_order = {
        "psyche": 0,
        "skill": 1,
        "service": 2,
        "prompt": 3,
        "bus": 4,
    }
    app.registered_commands.sort(
        key=lambda info: (command_order.get(info.name or "", 999), info.name or "")
    )
    app.registered_groups.sort(
        key=lambda info: (group_order.get(info.name or "", 999), info.name or "")
    )


_reorder_help_entries()


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    try:
        app(
            args=list(argv) if argv is not None else None,
            prog_name="toolang",
            standalone_mode=False,
        )
    except click.exceptions.Exit as exc:
        return exc.exit_code
    except click.ClickException as exc:
        exc.show(file=sys.stderr)
        return exc.exit_code
    except (FileNotFoundError, ToolangError) as exc:
        typer.echo(f"toolang error: {exc}", err=True)
        return 1
    return 0
