"""Plural model catalog, provider, and adapter commands."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
import json
from pathlib import Path
from typing import Annotated, cast

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
from toolang.cli.common.query import emit_query_discovery, query_items
from toolang.cli.config import load_config_layers
from toolang.common.layout import AgentLayout
from toolang.plugin.loading import list_plugin_infos
from toolang.plugin.models.catalog import (
    catalog_json_dumps,
)
from toolang.plugin.models.collections import (
    ADAPTER_DEFINITION,
    CATALOG_MODEL_SCHEMA,
    CATALOG_PROVIDER_SCHEMA,
    CatalogModelView,
    CatalogProviderView,
    catalog_model_dataset,
    catalog_provider_dataset,
    plugin_inventory_dataset,
)
from toolang.plugin.models.config import ProviderConfig, parse_model_aliases
from toolang.plugin.models.discovery import (
    absent_provider_env_vars,
    provider_env_requirements,
    required_provider_env_vars,
)
from toolang.plugin.models.resolution import ModelTargetResolver
from toolang.setup import AgentSetup, SetupWatcher


def models_command(
    ctx: typer.Context,
    model_catalog: ModelCatalogOption = None,
    query: Annotated[
        list[str] | None,
        typer.Option(
            "--query",
            "-q",
            help="Query models. Repeat values to add alternatives.",
        ),
    ] = None,
    query_help: Annotated[
        bool,
        typer.Option("--query-help", help="Show model query fields and operators."),
    ] = False,
    query_schema: Annotated[
        bool,
        typer.Option("--query-schema", help="Write the model query schema as JSON."),
    ] = False,
    json_: Annotated[
        bool,
        typer.Option("--json", help="Write a valid filtered models.json to stdout."),
    ] = False,
) -> None:
    """List or export model catalog entries."""

    if emit_query_discovery(
        CATALOG_MODEL_SCHEMA,
        query_help=query_help,
        query_schema=query_schema,
    ):
        return
    setup = _setup(ctx, model_catalog=model_catalog)
    snapshot = _catalog(setup)
    available = _available_identities(ctx, setup)
    adapters = _adapter_by_identity(setup)
    dataset = catalog_model_dataset(
        snapshot,
        available=available,
        adapters=adapters,
    )
    selected_views = cast(tuple[CatalogModelView, ...], query_items(dataset, query))
    selected = tuple(item.record for item in selected_views)
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
    displayed_views = tuple(
        item
        for item in selected_views
        if item.scope != "local" or item.key in available
    )
    displayed = tuple(item.record for item in displayed_views)
    headers, rows = dataset.table(displayed_views)
    if not rows:
        typer.echo("No models matched query." if query else "No models found.")
        return
    echo_table(
        headers,
        rows,
        justify=(None, None, "right", "right", None, None, "right"),
    )
    typer.echo()
    typer.echo(f" {_catalog_summary(snapshot, models=displayed)}")


def providers_command(
    ctx: typer.Context,
    model_catalog: ModelCatalogOption = None,
    query: Annotated[
        list[str] | None,
        typer.Option(
            "--query",
            "-q",
            help="Query providers. Repeat values to add alternatives.",
        ),
    ] = None,
    query_help: Annotated[
        bool,
        typer.Option("--query-help", help="Show provider query fields and operators."),
    ] = False,
    query_schema: Annotated[
        bool,
        typer.Option("--query-schema", help="Write the provider query schema as JSON."),
    ] = False,
    json_: Annotated[
        bool,
        typer.Option("--json", help="Write catalog providers as JSON."),
    ] = False,
) -> None:
    """List catalog providers and runtime availability."""

    if emit_query_discovery(
        CATALOG_PROVIDER_SCHEMA,
        query_help=query_help,
        query_schema=query_schema,
    ):
        return
    setup = _setup(ctx, model_catalog=model_catalog)
    snapshot = _catalog(setup)
    base_providers = tuple(
        provider
        for provider_id, provider in sorted(snapshot.providers.items())
        if provider_id != "custom"
    )
    available = _available_identities(ctx, setup)
    dataset = catalog_provider_dataset(
        base_providers,
        available=available,
        adapters={
            provider.id: _provider_adapters(provider) for provider in base_providers
        },
        apis={
            provider.id: _provider_api(setup, provider) for provider in base_providers
        },
        env_requirements={
            provider.id: provider_env_requirements(provider)
            for provider in base_providers
        },
        required_env={
            provider.id: required_provider_env_vars(provider)
            for provider in base_providers
        },
        missing_env={
            provider.id: absent_provider_env_vars(provider, environ=setup.envs)
            for provider in base_providers
        },
    )
    selected_views = cast(
        tuple[CatalogProviderView, ...],
        query_items(dataset, query),
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
    headers, base_rows = dataset.table(selected_views)
    adapters_index = headers.index("ADAPTERS")
    api_index = headers.index("API")
    env_index = headers.index("ENV")
    rows: list[tuple[str | Text, ...]] = []
    for item, base_row in zip(selected_views, base_rows, strict=True):
        row: list[str | Text] = list(base_row)
        row[adapters_index] = _provider_adapters_cell(setup, item)
        row[api_index] = _provider_api_cell(item)
        row[env_index] = _provider_env_cell(item)
        rows.append(tuple(row))
    if not rows:
        typer.echo("No providers matched query." if query else "No providers found.")
        return
    echo_table(
        headers,
        rows,
    )
    typer.echo()
    typer.echo(f" {_provider_catalog_summary(snapshot, providers=providers)}")


def adapters_command(
    query: Annotated[
        list[str] | None,
        typer.Option(
            "--query",
            "-q",
            help="Query adapters. Repeat values to add alternatives.",
        ),
    ] = None,
    query_help: Annotated[
        bool,
        typer.Option("--query-help", help="Show adapter query fields and operators."),
    ] = False,
    query_schema: Annotated[
        bool,
        typer.Option("--query-schema", help="Write adapter query schema as JSON."),
    ] = False,
    json_: Annotated[
        bool,
        typer.Option("--json", help="Write adapter metadata as JSON."),
    ] = False,
) -> None:
    """List installed protocol adapters."""

    if emit_query_discovery(
        ADAPTER_DEFINITION.schema,
        query_help=query_help,
        query_schema=query_schema,
    ):
        return
    base_infos = tuple(list_plugin_infos(group="toolang.model_adapter"))
    dataset = plugin_inventory_dataset(
        ADAPTER_DEFINITION,
        tuple((info.name, info.source) for info in base_infos),
    )
    infos = query_items(dataset, query)
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
        typer.echo("No adapters matched query." if query else "No adapters found.")
        return
    headers, rows = dataset.table(infos)
    echo_table(headers, rows)


def _setup(
    ctx: typer.Context,
    *,
    model_catalog: Path | None = None,
) -> AgentSetup:
    return asyncio.run(
        SetupWatcher(
            _layout(ctx),
            model_catalog=resolve_model_catalog_option(model_catalog),
        ).refresh()
    )


def _layout(ctx: typer.Context) -> AgentLayout:
    return AgentLayout.resident(context_root(ctx), context_agent(ctx) or "default")


def _catalog(setup: AgentSetup):
    if setup.catalog is None:
        raise RuntimeError("setup has no model catalog")
    return setup.catalog


def _available_identities(ctx: typer.Context, setup: AgentSetup) -> set[str]:
    layers = load_config_layers(setup.layout.root, context_agent(ctx) or "")
    targets = ModelTargetResolver(
        providers=setup.providers,
        models=setup.models,
        model_aliases=parse_model_aliases(layers),
        default_models=(),
        envs=setup.envs,
        provider_configs=cast(
            Mapping[str, ProviderConfig],
            setup.provider_configs,
        ),
    ).selectable()
    candidates = {target.ref for _query, target in targets}
    snapshot = _catalog(setup)
    return {
        model.identity
        for model in snapshot.models
        if model.identity in candidates and not _model_missing_reasons(setup, model)
    }


def _model_missing_reasons(setup: AgentSetup, model: Model) -> tuple[str, ...]:
    provider = setup.providers.get(model.provider_id)
    if provider is None or not isinstance(provider, Provider):
        return ("provider",)
    resolved = model.resolved
    return () if resolved is not None and resolved.ready else ("route",)


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


def _adapter_by_identity(setup: AgentSetup) -> dict[str, str]:
    return {info.ref: info.adapter for info in setup.models}


def _provider_api(setup: AgentSetup, provider: Provider) -> str | None:
    del setup
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
    setup: AgentSetup,
    provider: CatalogProviderView,
) -> Text:
    adapters = provider.adapters
    if not adapters:
        return Text("-", style="dim")
    cell = Text()
    for index, adapter in enumerate(adapters):
        if index:
            cell.append(",")
        cell.append(adapter, style="dim" if adapter not in setup.adapters else None)
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
