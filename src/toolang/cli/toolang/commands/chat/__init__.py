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
    allows: Annotated[
        list[str] | None,
        typer.Option("--allow", help="Set DOMAIN=SELECTORS. Repeat by domain."),
    ] = None,
    defaults: Annotated[
        list[str] | None,
        typer.Option("--default", help="Set FIELD=VALUE. Repeat for another field."),
    ] = None,
    sandbox: Annotated[
        str | None,
        typer.Option(
            "--sandbox",
            help="Execute the session in this sandbox.",
        ),
    ] = None,
    limits: Annotated[
        list[str] | None,
        typer.Option(
            "--limit",
            help="Set FIELD=VALUE. Repeat for another field.",
        ),
    ] = None,
) -> None:
    from .main import chat_command as run

    run(
        ctx,
        thread=thread,
        allows=allows,
        defaults=defaults,
        sandbox=sandbox,
        limits=limits,
    )
