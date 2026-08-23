"""Model, tool, channel, and sandbox commands."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Annotated

import typer

from ...common.context import context_agent, context_model_catalog, context_root
from ...common.output import echo_table
from toolang.cli.config import load_config_layers
from toolang.common.layout import AgentLayout
from toolang.setup import AgentSetup, SetupWatcher
from toolang.plugin.loading import list_plugin_infos

model_app = typer.Typer(
    help="Inspect available models.",
    add_completion=False,
    no_args_is_help=True,
    pretty_exceptions_enable=False,
    pretty_exceptions_show_locals=False,
)

tool_app = typer.Typer(
    help="Inspect available tools.",
    add_completion=False,
    no_args_is_help=True,
    pretty_exceptions_enable=False,
    pretty_exceptions_show_locals=False,
)

channel_app = typer.Typer(
    help="Inspect available channels.",
    add_completion=False,
    no_args_is_help=True,
    pretty_exceptions_enable=False,
    pretty_exceptions_show_locals=False,
)

sandbox_app = typer.Typer(
    help="Inspect available sandboxes.",
    add_completion=False,
    no_args_is_help=True,
    pretty_exceptions_enable=False,
    pretty_exceptions_show_locals=False,
)


@model_app.command("list", help="List available models.")
def list_models(
    ctx: typer.Context,
    filter_: Annotated[
        list[str] | None,
        typer.Option(
            "--filter",
            "-f",
            "--select",
            help="Filter models with selector-list syntax. Pass CSV or repeat.",
        ),
    ] = None,
    refresh: Annotated[
        bool,
        typer.Option("--refresh", help="Refresh cached provider model lists."),
    ] = False,
) -> None:
    _model_migration_warning("too models")
    from toolang.plugin.models.messages import NO_AVAILABLE_MODELS_MESSAGE
    from toolang.plugin.models.resolution import split_model_selectors

    layout = _layout(ctx)
    config_layers = load_config_layers(layout.root, _configured_agent(ctx))
    setup = _setup(
        layout,
        force=refresh,
        model_catalog=context_model_catalog(ctx),
    )
    selectors = split_model_selectors(tuple(filter_ or ()))
    rows = model_rows(
        setup,
        config_layers=config_layers,
        model_selectors=selectors,
    )
    if not rows:
        if selectors and model_rows(setup, config_layers=config_layers):
            typer.echo("No matched models.")
            typer.echo("Try: toolang model list --filter <selector>")
            typer.echo("Alias: toolang model list --select <selector>")
        else:
            typer.echo(NO_AVAILABLE_MODELS_MESSAGE)
        return
    echo_table(("MODEL", "PROVIDER", "PROFILE"), rows)
    typer.echo()
    provider_count = len({provider for _model, provider, _details in rows})
    typer.echo(
        f" {len(rows)} {'model' if len(rows) == 1 else 'models'}, "
        f"{provider_count} {'provider' if provider_count == 1 else 'providers'}"
    )


@model_app.command("providers", help="Show configured model providers.")
def list_model_providers(ctx: typer.Context) -> None:
    _model_migration_warning("too providers")
    layout = _layout(ctx)
    rows = model_provider_rows(
        _setup(layout, model_catalog=context_model_catalog(ctx)),
        config_layers=load_config_layers(layout.root, _configured_agent(ctx)),
    )
    if not rows:
        typer.echo("No model providers found.")
        return
    echo_table(("PROVIDER", "MODELS", "CONFIG"), rows)


@model_app.command("adapters", help="List available model API adapters.")
def list_model_adapters(ctx: typer.Context) -> None:
    _model_migration_warning("too adapters")
    setup = _setup(_layout(ctx), model_catalog=context_model_catalog(ctx))
    echo_table(("ADAPTER",), [(name,) for name in sorted(setup.adapters)])


@tool_app.command("list", help="List available tools.")
def list_tools(
    ctx: typer.Context,
    filter_: Annotated[
        list[str] | None,
        typer.Option(
            "--filter",
            "-f",
            "--select",
            help="Filter tools with selector-list syntax. Pass CSV or repeat.",
        ),
    ] = None,
) -> None:
    from toolang.plugin.tools.registry import split_tool_selectors

    setup = _setup(_layout(ctx))
    selectors = split_tool_selectors(tuple(filter_ or ()))
    rows = tool_rows(setup, tool_selectors=selectors)
    if not rows:
        if selectors and tool_rows(setup):
            typer.echo("No matched tools.")
            typer.echo("Try: toolang tool list --filter <selector>")
            typer.echo("Alias: toolang tool list --select <selector>")
        else:
            typer.echo("No tools found.")
        return
    echo_table(("SET", "TOOL", "DESCRIPTION"), rows)
    typer.echo()
    toolset_count = len({namespace for namespace, _tool, _description in rows})
    typer.echo(
        f" {len(rows)} {'tool' if len(rows) == 1 else 'tools'}, "
        f"{toolset_count} {'toolset' if toolset_count == 1 else 'toolsets'}"
    )


@channel_app.command("list", help="List installed channels.")
def list_channels() -> None:
    rows = plugin_info_rows("toolang.channel")
    if not rows:
        typer.echo("No channels found.")
        return
    echo_table(("CHANNEL", "SOURCE"), rows)


@sandbox_app.command("list", help="List installed sandboxes.")
def list_sandboxes() -> None:
    rows = plugin_info_rows("toolang.sandbox")
    if not rows:
        typer.echo("No sandboxes found.")
        return
    echo_table(("SANDBOX", "SOURCE"), rows)


def model_rows(
    setup: AgentSetup,
    *,
    config_layers: Sequence[Mapping[str, object]],
    model_selectors: Sequence[str] = (),
) -> list[tuple[str, str, str]]:
    from toolang.plugin.models.config import (
        parse_model_aliases,
    )
    from toolang.plugin.models.views import model_list_rows

    return model_list_rows(
        providers=setup.providers,
        models=setup.models,
        aliases=parse_model_aliases(config_layers),
        envs=setup.envs,
        selectors=model_selectors,
    )


def model_provider_rows(
    setup: AgentSetup,
    *,
    config_layers: Sequence[Mapping[str, object]],
) -> list[tuple[str, str, str]]:
    from toolang.plugin.models.config import (
        parse_model_aliases,
        parse_model_provider_configs,
    )
    from toolang.plugin.models.views import model_provider_rows as build_rows

    provider_configs = parse_model_provider_configs(config_layers)
    return build_rows(
        providers=setup.providers,
        models=setup.models,
        aliases=parse_model_aliases(config_layers),
        provider_configs=provider_configs,
        envs=setup.envs,
    )


def tool_rows(
    setup: AgentSetup,
    *,
    tool_selectors: Sequence[str] = (),
) -> list[tuple[str, str, str]]:
    from toolang.plugin.tools.views import tool_list_rows

    return tool_list_rows(
        tools=setup.tools,
        plugin_sources=plugin_sources("toolang.tool"),
        selectors=tool_selectors,
    )


def _setup(
    layout: AgentLayout,
    *,
    force: bool = False,
    model_catalog: Path | None = None,
) -> AgentSetup:
    return asyncio.run(
        SetupWatcher(layout, model_catalog=model_catalog).refresh(force=force)
    )


def _layout(ctx: typer.Context) -> AgentLayout:
    return AgentLayout.resident(
        context_root(ctx),
        context_agent(ctx) or "default",
    )


def _configured_agent(ctx: typer.Context) -> str:
    return context_agent(ctx) or ""


def plugin_info_rows(group: str) -> list[tuple[str, str]]:
    return [(info.name, info.source) for info in list_plugin_infos(group=group)]


def plugin_sources(group: str) -> dict[str, str]:
    return {info.name: info.source for info in list_plugin_infos(group=group)}


def _model_migration_warning(replacement: str) -> None:
    typer.echo(
        f"Warning: this singular model command is deprecated; use `{replacement}`.",
        err=True,
    )
