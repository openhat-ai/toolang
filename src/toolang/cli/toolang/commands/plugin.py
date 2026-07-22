"""Model, tool, channel, and sandbox commands."""

from __future__ import annotations

from collections.abc import Sequence
import os
from pathlib import Path
from typing import Annotated

import typer

from ...common.context import resolve_root
from ...common.output import echo_table
from toolang.cli.config import load_config_layers
from toolang.plugin.models.loading import load_model_providers
from toolang.plugin.loading import list_plugin_infos
from toolang.plugin.tools.loading import load_tool_plugins

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
    from toolang.plugin.models.messages import NO_AVAILABLE_MODELS_MESSAGE
    from toolang.plugin.models.resolution import split_model_selectors

    environ = dict(os.environ)
    root = resolve_root(None)
    selectors = split_model_selectors(tuple(filter_ or ()))
    rows = model_rows(root, environ, model_selectors=selectors, refresh=refresh)
    if not rows:
        if selectors and model_rows(root, environ, refresh=refresh):
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
def list_model_providers() -> None:
    environ = dict(os.environ)
    rows = model_provider_rows(resolve_root(None), environ)
    if not rows:
        typer.echo("No model providers found.")
        return
    echo_table(("PROVIDER", "MODELS", "CONFIG"), rows)


@model_app.command("adapters", help="List available model API adapters.")
def list_model_adapters() -> None:
    from toolang.plugin.models.views import available_model_adapters

    echo_table(("ADAPTER",), [(name,) for name in available_model_adapters()])


@tool_app.command("list", help="List available tools.")
def list_tools(
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

    environ = dict(os.environ)
    root = resolve_root(None)
    selectors = split_tool_selectors(tuple(filter_ or ()))
    rows = tool_rows(root, environ, tool_selectors=selectors)
    if not rows:
        if selectors and tool_rows(root, environ):
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
    root: Path,
    environ: dict[str, str],
    *,
    agent_name: str = "",
    model_selectors: Sequence[str] = (),
    refresh: bool = False,
) -> list[tuple[str, str, str]]:
    from toolang.plugin.models.config import (
        parse_model_aliases,
        parse_model_provider_configs,
    )
    from toolang.plugin.models.views import model_list_rows

    config_layers = load_config_layers(root, agent_name)
    provider_configs = parse_model_provider_configs(config_layers)
    return model_list_rows(
        providers=load_model_providers(provider_configs),
        aliases=parse_model_aliases(config_layers),
        environ=environ,
        selectors=model_selectors,
        cache_dir=root / ".runtime" / "model-cache",
        refresh=refresh,
    )


def model_provider_rows(
    root: Path,
    environ: dict[str, str],
) -> list[tuple[str, str, str]]:
    from toolang.plugin.models.config import (
        parse_model_aliases,
        parse_model_provider_configs,
    )
    from toolang.plugin.models.views import model_provider_rows as build_rows

    config_layers = load_config_layers(root)
    provider_configs = parse_model_provider_configs(config_layers)
    return build_rows(
        providers=load_model_providers(provider_configs),
        aliases=parse_model_aliases(config_layers),
        provider_configs=provider_configs,
        environ=environ,
        cache_dir=root / ".runtime" / "model-cache",
    )


def tool_rows(
    root: Path,
    environ: dict[str, str],
    *,
    agent_name: str = "",
    tool_selectors: Sequence[str] = (),
) -> list[tuple[str, str, str]]:
    from toolang.plugin.config import merge_named_configs
    from toolang.plugin.tools.views import tool_list_rows

    config = merge_named_configs(
        load_config_layers(root, agent_name),
        section="tools",
        environ=environ,
    )
    return tool_list_rows(
        tools=load_tool_plugins(config=config),
        plugin_sources=plugin_sources("toolang.tool"),
        selectors=tool_selectors,
    )


def plugin_info_rows(group: str) -> list[tuple[str, str]]:
    return [(info.name, info.source) for info in list_plugin_infos(group=group)]


def plugin_sources(group: str) -> dict[str, str]:
    return {info.name: info.source for info in list_plugin_infos(group=group)}
