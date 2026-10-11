"""Terminal chat command entry points."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from toolang.cli.common.agent_server import DEVELOPMENT_WHEEL_HELP
from toolang.cli.common.context import ModelCatalogOption
from toolang.cli.common.workspaces import (
    WorkspaceOptions,
    WorkdirOption,
    single_workdir,
)
from toolang.cli.common.parameters import (
    AllowOptions,
    CompactModelOption,
    DriverOption,
    DefaultOptions,
    LimitOptions,
    TextType,
    TitlePrefixOption,
)


def chat_command(
    ctx: typer.Context,
    thread: Annotated[
        str | None,
        typer.Option(
            "--thread",
            "-t",
            click_type=TextType(),
            help="Continue a thread instead of a new one",
            metavar="[THREAD]",
        ),
    ] = None,
    title_prefix: TitlePrefixOption = None,
    model_catalog: ModelCatalogOption = None,
    sandbox: Annotated[
        str | None,
        typer.Option(
            "--sandbox",
            metavar="SANDBOX",
            help="Execute the session in this sandbox",
        ),
    ] = None,
    allows: AllowOptions = None,
    limits: LimitOptions = None,
    defaults: DefaultOptions = None,
    compact_model: CompactModelOption = None,
    driver: DriverOption = None,
    workspace: WorkspaceOptions = None,
    workdir: WorkdirOption = None,
    dev: Annotated[
        Path | None,
        typer.Option("--dev", metavar="[PATH]", help=DEVELOPMENT_WHEEL_HELP),
    ] = None,
) -> None:
    from .main import chat_command as run

    run(
        ctx,
        thread=thread,
        title_prefix=title_prefix,
        model_catalog=model_catalog,
        allows=allows,
        defaults=defaults,
        compact_model=compact_model,
        driver=driver,
        sandbox=sandbox,
        dev=dev,
        limits=limits,
        workspace=workspace,
        workdir=single_workdir(workdir),
    )
