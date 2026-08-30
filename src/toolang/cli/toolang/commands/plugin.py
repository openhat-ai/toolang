"""Tool and installed-plugin listing commands."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Annotated

import typer

from ...common.context import context_agent, context_root
from ...common.output import echo_table
from ...common.query import emit_query_discovery
from toolang.common.layout import AgentLayout
from toolang.plugin.toolsets.collections import TOOL_SCHEMA
from toolang.setup import AgentSetup, SetupWatcher
from toolang.plugin.loading import list_plugin_infos

channel_app = typer.Typer(
    help="List available channels.",
    add_completion=False,
    no_args_is_help=True,
    pretty_exceptions_enable=False,
    pretty_exceptions_show_locals=False,
)


def list_tools(
    ctx: typer.Context,
    query: Annotated[
        list[str] | None,
        typer.Option(
            "--query",
            "-q",
            help="Query tools. Repeat values to add alternatives.",
        ),
    ] = None,
    query_help: Annotated[
        bool,
        typer.Option("--query-help", help="Show tool query fields and operators."),
    ] = False,
    query_schema: Annotated[
        bool,
        typer.Option("--query-schema", help="Write the tool query schema as JSON."),
    ] = False,
) -> None:
    if emit_query_discovery(
        TOOL_SCHEMA,
        query_help=query_help,
        query_schema=query_schema,
    ):
        return
    setup = _setup(_layout(ctx))
    rows = tool_rows(setup, queries=tuple(query or ()))
    if not rows:
        typer.echo("No tools matched query." if query else "No tools found.")
        return
    echo_table(("TOOLSET", "TOOL", "PLUGIN", "SOURCE", "DESCRIPTION"), rows)
    typer.echo()
    toolset_count = len({toolset for toolset, *_rest in rows})
    typer.echo(
        f" {len(rows)} {'tool' if len(rows) == 1 else 'tools'}, "
        f"{toolset_count} {'toolset' if toolset_count == 1 else 'toolsets'}"
    )


@channel_app.command("list", help="List installed channels.")
def list_channels() -> None:
    _list_plugins(
        group="toolang.channel",
        header="CHANNEL",
        empty_message="No channels found.",
    )


def list_catalogs() -> None:
    _list_plugins(
        group="toolang.model_catalog",
        header="CATALOG",
        empty_message="No catalogs found.",
    )


def list_toolsets() -> None:
    _list_plugins(
        group="toolang.toolset",
        header="TOOLSET",
        empty_message="No toolsets found.",
    )


def list_sandboxes() -> None:
    _list_plugins(
        group="toolang.sandbox",
        header="SANDBOX",
        empty_message="No sandboxes found.",
    )


def _list_plugins(*, group: str, header: str, empty_message: str) -> None:
    rows = plugin_info_rows(group)
    if not rows:
        typer.echo(empty_message)
        return
    echo_table((header, "SOURCE"), rows)


def model_rows(
    setup: AgentSetup,
    *,
    config_layers: Sequence[Mapping[str, object]],
    model_queries: Sequence[str] | None = None,
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
        queries=model_queries,
    )


def tool_rows(
    setup: AgentSetup,
    *,
    queries: Sequence[str] = (),
) -> list[tuple[str, str, str, str, str]]:
    from toolang.plugin.toolsets.views import tool_list_rows

    return tool_list_rows(
        tools=setup.tools,
        plugin_sources=plugin_sources("toolang.toolset"),
        queries=queries,
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


def plugin_info_rows(group: str) -> list[tuple[str, str]]:
    return [(info.name, info.source) for info in list_plugin_infos(group=group)]


def plugin_sources(group: str) -> dict[str, str]:
    return {info.name: info.source for info in list_plugin_infos(group=group)}
