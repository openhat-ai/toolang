"""Deferred chat command entry points.

Chat execution is intentionally loaded only when invoked so the remaining CLI
can use execution storage directly while the TUI is migrated separately.
"""

from __future__ import annotations

from typing import Annotated

import typer


def chat_command(
    ctx: typer.Context,
    thread: Annotated[
        str | None,
        typer.Argument(
            help="Thread id to continue. Run id also accepted. Omit to start a new one.",
            metavar="THREAD",
        ),
    ] = None,
    models: Annotated[
        list[str] | None,
        typer.Option("--models", help="Limit available models. Pass CSV or repeat."),
    ] = None,
    tools: Annotated[
        list[str] | None,
        typer.Option("--tools", help="Allow selected tools. Pass CSV or repeat."),
    ] = None,
    caps: Annotated[
        list[str] | None,
        typer.Option("--caps", help="Allow selected caps. Pass CSV or repeat."),
    ] = None,
    agic: Annotated[
        str | None, typer.Option("--agic", help="Use an agic for new runs.")
    ] = None,
    flow: Annotated[
        str | None, typer.Option("--flow", help="Use a flow for new runs.")
    ] = None,
    sandbox: Annotated[
        str | None,
        typer.Option(
            "--sandbox",
            help="Execute the session in this sandbox when no API is running.",
        ),
    ] = None,
) -> None:
    from .chat import chat_command as run

    run(
        ctx,
        thread=thread,
        models=models,
        tools=tools,
        caps=caps,
        agic=agic,
        flow=flow,
        sandbox=sandbox,
    )


def send_command(
    ctx: typer.Context,
    thread: Annotated[str, typer.Argument(help="Thread id.")],
    message: Annotated[str, typer.Argument(help="Message text.")],
    model: Annotated[
        str | None, typer.Option("--model", help="Model selector.")
    ] = None,
) -> None:
    from .chat import send_command as run

    run(ctx, thread=thread, message=message, model=model)


def attach_command(
    ctx: typer.Context,
    thread: Annotated[str, typer.Argument(help="Thread id.")],
) -> None:
    from .chat import attach_command as run

    run(ctx, thread=thread)
