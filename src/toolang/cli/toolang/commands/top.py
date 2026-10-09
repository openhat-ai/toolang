"""Observe existing agent or Hub services without acquiring an executor."""

import asyncio
from typing import Annotated, Literal

from rich.console import Console
import typer
from typer._click.exceptions import ClickException

from toolang.cli.common.activity import watch
from toolang.cli.common.activity_view import duration, since as stats_since
from toolang.execution.activity import ActivityQuery
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
    view: Annotated[
        Literal["agent", "thread", "execution"] | None,
        typer.Option(help="Row scope (team: agent; single agent: thread)"),
    ] = None,
    tree: Annotated[
        bool, typer.Option(help="Show running paths; requires --view execution")
    ] = False,
    sort: Annotated[
        Literal["activity", "cost", "time"],
        typer.Option(help="Sort whole objects by displayed values"),
    ] = "activity",
    recent: Annotated[
        str, typer.Option(help="Rolling activity range: 30m, 1d, 1w, all")
    ] = "30m",
    since: Annotated[
        str,
        typer.Option(
            help="Stats start: session, all, duration, or timezone-aware timestamp"
        ),
    ] = "session",
    filter: Annotated[
        str, typer.Option(help="Literal case-insensitive activity filter")
    ] = "",
    active: Annotated[bool, typer.Option(help="Show unfinished work only")] = False,
) -> None:
    """Show team or selected agent activity"""
    if tree and view != "execution":
        raise ClickException("--tree requires --view execution")
    try:
        query = ActivityQuery(stats_since(since), duration(recent), filter, active)
    except ValueError as exc:
        raise ClickException(str(exc)) from exc
    selected = cli_context(ctx)
    backend = None
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
        endpoint, backend = connection.endpoint, connection.identity
    console = Console()
    try:
        asyncio.run(
            watch(
                endpoint,
                agent=agent,
                backend=backend,
                once=once or not console.is_terminal,
                console=console,
                view=view,
                tree=tree,
                sort=sort,
                query=query,
                recent_label=recent,
            )
        )
    except ValueError as exc:
        raise ClickException(str(exc)) from exc
    except KeyboardInterrupt:
        return
