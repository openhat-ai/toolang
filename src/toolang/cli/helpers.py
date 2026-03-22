from __future__ import annotations

from typing import Annotated, Literal

import typer

from toolang.concepts.layout import AgentHome, ToolangRoot

from .support import (
    _fish_init_script,
    _init_install_note,
    _posix_init_script,
    _resolve_cli_agent,
    _toolang_root,
)


def register_helper_commands(app: typer.Typer) -> None:
    @app.command(
        hidden=True,
        help="Print the Toolang root or an agent home path.",
        rich_help_panel="Helper Commands",
    )
    def home(
        agent: Annotated[str | None, typer.Argument(help="Agent selector")] = None,
    ) -> None:
        toolang_root = _toolang_root()
        if agent is None:
            typer.echo(str(toolang_root))
            return
        db_path = ToolangRoot.resolve(toolang_root).agents_db_path
        resolved = _resolve_cli_agent(agent, db_path=db_path)
        typer.echo(str(resolved.home))

    @app.command(
        hidden=True,
        no_args_is_help=True,
        help="Print an agent source file path.",
        rich_help_panel="Helper Commands",
    )
    def source(
        agent: Annotated[str, typer.Argument(help="Agent selector")],
    ) -> None:
        db_path = ToolangRoot.resolve(_toolang_root()).agents_db_path
        resolved = _resolve_cli_agent(agent, db_path=db_path)
        typer.echo(str(resolved.source))

    @app.command(
        hidden=True,
        no_args_is_help=True,
        help="Print an agent room path.",
        rich_help_panel="Helper Commands",
    )
    def room(
        agent: Annotated[str, typer.Argument(help="Agent selector")],
    ) -> None:
        db_path = ToolangRoot.resolve(_toolang_root()).agents_db_path
        resolved = _resolve_cli_agent(agent, db_path=db_path)
        typer.echo(str(AgentHome.resolve(resolved.home).room(resolved.name).path))

    @app.command(
        hidden=True,
        no_args_is_help=True,
        help="Print shell helper setup.",
        rich_help_panel="Helper Commands",
    )
    def init(
        shell: Annotated[
            Literal["zsh", "bash", "fish"],
            typer.Argument(help="Shell to initialize"),
        ],
    ) -> None:
        typer.echo(_init_install_note(shell), err=True)
        if shell == "fish":
            typer.echo(_fish_init_script())
            return
        typer.echo(_posix_init_script())
