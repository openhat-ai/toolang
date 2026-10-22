"""Observe current execution and recent source statistics."""

import math
import sys
from typing import Annotated

from rich.console import Console
import typer

from toolang.cli.common.agent_argument import AgentArgument
from typer._click.exceptions import ClickException

from toolang.cli.common.context import context_agent, context_layout, context_root
from toolang.cli.common.terminal_session import prepare_terminal
from toolang.cli.common.top_view import TopView
from toolang.up.top import TopObserver
from toolang.up.types import TopPeriod as Period, TopRecent as Recent


def top(
    ctx: typer.Context,
    agent: Annotated[str | None, AgentArgument("home", placements="resident")] = None,
    period: Annotated[Period, typer.Option(help="Rolling usage period")] = Period.hour,
    recent: Annotated[
        Recent, typer.Option(help="Recently completed root Runs")
    ] = Recent.five_minutes,
    refresh: Annotated[float, typer.Option(help="Refresh interval in seconds")] = 1,
    once: Annotated[bool, typer.Option("--once", help="Print one snapshot")] = False,
    json_output: Annotated[
        bool, typer.Option("--json", help="Print one JSON snapshot")
    ] = False,
) -> None:
    """Show live trees, recent completions and rolling usage"""
    if not math.isfinite(refresh) or refresh <= 0:
        raise typer.BadParameter("must be finite and positive", param_hint="--refresh")
    agent = context_agent(ctx)
    root = context_root(ctx)
    observer = TopObserver(
        root,
        agent=agent,
        period=period.seconds,
        recent=recent.seconds,
        refresh=refresh,
    )
    view = TopView(scope=f"too {agent} top" if agent else "too top")
    try:
        if once or json_output or not (sys.stdin.isatty() and sys.stdout.isatty()):
            snapshot = observer.once()
            if json_output:
                typer.echo(snapshot.model_dump_json())
            else:
                console = Console()
                console.print(
                    view.render(snapshot, width=console.width), soft_wrap=True
                )
        else:
            from toolang.cli.common.top import run_top

            with prepare_terminal(
                "top",
                root=root,
                layout=context_layout(ctx) if agent else None,
                label=f"{agent} top" if agent else "top",
                context="" if agent else str(root.resolve()),
            ):
                run_top(observer, view)
    except KeyboardInterrupt:
        pass
    except (OSError, RuntimeError, ValueError) as exc:
        raise ClickException(str(exc)) from exc
    finally:
        observer.close()
