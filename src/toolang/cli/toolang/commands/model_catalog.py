"""Plural model catalog, provider, and adapter commands."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from decimal import Decimal
import json
from typing import Annotated, cast

from rich.text import Text
import typer

from toolang.base.types.model import Model, ModelCatalogSnapshot, Provider
from toolang.cli.common.context import (
    context_agent,
    context_model_catalog,
    context_root,
)
from toolang.cli.common.output import echo_table
from toolang.cli.config import load_config_layers
from toolang.common.layout import AgentLayout
from toolang.common.selectors import split_selector_list
from toolang.plugin.loading import list_plugin_infos
from toolang.plugin.models.catalog import (
    catalog_json_dumps,
    filter_catalog_models,
)
from toolang.plugin.models.config import ProviderConfig, parse_model_aliases
from toolang.plugin.models.resolution import ModelTargetResolver
from toolang.setup import AgentSetup, SetupWatcher


def models_command(
    ctx: typer.Context,
    filter_: Annotated[
        list[str] | None,
        typer.Option(
            "--filter",
            "-f",
            help="Filter models with selector-list syntax. Pass CSV or repeat.",
        ),
    ] = None,
    json_: Annotated[
        bool,
        typer.Option("--json", help="Write a valid filtered models.json to stdout."),
    ] = False,
) -> None:
    """List or export model catalog entries."""

    setup = _setup(ctx)
    snapshot = _catalog(setup)
    selectors = _selectors(filter_)
    available = _available_identities(ctx, setup)
    adapters = _adapter_by_identity(setup)
    selected = filter_catalog_models(
        snapshot,
        selectors,
        available=available,
        adapters=adapters,
    )
    if json_:
        exportable = tuple(model for model in selected if not model.local)
        if len(exportable) != len(selected):
            local = ", ".join(model.identity for model in selected if model.local)
            raise typer.BadParameter(
                f"local-only models cannot be exported: {local}",
                param_hint="--filter",
            )
        content = catalog_json_dumps(snapshot.to_data(models=exportable))
        typer.echo(content, nl=False)
        return
    displayed = tuple(
        model for model in selected if not model.local or model.identity in available
    )
    rows = [
        (
            model.identity,
            "yes" if model.identity in available else "no",
            *_model_table_fields(model),
        )
        for model in displayed
    ]
    if not rows:
        typer.echo("No matched models.")
        return
    echo_table(
        (
            "MODEL",
            "AVAILABLE",
            "CONTEXT",
            "OUTPUT",
            "INPUT",
            "CAPABILITY",
            "PRICE ($/1M)",
        ),
        rows,
        justify=(None, None, "right", "right", None, None, "right"),
    )
    typer.echo()
    typer.echo(f" {_catalog_summary(snapshot, models=displayed)}")


def providers_command(
    ctx: typer.Context,
    filter_: Annotated[
        list[str] | None,
        typer.Option("--filter", "-f", help="Filter provider IDs with globs."),
    ] = None,
    json_: Annotated[
        bool,
        typer.Option("--json", help="Write catalog providers as JSON."),
    ] = False,
) -> None:
    """List catalog providers and runtime availability."""

    from fnmatch import fnmatchcase

    setup = _setup(ctx)
    snapshot = _catalog(setup)
    patterns = _selectors(filter_) or ("*",)
    providers = tuple(
        provider
        for provider_id, provider in sorted(snapshot.providers.items())
        if provider_id != "custom"
        and any(fnmatchcase(provider_id, pattern) for pattern in patterns)
    )
    available = _available_identities(ctx, setup)
    if json_:
        typer.echo(
            catalog_json_dumps(
                {provider.id: provider.to_data() for provider in providers}
            ),
            nl=False,
        )
        return
    rows = [
        (
            provider.id,
            provider.name,
            _provider_availability(provider, available=available),
            _provider_adapters_cell(setup, provider),
            _provider_api_cell(setup, provider),
            _provider_env_cell(setup, provider),
        )
        for provider in providers
    ]
    echo_table(
        ("PROVIDER", "NAME", "AVAILABLE", "ADAPTERS", "API", "ENV"),
        rows,
    )
    typer.echo()
    typer.echo(f" {_provider_catalog_summary(snapshot, providers=providers)}")


def adapters_command(
    filter_: Annotated[
        list[str] | None,
        typer.Option("--filter", "-f", help="Filter adapter IDs with globs."),
    ] = None,
    json_: Annotated[
        bool,
        typer.Option("--json", help="Write adapter metadata as JSON."),
    ] = False,
) -> None:
    """List installed protocol adapters."""

    from fnmatch import fnmatchcase

    patterns = _selectors(filter_) or ("*",)
    infos = tuple(
        info
        for info in list_plugin_infos(group="toolang.model_adapter")
        if any(fnmatchcase(info.name, pattern) for pattern in patterns)
    )
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
    echo_table(("ADAPTER", "SOURCE"), [(info.name, info.source) for info in infos])


def _setup(ctx: typer.Context) -> AgentSetup:
    return asyncio.run(
        SetupWatcher(
            _layout(ctx),
            model_catalog=context_model_catalog(ctx),
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
    candidates = {target.ref for _selector, target in targets}
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


def _provider_availability(provider: Provider, *, available: set[str]) -> str:
    count = sum(
        f"{provider.id}/{model_id}" in available for model_id in provider.models
    )
    if provider.local and _provider_offline(provider):
        return "0"
    return f"{count}/{len(provider.models)}"


def _provider_adapters_cell(setup: AgentSetup, provider: Provider) -> Text:
    adapters = _provider_adapters(provider)
    if not adapters:
        return Text("-", style="dim")
    cell = Text()
    for index, adapter in enumerate(adapters):
        if index:
            cell.append(",")
        cell.append(adapter, style="dim" if adapter not in setup.adapters else None)
    return cell


def _provider_api_cell(setup: AgentSetup, provider: Provider) -> Text:
    api = _provider_api(setup, provider)
    unavailable = api is None or (provider.local and _provider_offline(provider))
    return Text(api or "-", style="dim" if unavailable else "")


def _provider_env_cell(setup: AgentSetup, provider: Provider) -> Text:
    resolved = provider.resolved
    if resolved is None or not resolved.env:
        return Text("-")
    cell = Text()
    for index, alternative in enumerate(resolved.env):
        if index:
            cell.append(", ")
        names = (alternative,) if isinstance(alternative, str) else alternative
        for group_index, name in enumerate(names):
            if group_index:
                cell.append(" + ")
            missing = not str(setup.envs.get(name, "")).strip()
            cell.append(name, style="dim" if missing else None)
    return cell


def _provider_offline(provider: Provider) -> bool:
    runtime = provider.extra.get("runtime")
    return (
        isinstance(runtime, Mapping)
        and cast(Mapping[str, object], runtime).get("status") == "offline"
    )


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


def _model_table_fields(model: Model) -> tuple[str, str, str, str, str]:
    capabilities = [
        name
        for name, value in (
            ("tool_call", model.tool_call),
            ("reasoning", model.reasoning),
            ("temperature", model.temperature),
            ("structured", model.structured_output),
        )
        if value is True
    ]
    return (
        _format_limit(model, "context"),
        _format_limit(model, "output"),
        ",".join(model.modalities.get("input", ())) or "-",
        ",".join(capabilities) or "-",
        _price_pair(model),
    )


def _format_limit(model: Model, name: str) -> str:
    value = model.limit.get(name)
    return f"{value:_}" if value is not None else "-"


def _price_pair(model: Model) -> str:
    if not model.cost:
        return "-"
    return " / ".join(_price_rate(model.cost.get(name)) for name in ("input", "output"))


def _price_rate(value: object | None) -> str:
    if value is None:
        return "-"
    if isinstance(value, Decimal | int | float) and not isinstance(value, bool):
        return f"${value:.2f}"
    return f"${value}"


def _selectors(values: Sequence[str] | None) -> tuple[str, ...]:
    return split_selector_list(values)
