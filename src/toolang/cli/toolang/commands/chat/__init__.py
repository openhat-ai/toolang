"""Terminal chat command entry points."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from toolang.cli.common.agent_server import DEVELOPMENT_WHEEL_HELP
from toolang.cli.common.context import ModelCatalogOption
from toolang.cli.common.parameters import (
    AllowOptions,
    CompactModelOption,
    DefaultOptions,
    LimitOptions,
    TextType,
)


def chat_command(
    ctx: typer.Context,
    thread: Annotated[
        str | None,
        typer.Option(
            "--thread",
            "-t",
            click_type=TextType(),
            help=(
                "Continue a thread (run id also accepted). Bare --thread resumes "
                "the most recently updated thread; omit to start a new one"
            ),
            metavar="[THREAD]",
        ),
    ] = None,
    model_catalog: ModelCatalogOption = None,
    sandbox: Annotated[
        str | None,
        typer.Option(
            "--sandbox",
            metavar="SANDBOX_SPEC",
            help="Execute the session in this sandbox",
        ),
    ] = None,
    dev: Annotated[
        Path | None,
        typer.Option("--dev", metavar="PATH", help=DEVELOPMENT_WHEEL_HELP),
    ] = None,
    allows: AllowOptions = None,
    limits: LimitOptions = None,
    defaults: DefaultOptions = None,
    compact_model: CompactModelOption = None,
) -> None:
    from .main import chat_command as run

    run(
        ctx,
        thread=thread,
        model_catalog=model_catalog,
        allows=allows,
        defaults=defaults,
        compact_model=compact_model,
        sandbox=sandbox,
        dev=dev,
        limits=limits,
    )
