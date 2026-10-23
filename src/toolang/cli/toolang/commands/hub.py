"""Status and lifecycle commands for the root-scoped teaming Hub."""

import os
import sys
from typing import Annotated

import typer
from rich.console import Console
from typer._click.exceptions import ClickException

from toolang.cli.common.context import context_root, user_call
from toolang.cli.common.banner import version_label
from toolang.cli.common.terminal_session import prepare_terminal
from toolang.cli.common.help import CliCommand, CliGroup
from toolang.setup.teaming import load_teaming_root
from toolang.up.config import resolve_port_override
from toolang.up.hub import HubProcess, serve as serve_hub
from toolang.up.types import RuntimeInstanceSnapshot
from toolang.up.watcher import runtime_snapshot
from .hub_status import HubSnapshot, render_hub_snapshot

PortOption = Annotated[
    int | None,
    typer.Option(
        "--port", metavar="PORT", min=1, max=65535, help="Bind the hub API to this port"
    ),
]


def hub_app() -> typer.Typer:
    app = typer.Typer(
        cls=CliGroup,
        help="Manage the hub service",
        epilog="Run 'too hub' to show hub and agent status.",
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
    """Start the hub in the background"""
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
        bool, typer.Option(help="Force-stop when graceful shutdown does not complete")
    ] = False,
) -> None:
    """Stop the hub"""
    if not user_call(HubProcess(context_root(ctx)).stop, force=force):
        raise ClickException("Hub is not running")
    typer.echo("Hub stopped")


def status(ctx: typer.Context) -> None:
    """Show hub and agent status"""
    if ctx.invoked_subcommand is not None:
        return
    root = context_root(ctx)
    try:
        agents = runtime_snapshot(root)
    except Exception as exc:
        agents = RuntimeInstanceSnapshot(
            (), (f"agent discovery: {str(exc).strip() or type(exc).__name__}",)
        )

    hub = HubProcess(root)
    hub_diagnostic = None
    try:
        record = hub.current()
    except Exception as exc:
        record = None
        state = "-"
        version = None
        hub_diagnostic = (
            f"record inspection failed: {str(exc).strip() or type(exc).__name__}"
        )
    else:
        if record is None:
            state = "not running"
            version = None
        else:
            try:
                state = (
                    "starting"
                    if record.status == "starting"
                    else "running"
                    if hub.ready(record)
                    else "unavailable"
                )
                version = version_label(hub.version(record))
            except Exception as exc:
                state = "-"
                version = None
                hub_diagnostic = (
                    f"state inspection failed: {str(exc).strip() or type(exc).__name__}"
                )

    render_hub_snapshot(HubSnapshot(state, version, agents, hub_diagnostic), Console())
