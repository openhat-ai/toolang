"""Agent workspace configuration commands."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from typer._click.exceptions import ClickException

from toolang.cli.common.parameters import PathType, TextType


from toolang.state.config import ConfiguredWorkspaces
from toolang.common.layout import AgentLayout
from ...common.context import (
    cli_context,
    context_layout,
    prepare_agent,
    require_prefix_agent,
    user_call,
)
from ...common.output import echo_collection_summary, echo_table
from ...common.agent_argument import AgentArgument
from ...common.help import CliCommand, CliGroup


def workspace_app() -> typer.Typer:
    app = typer.Typer(
        cls=CliGroup,
        help="Manage agent workspaces",
        add_completion=False,
        no_args_is_help=True,
        pretty_exceptions_enable=False,
        pretty_exceptions_show_locals=False,
    )
    app.command(
        "list",
        help="List configured workspaces",
        cls=CliCommand,
    )(list_workspaces)
    app.command(
        "add",
        help="Add a workspace",
        cls=CliCommand,
        no_args_is_help=True,
    )(add_workspace)
    app.command(
        "remove",
        help="Remove a workspace",
        cls=CliCommand,
        no_args_is_help=True,
    )(remove_workspace)
    return app


def add_workspace(
    ctx: typer.Context,
    agent: Annotated[str, AgentArgument("source", placements="resident,roaming")],
    path: Annotated[
        Path,
        typer.Argument(
            metavar="PATH", click_type=PathType(), help="existing directory path"
        ),
    ],
    name: Annotated[
        str | None,
        typer.Option(
            "--name", metavar="NAME", help="Workspace name, normalized to kebab case"
        ),
    ] = None,
) -> None:
    layout = _editable_layout(ctx)
    configured = ConfiguredWorkspaces(
        _authored_config(layout),
        lock_path=_workspace_edit_lock(layout),
    )
    selected_name, selected_path = user_call(configured.add, path, name=name)
    _refresh_project(layout)
    typer.echo(f"Workspace {selected_name} added: {selected_path}")


def list_workspaces(
    ctx: typer.Context,
    agent: Annotated[str, AgentArgument("source")],
) -> None:
    require_prefix_agent(ctx)
    layout = context_layout(ctx)
    if layout.placement == "visiting":
        layout = prepare_agent(ctx)
    if layout.placement == "roaming":
        source = cli_context(ctx).source
        assert source is not None
        if not source.is_file():
            raise ClickException(f"agent program not found: {source}")
        from toolang.up.source import roaming_layout

        user_call(roaming_layout, source)
        config_path = source.parent / "toolang.toml"
    else:
        config_path = layout.config
    workspaces = user_call(
        ConfiguredWorkspaces(config_path, lock_path=_workspace_edit_lock(layout)).list
    )
    if workspaces:
        echo_table(
            ("WORKSPACE", "PATH", "AVAILABLE"),
            tuple(
                (name, path, "yes" if Path(path).is_dir() else "no")
                for name, path in workspaces.items()
            ),
        )
    echo_collection_summary(len(workspaces), "workspace")


def _editable_layout(ctx: typer.Context) -> AgentLayout:
    require_prefix_agent(ctx)
    if context_layout(ctx).placement == "visiting":
        raise typer.BadParameter(
            "visiting agents have no durable workspace configuration; use -w or --workdir"
        )
    return prepare_agent(ctx)


def _authored_config(layout: AgentLayout) -> Path:
    if layout.placement == "roaming":
        return layout.program.resolve(strict=True).parent / "toolang.toml"
    return layout.config


def _workspace_edit_lock(layout: AgentLayout) -> Path:
    return (
        layout.root_edit_lock
        if layout.placement == "roaming"
        else layout.home_edit_lock
    )


def remove_workspace(
    ctx: typer.Context,
    agent: Annotated[str, AgentArgument("source", placements="resident,roaming")],
    name: Annotated[
        str,
        typer.Argument(metavar="NAME", click_type=TextType(), help="workspace name"),
    ],
) -> None:
    layout = _editable_layout(ctx)
    configured = ConfiguredWorkspaces(
        _authored_config(layout),
        lock_path=_workspace_edit_lock(layout),
    )
    path = user_call(configured.remove, name)
    _refresh_project(layout)
    typer.echo(f"Workspace {name} removed: {path}")


__all__ = ["workspace_app"]


def _refresh_project(layout: AgentLayout) -> None:
    if layout.placement == "roaming":
        from toolang.up.source import materialize_roaming_program

        materialize_roaming_program(layout.program.resolve(strict=True))
