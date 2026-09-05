"""Plural model catalog, provider, and adapter commands."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
import json
from pathlib import Path
from typing import Annotated, cast

import click
from rich.text import Text
import typer

from toolang.base.types.model import Model, ModelCatalogSnapshot, Provider
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
from toolang.plugin.loading import list_plugin_infos
from toolang.plugin.models.catalog import (
    catalog_json_dumps,
)
from toolang.plugin.models.collections import (
    MODEL_SCHEMA,
    CatalogProviderView,
    ModelQueryView,
    catalog_provider_views,
)
from toolang.plugin.models.discovery import (
    absent_provider_env_vars,
    provider_env_requirements,
    required_provider_env_vars,
)
from toolang.setup.catalog import (
    CatalogInspection,
    load_catalog_inspection,
    load_matching_catalog_inspection,
)


def models_command(
    ctx: typer.Context,
    model_catalog: ModelCatalogOption = None,
    query: Annotated[
        list[str] | None,
        typer.Option(
            "--query",
            "-q",
            help="Query models. Repeat to add matches; see 'too query models'.",
        ),
    ] = None,
    json_: Annotated[
        bool,
        typer.Option("--json", help="Write filtered models as JSON."),
    ] = False,
) -> None:
    """List or export model catalog entries."""

    inspection = (
        _matching_inspection(ctx, model_catalog=model_catalog, query=query)
        if query and not json_
        else _inspection(ctx, model_catalog=model_catalog)
    )
    if inspection is None:
        typer.echo("No models matched query.")
        return
    snapshot = inspection.snapshot
    dataset = inspection.catalog_models
    selected_views = cast(tuple[ModelQueryView, ...], query_items(dataset, query))
    selected = tuple(cast(Model, item.record) for item in selected_views)
    if json_:
        exportable = tuple(model for model in selected if not model.local)
        if len(exportable) != len(selected):
            local = ", ".join(model.identity for model in selected if model.local)
            raise typer.BadParameter(
                f"local-only models cannot be exported: {local}",
                param_hint="--query",
            )
        content = catalog_json_dumps(snapshot.to_data(models=exportable))
        typer.echo(content, nl=False)
        return
    headers, rows = dataset.table(selected_views)
    if not rows:
        typer.echo("No models matched query." if query else "No models found.")
        return
    echo_table(
        headers,
        rows,
        justify=(None, None, "right", "right", None, None, "right"),
    )
    typer.echo()
    typer.echo(f" {_catalog_summary(snapshot, models=selected)}")


def providers_command(
    ctx: typer.Context,
    model_catalog: ModelCatalogOption = None,
    json_: Annotated[
        bool,
        typer.Option("--json", help="Write catalog providers as JSON."),
    ] = False,
) -> None:
    """List catalog providers and runtime availability."""

    inspection = _inspection(ctx, model_catalog=model_catalog)
    snapshot = inspection.snapshot
    base_providers = tuple(
        provider
        for provider_id, provider in sorted(snapshot.providers.items())
        if provider_id != "custom"
    )
    available = set(inspection.models.refs())
    selected_views = catalog_provider_views(
        base_providers,
        available=available,
        adapters={
            provider.id: _provider_adapters(provider) for provider in base_providers
        },
        apis={provider.id: _provider_api(provider) for provider in base_providers},
        env_requirements={
            provider.id: provider_env_requirements(provider)
            for provider in base_providers
        },
        required_env={
            provider.id: required_provider_env_vars(provider)
            for provider in base_providers
        },
        missing_env={
            provider.id: absent_provider_env_vars(provider, environ=inspection.envs)
            for provider in base_providers
        },
    )
    providers = tuple(item.record for item in selected_views)
    if json_:
        typer.echo(
            catalog_json_dumps(
                {provider.id: provider.to_data() for provider in providers}
            ),
            nl=False,
        )
        return
    headers = ("PROVIDER", "NAME", "AVAILABLE", "ADAPTERS", "API", "ENV")
    rows = [
        (
            item.id,
            item.name,
            f"{item.available_models}/{item.model_count}",
            _provider_adapters_cell(inspection, item),
            _provider_api_cell(item),
            _provider_env_cell(item),
        )
        for item in selected_views
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


def adapters_command(
    json_: Annotated[
        bool,
        typer.Option("--json", help="Write adapter metadata as JSON."),
    ] = False,
) -> None:
    """List installed protocol adapters."""

    infos = tuple(list_plugin_infos(group="toolang.model_adapter"))
    if json_:
        typer.echo(
            json.dumps(
                [{"id": info.name, "source": info.source} for info in infos],
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        return
    if not infos:
        typer.echo("No adapters found.")
        return
    echo_table(
        ("ADAPTER", "SOURCE"),
        tuple((info.name, info.source) for info in infos),
    )


def _inspection(
    ctx: typer.Context,
    *,
    model_catalog: Path | None = None,
) -> CatalogInspection:
    agent = context_agent(ctx)
    return asyncio.run(
        load_catalog_inspection(
            AgentLayout.resident(context_root(ctx), agent or "default"),
            model_catalog=resolve_model_catalog_option(model_catalog),
            agent_context=agent is not None,
        )
    )


def _matching_inspection(
    ctx: typer.Context,
    *,
    model_catalog: Path | None,
    query: Sequence[str],
) -> CatalogInspection | None:
    agent = context_agent(ctx)
    try:
        queries = MODEL_SCHEMA.parse(query)
    except ToolangError as error:
        raise click.ClickException(str(error)) from error
    return asyncio.run(
        load_matching_catalog_inspection(
            AgentLayout.resident(context_root(ctx), agent or "default"),
            model_catalog=resolve_model_catalog_option(model_catalog),
            agent_context=agent is not None,
            queries=queries,
        )
    )


def _catalog_summary(
    snapshot: ModelCatalogSnapshot,
    *,
    models: Sequence[Model],
) -> str:
    catalogs = _catalog_names(snapshot)
    parts = [
        f"{catalog} {sum(model.catalog == catalog for model in models)}"
        for catalog in catalogs
    ]
    model_noun = "model" if len(models) == 1 else "models"
    catalog_noun = "catalog" if len(parts) == 1 else "catalogs"
    return f"{len(models)} {model_noun} from {len(parts)} {catalog_noun}: " + ", ".join(
        parts
    )


def _provider_api(provider: Provider) -> str | None:
    return provider.resolved.api if provider.resolved is not None else None


def _provider_adapters(provider: Provider) -> tuple[str, ...]:
    adapters = {
        model.resolved.adapter
        for model in provider.models.values()
        if model.resolved is not None and model.resolved.adapter
    }
    if not adapters and provider.resolved is not None and provider.resolved.adapter:
        adapters.add(provider.resolved.adapter)
    return tuple(sorted(adapters))


def _provider_adapters_cell(
    inspection: CatalogInspection,
    provider: CatalogProviderView,
) -> Text:
    adapters = provider.adapters
    if not adapters:
        return Text("-", style="dim")
    cell = Text()
    for index, adapter in enumerate(adapters):
        if index:
            cell.append(",")
        cell.append(
            adapter,
            style="dim" if adapter not in inspection.adapters else None,
        )
    return cell


def _provider_api_cell(provider: CatalogProviderView) -> Text:
    api = provider.api
    unavailable = api is None or (provider.local and provider.offline)
    return Text(api or "-", style="dim" if unavailable else "")


def _provider_env_cell(provider: CatalogProviderView) -> Text:
    if not provider.env_requirements:
        return Text("-")
    missing = set(provider.missing_env)
    cell = Text()
    for index, requirement in enumerate(provider.env_requirements):
        if index:
            cell.append(", ")
        names = requirement.split(" + ")
        for group_index, name in enumerate(names):
            if group_index:
                cell.append(" + ")
            cell.append(name, style="dim" if name in missing else None)
    return cell


def _provider_catalog_summary(
    snapshot: ModelCatalogSnapshot,
    *,
    providers: Sequence[Provider],
) -> str:
    parts = [
        f"{catalog} {sum(provider.catalog == catalog for provider in providers)}"
        for catalog in _catalog_names(snapshot)
    ]
    provider_noun = "provider" if len(providers) == 1 else "providers"
    catalog_noun = "catalog" if len(parts) == 1 else "catalogs"
    return (
        f"{len(providers)} {provider_noun} from {len(parts)} {catalog_noun}: "
        + ", ".join(parts)
    )


def _catalog_names(snapshot: ModelCatalogSnapshot) -> tuple[str, ...]:
    names = tuple(
        dict.fromkeys(
            provider.catalog
            for provider in snapshot.providers.values()
            if provider.catalog is not None
        )
    )
    priority = {"models.dev": 0, "ollama": 1, "llama_cpp": 2}
    return tuple(sorted(names, key=lambda name: (priority.get(name, 3), name)))
