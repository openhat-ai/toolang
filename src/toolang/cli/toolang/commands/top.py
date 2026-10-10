"""Observe existing agent or Hub services without acquiring an executor."""

import asyncio
import math
from typing import Annotated, Literal

from rich.console import Console
import typer
from typer._click.exceptions import ClickException

from toolang.cli.common.activity import watch
from toolang.cli.common.activity_view import since as stats_since
from toolang.execution.activity import ActivityQuery, ActivityReader, duration
from toolang.teaming.observation import LocalObservation, Presence
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
        Literal["activity", "spend", "cost", "time"],
        typer.Option(help="Sort whole objects by displayed values"),
    ] = "activity",
    recent: Annotated[
        str, typer.Option(help="Rolling activity range: 30m, 1d, 1w, all")
    ] = "30m",
    since: Annotated[
        str,
        typer.Option(
            help="Stats: session, all, rolling duration, or fixed timezone-aware timestamp"
        ),
    ] = "session",
    filter: Annotated[
        str, typer.Option(help="Literal case-insensitive activity filter")
    ] = "",
    active: Annotated[bool, typer.Option(help="Show unfinished work only")] = False,
    refresh: Annotated[
        float, typer.Option(help="Screen refresh interval in seconds")
    ] = 0.1,
) -> None:
    """Show team or selected agent activity"""
    if tree and view != "execution":
        raise ClickException("--tree requires --view execution")
    if not math.isfinite(refresh) or refresh <= 0:
        raise ClickException("Refresh must be a finite positive number of seconds")
    try:
        query = ActivityQuery(stats_since(since), duration(recent), filter, active)
    except ValueError as exc:
        raise ClickException(str(exc)) from exc
    selected = cli_context(ctx)
    backend = None
    agent = None
    source = None
    if selected.agent is not None or selected.layout is not None:
        layout = context_layout(ctx)
        process = AgentProcess(layout)

        def presence() -> Presence:
            status = process.status(ui_base_url="", check_health=False)
            if status is None or status.status in {"stopped", "failed"}:
                return "offline"
            return "online" if status.status == "running" else "unknown"

        endpoint = None
        agent = f"agent:{layout.name}"
        source = LocalObservation(
            ActivityReader(layout.run_store, agent), presence=presence
        )
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
                refresh=refresh,
                source=source,
            )
        )
    except ValueError as exc:
        raise ClickException(str(exc)) from exc
    except KeyboardInterrupt:
        return
