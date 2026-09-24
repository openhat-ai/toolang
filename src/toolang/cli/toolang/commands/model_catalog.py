"""Effective model catalog and provider inspection commands."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated, cast

from rich.text import Text
import typer
from typer._click.exceptions import ClickException

from toolang.base.types.model import Model, Provider
from toolang.cli.common.context import (
    ModelCatalogOption,
    context_agent,
    context_root,
    resolve_model_catalog_option,
)
from toolang.cli.common.output import (
    echo_collection_summary,
    echo_table,
    inspection_status,
)
from toolang.cli.common.query import query_items
from toolang.common.errors import ToolangError
from toolang.common.layout import AgentLayout
from toolang.common.json import dumps
from toolang.plugin.models.collections import (
    MODEL_SCHEMA,
    CatalogProviderView,
    catalog_provider_views,
)
from toolang.setup import AgentSetup
from toolang.setup.watcher import SetupWatcher, load_setup
from toolang.setup.model_listing import ModelListing
from toolang.setup.records import ModelRecord


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
            help="Query models. Repeat to add matches; see 'too query models'",
        ),
    ] = None,
    json_: Annotated[
        bool,
        typer.Option("--json", help="Write filtered models as JSON"),
    ] = False,
) -> None:
    """List or export model catalog entries."""

    try:
        listing = _listing(ctx, model_catalog=model_catalog)
    except TypeError as error:
        raise ClickException(str(error)) from error
    dataset = listing.all if all_ else listing.default
    try:
        if query:
            MODEL_SCHEMA.parse(query)
        selected = cast(tuple[ModelRecord, ...], query_items(dataset, query))
    except ToolangError as error:
        raise ClickException(str(error)) from error
    if json_:
        content = dumps(listing.export(selected), sort_keys=False)
        typer.echo(content, nl=False)
        return
    headers, raw_rows = dataset.table(selected)
    # Readiness remains queryable; the table groups it with policy in STATUS.
    headers = (headers[0], *headers[2:])
    rows = [(row[0], *row[2:]) for row in raw_rows]
    justify = (None, "right", "right", None, None, "right")
    if all_:
        headers = (*headers, "STATUS")
        rows = [
            (
                *row,
                _model_status(model),
            )
            for row, model in zip(rows, selected, strict=True)
        ]
        justify = (*justify, None)
    if rows:
        echo_table(headers, rows, justify=justify)
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
        bool,
        typer.Option("--json", help="Write catalog providers as JSON"),
    ] = False,
) -> None:
    """List catalog providers and runtime availability."""

    setup = _setup(ctx, model_catalog=model_catalog)
    snapshot = setup.model_catalog(all=all_)
    base_providers = tuple(
        snapshot.providers[provider_id] for provider_id in sorted(snapshot.providers)
    )
    by_provider: dict[str, list[Model]] = {
        provider.id: [] for provider in base_providers
    }
    for model in snapshot.models:
        by_provider[model._toolang.provider].append(model)
    available = set(setup.models.refs())
    selected_views = catalog_provider_views(
        base_providers,
        models=by_provider,
        available=available,
        adapters={
            provider.id: _provider_adapters(provider, by_provider[provider.id])
            for provider in base_providers
        },
        apis={provider.id: provider._toolang.route.api for provider in base_providers},
        env_requirements={
            provider.id: _provider_env_declarations(provider)
            for provider in base_providers
        },
    )
    providers = tuple(item.record for item in selected_views)
    if json_:
        typer.echo(
            dumps(
                {
                    provider.id: provider.to_data(
                        models={model.id: model for model in by_provider[provider.id]}
                    )
                    for provider in providers
                },
                sort_keys=False,
            ),
            nl=False,
        )
        return
    headers = (
        "PROVIDER",
        "MODELS",
        "ADAPTERS",
        "DEFAULT API",
        "ENV",
    )
    rows = [
        (
            item.id,
            Text(
                f"{item.available_models}/{item.model_count}"
                if all_
                else str(item.available_models),
                style="red" if item.available_models == 0 else "",
            ),
            _provider_adapters_cell(item),
            _provider_api_cell(item, by_provider[item.id]),
            _provider_env_cell(item),
        )
        for item in selected_views
    ]
    if rows:
        echo_table(headers, rows)
    echo_collection_summary(len(providers), "provider")


def _layout(ctx: typer.Context) -> tuple[AgentLayout, bool]:
    agent = context_agent(ctx)
    return (
        AgentLayout.resident(context_root(ctx), agent or "default"),
        agent is not None,
    )


def _listing(ctx: typer.Context, *, model_catalog: Path | None = None) -> ModelListing:
    """Load the complete cached catalog in the selected inspection scope."""

    layout, agent_context = _layout(ctx)
    return asyncio.run(
        SetupWatcher(
            layout,
            model_catalog=resolve_model_catalog_option(model_catalog),
            agent_context=agent_context,
            validate_defaults=False,
        ).load_catalog_listing()
    )


def _setup(ctx: typer.Context, *, model_catalog: Path | None = None) -> AgentSetup:
    """Build one setup version for the catalog commands."""

    layout, agent_context = _layout(ctx)
    return asyncio.run(
        load_setup(
            layout,
            model_catalog=resolve_model_catalog_option(model_catalog),
            agent_context=agent_context,
            validate_defaults=False,
        )
    )


def _provider_adapters(provider: Provider, models: Sequence[Model]) -> tuple[str, ...]:
    adapters = {
        model._toolang.route.adapter
        for model in models
        if model._toolang.route.adapter is not None
    }
    if not models and provider._toolang.route.adapter is not None:
        adapters.add(provider._toolang.route.adapter)
    return tuple(sorted(adapters))


def _provider_env_declarations(provider: Provider) -> tuple[str, ...]:
    rule = provider._toolang.route.env
    if rule is None:
        rule = provider._toolang.env or provider.env
    return tuple(item if isinstance(item, str) else " + ".join(item) for item in rule)


def _model_status(model: ModelRecord) -> str:
    status = inspection_status(
        allowed=model.allowed_order is not None, ready=model.ready
    )
    reason = "; ".join(
        reason
        for missing, reason in (
            (model.adapter is None, "No adapter"),
            (not model.api_present, "No API URL"),
            (not model.env_present, "Missing env"),
        )
        if missing
    )
    return f"{status} ({reason})" if reason else status


def _provider_adapters_cell(provider: CatalogProviderView) -> Text:
    return (
        Text(",".join(provider.adapters))
        if provider.adapters
        else Text("-", style="dim")
    )


def _provider_api_cell(provider: CatalogProviderView, models: Sequence[Model]) -> Text:
    api = provider.api
    unavailable = api is None
    overridden = any(model._toolang.route.api != api for model in models)
    label = (api or "-") + (" (model overrides)" if overridden else "")
    return Text(label, style="red" if unavailable else "")


def _provider_env_cell(provider: CatalogProviderView) -> Text:
    if not provider.env_requirements:
        return Text("-")
    cell = Text()
    for index, requirement in enumerate(provider.env_requirements):
        if index:
            cell.append(", ")
        cell.append(
            requirement,
            style="red" if provider.record._toolang.route.env is None else "",
        )
    return cell
