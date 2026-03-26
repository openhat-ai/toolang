from __future__ import annotations

from typing import Annotated

import typer

from toolang.agent.managed import (
    clone_managed_agent,
    create_managed_agent,
    remove_managed_agent,
)
from toolang.agent.registry import delete_known_agent, get_running_agent
from toolang.errors import ToolangError
from toolang.concepts.layout import ToolangRoot

from .support import (
    _agent_link_from_endpoint,
    _append_agent_created,
    _append_agent_removed,
    _format_rows,
    _fresh_known_agents,
    _load_clone_source_text,
    _remember_agent,
    _resolve_cli_agent,
    _resolve_resident_target,
    _toolang_root,
)
from .run import _drop_stale_running_agent


def register_agent_commands(app: typer.Typer) -> None:
    @app.command("new", help="Create a new agent.", no_args_is_help=True)
    def agent_new(
        target: Annotated[
            str,
            typer.Argument(help="Agent name or home/agent target"),
        ],
    ) -> None:
        toolang_root = _toolang_root()
        db_path = ToolangRoot.resolve(toolang_root).agents_db_path
        agent_ref = _resolve_resident_target(target)
        create_managed_agent(agent_ref)
        _remember_agent(agent_ref, db_path=db_path)
        _append_agent_created(
            toolang_root,
            agent_ref,
            detail="agent created",
        )
        typer.echo(str(agent_ref.source))

    @app.command("clone", help="Clone an existing agent.", no_args_is_help=True)
    def agent_clone(
        source: Annotated[str, typer.Argument(help="Agent to clone")],
        target: Annotated[
            str,
            typer.Argument(help="New agent name or home/agent target"),
        ],
    ) -> None:
        toolang_root = _toolang_root()
        db_path = ToolangRoot.resolve(toolang_root).agents_db_path
        source_ref = _resolve_cli_agent(source, db_path=db_path)
        target_ref = _resolve_resident_target(target)
        clone_managed_agent(target_ref, source_text=_load_clone_source_text(source_ref))
        _remember_agent(target_ref, db_path=db_path)
        _append_agent_created(
            toolang_root,
            target_ref,
            detail=f"cloned from {source_ref.uri}",
        )
        typer.echo(str(target_ref.source))

    @app.command("remove", help="Remove an agent and its local state.", no_args_is_help=True)
    def agent_remove(
        agent: Annotated[str, typer.Argument(help="Agent to remove")],
    ) -> None:
        toolang_root = _toolang_root()
        db_path = ToolangRoot.resolve(toolang_root).agents_db_path
        agent_ref = _resolve_cli_agent(agent, db_path=db_path)
        if agent_ref.kind != "resident":
            raise ToolangError("toolang remove only supports managed agents.")

        _drop_stale_running_agent(db_path, agent_ref)
        if get_running_agent(db_path, agent_ref.uri) is not None:
            raise ToolangError(
                f"Agent {agent_ref.uri} is currently running. Stop it before removal."
            )

        removed_files = remove_managed_agent(agent_ref)
        removed_registry = delete_known_agent(db_path, agent_ref.uri)
        if not removed_files and not removed_registry:
            raise ToolangError(f"Resident agent not found: {agent_ref.source}")

        _append_agent_removed(
            toolang_root,
            agent_ref,
            detail="agent removed",
        )
        typer.echo(str(agent_ref.source))

    @app.command("list", help="List known agents and their current status.")
    def list_agents() -> None:
        db_path = ToolangRoot.resolve(_toolang_root()).agents_db_path
        snapshots = _fresh_known_agents(db_path)
        if not snapshots:
            typer.echo("No agents found.")
            return

        rows = [
            (
                snapshot.agent_id,
                snapshot.running_status or "stopped",
                snapshot.agent_name,
                snapshot.agent_uri,
                _agent_link_from_endpoint(snapshot.endpoint) or "-",
            )
            for snapshot in snapshots
        ]
        typer.echo(_format_rows(("ID", "STATUS", "NAME", "URI", "LINK"), rows))
