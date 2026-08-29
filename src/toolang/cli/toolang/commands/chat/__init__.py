"""Terminal chat command entry points."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from toolang.cli.common.context import ModelCatalogOption
from toolang.cli.common.execution_runtime import DEVELOPMENT_WHEEL_HELP


def chat_command(
    ctx: typer.Context,
    thread: Annotated[
        str | None,
        typer.Argument(
            help="Thread id to continue. Run id also accepted. Omit to start a new one.",
            metavar="THREAD",
        ),
    ] = None,
    model_catalog: ModelCatalogOption = None,
    sandbox: Annotated[
        str | None,
        typer.Option(
            "--sandbox",
            help="Execute the session in this sandbox.",
        ),
    ] = None,
    dev: Annotated[
        Path | None,
        typer.Option(
            "--dev",
            help=DEVELOPMENT_WHEEL_HELP,
        ),
    ] = None,
    allows: Annotated[
        list[str] | None,
        typer.Option("--allow", help="Set DOMAIN=SELECTORS. Repeat by domain."),
    ] = None,
    limits: Annotated[
        list[str] | None,
        typer.Option(
            "--limit",
            help="Set FIELD=VALUE. Repeat for another field.",
        ),
    ] = None,
    defaults: Annotated[
        list[str] | None,
        typer.Option("--default", help="Set FIELD=VALUE. Repeat for another field."),
    ] = None,
) -> None:
    from .main import chat_command as run

    run(
        ctx,
        thread=thread,
        model_catalog=model_catalog,
        allows=allows,
        defaults=defaults,
        sandbox=sandbox,
        dev=dev,
        limits=limits,
    )
