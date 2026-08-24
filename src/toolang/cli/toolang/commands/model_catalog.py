"""Plural model catalog, provider, and adapter commands."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from decimal import Decimal
import json
from pathlib import Path
from typing import Annotated

import typer

from toolang.base.types.model import Model, Provider
from toolang.cli.common.context import (
    context_agent,
    context_model_catalog,
    context_root,
    user_call,
)
from toolang.cli.common.output import echo_table
from toolang.cli.config import load_config_layers
from toolang.common.files import atomic_write_text
from toolang.common.layout import AgentLayout
from toolang.common.selectors import split_selector_list
from toolang.plugin.loading import list_plugin_infos
from toolang.plugin.models.catalog import (
    catalog_json_dumps,
    filter_catalog_models,
)
from toolang.plugin.models.config import parse_model_aliases
from toolang.plugin.models.resolution import ModelTargetResolver
from toolang.plugin.models.update import DEFAULT_MODELS_DEV_URL, update_model_catalog
from toolang.setup import AgentSetup, SetupWatcher

models_app = typer.Typer(
    help="Inspect models.",
    add_completion=False,
    invoke_without_command=True,
    pretty_exceptions_enable=False,
    pretty_exceptions_show_locals=False,
)


@models_app.callback()
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
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Write a valid filtered models.json."),
    ] = None,
    force: Annotated[
        bool,
        typer.Option("--force", help="Replace an existing output file."),
    ] = False,
) -> None:
    """List or export model catalog entries when no subcommand is given."""

    if ctx.invoked_subcommand is not None:
        return
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
    if output is not None or json_:
        exportable = tuple(model for model in selected if not model.local)
        if len(exportable) != len(selected):
            local = ", ".join(model.identity for model in selected if model.local)
            raise typer.BadParameter(
                f"local-only models cannot be exported: {local}",
                param_hint="--filter",
            )
        content = catalog_json_dumps(snapshot.to_data(models=exportable))
        if output is not None:
            user_call(_write_output, output, content, force=force)
            typer.echo(str(output.expanduser().resolve(strict=False)))
        if json_:
            typer.echo(content, nl=False)
        return
    rows = [
        (
            model.identity,
            "yes" if model.identity in available else "no",
            *_model_table_fields(model),
        )
        for model in selected
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
            "CAPS",
            "PRICE ($/1M)",
        ),
        rows,
    )
    typer.echo()
    typer.echo(
        f" {len(rows)} {'model' if len(rows) == 1 else 'models'}, "
        f"catalog={snapshot.revision[:19]}"
    )


@models_app.command("inspect", help="Inspect model catalog entries and availability.")
def inspect_models(
    ctx: typer.Context,
    identity: Annotated[
        str | None, typer.Argument(help="Model identity or pattern.")
    ] = None,
    available: Annotated[
        bool,
        typer.Option("--available", help="Show only currently selectable models."),
    ] = False,
    json_: Annotated[
        bool,
        typer.Option("--json", help="Write structured inspection JSON."),
    ] = False,
) -> None:
    setup = _setup(ctx)
    snapshot = _catalog(setup)
    available_ids = _available_identities(ctx, setup)
    selected = filter_catalog_models(
        snapshot,
        (identity,) if identity else (),
        available=available_ids,
        adapters=_adapter_by_identity(setup),
    )
    if available:
        selected = tuple(model for model in selected if model.identity in available_ids)
    if json_:
        typer.echo(
            catalog_json_dumps(
                {
                    "source": str(snapshot.source) if snapshot.source else None,
                    "revision": snapshot.revision,
                    "models": [
                        {
                            "identity": model.identity,
                            "available": model.identity in available_ids,
                            "adapter": _model_adapter(setup, model),
                            "catalog": model.to_data(),
                        }
                        for model in selected
                    ],
                }
            ),
            nl=False,
        )
        return
    rows = [
        (
            model.identity,
            _model_adapter(setup, model),
            "yes" if model.identity in available_ids else "no",
            model.last_updated or "-",
            *_model_table_fields(model),
        )
        for model in selected
    ]
    if not rows:
        typer.echo("No matched models.")
        return
    typer.echo(f"Source: {snapshot.source or 'runtime'}")
    typer.echo(f"Revision: {snapshot.revision}")
    echo_table(
        (
            "MODEL",
            "ADAPTER",
            "AVAILABLE",
            "UPDATED",
            "CONTEXT",
            "OUTPUT",
            "INPUT",
            "CAPS",
            "PRICE ($/1M)",
        ),
        rows,
    )


@models_app.command("update", help="Download and activate a complete models.json.")
def update_models(
    ctx: typer.Context,
    root: Annotated[
        bool,
        typer.Option("--root", help="Update the Toolang root catalog."),
    ] = False,
    home: Annotated[
        bool,
        typer.Option("--home", help="Update the active agent-home catalog."),
    ] = False,
    url: Annotated[
        str,
        typer.Option("--url", help="Complete models.dev-compatible catalog URL."),
    ] = DEFAULT_MODELS_DEV_URL,
) -> None:
    if root == home:
        raise typer.BadParameter("choose exactly one of --root or --home")
    layout = _layout(ctx)
    directory = layout.root if root else layout.home
    result = user_call(update_model_catalog, directory, url=url)
    action = "Updated" if result.changed else "Already current"
    typer.echo(f"{action}: {result.active}")
    typer.echo(f"Revision: {result.revision}")


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
        if any(fnmatchcase(provider_id, pattern) for pattern in patterns)
    )
    available = _available_identities(ctx, setup)
    if json_:
        typer.echo(
            catalog_json_dumps(
                {
                    provider.id: {
                        **provider.to_data(),
                        "available_models": sum(
                            f"{provider.id}/{model_id}" in available
                            for model_id in provider.models
                        ),
                        "adapter": _provider_adapter(setup, provider),
                    }
                    for provider in providers
                }
            ),
            nl=False,
        )
        return
    rows = [
        (
            provider.id,
            provider.name,
            f"{sum(f'{provider.id}/{model_id}' in available for model_id in provider.models)}/{len(provider.models)}",
            _provider_adapter(setup, provider),
            "+".join(provider.env) or "-",
        )
        for provider in providers
    ]
    echo_table(("PROVIDER", "NAME", "AVAILABLE", "ADAPTER", "ENV"), rows)


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
    ).selectable()
    return {target.ref for _selector, target in targets}


def _model_adapter(setup: AgentSetup, model: Model) -> str:
    return next(
        (
            info.adapter
            for info in setup.models
            if info.provider == model.provider_id and info.model == model.id
        ),
        "unavailable",
    )


def _adapter_by_identity(setup: AgentSetup) -> dict[str, str]:
    return {info.ref: info.adapter for info in setup.models}


def _provider_adapter(setup: AgentSetup, provider: Provider) -> str:
    adapters = sorted(
        {
            info.adapter
            for info in setup.models
            if info.provider == provider.id and info.adapter != "unavailable"
        }
    )
    return "+".join(adapters) or "-"


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
    return f"{value:,}" if value is not None else "-"


def _price_pair(model: Model) -> str:
    if not model.cost:
        return "-"
    return "/".join(_price_rate(model.cost.get(name)) for name in ("input", "output"))


def _price_rate(value: object | None) -> str:
    if value is None:
        return "-"
    if isinstance(value, int) and not isinstance(value, bool):
        return f"${value}"
    if isinstance(value, Decimal | float):
        return f"${value:.2f}"
    return f"${value}"


def _selectors(values: Sequence[str] | None) -> tuple[str, ...]:
    return split_selector_list(values)


def _write_output(path: Path, content: str, *, force: bool) -> None:
    path = path.expanduser().resolve(strict=False)
    if path.exists() and not force:
        raise FileExistsError(f"output already exists: {path}")
    atomic_write_text(path, content)
