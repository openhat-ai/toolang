"""Agent workspace configuration commands."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from toolang.cli.common.parameters import PathType, TextType


from toolang.state.config import ConfiguredWorkspaces
from toolang.common.layout import AgentLayout
from ...common.context import (
    cli_context,
    context_layout,
    require_prefix_agent,
    user_call,
)
from ...common.output import echo_table
from ...common.routing import RequiredPrefixAgentCommand, RequiredPrefixAgentGroup


class _WorkspaceGroup(RequiredPrefixAgentGroup):
    argument_help = "Agent name, .too file, reference, or URL"


class _WorkspaceCommand(RequiredPrefixAgentCommand):
    argument_help = "Agent name, .too file, reference, or URL"


def workspace_app() -> typer.Typer:
    app = typer.Typer(
        cls=_WorkspaceGroup,
        help="Manage agent workspaces",
        add_completion=False,
        no_args_is_help=True,
        pretty_exceptions_enable=False,
        pretty_exceptions_show_locals=False,
    )
    app.command(
        "list",
        help="List configured workspaces",
        cls=_WorkspaceCommand,
    )(list_workspaces)
    app.command(
        "add",
        help="Add a workspace",
        cls=_WorkspaceCommand,
        no_args_is_help=True,
    )(add_workspace)
    app.command(
        "remove",
        help="Remove a workspace",
        cls=_WorkspaceCommand,
        no_args_is_help=True,
    )(remove_workspace)
    return app


def add_workspace(
    ctx: typer.Context,
    path: Annotated[
        Path,
        typer.Argument(
            metavar="PATH", click_type=PathType(), help="Existing directory path"
        ),
    ],
    name: Annotated[
        str | None,
        typer.Option(
            "--name", metavar="NAME", help="Workspace name, normalized to kebab case"
        ),
    ] = None,
) -> None:
    require_prefix_agent(ctx)
    configured = ConfiguredWorkspaces(_authored_config(context_layout(ctx)))
    selected_name, selected_path = user_call(configured.add, path, name=name)
    _refresh_project(context_layout(ctx))
    typer.echo(f"Workspace {selected_name} added: {selected_path}")


def list_workspaces(ctx: typer.Context) -> None:
    require_prefix_agent(ctx)
    layout = context_layout(ctx)
    if layout.placement == "roaming":
        source = cli_context(ctx).source
        assert source is not None
        config_path = source.parent / "toolang.toml"
    else:
        config_path = layout.config
    workspaces = user_call(ConfiguredWorkspaces(config_path).list)
    echo_table(
        ("NAME", "PATH", "AVAILABLE"),
        tuple(
            (name, path, "yes" if Path(path).is_dir() else "no")
            for name, path in workspaces.items()
        ),
    )


def _authored_config(layout: AgentLayout) -> Path:
    if layout.placement == "visiting":
        raise typer.BadParameter(
            "visiting agents have no durable workspace configuration; use -w or --workdir"
        )
    if layout.placement == "roaming":
        return layout.program.resolve(strict=True).parent / "toolang.toml"
    return layout.config


def remove_workspace(
    ctx: typer.Context,
    name: Annotated[
        str,
        typer.Argument(metavar="NAME", click_type=TextType(), help="Workspace name"),
    ],
) -> None:
    require_prefix_agent(ctx)
    configured = ConfiguredWorkspaces(_authored_config(context_layout(ctx)))
    path = user_call(configured.remove, name)
    _refresh_project(context_layout(ctx))
    typer.echo(f"Workspace {name} removed: {path}")


__all__ = ["workspace_app"]


def _refresh_project(layout: AgentLayout) -> None:
    if layout.placement == "roaming":
        from toolang.up.process import materialize_roaming_program

        materialize_roaming_program(layout.program.resolve(strict=True))
