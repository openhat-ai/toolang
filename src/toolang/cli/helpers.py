from __future__ import annotations

from typing import Annotated, Literal

import typer

from toolang.layout import agent_room, agent_source_path, agents_db_path

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
        db_path = agents_db_path(toolang_root)
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
        db_path = agents_db_path(_toolang_root())
        resolved = _resolve_cli_agent(agent, db_path=db_path)
        typer.echo(str(agent_source_path(resolved.home, resolved.name)))

    @app.command(
        hidden=True,
        no_args_is_help=True,
        help="Print an agent room path.",
        rich_help_panel="Helper Commands",
    )
    def room(
        agent: Annotated[str, typer.Argument(help="Agent selector")],
    ) -> None:
        db_path = agents_db_path(_toolang_root())
        resolved = _resolve_cli_agent(agent, db_path=db_path)
        typer.echo(str(agent_room(resolved.home, resolved.name)))

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
