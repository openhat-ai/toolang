"""Agent workspace configuration commands."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from toolang.state.config import ConfiguredWorkspaces
from ...common.context import context_layout, require_prefix_agent, user_call
from ...common.output import echo_table
from ...common.routing import RequiredPrefixAgentCommand, RequiredPrefixAgentGroup


def workspace_app() -> typer.Typer:
    app = typer.Typer(
        cls=RequiredPrefixAgentGroup,
        help="Manage agent workspaces.",
        add_completion=False,
        no_args_is_help=True,
        pretty_exceptions_enable=False,
        pretty_exceptions_show_locals=False,
    )
    app.command(
        "list",
        help="List workspaces.",
        cls=RequiredPrefixAgentCommand,
    )(list_workspaces)
    app.command(
        "add",
        help="Add a workspace.",
        cls=RequiredPrefixAgentCommand,
        no_args_is_help=True,
    )(add_workspace)
    app.command(
        "remove",
        help="Remove a workspace.",
        cls=RequiredPrefixAgentCommand,
        no_args_is_help=True,
    )(remove_workspace)
    return app


def add_workspace(
    ctx: typer.Context,
    path: Annotated[Path, typer.Argument(help="Existing directory path.")],
    name: Annotated[
        str | None,
        typer.Option("--name", help="Workspace name, normalized to kebab case."),
    ] = None,
) -> None:
    require_prefix_agent(ctx)
    configured = ConfiguredWorkspaces(context_layout(ctx).config)
    selected_name, selected_path = user_call(configured.add, path, name=name)
    typer.echo(f"Workspace {selected_name} added: {selected_path}")


def list_workspaces(ctx: typer.Context) -> None:
    require_prefix_agent(ctx)
    workspaces = user_call(ConfiguredWorkspaces(context_layout(ctx).config).list)
    if not workspaces:
        typer.echo("No workspaces found.")
        return
    echo_table(
        ("NAME", "PATH", "AVAILABLE"),
        tuple(
            (name, path, "yes" if Path(path).is_dir() else "no")
            for name, path in workspaces.items()
        ),
    )


def remove_workspace(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="Workspace name.")],
) -> None:
    require_prefix_agent(ctx)
    configured = ConfiguredWorkspaces(context_layout(ctx).config)
    path = user_call(configured.remove, name)
    typer.echo(f"Workspace {name} removed: {path}")


__all__ = ["workspace_app"]
