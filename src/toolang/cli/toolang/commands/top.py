"""Observe existing agent or Hub services without acquiring an executor."""

import asyncio
from typing import Annotated

from rich.console import Console
import typer
from typer._click.exceptions import ClickException

from toolang.cli.common.activity import watch
from toolang.cli.common.context import (
    cli_context,
    context_layout,
    context_root,
    user_call,
)
from toolang.up.hub import HubProcess
from toolang.up.process import AgentProcess


def top_command(
    ctx: typer.Context,
    once: Annotated[
        bool, typer.Option(help="Print one activity snapshot and exit")
    ] = False,
) -> None:
    """Show team or selected agent activity"""
    selected = cli_context(ctx)
    token = None
    agent = None
    if selected.agent is not None or selected.layout is not None:
        layout = context_layout(ctx)
        status = user_call(
            AgentProcess(layout).status, ui_base_url="", check_health=True
        )
        if status is None or status.status != "running" or not status.endpoint:
            raise ClickException(
                "Agent is not running; start it before observing activity"
            )
        endpoint = status.endpoint
        agent = f"agent:{layout.name}"
    else:
        connection = user_call(HubProcess(context_root(ctx)).connection)
        endpoint, token = connection.endpoint, connection.token
    console = Console()
    try:
        asyncio.run(
            watch(
                endpoint,
                agent=agent,
                token=token,
                once=once or not console.is_terminal,
                console=console,
            )
        )
    except ValueError as exc:
        raise ClickException(str(exc)) from exc
    except KeyboardInterrupt:
        return
