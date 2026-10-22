"""Terminal chat command entry points."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from typer._click.exceptions import ClickException

from toolang.cli.common.agent_argument import AgentArgument

from toolang.cli.common.parameters import DEVELOPMENT_WHEEL_HELP
from toolang.cli.common.context import (
    ModelCatalogOption,
    cli_context,
    context_layout,
    select_agent_instance,
    user_call,
)
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
)


def chat_command(
    ctx: typer.Context,
    agent: Annotated[str, AgentArgument("execute")],
    thread: Annotated[
        str | None,
        typer.Option(
            "--thread",
            "-t",
            click_type=TextType(),
            help="Continue a thread by ID",
            metavar="[THREAD]",
        ),
    ] = None,
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

    selected_workdir = single_workdir(workdir)
    owned = _prepare_roaming_chat(ctx, thread)
    try:
        run(
            ctx,
            thread=thread,
            model_catalog=model_catalog,
            allows=allows,
            defaults=defaults,
            compact_model=compact_model,
            driver=driver,
            sandbox=sandbox,
            dev=dev,
            limits=limits,
            workspace=workspace,
            workdir=selected_workdir,
        )
    finally:
        if owned:
            from toolang.up.workload import finish_preparation

            layout = cli_context(ctx).layout
            assert layout is not None
            finish_preparation(layout, "Chat ended before runtime startup")


def _prepare_roaming_chat(ctx: typer.Context, thread: str | None) -> bool:
    """Allocate or continue a roaming owner after native parsing has completed."""
    state = cli_context(ctx)
    if state.address is not None:
        context_layout(ctx)
        return False
    if state.source is None or state.prepared or state.instance_selected:
        return False
    from toolang.up.identity import allocate_roaming
    from toolang.up.source import materialize_roaming_program

    if thread is not None:
        state.layout = select_agent_instance(ctx)
        state.prepared = True
        return False
    if not state.source.is_file():
        raise ClickException(f"agent program not found: {state.source}")
    state.layout = user_call(allocate_roaming, state.source, toolang_root=state.root)
    try:
        user_call(materialize_roaming_program, state.source, layout=state.layout)
    except BaseException:
        from toolang.up.workload import finish_preparation

        finish_preparation(state.layout, "Chat source preparation failed")
        raise
    state.prepared = True
    return True
