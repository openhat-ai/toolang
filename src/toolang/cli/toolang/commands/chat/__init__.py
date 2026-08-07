"""Terminal chat command entry points."""

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
    model: Annotated[
        str | None,
        typer.Option("--model", help="Select the initial model for new runs."),
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
            help="Execute the session in this sandbox.",
        ),
    ] = None,
    limit: Annotated[
        list[str] | None,
        typer.Option(
            "--limit",
            help="Override run limits as field=value pairs. Pass CSV or repeat.",
        ),
    ] = None,
) -> None:
    from .main import chat_command as run

    run(
        ctx,
        thread=thread,
        models=models,
        model=model,
        tools=tools,
        caps=caps,
        agic=agic,
        flow=flow,
        sandbox=sandbox,
        limit=limit,
    )


def send_command(
    ctx: typer.Context,
    thread: Annotated[str, typer.Argument(help="Thread id.")],
    message: Annotated[str, typer.Argument(help="Message text.")],
    model: Annotated[
        str | None, typer.Option("--model", help="Model selector.")
    ] = None,
    limit: Annotated[
        list[str] | None,
        typer.Option(
            "--limit",
            help="Override run limits as field=value pairs. Pass CSV or repeat.",
        ),
    ] = None,
) -> None:
    from .main import send_command as run

    run(ctx, thread=thread, message=message, model=model, limit=limit)


def attach_command(
    ctx: typer.Context,
    thread: Annotated[str, typer.Argument(help="Thread id.")],
) -> None:
    from .main import attach_command as run

    run(ctx, thread=thread)
