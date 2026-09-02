"""Terminal chat command entry points."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from toolang.cli.common.context import ModelCatalogOption
from toolang.cli.common.agent_server import DEVELOPMENT_WHEEL_HELP


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
        typer.Option("--allow", help="Set COLLECTION=QUERY. Repeat by collection."),
    ] = None,
    limits: Annotated[
        list[str] | None,
        typer.Option(
            "--limit",
            help="Set FIELD=VALUE. Repeat for another field.",
        ),
    ] = None,
    model: Annotated[
        str | None,
        typer.Option(
            "--model",
            help="Set the model identity and parameters for this session.",
            metavar="MODEL_BODY",
        ),
    ] = None,
    runnable: Annotated[
        str | None,
        typer.Option(
            "--runnable",
            help="Set the runnable for this session.",
            metavar="RUNNABLE",
        ),
    ] = None,
    defaults: Annotated[
        list[str] | None,
        typer.Option("--default", hidden=True),
    ] = None,
) -> None:
    from .main import chat_command as run

    run(
        ctx,
        thread=thread,
        model_catalog=model_catalog,
        allows=allows,
        model=model,
        runnable=runnable,
        defaults=defaults,
        sandbox=sandbox,
        dev=dev,
        limits=limits,
    )
