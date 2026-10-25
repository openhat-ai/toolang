"""Status and lifecycle commands for the root-scoped teaming Hub."""

import httpx
import psutil

import os
import sys
from typing import Annotated

import typer
from typer._click import Context
from typer._click.exceptions import ClickException

from toolang.cli.common.context import context_root, user_call
from toolang.cli.common.output import runtime_status_label
from toolang.cli.common.terminal_session import prepare_terminal
from toolang.cli.common.help import CliCommand, CliGroup, MetadataCommand, show_help
from toolang.common.config_sources import read_config
from toolang.teaming.schemas import HubInfo
from toolang.up.config import resolve_port_override, hub_config
from toolang.up.hub import HubProcess, serve as serve_hub

PortOption = Annotated[
    int | None,
    typer.Option(
        "--port", metavar="PORT", min=1, max=65535, help="Bind the hub API to this port"
    ),
]


class _HubGroup(CliGroup):
    def collect_usage_pieces(self, ctx: Context) -> list[str]:
        return [self.subcommand_metavar]


def hub_app() -> typer.Typer:
    app = typer.Typer(
        cls=_HubGroup,
        help="Manage the hub service",
        subcommand_metavar="[COMMAND]",
        add_completion=False,
        invoke_without_command=True,
        no_args_is_help=False,
        pretty_exceptions_enable=False,
        pretty_exceptions_show_locals=False,
    )
    app.callback()(_help)
    app.command("status", cls=MetadataCommand)(status)
    for name, command in (
        ("start", start),
        ("stop", stop),
        ("serve", serve),
    ):
        app.command(name, cls=CliCommand)(command)
    return app


def _help(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        show_help(ctx)


def _settings(ctx: typer.Context, port: int | None):
    root = context_root(ctx)
    path = root / "config.toml"
    config = user_call(
        hub_config,
        user_call(read_config, path, include_workspaces=False),
        source=path,
        root=str(root.resolve()),
    )
    selected = user_call(
        resolve_port_override,
        port,
        environ=os.environ,
        env_name="TOOLANG_HUB_PORT",
        configured=config.port,
    )
    assert selected is not None
    return config, selected


def start(ctx: typer.Context, port: PortOption = None) -> None:
    """Start hub in the background"""
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
    typer.echo(f"Hub started: {record.endpoint}")


def serve(ctx: typer.Context, port: PortOption = None) -> None:
    """Run hub in the foreground"""
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
    """Stop hub"""
    if not user_call(HubProcess(context_root(ctx)).stop, force=force):
        raise ClickException("Hub is not running")
    typer.echo("Hub stopped")


def status(ctx: typer.Context) -> None:
    """Show hub status"""
    try:
        record = HubProcess(context_root(ctx)).current()
    except (ValueError, OSError, psutil.Error) as exc:
        typer.echo(f"hub unknown: {' '.join(str(exc).split())}")
        return
    if record is None or runtime_status_label(record.status) == "down":
        typer.echo("hub not running")
        return
    version = "unknown"
    try:
        with httpx.Client(trust_env=False, timeout=1) as client:
            response = client.get(record.endpoint + "/info")
            response.raise_for_status()
            info = HubInfo.model_validate(response.json())
            version = info.version.strip() or "unknown"
    except (httpx.HTTPError, ValueError):
        pass
    host = f"[{record.host}]" if ":" in record.host else record.host
    typer.echo(f"hub running: {version}, {host}:{record.port}, {record.pid}")
