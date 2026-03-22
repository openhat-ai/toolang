from __future__ import annotations

from typing import Annotated

import typer

from toolang.concepts.layout import ToolangRoot

from .support import _cors_allow_origins, _toolang_root
from .invoke import invoke_command, sync_command
from .serve import serve_command, start_command, stop_command

bus_app = typer.Typer(
    help="Bus commands",
    add_completion=False,
    no_args_is_help=True,
    pretty_exceptions_enable=False,
    pretty_exceptions_show_locals=False,
)


def register_runtime_commands(app: typer.Typer) -> None:
    app.command(
        "invoke",
        help="Run one non-interactive agent turn.",
        no_args_is_help=True,
    )(invoke_command)
    app.command(
        "sync",
        help="Sync one agent state.",
        no_args_is_help=True,
    )(sync_command)
    app.command(
        "serve",
        help="Serve one agent in the foreground.",
        no_args_is_help=True,
    )(serve_command)
    app.command(
        "start",
        help="Start serving one agent in the background.",
        no_args_is_help=True,
    )(start_command)
    app.command(
        "stop",
        help="Stop one running agent.",
        no_args_is_help=True,
    )(stop_command)

    @bus_app.command("serve")
    def bus_serve(
        host: Annotated[str, typer.Option(help="Host interface to bind")] = "127.0.0.1",
        port: Annotated[int, typer.Option(help="Port to listen on")] = 8780,
    ) -> None:
        toolang_root = _toolang_root()
        from toolang.bus.app import serve_bus_app

        serve_bus_app(
            ToolangRoot.resolve(toolang_root).bus_events_db_path,
            host=host,
            port=port,
            cors_allow_origins=_cors_allow_origins(),
        )

    app.add_typer(bus_app, name="bus", no_args_is_help=True)
