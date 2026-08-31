"""Tool and installed-plugin listing commands."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated

import typer

from ...common.context import context_agent, context_root
from ...common.output import echo_table
from ...common.query import query_items
from toolang.common.layout import AgentLayout
from toolang.common.query import QueryDataset
from toolang.plugin.toolsets.collections import (
    ToolQueryView,
    tool_dataset,
)
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
            help="Query tools. Repeat to add matches; see 'too query tools'.",
        ),
    ] = None,
) -> None:
    setup = _setup(_layout(ctx))
    dataset = setup_tool_dataset(setup)
    selected = query_items(dataset, query)
    if not selected:
        typer.echo("No tools matched query." if query else "No tools found.")
        return
    headers, rows = dataset.table(selected)
    echo_table(headers, rows)
    typer.echo()
    toolset_count = len({item.toolset for item in selected})
    typer.echo(
        f" {len(selected)} {'tool' if len(selected) == 1 else 'tools'}, "
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
    model_queries: Sequence[str] | None = None,
) -> list[tuple[str, str, str]]:
    from toolang.plugin.models.views import model_target_profile

    models = setup.models.match(model_queries) if model_queries else setup.models
    return [
        (
            entry.ref,
            entry.target.provider,
            model_target_profile(entry.target, models=(entry.info,)),
        )
        for entry in models.entries
    ]


def setup_tool_dataset(setup: AgentSetup) -> QueryDataset[ToolQueryView]:
    """Return the schema-owned tool query and display dataset for one setup."""

    return tool_dataset(
        setup.tools,
        plugin_sources=plugin_sources("toolang.toolset"),
    )


def _setup(
    layout: AgentLayout,
    *,
    model_catalog: Path | None = None,
) -> AgentSetup:
    return asyncio.run(SetupWatcher(layout, model_catalog=model_catalog).refresh())


def _layout(ctx: typer.Context) -> AgentLayout:
    return AgentLayout.resident(
        context_root(ctx),
        context_agent(ctx) or "default",
    )


def plugin_info_rows(group: str) -> list[tuple[str, str]]:
    return [(info.name, info.source) for info in list_plugin_infos(group=group)]


def plugin_sources(group: str) -> dict[str, str]:
    return {info.name: info.source for info in list_plugin_infos(group=group)}
