"""Tool and installed-plugin listing commands."""

from __future__ import annotations

from toolang.cli.common.workspaces import (
    WorkspaceOptions,
    WorkdirOption,
    NoAutoWorkspaceOption,
    inspect_workspaces,
)

from toolang.cli.common.context import context_layout

import asyncio
import json
from collections.abc import Sequence
from typing import Annotated

import typer

from ...common.context import context_agent, context_root
from ...common.output import echo_collection_summary, echo_table
from ...common.records import check_output_options, echo_records
from tq import Query
from ...common.context import user_call
from toolang.base.utils.tools import is_internal_toolset_name
from toolang.common.layout import AgentLayout
from toolang.plugin.loading import list_plugin_infos
from toolang.plugin.toolsets.collections import (
    tool_record,
)
from toolang.plugin.models.query import filter_models
from toolang.setup import AgentSetup
from toolang.setup.watcher import load_setup

channel_app = typer.Typer(
    help="List installed channels",
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
            help="TQ query; repeat for union. See --json for fields",
        ),
    ] = None,
    all_: Annotated[
        bool,
        typer.Option("--all", "-a", help="Include internal and allow-excluded tools"),
    ] = False,
    json_: Annotated[
        bool, typer.Option("--json", help="Write public tool records as JSON")
    ] = False,
    human: Annotated[
        bool, typer.Option("--human", help="Display a table (default)")
    ] = False,
    workspace: WorkspaceOptions = None,
    workdir: WorkdirOption = None,
    no_auto_workspace: NoAutoWorkspaceOption = False,
) -> None:
    check_output_options(human=human, json_=json_)
    inspect_workspaces(ctx, workspace, workdir, no_auto=no_auto_workspace)
    agent = context_agent(ctx)
    setup = asyncio.run(
        load_setup(
            (
                context_layout(ctx)
                if agent is not None
                else AgentLayout.resident(context_root(ctx), "default")
            ),
            agent_context=agent is not None,
            validate_defaults=False,
        )
    )
    tools = setup.tools(all=all_)
    allowed = setup.tools()
    records = [
        tool_record(view, allowed=view.model_name in allowed)
        for view in tools.query()
        if all_ or not is_internal_toolset_name(view.toolset)
    ]
    if query:
        parsed = user_call(lambda: Query.parse(query).validate({"key": "ref"}))
        records = [record for record in records if parsed.match(record) is not None]
    echo_records(records, ("ref", "description", "source", "tags"), json_=json_)
    if not json_:
        echo_collection_summary(
            len(records),
            "tool",
            group=(len({str(record["toolset"]) for record in records}), "toolset"),
        )


@channel_app.command("list", help="List installed channels")
def list_channels() -> None:
    _list_plugins(
        group="toolang.channel",
        header="CHANNEL",
    )


def adapters_command(
    json_: Annotated[
        bool,
        typer.Option("--json", help="Write adapter metadata as JSON"),
    ] = False,
) -> None:
    """List installed model-adapter entry points without loading plugins."""

    rows = plugin_info_rows("toolang.model_adapter")
    if json_:
        typer.echo(
            json.dumps(
                [{"id": name, "source": source} for name, source in rows],
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        return
    if rows:
        echo_table(("ADAPTER", "SOURCE"), rows)
    echo_collection_summary(len(rows), "adapter")


def list_catalogs() -> None:
    _list_plugins(
        group="toolang.model_catalog",
        header="CATALOG",
    )


def list_toolsets(
    all_: Annotated[
        bool, typer.Option("--all", "-a", help="Include internal toolsets")
    ] = False,
) -> None:
    _list_plugins(
        group="toolang.toolset",
        header="TOOLSET",
        include_internal=all_,
    )


def list_sandboxes() -> None:
    _list_plugins(
        group="toolang.sandbox",
        header="SANDBOX",
    )


def _list_plugins(
    *,
    group: str,
    header: str,
    include_internal: bool = True,
) -> None:
    rows = plugin_info_rows(group)
    if not include_internal:
        rows = [row for row in rows if not is_internal_toolset_name(row[0])]
    if rows:
        echo_table((header, "SOURCE"), rows)
    echo_collection_summary(len(rows), header.lower())


def model_rows(
    setup: AgentSetup,
    *,
    model_queries: Sequence[str] | None = None,
) -> list[tuple[str, str, str]]:
    from toolang.plugin.models.views import model_target_profile

    models = filter_models(setup.models_effective(), model_queries or None)
    return [
        (
            model.ref,
            model._toolang.provider,
            model_target_profile(model),
        )
        for model in models
    ]


def plugin_info_rows(group: str) -> list[tuple[str, str]]:
    return [(info.name, info.source) for info in list_plugin_infos(group=group)]
