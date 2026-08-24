"""Resident-agent workspace grant commands."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from toolang.catalog.workspace import AgentWorkspaces
from toolang.common.layout import AgentLayout
from toolang.up import process as agents
from toolang.up.hosting import HostingState

from ...common.context import context_layout, ui_base_url, user_call
from ...common.output import echo_table
from ...common.routing import PrefixAgentJobGroup

workspace_app = typer.Typer(
    cls=PrefixAgentJobGroup,
    help="Manage external workspace grants.",
    no_args_is_help=True,
)


@workspace_app.command("add", help="Grant access to a workspace directory.")
def add_workspace(
    ctx: typer.Context,
    path: Annotated[Path, typer.Argument(help="Existing directory path.")],
    name: Annotated[
        str | None,
        typer.Option("--name", help="Stable workspace name."),
    ] = None,
) -> None:
    layout = context_layout(ctx)
    workspace = user_call(AgentWorkspaces(layout).add, path, name=name)
    typer.echo(f"Added workspace {workspace.name}: {workspace.path}")
    _report_restart(layout)


@workspace_app.command("list", help="List configured workspace grants.")
def list_workspaces(ctx: typer.Context) -> None:
    layout = context_layout(ctx)
    workspaces = user_call(AgentWorkspaces(layout).list)
    if not workspaces:
        typer.echo("No workspaces configured.")
        return
    active = _active_docker_workspaces(layout)
    echo_table(
        ("NAME", "PATH", "STATUS"),
        tuple(
            (
                workspace.name,
                str(workspace.path),
                (
                    "active"
                    if active is None or active.get(workspace.name) == workspace.path
                    else "restart required"
                ),
            )
            for workspace in workspaces
        ),
    )


@workspace_app.command("remove", help="Revoke a workspace grant.")
def remove_workspace(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="Workspace name.")],
) -> None:
    layout = context_layout(ctx)
    workspace = user_call(AgentWorkspaces(layout).remove, name)
    typer.echo(f"Removed workspace {workspace.name}: {workspace.path}")
    _report_restart(layout)


def _report_restart(layout: AgentLayout) -> None:
    if _active_docker_workspaces(layout) is not None:
        typer.echo("Restart required for the running Docker agent.")


def _active_docker_workspaces(layout: AgentLayout) -> dict[str, Path] | None:
    status = user_call(agents.AgentProcess(layout).status, ui_base_url=ui_base_url())
    if (
        status is None
        or status.status != "running"
        or (status.sandbox or "").partition(":")[0] != "docker"
    ):
        return None
    hosting = HostingState.load(layout.hosting_state)
    if hosting is None:
        return {}
    raw = hosting.ref.meta.get("workspaces", {})
    if not isinstance(raw, dict):
        return {}
    result: dict[str, Path] = {}
    for name, publication in raw.items():
        if not isinstance(name, str) or not isinstance(publication, dict):
            continue
        configured_path = publication.get("configured_path")
        if isinstance(configured_path, str):
            result[name] = Path(configured_path).resolve()
    return result
