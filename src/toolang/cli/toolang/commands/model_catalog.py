"""Model resource queries and provider inspection."""

from __future__ import annotations
import asyncio
from collections.abc import Mapping, Sequence
from pathlib import Path
from toolang.cli.common.context import get_terminal_size
from typing import Annotated
import typer

from toolang.cli.common.agent_argument import AgentArgument
from wcwidth import wcswidth
from toolang.base.types.model import Provider
from toolang.cli.common.context import (
    prepare_agent,
    context_agent,
    context_root,
    ModelCatalogOption,
    resolve_model_catalog_option,
    user_call,
)
from toolang.cli.common.output import (
    MIN_TABLE_WIDTH,
    echo_collection_summary,
    echo_table,
)
from toolang.cli.common.records import check_output_options, echo_records
from toolang.cli.common.progress import make_cli_progress
from toolang.cli.common.terminal_title import clip_title, label_text, safe_text
from toolang.common.progress import ProgressSink
from toolang.common.layout import AgentLayout
from toolang.plugin.models.query import filter_models
from toolang.plugin.models.records import model_record, provider_record
from toolang.setup import AgentSetup
from toolang.setup.routes import provider_environment
from toolang.setup.watcher import load_setup


MODEL_COLUMNS = (
    "model",
    "context",
    "max_output",
    "price",
    "input",
    "output",
    "features",
    "status",
)
MODEL_DROP_ORDER = ("output", "max_output", "features", "input")
PROVIDER_COLUMNS = ("provider", "models", "api", "env")


def models_command(
    ctx: typer.Context,
    agent: Annotated[
        str | None,
        AgentArgument("source"),
    ] = None,
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
        bool, typer.Option("--json", help="Output models as JSON")
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
    records = [model_record(model) for model in selected]
    if json_:
        echo_records(records, MODEL_COLUMNS, json_=True)
        return
    _echo_models(
        records,
        width=max(MIN_TABLE_WIDTH, get_terminal_size(fallback=(120, 24)).columns),
    )
    echo_collection_summary(
        len(selected),
        "model",
        group=(len({model.provider for model in selected}), "provider"),
    )


def _echo_models(records: Sequence[Mapping[str, object]], *, width: int) -> None:
    """Omit optional columns in order while preserving complete required values."""

    if not records:
        return
    prices = [str(record.get("price") or "-").partition("/") for record in records]
    price_widths = [
        max((len(parts[index].strip()) for parts in prices if parts[1]), default=1)
        for index in (0, 2)
    ]
    formatted_prices = [
        f"{parts[0].strip():>{price_widths[0]}}/{parts[2].strip():>{price_widths[1]}}"
        if parts[1]
        else "-"
        for parts in prices
    ]

    def cell(record: Mapping[str, object], column: str, price: str) -> str:
        value = record.get({"model": "ref", "status": "tags"}.get(column, column))
        if column == "price":
            value = price
        elif column == "status":
            value = ",".join(
                tag.replace("_", " ")
                for tag in (value if isinstance(value, (list, tuple)) else ())
                if isinstance(tag, str)
                and tag in {"ready", "not_allowed", "no_env", "no_api", "no_adapter"}
            )
        elif column in {"context", "max_output"} and isinstance(value, int):
            value = f"{value:,}"
        elif isinstance(value, (list, tuple)):
            value = ",".join(str(item) for item in value)
        if value is None:
            return "-"
        return (
            safe_text(
                str(value).replace("\r", " ").replace("\n", " ").replace("\t", " ")
            )
            or "-"
        )

    rows = [
        tuple(cell(record, column, price) for column in MODEL_COLUMNS)
        for record, price in zip(records, formatted_prices)
    ]
    headers = [
        "PRICE ($/1M)" if column == "price" else column.upper()
        for column in MODEL_COLUMNS
    ]
    widths = [
        max(wcswidth(header), *(wcswidth(row[index]) for row in rows))
        for index, header in enumerate(headers)
    ]
    indices = list(range(len(MODEL_COLUMNS)))
    table_width = sum(widths) + 2 * (len(indices) - 1)
    for column in MODEL_DROP_ORDER:
        if table_width <= width:
            break
        index = MODEL_COLUMNS.index(column)
        indices.remove(index)
        table_width -= widths[index] + 2
    echo_table(
        [headers[index] for index in indices],
        [tuple(row[index] for index in indices) for row in rows],
        justify=tuple(
            "right"
            if MODEL_COLUMNS[index] in {"context", "price", "max_output"}
            else "left"
            for index in indices
        ),
        width=max(width, table_width),
    )


def providers_command(
    ctx: typer.Context,
    agent: Annotated[
        str | None,
        AgentArgument("source"),
    ] = None,
    model_catalog: ModelCatalogOption = None,
    all_: Annotated[
        bool,
        typer.Option(
            "--all", "-a", help="Include unready, allow-excluded, and empty providers"
        ),
    ] = False,
    json_: Annotated[
        bool, typer.Option("--json", help="Output providers as JSON")
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
    if json_:
        echo_records(records, PROVIDER_COLUMNS, json_=True)
        return
    _echo_providers(
        providers,
        width=max(MIN_TABLE_WIDTH, get_terminal_size(fallback=(120, 24)).columns),
    )
    echo_collection_summary(len(records), "provider")


def _echo_providers(providers: Sequence[Provider], *, width: int) -> None:
    """Keep identities and counts intact, shrinking API before environment rules."""

    if not providers:
        return
    rows: list[tuple[str, ...]] = []
    for provider in providers:
        env = provider_environment(provider)
        requirements = " OR ".join(
            alternative
            if isinstance(alternative, str)
            else "(" + " + ".join(alternative) + ")"
            for alternative in env
        )
        rows.append(
            (
                label_text(provider.id),
                f"{provider._toolang.ready_count}/{provider._toolang.model_count}",
                label_text(provider._toolang.route.api or "-"),
                label_text(requirements) or "-",
            )
        )
    columns = list(PROVIDER_COLUMNS)
    widths = [
        max(len(column), *(wcswidth(row[index]) for row in rows))
        for index, column in enumerate(columns)
    ]
    api_space = width - widths[0] - widths[1] - widths[3] - 6
    # Below a recognizable address, the whole API column yields to ENV.
    if api_space < min(widths[2], 24):
        columns.pop(2)
        widths.pop(2)
        rows = [(*row[:2], row[3]) for row in rows]
    else:
        widths[2] = min(widths[2], api_space)
    widths[-1] = max(
        len("ENV"),
        min(widths[-1], width - sum(widths[:-1]) - 2 * (len(columns) - 1)),
    )
    echo_table(
        tuple(column.upper() for column in columns),
        [
            tuple(clip_title(value, width=size) for value, size in zip(row, widths))
            for row in rows
        ],
        justify=tuple("right" if column == "models" else "left" for column in columns),
    )


def _layout(ctx: typer.Context) -> tuple[AgentLayout, bool]:
    agent = context_agent(ctx)
    return (
        (
            prepare_agent(ctx)
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
