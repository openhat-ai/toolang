"""Effective model catalog and provider inspection commands."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated

from rich.text import Text
import typer
from typer._click.exceptions import ClickException

from toolang.base.types.model import Model, ModelCatalogSnapshot, Provider
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
from toolang.common.errors import ToolangError
from toolang.common.layout import AgentLayout
from toolang.common.json import dumps
from toolang.plugin.models.collections import (
    CatalogProviderView,
    catalog_provider_views,
)
from toolang.plugin.models.query import filter_models, model_refs
from toolang.setup import AgentSetup
from toolang.setup.watcher import load_setup


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

    setup = _setup(ctx, model_catalog=model_catalog)
    models = setup.models() if all_ else setup.models_effective()
    providers = setup.providers() if all_ else setup.providers_effective()
    snapshot = ModelCatalogSnapshot(
        providers={provider.id: provider for provider in providers},
        models=tuple(models),
        revision=setup.revision,
    )
    try:
        selected_models = filter_models(models, query)
    except (ToolangError, ValueError) as error:
        raise ClickException(str(error)) from error
    if json_:
        typer.echo(dumps(snapshot.to_data(models=selected_models)), nl=False)
        return
    headers = (
        "MODEL",
        "CONTEXT",
        "OUTPUT",
        "INPUT",
        "CAPABILITIES",
        "PRICE ($/1M)",
    )
    rows = [_model_row(model) for model in selected_models]
    rows = _align_model_prices(rows)
    justify = (None, "right", "right", None, None, "right")
    if all_:
        headers = (*headers, "STATUS")
        rows = [
            (*row, _model_status(model))
            for row, model in zip(rows, selected_models, strict=True)
        ]
        justify = (*justify, None)
    if rows:
        echo_table(headers, rows, justify=justify)
    echo_collection_summary(
        len(selected_models),
        "model",
        group=(len({model._toolang.provider for model in selected_models}), "provider"),
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
    models = setup.models() if all_ else setup.models_effective()
    base_providers = setup.providers() if all_ else setup.providers_effective()
    by_provider: dict[str, list[Model]] = {
        provider.id: [] for provider in base_providers
    }
    for model in models:
        by_provider[model._toolang.provider].append(model)
    available = set(model_refs(setup.models_effective()))
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


def _model_row(model: Model) -> tuple[str, ...]:
    """Format the public model table without constructing a query dataset."""

    context = model.limit.get("context")
    output = model.limit.get("output")
    modalities = ",".join(model.modalities.get("input", ())) or "-"
    capabilities = (
        ",".join(
            name
            for name in ("tool_call", "reasoning", "temperature", "structured_output")
            if getattr(model, name) is True
        )
        or "-"
    )
    cost = model.cost or {}
    prices = " / ".join(_format_price(cost.get(name)) for name in ("input", "output"))
    return (
        model.ref,
        "-" if context is None else f"{context:_}",
        "-" if output is None else f"{output:_}",
        modalities,
        capabilities,
        prices,
    )


def _align_model_prices(rows: list[tuple[str, ...]]) -> list[tuple[str, ...]]:
    """Align the two public price columns as the former model dataset did."""

    if not rows:
        return rows
    pairs = [row[-1].split(" / ") for row in rows]
    widths = [max(len(pair[side]) for pair in pairs) for side in range(2)]
    return [
        (
            *row[:-1],
            " / ".join(
                value.rjust(width) for value, width in zip(pair, widths, strict=True)
            ),
        )
        for row, pair in zip(rows, pairs, strict=True)
    ]


def _format_price(value: object) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"model price must be numeric, got {value!r}")
    return f"{value:.2f}"


def _model_status(model: Model) -> str:
    route = model._toolang.route
    status = inspection_status(
        allowed=model._toolang.allowed,
        ready=model._toolang.routable,
    )
    reason = "; ".join(
        reason
        for missing, reason in (
            (route.adapter is None, "No adapter"),
            (route.api is None, "No API URL"),
            (route.env is None, "Missing env"),
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
