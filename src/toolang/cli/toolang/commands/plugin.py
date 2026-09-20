"""Tool and installed-plugin listing commands."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated

import typer

from ...common.context import context_agent, context_root
from ...common.output import echo_table
from ...common.query import query_items
from toolang.base.utils.tools import is_internal_toolset_name
from toolang.common.layout import AgentLayout
from toolang.common.query import QueryDataset
from toolang.plugin.config import merge_plugin_configs
from toolang.plugin.loading import list_plugin_infos
from toolang.plugin.toolsets.collections import (
    ToolCollection,
    ToolQueryView,
    tool_dataset,
)
from toolang.plugin.toolsets.loading import load_tools
from toolang.setup import AgentSetup
from toolang.setup.config import load_agent_config, load_setup_config

channel_app = typer.Typer(
    help="List available channels",
    subcommand_metavar="<COMMAND> [ARGUMENTS]",
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
            metavar="QUERY",
            help="Query tools. Repeat to add matches; see 'too query tools'",
        ),
    ] = None,
    all_: Annotated[
        bool, typer.Option("--all", help="Include internal toolsets and their tools")
    ] = False,
) -> None:
    layout = _layout(ctx)
    configs = (load_setup_config(layout), load_agent_config(layout))
    dataset = tool_dataset(
        load_tools(toolset_config=merge_plugin_configs(configs, family="toolset"))
    )
    selected = tuple(
        item
        for item in query_items(dataset, query)
        if all_ or not is_internal_toolset_name(item.toolset)
    )
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


@channel_app.command("list", help="List installed channels")
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


def list_toolsets(
    all_: Annotated[
        bool, typer.Option("--all", help="Include internal toolsets")
    ] = False,
) -> None:
    _list_plugins(
        group="toolang.toolset",
        header="TOOLSET",
        empty_message="No toolsets found.",
        include_internal=all_,
    )


def list_sandboxes() -> None:
    _list_plugins(
        group="toolang.sandbox",
        header="SANDBOX",
        empty_message="No sandboxes found.",
    )


def _list_plugins(
    *,
    group: str,
    header: str,
    empty_message: str,
    include_internal: bool = True,
) -> None:
    rows = plugin_info_rows(group)
    if not include_internal:
        rows = [row for row in rows if not is_internal_toolset_name(row[0])]
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
            model.ref,
            model._toolang.provider,
            model_target_profile(model),
        )
        for model in models.entries
    ]


def setup_tool_dataset(setup: AgentSetup) -> QueryDataset[ToolQueryView]:
    """Return the schema-owned tool query and display dataset for one setup."""

    return _tool_dataset(setup.tools)


def _tool_dataset(tools: ToolCollection) -> QueryDataset[ToolQueryView]:
    return tool_dataset(
        tools,
        plugin_sources=plugin_sources("toolang.toolset"),
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
