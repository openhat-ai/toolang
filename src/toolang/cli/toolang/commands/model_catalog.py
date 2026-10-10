"""Model resource queries and provider inspection."""

from __future__ import annotations
import asyncio
from pathlib import Path
from typing import Annotated
import typer
from toolang.cli.common.context import (
    context_layout,
    context_agent,
    context_root,
    ModelCatalogOption,
    resolve_model_catalog_option,
    user_call,
)
from toolang.cli.common.output import echo_collection_summary
from toolang.cli.common.records import check_output_options, echo_records
from toolang.cli.common.progress import make_cli_progress
from toolang.common.progress import ProgressSink
from toolang.common.layout import AgentLayout
from toolang.plugin.models.query import filter_models
from toolang.plugin.models.records import model_record, provider_record
from toolang.setup import AgentSetup
from toolang.setup.watcher import load_setup


MODEL_COLUMNS = (
    "ref",
    "context",
    "max_output",
    "price",
    "input",
    "output",
    "features",
    "tags",
)
PROVIDER_COLUMNS = ("id", "models", "adapter", "api", "env")


def models_command(
    ctx: typer.Context,
    model_catalog: ModelCatalogOption = None,
    all_: Annotated[
        bool,
        typer.Option("--all", "-a", help="Include unready and allow-excluded models"),
    ] = False,
    query: Annotated[
        list[str] | None,
        typer.Option(
            "--query",
            "-q",
            metavar="QUERY",
            help="TQ query; repeat for union. See --json for fields. Costs are per million tokens",
        ),
    ] = None,
    json_: Annotated[
        bool, typer.Option("--json", help="Write public model records as JSON")
    ] = False,
    human: Annotated[
        bool, typer.Option("--human", help="Display a table (default)")
    ] = False,
) -> None:
    check_output_options(human=human, json_=json_)
    with make_cli_progress() as progress:
        setup = _setup(ctx, model_catalog=model_catalog, progress=progress.sink)
        models = (
            setup.models(progress=progress.sink)
            if all_
            else setup.models_effective(progress=progress.sink)
        )
    selected = user_call(filter_models, models, query)
    echo_records(
        [model_record(model) for model in selected],
        MODEL_COLUMNS,
        json_=json_,
        align_ref_continuations=True,
        right_align=("max_output", "price", "output"),
    )
    if not json_:
        echo_collection_summary(
            len(selected),
            "model",
            group=(len({model.provider for model in selected}), "provider"),
        )


def providers_command(
    ctx: typer.Context,
    model_catalog: ModelCatalogOption = None,
    all_: Annotated[
        bool,
        typer.Option(
            "--all", "-a", help="Include unready, allow-excluded, and empty providers"
        ),
    ] = False,
    json_: Annotated[
        bool, typer.Option("--json", help="Write public provider records as JSON")
    ] = False,
    human: Annotated[
        bool, typer.Option("--human", help="Display a table (default)")
    ] = False,
) -> None:
    check_output_options(human=human, json_=json_)
    with make_cli_progress() as progress:
        setup = _setup(ctx, model_catalog=model_catalog, progress=progress.sink)
        providers = (
            setup.providers(progress=progress.sink)
            if all_
            else setup.providers_effective(progress=progress.sink)
        )
    records = [provider_record(provider) for provider in providers]
    echo_records(records, PROVIDER_COLUMNS, json_=json_)
    if not json_:
        echo_collection_summary(len(records), "provider")


def _layout(ctx: typer.Context) -> tuple[AgentLayout, bool]:
    agent = context_agent(ctx)
    return (
        (
            context_layout(ctx)
            if agent is not None
            else AgentLayout.resident(context_root(ctx), "default")
        ),
        agent is not None,
    )


def _setup(
    ctx: typer.Context,
    *,
    model_catalog: Path | None = None,
    progress: ProgressSink | None = None,
) -> AgentSetup:
    """Build one setup version for the catalog commands."""

    layout, agent_context = _layout(ctx)
    return asyncio.run(
        load_setup(
            layout,
            model_catalog=resolve_model_catalog_option(model_catalog),
            agent_context=agent_context,
            validate_defaults=False,
            progress=progress,
        )
    )
