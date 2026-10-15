"""Observe existing agent or Hub services without acquiring an executor."""

import asyncio
import math
from typing import Annotated, Literal

from rich.console import Console
import typer
from typer._click.exceptions import ClickException

from toolang.cli.common.messaging import talk_context
from toolang.cli.common.terminal_session import prepare_terminal
from toolang.cli.common.terminal_title import agent_label
from toolang.cli.common.activity import watch
from toolang.cli.common.activity_view import since as stats_since
from toolang.execution.activity import ActivityQuery, ActivityReader, duration
from toolang.teaming.observation import CombinedObservation, LocalObservation, Presence
from toolang.teaming.errors import TeamingError
from toolang.cli.common.context import (
    cli_context,
    context_layout,
    context_root,
)
from toolang.up.hub import HubProcess
from toolang.up.activity import RuntimeObservation
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
        typer.Option(help="Sort rows by this metric"),
    ] = "activity",
    recent: Annotated[
        str, typer.Option(help="Rolling activity range: 30m, 1d, 1w, all")
    ] = "30m",
    since: Annotated[
        str,
        typer.Option(
            help="Stats since session, all, a duration, or a timestamp with timezone"
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
    """Monitor runtime activity"""
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
    layout = None
    context = ""
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
        source = CombinedObservation(
            LocalObservation(
                ActivityReader(layout.run_store, agent), presence=presence
            ),
            RuntimeObservation(layout.root, home=layout.home),
        )
    else:
        source = RuntimeObservation(context_root(ctx))
        endpoint = None
        try:
            connection = HubProcess(context_root(ctx)).connection()
        except (OSError, ValueError, RuntimeError, TeamingError):
            pass
        else:
            endpoint, backend = connection.endpoint, connection.identity
            context = talk_context(
                context_root(ctx),
                connection.identity,
                connection.human,
                connection.endpoint,
            )
    console = Console()
    try:
        with prepare_terminal(
            "top",
            layout=layout,
            root=context_root(ctx),
            label=f"top {agent_label(layout.name) if layout is not None else 'team'}",
            context=context,
            eligible=not once and console.is_terminal,
            stream=console.file,
        ):
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
