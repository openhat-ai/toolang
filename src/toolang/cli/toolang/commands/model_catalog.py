"""Plural model catalog, provider, and adapter commands."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
import json
from pathlib import Path
from string import Template
from typing import Annotated, cast

from rich.text import Text
import typer
from typer._click.exceptions import ClickException

from toolang.base.protocols.model import ModelAdapter
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
from toolang.common.query import QueryDataset
from toolang.common.layout import AgentLayout
from toolang.plugin.loading import list_plugin_infos
from toolang.common.json import dumps
from toolang.plugin.models.collections import (
    MODEL_SCHEMA,
    CatalogProviderView,
    ModelQueryView,
    catalog_model_dataset,
    catalog_provider_views,
)
from toolang.plugin.models.provider_resolver import model_adapter
from toolang.plugin.models.discovery import (
    absent_provider_env_vars,
    provider_env_requirements,
    required_provider_env_vars,
)
from toolang.setup import AgentSetup
from toolang.setup.catalog import load_models_dev_snapshot
from toolang.setup.watcher import load_setup


def models_command(
    ctx: typer.Context,
    model_catalog: ModelCatalogOption = None,
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
    snapshot = _effective_snapshot(setup)
    dataset = _model_dataset(snapshot)
    try:
        if query:
            MODEL_SCHEMA.parse(query)
        selected_views = cast(tuple[ModelQueryView, ...], query_items(dataset, query))
    except ToolangError as error:
        raise ClickException(str(error)) from error
    selected = tuple(cast(Model, item.record) for item in selected_views)
    if json_:
        source = _source_snapshot(ctx, model_catalog=model_catalog)
        selected_refs = {view.key for view in selected_views}
        content = dumps(
            source.to_data(
                models=tuple(
                    model for model in source.models if model.identity in selected_refs
                )
            )
        )
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
        typer.Option("--json", help="Write catalog providers as JSON"),
    ] = False,
) -> None:
    """List catalog providers and runtime availability."""

    setup = _setup(ctx, model_catalog=model_catalog)
    snapshot = _effective_snapshot(setup)
    base_providers = tuple(
        snapshot.providers[provider_id] for provider_id in sorted(snapshot.providers)
    )
    available = {model.ref for model in snapshot.models if model._toolang.ready}
    selected_views = catalog_provider_views(
        base_providers,
        available=available,
        adapters={
            provider.id: _provider_adapters(provider) for provider in base_providers
        },
        apis={
            provider.id: _provider_api(
                provider,
                adapters=setup.adapters,
                environ=setup.envs,
            )
            for provider in base_providers
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
    providers = tuple(item.record for item in selected_views)
    if json_:
        typer.echo(
            dumps({provider.id: provider.to_data() for provider in providers}),
            nl=False,
        )
        return
    headers = ("PROVIDER", "AVAILABLE MODELS", "ADAPTERS", "API", "ENV")
    rows = [
        (
            item.id,
            Text(
                f"{item.available_models}/{item.model_count}",
                style="red" if item.available_models == 0 else "",
            ),
            _provider_adapters_cell(setup.adapters, item),
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
        typer.Option("--json", help="Write adapter metadata as JSON"),
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
        )
    )


def _source_snapshot(
    ctx: typer.Context,
    *,
    model_catalog: Path | None = None,
) -> ModelCatalogSnapshot:
    """Read the models.dev source that an export must reproduce."""

    layout, agent_context = _layout(ctx)
    return load_models_dev_snapshot(
        layout,
        model_catalog=resolve_model_catalog_option(model_catalog),
        agent_context=agent_context,
    )


def _effective_snapshot(setup: AgentSetup) -> ModelCatalogSnapshot:
    """Project one published setup version back into one catalog snapshot."""

    return ModelCatalogSnapshot(
        providers=dict(setup.providers),
        models=setup.models.entries,
        revision=setup.revision,
    )


def _model_dataset(snapshot: ModelCatalogSnapshot) -> QueryDataset[ModelQueryView]:
    """Build the queryable model rows of one published setup version."""

    available = {model.ref for model in snapshot.models if model._toolang.ready}
    adapter_by_identity = {
        model.identity: adapter
        for model in snapshot.models
        if model._toolang.provider in snapshot.providers
        for adapter in (
            model_adapter(snapshot.providers[model._toolang.provider], model),
        )
        if adapter is not None
    }
    return catalog_model_dataset(
        snapshot,
        available=available,
        adapters=adapter_by_identity,
    )


def _catalog_summary(
    snapshot: ModelCatalogSnapshot,
    *,
    models: Sequence[Model],
) -> str:
    del snapshot
    model_noun = "model" if len(models) == 1 else "models"
    return f"{len(models)} {model_noun}"


def _provider_api(
    provider: Provider,
    *,
    adapters: Mapping[str, ModelAdapter],
    environ: Mapping[str, str],
) -> str | None:
    """Return the effective provider base URL without model-level overrides."""

    adapter_name = provider._toolang.adapter
    adapter = adapters.get(adapter_name) if adapter_name is not None else None
    template = provider.api.strip() if provider.api and provider.api.strip() else None
    if template is None and adapter is not None:
        template = adapter.default_api
    if template is None:
        return None
    try:
        api = Template(template).substitute(environ).strip()
    except (KeyError, ValueError):
        return None
    return api or None


def _provider_adapters(provider: Provider) -> tuple[str, ...]:
    from toolang.plugin.models.provider_resolver import model_adapter

    adapters = {
        adapter
        for model in provider.models.values()
        for adapter in (model_adapter(provider, model),)
        if adapter
    }
    if not adapters and provider._toolang.adapter:
        adapters.add(provider._toolang.adapter)
    return tuple(sorted(adapters))


def _provider_adapters_cell(
    published: Mapping[str, ModelAdapter],
    provider: CatalogProviderView,
) -> Text:
    if not provider.adapters:
        return Text("-", style="dim")
    cell = Text()
    for index, adapter in enumerate(provider.adapters):
        if index:
            cell.append(",")
        cell.append(
            adapter,
            style="dim" if adapter not in published else None,
        )
    return cell


def _provider_api_cell(provider: CatalogProviderView) -> Text:
    api = provider.api
    unavailable = api is None
    return Text(api or "-", style="red" if unavailable else "")


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
            cell.append(name, style="red" if name in missing else None)
    return cell


def _provider_catalog_summary(
    snapshot: ModelCatalogSnapshot,
    *,
    providers: Sequence[Provider],
) -> str:
    del snapshot
    provider_noun = "provider" if len(providers) == 1 else "providers"
    return f"{len(providers)} {provider_noun}"
