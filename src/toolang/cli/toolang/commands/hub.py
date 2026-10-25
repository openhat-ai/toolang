"""Status and lifecycle commands for the root-scoped teaming Hub."""

import httpx
import psutil

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
from toolang.common.config_sources import read_config
from toolang.setup.teaming import load_teaming_root
from toolang.teaming.schemas import HubInfo
from toolang.up.config import resolve_port_override, hub_config
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
        subcommand_metavar="[COMMAND] [ARGUMENTS]",
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
    typer.echo(f"Hub started: {record.endpoint}")


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

    diagnostic = None
    try:
        record = HubProcess(root).current()
        local_state = record.status if record is not None else "not running"
    except (ValueError, OSError, psutil.Error) as exc:
        local_state = "unknown"
        diagnostic = f"record inspection failed: {exc}"
    endpoint = None
    version = None
    try:
        endpoint = load_teaming_root(root).hub
        with httpx.Client(trust_env=False, timeout=1) as client:
            response = client.get(endpoint + "/info")
            response.raise_for_status()
            info = HubInfo.model_validate(response.json())
            if not info.identity:
                raise ValueError("Hub metadata has no identity")
            version = version_label(info.version)
            health = client.get(
                endpoint + "/healthz", headers={"X-Toolang-Backend": info.identity}
            )
            state = (
                "running"
                if health.status_code == 200 and health.json() == {"ok": True}
                else "unavailable"
            )
    except httpx.HTTPError:
        state = "unavailable"
    except Exception as exc:
        state = "unknown"
        detail = str(exc).strip() or type(exc).__name__
        diagnostic = f"{diagnostic}; {detail}" if diagnostic else detail
    render_hub_snapshot(
        HubSnapshot(state, version, agents, diagnostic, endpoint, local_state),
        Console(),
    )
