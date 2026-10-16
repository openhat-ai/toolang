"""Status and lifecycle commands for the root-scoped teaming Hub."""

import os
import sys
from typing import Annotated

import typer
from typer._click.exceptions import ClickException

from toolang.cli.common.context import context_root, user_call
from toolang.cli.common.banner import version_label
from toolang.cli.common.terminal_session import prepare_terminal
from toolang.cli.common.help import CliCommand, CliGroup
from toolang.setup.teaming import load_teaming_root
from toolang.up.config import resolve_port_override
from toolang.up.hub import HubProcess, serve as serve_hub

PortOption = Annotated[
    int | None, typer.Option("--port", min=1, max=65535, help="Hub API port")
]


def hub_app() -> typer.Typer:
    app = typer.Typer(
        cls=CliGroup,
        help="Show or control the teaming service",
        add_completion=False,
        invoke_without_command=True,
        no_args_is_help=False,
        pretty_exceptions_enable=False,
        pretty_exceptions_show_locals=False,
    )
    app.callback()(status)
    for name, command in (
        ("start", start),
        ("serve", serve),
        ("stop", stop),
    ):
        app.command(name, cls=CliCommand)(command)
    return app


def _settings(ctx: typer.Context, port: int | None):
    config = user_call(load_teaming_root, context_root(ctx))
    selected = user_call(
        resolve_port_override,
        port,
        environ=os.environ,
        env_name="TOOLANG_HUB_PORT",
        configured=config.hub_port,
    )
    assert selected is not None
    return config, selected


def start(ctx: typer.Context, port: PortOption = None) -> None:
    """Start the hub in the background; requires Redis/Valkey"""
    _, selected = _settings(ctx, port)
    root = context_root(ctx)
    record = user_call(
        HubProcess(root).start,
        [
            sys.executable,
            "-m",
            "toolang.cli.toolang.main",
            "--root",
            str(root),
            "hub",
            "serve",
            "--port",
            str(selected),
        ],
    )
    typer.echo(f"Hub started: {record.connection.endpoint}")


def serve(ctx: typer.Context, port: PortOption = None) -> None:
    """Run the hub in the foreground"""
    config, selected = _settings(ctx, port)
    with prepare_terminal(
        "serve",
        root=context_root(ctx),
        label="serve hub",
        stream=sys.stderr,
    ):
        raise typer.Exit(user_call(serve_hub, context_root(ctx), config, port=selected))


def stop(
    ctx: typer.Context,
    force: Annotated[
        bool, typer.Option(help="Kill after graceful shutdown times out")
    ] = False,
) -> None:
    """Stop this root's hub; keep agents and Redis/Valkey running"""
    if not user_call(HubProcess(context_root(ctx)).stop, force=force):
        raise ClickException("Hub is not running")
    typer.echo("Hub stopped")


def status(ctx: typer.Context) -> None:
    """Show the hub endpoint, version, and readiness"""
    if ctx.invoked_subcommand is not None:
        return
    hub = HubProcess(context_root(ctx))
    record = user_call(hub.current)
    if record is None:
        typer.echo("Hub stopped")
        return
    state = (
        "starting"
        if record.status == "starting"
        else "running"
        if hub.ready(record)
        else "unavailable"
    )
    version = version_label(hub.version(record))
    typer.echo(f"Hub {state}: {record.connection.endpoint}, {version}")
