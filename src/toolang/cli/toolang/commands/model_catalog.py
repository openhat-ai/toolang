"""Effective model catalog and provider inspection commands."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated, cast

from rich.text import Text
import typer
from typer._click.exceptions import ClickException

from toolang.base.types.model import Model, ModelCatalogSnapshot, ModelRoute, Provider
from toolang.cli.common.context import (
    ModelCatalogOption,
    context_agent,
    context_root,
    resolve_model_catalog_option,
)
from toolang.cli.common.output import echo_table
from toolang.cli.common.query import query_items
from toolang.common.errors import ToolangError
from toolang.common.layout import AgentLayout
from toolang.common.json import dumps
from toolang.plugin.models.collections import (
    MODEL_SCHEMA,
    CatalogProviderView,
    ModelQueryView,
    catalog_model_dataset,
    catalog_provider_views,
)
from toolang.setup import AgentSetup
from toolang.setup.watcher import load_setup


def models_command(
    ctx: typer.Context,
    model_catalog: ModelCatalogOption = None,
    all_: Annotated[
        bool,
        typer.Option("--all", help="Include unready and allow-excluded models"),
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
        setup = _setup(ctx, model_catalog=model_catalog)
    except TypeError as error:
        raise ClickException(str(error)) from error
    snapshot = setup.model_catalog(all=all_)
    dataset = catalog_model_dataset(snapshot)
    try:
        if query:
            MODEL_SCHEMA.parse(query)
        selected_views = cast(tuple[ModelQueryView, ...], query_items(dataset, query))
    except ToolangError as error:
        raise ClickException(str(error)) from error
    selected = tuple(cast(Model, item.record) for item in selected_views)
    if json_:
        content = dumps(snapshot.to_data(models=selected))
        typer.echo(content, nl=False)
        return
    headers, rows = dataset.table(selected_views)
    if all_:
        headers = (*headers, "ALLOWED", "REASON")
        rows = [
            (
                *row,
                "yes" if setup.model_allowed(model.ref) else "no",
                _route_reason(model._toolang.route),
            )
            for row, model in zip(rows, selected, strict=True)
        ]
    if not rows:
        typer.echo("No models matched query." if query else "No models found.")
        return
    echo_table(
        headers,
        rows,
        justify=(
            None,
            None,
            "right",
            "right",
            None,
            None,
            "right",
            *((None, None) if all_ else ()),
        ),
    )
    typer.echo()
    typer.echo(f" {_catalog_summary(snapshot, models=selected)}")


def providers_command(
    ctx: typer.Context,
    model_catalog: ModelCatalogOption = None,
    all_: Annotated[
        bool,
        typer.Option(
            "--all", help="Include unready, allow-excluded, and empty providers"
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
    available = {model.ref for model in snapshot.models if model._toolang.ready}
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
                }
            ),
            nl=False,
        )
        return
    headers = (
        "PROVIDER",
        "AVAILABLE MODELS",
        "ADAPTERS",
        "DEFAULT API",
        "ENV",
        "REASON",
    )
    rows = [
        (
            item.id,
            Text(
                f"{item.available_models}/{item.model_count}",
                style="red" if item.available_models == 0 else "",
            ),
            _provider_adapters_cell(item),
            _provider_api_cell(item, by_provider[item.id]),
            _provider_env_cell(item),
            _provider_reason(item.record, by_provider[item.id]),
        )
        for item in selected_views
    ]
    if all_:
        headers = (*headers[:-1], "ALLOWED MODELS", headers[-1])
        rows = [
            (
                *row[:-1],
                f"{sum(setup.model_allowed(model.ref) for model in by_provider[item.id])}/{item.model_count}",
                row[-1],
            )
            for row, item in zip(rows, selected_views, strict=True)
        ]
    if not rows:
        typer.echo("No providers found.")
        return
    echo_table(
        headers,
        rows,
    )
    typer.echo()
    typer.echo(f" {_provider_catalog_summary(snapshot, providers=providers)}")


def _layout(ctx: typer.Context) -> tuple[AgentLayout, bool]:
    agent = context_agent(ctx)
    return (
        AgentLayout.resident(context_root(ctx), agent or "default"),
        agent is not None,
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


def _catalog_summary(
    snapshot: ModelCatalogSnapshot,
    *,
    models: Sequence[Model],
) -> str:
    del snapshot
    model_noun = "model" if len(models) == 1 else "models"
    return f"{len(models)} {model_noun}"


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


def _route_reason(route: ModelRoute) -> str:
    return "; ".join(
        reason
        for missing, reason in (
            (route.adapter is None, "Adapter unresolved or not installed"),
            (route.api is None, "API missing or unresolved"),
            (route.env is None, "Environment requirements unmet"),
        )
        if missing
    )


def _provider_reason(provider: Provider, models: Sequence[Model]) -> str:
    if not models:
        return _route_reason(provider._toolang.route) or "No models"
    return "; ".join(
        sorted(
            {
                reason
                for model in models
                if (reason := _route_reason(model._toolang.route))
            }
        )
    )


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


def _provider_catalog_summary(
    snapshot: ModelCatalogSnapshot,
    *,
    providers: Sequence[Provider],
) -> str:
    del snapshot
    provider_noun = "provider" if len(providers) == 1 else "providers"
    return f"{len(providers)} {provider_noun}"
