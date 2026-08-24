"""Tool, channel, and sandbox inspection commands."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Annotated

import typer

from ...common.context import context_agent, context_root
from ...common.output import echo_table
from toolang.common.layout import AgentLayout
from toolang.setup import AgentSetup, SetupWatcher
from toolang.plugin.loading import list_plugin_infos

channel_app = typer.Typer(
    help="Inspect available channels.",
    add_completion=False,
    no_args_is_help=True,
    pretty_exceptions_enable=False,
    pretty_exceptions_show_locals=False,
)


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
            typer.echo("Try: toolang tools --filter <selector>")
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


def plugin_info_rows(group: str) -> list[tuple[str, str]]:
    return [(info.name, info.source) for info in list_plugin_infos(group=group)]


def plugin_sources(group: str) -> dict[str, str]:
    return {info.name: info.source for info in list_plugin_infos(group=group)}
