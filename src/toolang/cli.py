from __future__ import annotations

import os
import shlex
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version as package_version
from pathlib import Path
from typing import Annotated, Literal, Sequence

import click
import httpx
import typer
from dotenv import load_dotenv
from typer.core import TyperGroup

from toolang.agent_homes import clone_resident_agent, create_resident_agent, remove_resident_agent
from toolang.bus.app import serve_bus_app
from toolang.bus.db import BusStore
from toolang.bus.events import AgentUpdated, utc_now
from toolang.agent_refs import ResolvedAgentRef, resolve_agent_ref
from toolang.agent_registry import (
    KnownAgentRecord,
    KnownAgentSnapshot,
    delete_known_agent,
    delete_running_agent,
    find_known_agents_by_id_prefix,
    find_known_agents_by_name,
    get_running_agent,
    list_known_agents,
    upsert_known_agent,
)
from toolang.errors import ToolangError
from toolang.files._toml import load_toml
from toolang.files.agent_run import AgentRunState
from toolang.invoke import invoke_prepared_agent
from toolang.layout import (
    agent_log_path,
    agent_room,
    agent_room_sandbox_dir,
    agent_run_path,
    agent_source_path,
    agents_db_path,
    bus_events_db_path,
    ensure_toolang_root_layout,
    global_caps_dir,
    global_source_path,
    resolve_toolang_root,
    sandbox_args_path,
    sandbox_exec_path,
    sandbox_host,
    shared_caps_dir,
    shared_source_path,
)
from toolang.prepared import PreparedAgent, prepare_agent
from toolang.server import serve_agent
from toolang.sandbox import (
    HOST_SANDBOX,
    docker_container_name,
    docker_remove_container,
    docker_run_detached,
    forwarded_sandbox_env_names,
    normalize_sandbox_spec,
    parse_sandbox_spec,
    sandbox_key,
    sandbox_process_alive,
    write_sandbox_args_file,
    write_sandbox_exec_file,
)
from toolang.sync import sync_agent
from toolang_caps.github import fetch_github_artifact, resolve_github_cap_ref
from toolang_caps.models import CapKind
from toolang_caps.source_ops import (
    add_cap_ref,
    create_local_cap,
    delete_local_cap,
    install_local_cap,
    local_cap_path,
    prune_empty_local_kind_dir,
    remove_cap_ref,
)


def _version_callback(value: bool | None) -> None:
    if value:
        typer.echo(f"toolang {_toolang_version()}")
        raise typer.Exit()


def _show_hidden_callback(ctx: click.Context, _param: click.Parameter, value: bool) -> bool:
    if value:
        ctx.meta["show_hidden_commands"] = True
    return value


class ToolangGroup(TyperGroup):
    def format_help(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        if not _show_hidden_commands(ctx):
            return super().format_help(ctx, formatter)

        hidden_commands: list[click.Command] = []
        for command_name in self.list_commands(ctx):
            command = self.get_command(ctx, command_name)
            if command is None or not command.hidden:
                continue
            hidden_commands.append(command)

        for command in hidden_commands:
            command.hidden = False
        try:
            return super().format_help(ctx, formatter)
        finally:
            for command in hidden_commands:
                command.hidden = True


app = typer.Typer(
    cls=ToolangGroup,
    help="Toolang CLI",
    add_completion=False,
    invoke_without_command=True,
    pretty_exceptions_enable=False,
    pretty_exceptions_show_locals=False,
)
bus_app = typer.Typer(
    help="Bus commands",
    add_completion=False,
    no_args_is_help=True,
    pretty_exceptions_enable=False,
    pretty_exceptions_show_locals=False,
)
skill_app = typer.Typer(
    help="Skill commands",
    add_completion=False,
    no_args_is_help=True,
    pretty_exceptions_enable=False,
    pretty_exceptions_show_locals=False,
)
skill_local_app = typer.Typer(
    help="Local skill commands",
    add_completion=False,
    no_args_is_help=True,
    pretty_exceptions_enable=False,
    pretty_exceptions_show_locals=False,
)
service_app = typer.Typer(
    help="Service commands",
    add_completion=False,
    no_args_is_help=True,
    pretty_exceptions_enable=False,
    pretty_exceptions_show_locals=False,
)
service_local_app = typer.Typer(
    help="Local service commands",
    add_completion=False,
    no_args_is_help=True,
    pretty_exceptions_enable=False,
    pretty_exceptions_show_locals=False,
)
prompt_app = typer.Typer(
    help="Prompt commands",
    add_completion=False,
    no_args_is_help=True,
    pretty_exceptions_enable=False,
    pretty_exceptions_show_locals=False,
)
prompt_local_app = typer.Typer(
    help="Local prompt commands",
    add_completion=False,
    no_args_is_help=True,
    pretty_exceptions_enable=False,
    pretty_exceptions_show_locals=False,
)
psyche_app = typer.Typer(
    help="Psyche commands",
    add_completion=False,
    no_args_is_help=True,
    pretty_exceptions_enable=False,
    pretty_exceptions_show_locals=False,
)
psyche_local_app = typer.Typer(
    help="Local psyche commands",
    add_completion=False,
    no_args_is_help=True,
    pretty_exceptions_enable=False,
    pretty_exceptions_show_locals=False,
)
@app.callback()
def callback(
    ctx: typer.Context,
    version: Annotated[
        bool | None,
        typer.Option(
            "--version",
            help="Show the version and exit.",
            callback=_version_callback,
            is_eager=True,
        ),
    ] = None,
    show_hidden: Annotated[
        bool,
        typer.Option(
            "--hidden",
            help="Show hidden commands in help output.",
            callback=_show_hidden_callback,
            is_eager=True,
        ),
    ] = False,
) -> None:
    """Toolang CLI."""
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())
        raise typer.Exit()

@app.command(
    hidden=True,
    help="Print the Toolang root or an agent home path.",
    rich_help_panel="Helper Commands",
)
def home(
    agent: Annotated[str | None, typer.Argument(help="Agent selector")] = None,
) -> None:
    toolang_root = _toolang_root()
    if agent is None:
        typer.echo(str(toolang_root))
        return
    db_path = agents_db_path(toolang_root)
    resolved = _resolve_cli_agent(agent, db_path=db_path)
    typer.echo(str(resolved.agent_home))


@app.command(
    hidden=True,
    no_args_is_help=True,
    help="Print an agent source file path.",
    rich_help_panel="Helper Commands",
)
def source(
    agent: Annotated[str, typer.Argument(help="Agent selector")],
) -> None:
    db_path = agents_db_path(_toolang_root())
    resolved = _resolve_cli_agent(agent, db_path=db_path)
    typer.echo(str(agent_source_path(resolved.agent_home, resolved.agent_name)))


@app.command(
    hidden=True,
    no_args_is_help=True,
    help="Print an agent room path.",
    rich_help_panel="Helper Commands",
)
def room(
    agent: Annotated[str, typer.Argument(help="Agent selector")],
) -> None:
    db_path = agents_db_path(_toolang_root())
    resolved = _resolve_cli_agent(agent, db_path=db_path)
    typer.echo(str(agent_room(resolved.agent_home, resolved.agent_name)))


@app.command(
    hidden=True,
    no_args_is_help=True,
    help="Print shell helper setup.",
    rich_help_panel="Helper Commands",
)
def init(
    shell: Annotated[
        Literal["zsh", "bash", "fish"],
        typer.Argument(help="Shell to initialize"),
    ],
) -> None:
    typer.echo(_init_install_note(shell), err=True)
    if shell == "fish":
        typer.echo(_fish_init_script())
        return
    typer.echo(_posix_init_script())


@app.command("new", help="Create a new agent.", no_args_is_help=True)
def agent_new(
    target: Annotated[
        str,
        typer.Argument(help="Agent name or home/agent target"),
    ],
) -> None:
    toolang_root = _toolang_root()
    db_path = agents_db_path(toolang_root)
    agent_ref = _resolve_resident_target(target)
    create_resident_agent(agent_ref.source_path, agent_name=agent_ref.agent_name)
    _remember_agent(agent_ref, db_path=db_path)
    _append_agent_updated(
        toolang_root,
        agent_ref,
        update_kind="create",
        detail="resident agent created",
    )
    typer.echo(str(agent_ref.source_path))


@app.command("clone", help="Clone an existing agent.", no_args_is_help=True)
def agent_clone(
    source: Annotated[str, typer.Argument(help="Agent to clone")],
    target: Annotated[
        str,
        typer.Argument(help="New agent name or home/agent target"),
    ],
) -> None:
    toolang_root = _toolang_root()
    db_path = agents_db_path(toolang_root)
    source_ref = _resolve_cli_agent(source, db_path=db_path)
    target_ref = _resolve_resident_target(target)
    clone_resident_agent(
        target_ref.source_path,
        source_text=_load_clone_source_text(source_ref),
    )
    _remember_agent(target_ref, db_path=db_path)
    _append_agent_updated(
        toolang_root,
        target_ref,
        update_kind="clone",
        detail=f"cloned from {source_ref.agent_uri}",
    )
    typer.echo(str(target_ref.source_path))


@app.command("remove", help="Remove an agent and its local state.", no_args_is_help=True)
def agent_remove(
    agent: Annotated[str, typer.Argument(help="Agent to remove")],
) -> None:
    toolang_root = _toolang_root()
    db_path = agents_db_path(toolang_root)
    agent_ref = _resolve_cli_agent(agent, db_path=db_path)
    if agent_ref.agent_kind != "resident":
        raise ToolangError("toolang remove only supports resident agents.")

    _drop_stale_running_agent(db_path, agent_ref)
    if get_running_agent(db_path, agent_ref.agent_uri) is not None:
        raise ToolangError(
            f"Resident agent {agent_ref.agent_uri} is currently running. Stop it before removal."
        )

    removed_files = remove_resident_agent(agent_ref.agent_home, agent_name=agent_ref.agent_name)
    removed_registry = delete_known_agent(db_path, agent_ref.agent_uri)
    if not removed_files and not removed_registry:
        raise ToolangError(f"Resident agent not found: {agent_ref.source_path}")

    _append_agent_updated(
        toolang_root,
        agent_ref,
        update_kind="remove",
        detail="resident agent removed",
    )
    typer.echo(str(agent_ref.source_path))


@skill_app.command("add", no_args_is_help=True)
def skill_add(
    ref: Annotated[str, typer.Argument(help="Skill ref in owner/name form")],
    scope: Annotated[
        Literal["agent", "shared", "global"],
        typer.Option(help="Target scope"),
    ] = "agent",
    agent: Annotated[
        str | None,
        typer.Option("--agent", help="Agent selector used to resolve agent or shared scope"),
    ] = None,
) -> None:
    _cap_add("skill", ref=ref, scope=scope, agent=agent)


@skill_app.command("remove", no_args_is_help=True)
def skill_remove(
    name: Annotated[str, typer.Argument(help="Skill name")],
    scope: Annotated[
        Literal["agent", "shared", "global"],
        typer.Option(help="Target scope"),
    ] = "agent",
    agent: Annotated[
        str | None,
        typer.Option("--agent", help="Agent selector used to resolve agent or shared scope"),
    ] = None,
) -> None:
    _cap_remove("skill", name=name, scope=scope, agent=agent)


@skill_local_app.command("new", no_args_is_help=True)
def skill_local_new(
    name: Annotated[str, typer.Argument(help="Skill name")],
    scope: Annotated[
        Literal["shared", "global"],
        typer.Option(help="Target local scope"),
    ] = "shared",
    from_ref: Annotated[
        str | None,
        typer.Option("--from", help="Initialize the local skill from a remote ref"),
    ] = None,
    agent: Annotated[
        str | None,
        typer.Option("--agent", help="Agent selector used to resolve shared scope"),
    ] = None,
) -> None:
    _cap_local_new("skill", name=name, scope=scope, from_ref=from_ref, agent=agent)


@skill_local_app.command("path", no_args_is_help=True)
def skill_local_path(
    name: Annotated[str, typer.Argument(help="Skill name")],
    scope: Annotated[
        Literal["shared", "global"],
        typer.Option(help="Target local scope"),
    ] = "shared",
    agent: Annotated[
        str | None,
        typer.Option("--agent", help="Agent selector used to resolve shared scope"),
    ] = None,
) -> None:
    _cap_local_path("skill", name=name, scope=scope, agent=agent)


@skill_local_app.command("delete", no_args_is_help=True)
def skill_local_delete(
    name: Annotated[str, typer.Argument(help="Skill name")],
    scope: Annotated[
        Literal["shared", "global"],
        typer.Option(help="Target local scope"),
    ] = "shared",
    agent: Annotated[
        str | None,
        typer.Option("--agent", help="Agent selector used to resolve shared scope"),
    ] = None,
) -> None:
    _cap_local_delete("skill", name=name, scope=scope, agent=agent)


@service_app.command("add", no_args_is_help=True)
def service_add(
    ref: Annotated[str, typer.Argument(help="Service ref in owner/name form")],
    scope: Annotated[
        Literal["agent", "shared", "global"],
        typer.Option(help="Target scope"),
    ] = "agent",
    agent: Annotated[
        str | None,
        typer.Option("--agent", help="Agent selector used to resolve agent or shared scope"),
    ] = None,
) -> None:
    _cap_add("service", ref=ref, scope=scope, agent=agent)


@service_app.command("remove", no_args_is_help=True)
def service_remove(
    name: Annotated[str, typer.Argument(help="Service name")],
    scope: Annotated[
        Literal["agent", "shared", "global"],
        typer.Option(help="Target scope"),
    ] = "agent",
    agent: Annotated[
        str | None,
        typer.Option("--agent", help="Agent selector used to resolve agent or shared scope"),
    ] = None,
) -> None:
    _cap_remove("service", name=name, scope=scope, agent=agent)


@service_local_app.command("new", no_args_is_help=True)
def service_local_new(
    name: Annotated[str, typer.Argument(help="Service name")],
    scope: Annotated[
        Literal["shared", "global"],
        typer.Option(help="Target local scope"),
    ] = "shared",
    from_ref: Annotated[
        str | None,
        typer.Option("--from", help="Initialize the local service from a remote ref"),
    ] = None,
    agent: Annotated[
        str | None,
        typer.Option("--agent", help="Agent selector used to resolve shared scope"),
    ] = None,
) -> None:
    _cap_local_new("service", name=name, scope=scope, from_ref=from_ref, agent=agent)


@service_local_app.command("path", no_args_is_help=True)
def service_local_path(
    name: Annotated[str, typer.Argument(help="Service name")],
    scope: Annotated[
        Literal["shared", "global"],
        typer.Option(help="Target local scope"),
    ] = "shared",
    agent: Annotated[
        str | None,
        typer.Option("--agent", help="Agent selector used to resolve shared scope"),
    ] = None,
) -> None:
    _cap_local_path("service", name=name, scope=scope, agent=agent)


@service_local_app.command("delete", no_args_is_help=True)
def service_local_delete(
    name: Annotated[str, typer.Argument(help="Service name")],
    scope: Annotated[
        Literal["shared", "global"],
        typer.Option(help="Target local scope"),
    ] = "shared",
    agent: Annotated[
        str | None,
        typer.Option("--agent", help="Agent selector used to resolve shared scope"),
    ] = None,
) -> None:
    _cap_local_delete("service", name=name, scope=scope, agent=agent)


@prompt_app.command("add", no_args_is_help=True)
def prompt_add(
    ref: Annotated[str, typer.Argument(help="Prompt ref in owner/name form")],
    scope: Annotated[
        Literal["agent", "shared", "global"],
        typer.Option(help="Target scope"),
    ] = "agent",
    agent: Annotated[
        str | None,
        typer.Option("--agent", help="Agent selector used to resolve agent or shared scope"),
    ] = None,
) -> None:
    _cap_add("prompt", ref=ref, scope=scope, agent=agent)


@prompt_app.command("remove", no_args_is_help=True)
def prompt_remove(
    name: Annotated[str, typer.Argument(help="Prompt name")],
    scope: Annotated[
        Literal["agent", "shared", "global"],
        typer.Option(help="Target scope"),
    ] = "agent",
    agent: Annotated[
        str | None,
        typer.Option("--agent", help="Agent selector used to resolve agent or shared scope"),
    ] = None,
) -> None:
    _cap_remove("prompt", name=name, scope=scope, agent=agent)


@prompt_local_app.command("new", no_args_is_help=True)
def prompt_local_new(
    name: Annotated[str, typer.Argument(help="Prompt name")],
    scope: Annotated[
        Literal["shared", "global"],
        typer.Option(help="Target local scope"),
    ] = "shared",
    from_ref: Annotated[
        str | None,
        typer.Option("--from", help="Initialize the local prompt from a remote ref"),
    ] = None,
    agent: Annotated[
        str | None,
        typer.Option("--agent", help="Agent selector used to resolve shared scope"),
    ] = None,
) -> None:
    _cap_local_new("prompt", name=name, scope=scope, from_ref=from_ref, agent=agent)


@prompt_local_app.command("path", no_args_is_help=True)
def prompt_local_path(
    name: Annotated[str, typer.Argument(help="Prompt name")],
    scope: Annotated[
        Literal["shared", "global"],
        typer.Option(help="Target local scope"),
    ] = "shared",
    agent: Annotated[
        str | None,
        typer.Option("--agent", help="Agent selector used to resolve shared scope"),
    ] = None,
) -> None:
    _cap_local_path("prompt", name=name, scope=scope, agent=agent)


@prompt_local_app.command("delete", no_args_is_help=True)
def prompt_local_delete(
    name: Annotated[str, typer.Argument(help="Prompt name")],
    scope: Annotated[
        Literal["shared", "global"],
        typer.Option(help="Target local scope"),
    ] = "shared",
    agent: Annotated[
        str | None,
        typer.Option("--agent", help="Agent selector used to resolve shared scope"),
    ] = None,
) -> None:
    _cap_local_delete("prompt", name=name, scope=scope, agent=agent)


@psyche_app.command("add", no_args_is_help=True)
def psyche_add(
    ref: Annotated[str, typer.Argument(help="Psyche ref in owner/name form")],
    scope: Annotated[
        Literal["agent", "shared", "global"],
        typer.Option(help="Target scope"),
    ] = "agent",
    agent: Annotated[
        str | None,
        typer.Option("--agent", help="Agent selector used to resolve agent or shared scope"),
    ] = None,
) -> None:
    _cap_add("psyche", ref=ref, scope=scope, agent=agent)


@psyche_app.command("remove", no_args_is_help=True)
def psyche_remove(
    name: Annotated[str, typer.Argument(help="Psyche name")],
    scope: Annotated[
        Literal["agent", "shared", "global"],
        typer.Option(help="Target scope"),
    ] = "agent",
    agent: Annotated[
        str | None,
        typer.Option("--agent", help="Agent selector used to resolve agent or shared scope"),
    ] = None,
) -> None:
    _cap_remove("psyche", name=name, scope=scope, agent=agent)


@psyche_local_app.command("new", no_args_is_help=True)
def psyche_local_new(
    name: Annotated[str, typer.Argument(help="Psyche name")],
    scope: Annotated[
        Literal["shared", "global"],
        typer.Option(help="Target local scope"),
    ] = "shared",
    from_ref: Annotated[
        str | None,
        typer.Option("--from", help="Initialize the local psyche from a remote ref"),
    ] = None,
    agent: Annotated[
        str | None,
        typer.Option("--agent", help="Agent selector used to resolve shared scope"),
    ] = None,
) -> None:
    _cap_local_new("psyche", name=name, scope=scope, from_ref=from_ref, agent=agent)


@psyche_local_app.command("path", no_args_is_help=True)
def psyche_local_path(
    name: Annotated[str, typer.Argument(help="Psyche name")],
    scope: Annotated[
        Literal["shared", "global"],
        typer.Option(help="Target local scope"),
    ] = "shared",
    agent: Annotated[
        str | None,
        typer.Option("--agent", help="Agent selector used to resolve shared scope"),
    ] = None,
) -> None:
    _cap_local_path("psyche", name=name, scope=scope, agent=agent)


@psyche_local_app.command("delete", no_args_is_help=True)
def psyche_local_delete(
    name: Annotated[str, typer.Argument(help="Psyche name")],
    scope: Annotated[
        Literal["shared", "global"],
        typer.Option(help="Target local scope"),
    ] = "shared",
    agent: Annotated[
        str | None,
        typer.Option("--agent", help="Agent selector used to resolve shared scope"),
    ] = None,
) -> None:
    _cap_local_delete("psyche", name=name, scope=scope, agent=agent)


@app.command("list", help="List known agents and their current status.")
def list_agents() -> None:
    db_path = agents_db_path(_toolang_root())
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
            snapshot.endpoint or "-",
        )
        for snapshot in snapshots
    ]
    typer.echo(_format_rows(("ID", "STATUS", "NAME", "URI", "ENDPOINT"), rows))


@app.command("invoke", help="Run one non-interactive agent turn.", no_args_is_help=True)
def invoke(
    agent: Annotated[str, typer.Argument(help="Agent selector")],
    thunk: Annotated[str | None, typer.Option(help="Thunk name to invoke")] = None,
    user_input: Annotated[
        str | None,
        typer.Option("--input", help="User input for a thunk(user) entrypoint"),
    ] = None,
    model: Annotated[str | None, typer.Option(help="Override model selection")] = None,
) -> None:
    toolang_root = _toolang_root()
    db_path = agents_db_path(toolang_root)
    bus_db_path = bus_events_db_path(toolang_root)
    prepared = prepare_agent(_resolve_cli_agent(agent, db_path=db_path))
    _remember_agent(prepared.ref, db_path=db_path)
    selected_thunk = prepared.program.get_thunk(thunk)

    if selected_thunk.input_name and user_input is None and not sys.stdin.isatty():
        user_input = sys.stdin.read()

    result = invoke_prepared_agent(
        prepared,
        selected_thunk,
        bus_db_path=bus_db_path,
        user_input=user_input,
        model=model,
    )
    typer.echo(result.output)


@app.command("sync", help="Sync one agent state.", no_args_is_help=True)
def sync(
    agent: Annotated[str, typer.Argument(help="Agent selector")],
) -> None:
    toolang_root = _toolang_root()
    db_path = agents_db_path(toolang_root)
    agent_ref = _resolve_cli_agent(agent, db_path=db_path)
    sync_agent(agent_ref)
    _remember_agent(agent_ref, db_path=db_path)
    _append_agent_updated(
        toolang_root,
        agent_ref,
        update_kind="sync",
        detail="sync completed",
    )
    typer.echo("synced")


@app.command("serve", help="Serve one agent in the foreground.", no_args_is_help=True)
def serve(
    agent: Annotated[str, typer.Argument(help="Agent selector")],
    host: Annotated[str, typer.Option(help="Host interface to bind")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port to listen on")] = 8765,
    sandbox: Annotated[
        str,
        typer.Option(help="Sandbox to use: host or docker:<image>"),
    ] = HOST_SANDBOX,
    public_host: Annotated[
        str | None,
        typer.Option("--public-host", help="Published host name", hidden=True),
    ] = None,
) -> None:
    toolang_root = _toolang_root()
    db_path = agents_db_path(toolang_root)
    bus_db_path = bus_events_db_path(toolang_root)
    sandbox_spec = normalize_sandbox_spec(sandbox)
    parsed_sandbox = _parse_sandbox_or_raise(sandbox_spec)
    if parsed_sandbox.kind != "host":
        raise ToolangError("toolang serve only supports host sandbox; use start for docker.")
    prepared = prepare_agent(_resolve_cli_agent(agent, db_path=db_path))
    _remember_agent(prepared.ref, db_path=db_path)
    serve_agent(
        prepared,
        agents_db_path=db_path,
        bus_db_path=bus_db_path,
        host=host,
        port=port,
        sandbox=sandbox_spec,
        public_host=public_host,
        cors_allow_origins=_cors_allow_origins(),
    )


@app.command("start", help="Start serving one agent in the background.", no_args_is_help=True)
def start(
    agent: Annotated[str, typer.Argument(help="Agent selector")],
    host: Annotated[str, typer.Option(help="Host interface to bind")] = "127.0.0.1",
    port: Annotated[int | None, typer.Option(help="Port to bind; chooses a free port by default")] = None,
    sandbox: Annotated[
        str,
        typer.Option(help="Sandbox to use: host or docker:<image>"),
    ] = HOST_SANDBOX,
) -> None:
    toolang_root = _toolang_root()
    db_path = agents_db_path(toolang_root)
    prepared = prepare_agent(_resolve_cli_agent(agent, db_path=db_path))
    _remember_agent(prepared.ref, db_path=db_path)
    _drop_stale_running_agent(db_path, prepared.ref)

    active = get_running_agent(db_path, prepared.ref.agent_uri)
    if active is not None:
        raise ToolangError(f"Agent is already being served: {prepared.ref.agent_uri}")

    sandbox_spec = normalize_sandbox_spec(sandbox)
    parsed_sandbox = _parse_sandbox_or_raise(sandbox_spec)
    selected_port = port if port is not None else _pick_free_port(host)
    endpoint = f"http://{host}:{selected_port}"
    log_path = agent_log_path(prepared.ref.agent_home, prepared.ref.agent_name)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    if parsed_sandbox.kind == "docker":
        _start_docker_agent(
            prepared=prepared,
            host=host,
            port=selected_port,
            sandbox_image=parsed_sandbox.image or "",
            endpoint=endpoint,
        )
        _wait_for_running_agent_sandbox(
            db_path=db_path,
            agent=prepared.ref,
            sandbox_spec=sandbox_spec,
            endpoint=endpoint,
        )
    else:
        process = _start_host_agent(
            prepared=prepared,
            host=host,
            port=selected_port,
            sandbox_spec=sandbox_spec,
            log_path=log_path,
        )
        _wait_for_running_agent_process(
            db_path=db_path,
            agent=prepared.ref,
            process=process,
            endpoint=endpoint,
            log_path=log_path,
        )
    typer.echo(f"started {prepared.ref.agent_id[:12]} {endpoint}")


@bus_app.command("serve")
def bus_serve(
    host: Annotated[str, typer.Option(help="Host interface to bind")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port to listen on")] = 8780,
) -> None:
    toolang_root = _toolang_root()
    serve_bus_app(
        bus_events_db_path(toolang_root),
        host=host,
        port=port,
        cors_allow_origins=_cors_allow_origins(),
    )


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    try:
        app(
            args=list(argv) if argv is not None else None,
            prog_name="toolang",
            standalone_mode=False,
        )
    except click.exceptions.Exit as exc:
        return exc.exit_code
    except click.ClickException as exc:
        exc.show(file=sys.stderr)
        return exc.exit_code
    except (FileNotFoundError, ToolangError) as exc:
        typer.echo(f"toolang error: {exc}", err=True)
        return 1
    return 0


@dataclass(frozen=True, slots=True)
class CapSourceTarget:
    toolang_root: Path
    agent_home: Path | None
    agent_name: str | None
    source_path: Path


@dataclass(frozen=True, slots=True)
class CapLocalTarget:
    toolang_root: Path
    agent_home: Path | None
    kind: CapKind
    kind_dir: Path
    cap_path: Path


@dataclass(frozen=True, slots=True)
class InferredAgentContext:
    agent_home: Path
    agent_name: str | None


def _resolve_cli_agent(raw: str, *, db_path: Path | None = None) -> ResolvedAgentRef:
    toolang_root = _toolang_root()
    guest_resolver = _guest_resolver()
    text = raw.strip()
    resolved_db_path = db_path if db_path is not None else agents_db_path(toolang_root)

    if _looks_like_explicit_source_selector(text):
        return resolve_agent_ref(
            text,
            cwd=Path.cwd(),
            toolang_root=toolang_root,
            guest_resolver=guest_resolver,
        )

    resolved_from_registry = _resolve_known_agent(
        text,
        db_path=resolved_db_path,
        toolang_root=toolang_root,
        guest_resolver=guest_resolver,
    )
    if resolved_from_registry is not None:
        return resolved_from_registry

    return resolve_agent_ref(
        text,
        cwd=Path.cwd(),
        toolang_root=toolang_root,
        guest_resolver=guest_resolver,
    )


def _show_hidden_commands(ctx: click.Context) -> bool:
    if ctx.meta.get("show_hidden_commands") is True:
        return True
    value = ctx.params.get("show_hidden")
    return isinstance(value, bool) and value


def _toolang_root() -> Path:
    root = resolve_toolang_root(os.environ.get("TOOLANG_ROOT", "~/.toolang"))
    return ensure_toolang_root_layout(root)


def _resolve_resident_target(raw: str) -> ResolvedAgentRef:
    toolang_root = _toolang_root()
    agent_ref = resolve_agent_ref(
        raw,
        cwd=Path.cwd(),
        toolang_root=toolang_root,
        guest_resolver=_guest_resolver(),
    )
    if agent_ref.agent_kind != "resident":
        raise ToolangError(
            "Resident agent targets must use resident shorthand or an agent:// URI."
        )
    return agent_ref


def _load_clone_source_text(agent: ResolvedAgentRef) -> str:
    if agent.source_path.exists():
        return agent.source_path.read_text(encoding="utf-8")

    if agent.agent_kind == "visiting":
        response = httpx.get(agent.agent_uri, follow_redirects=True, timeout=10.0)
        response.raise_for_status()
        return response.text

    raise ToolangError(f"Agent source file not found: {agent.source_path}")


def _append_agent_updated(
    toolang_root: Path,
    agent: ResolvedAgentRef,
    *,
    update_kind: str,
    detail: str,
) -> None:
    bus = BusStore(bus_events_db_path(toolang_root))
    bus.append(
        AgentUpdated(
            at=utc_now(),
            agent_uri=agent.agent_uri,
            agent_id=agent.agent_id[:12],
            name=agent.agent_name,
            update_kind=update_kind,
            detail=detail,
            agent_home=str(agent.agent_home),
            source_file=agent.source_path.name,
        )
    )
    bus.close()


def _cap_add(
    kind: CapKind,
    *,
    ref: str,
    scope: Literal["agent", "shared", "global"],
    agent: str | None,
) -> None:
    target = _resolve_cap_scope_target(scope=scope, agent=agent)
    changed = add_cap_ref(target.source_path, kind, ref)
    typer.echo(str(target.source_path))
    if not changed:
        typer.echo("unchanged", err=True)


def _cap_remove(
    kind: CapKind,
    *,
    name: str,
    scope: Literal["agent", "shared", "global"],
    agent: str | None,
) -> None:
    target = _resolve_cap_scope_target(scope=scope, agent=agent)
    changed = remove_cap_ref(
        target.source_path,
        kind,
        name,
        delete_when_empty=target.source_path.name == "agents.too",
    )
    if not changed:
        raise ToolangError(f"{kind.title()} {name!r} is not referenced in {target.source_path}.")
    typer.echo(str(target.source_path))


def _cap_local_new(
    kind: CapKind,
    *,
    name: str,
    scope: Literal["shared", "global"],
    from_ref: str | None,
    agent: str | None,
) -> None:
    target = _resolve_cap_local_target(kind=kind, scope=scope, agent=agent, name=name)
    if from_ref is None:
        create_local_cap(target.cap_path, kind, name)
        typer.echo(str(target.cap_path))
        return

    resolved = resolve_github_cap_ref(kind, from_ref)
    source_path, _ = fetch_github_artifact(resolved)
    try:
        install_local_cap(target.cap_path, kind, source_path)
    finally:
        shutil.rmtree(source_path.parent.parent, ignore_errors=True)
    typer.echo(str(target.cap_path))


def _cap_local_path(
    kind: CapKind,
    *,
    name: str,
    scope: Literal["shared", "global"],
    agent: str | None,
) -> None:
    target = _resolve_cap_local_target(kind=kind, scope=scope, agent=agent, name=name)
    typer.echo(str(target.cap_path))


def _cap_local_delete(
    kind: CapKind,
    *,
    name: str,
    scope: Literal["shared", "global"],
    agent: str | None,
) -> None:
    target = _resolve_cap_local_target(kind=kind, scope=scope, agent=agent, name=name)
    if not delete_local_cap(target.cap_path):
        raise ToolangError(f"Local {kind} not found: {target.cap_path}")
    prune_empty_local_kind_dir(target.kind_dir)
    typer.echo(str(target.cap_path))


def _resolve_cap_scope_target(
    *,
    scope: Literal["agent", "shared", "global"],
    agent: str | None,
) -> CapSourceTarget:
    toolang_root = _toolang_root()
    if scope == "global":
        return CapSourceTarget(
            toolang_root=toolang_root,
            agent_home=None,
            agent_name=None,
            source_path=global_source_path(toolang_root),
        )

    if agent is not None:
        resolved = _resolve_cli_agent(agent, db_path=agents_db_path(toolang_root))
        if scope == "shared":
            source_path = shared_source_path(resolved.agent_home)
        else:
            source_path = agent_source_path(resolved.agent_home, resolved.agent_name)
        return CapSourceTarget(
            toolang_root=toolang_root,
            agent_home=resolved.agent_home,
            agent_name=resolved.agent_name,
            source_path=source_path,
        )

    inferred = _infer_agent_context_from_cwd(Path.cwd(), toolang_root)
    if inferred is None:
        raise ToolangError(
            f"Could not infer a {scope} scope target from the current directory. "
            "Run the command from an agent home or pass --agent."
        )
    if scope == "agent" and inferred.agent_name is None:
        raise ToolangError(
            "Could not infer a single agent source from the current directory. Pass --agent."
        )
    source_path = (
        shared_source_path(inferred.agent_home)
        if scope == "shared"
        else agent_source_path(inferred.agent_home, inferred.agent_name or "")
    )
    return CapSourceTarget(
        toolang_root=toolang_root,
        agent_home=inferred.agent_home,
        agent_name=inferred.agent_name,
        source_path=source_path,
    )


def _resolve_cap_local_target(
    *,
    kind: CapKind,
    scope: Literal["shared", "global"],
    agent: str | None,
    name: str,
) -> CapLocalTarget:
    toolang_root = _toolang_root()
    if scope == "global":
        kind_dir = global_caps_dir(toolang_root, kind)
        return CapLocalTarget(
            toolang_root=toolang_root,
            agent_home=None,
            kind=kind,
            kind_dir=kind_dir,
            cap_path=local_cap_path(kind_dir, kind, name),
        )

    if agent is not None:
        resolved = _resolve_cli_agent(agent, db_path=agents_db_path(toolang_root))
        kind_dir = shared_caps_dir(resolved.agent_home, kind)
        return CapLocalTarget(
            toolang_root=toolang_root,
            agent_home=resolved.agent_home,
            kind=kind,
            kind_dir=kind_dir,
            cap_path=local_cap_path(kind_dir, kind, name),
        )

    inferred = _infer_agent_context_from_cwd(Path.cwd(), toolang_root)
    if inferred is None:
        raise ToolangError(
            "Could not infer a shared scope target from the current directory. "
            "Run the command from an agent home or pass --agent."
        )
    kind_dir = shared_caps_dir(inferred.agent_home, kind)
    return CapLocalTarget(
        toolang_root=toolang_root,
        agent_home=inferred.agent_home,
        kind=kind,
        kind_dir=kind_dir,
        cap_path=local_cap_path(kind_dir, kind, name),
    )


def _infer_agent_context_from_cwd(cwd: Path, toolang_root: Path) -> InferredAgentContext | None:
    resolved_cwd = cwd.resolve()
    for candidate in (resolved_cwd, *resolved_cwd.parents):
        if candidate == toolang_root:
            break
        if _is_managed_agent_home(candidate, toolang_root) or _looks_like_roaming_home(candidate):
            agent_name = _infer_agent_name(candidate, resolved_cwd)
            return InferredAgentContext(agent_home=candidate, agent_name=agent_name)
    return None


def _is_managed_agent_home(candidate: Path, toolang_root: Path) -> bool:
    parent = candidate.parent
    grandparent = parent.parent
    return grandparent == toolang_root and parent.name in {"agents", "guests"}


def _looks_like_roaming_home(candidate: Path) -> bool:
    if not candidate.is_dir():
        return False
    if (candidate / ".toolang").exists():
        return True
    return any(
        path.is_file()
        for path in candidate.glob("*.too")
        if path.name != "agents.too"
    )


def _infer_agent_name(agent_home: Path, cwd: Path) -> str | None:
    try:
        relative = cwd.relative_to(agent_home)
    except ValueError:
        return None

    if len(relative.parts) >= 3 and relative.parts[:2] == (".toolang", "agents"):
        return relative.parts[2]

    agent_names = sorted(
        path.stem
        for path in agent_home.glob("*.too")
        if path.name != "agents.too"
    )
    if len(agent_names) == 1:
        return agent_names[0]
    return None


def _guest_resolver():
    guest_base_url = os.environ.get("TOOLANG_GUEST_BASE_URL", "").strip()
    guest_resolver = None
    if guest_base_url:
        base = guest_base_url.rstrip("/")

        def resolve_guest_name(name: str) -> str:
            return f"{base}/{name.lstrip('/')}"

        guest_resolver = resolve_guest_name
    return guest_resolver


def _cors_allow_origins() -> list[str] | None:
    raw = os.environ.get("TOOLANG_CORS_ORIGINS", "").strip()
    if not raw:
        return None
    items = [item.strip() for item in raw.split(",") if item.strip()]
    return items or None


def _resolve_known_agent(
    raw: str,
    *,
    db_path: Path,
    toolang_root: Path,
    guest_resolver,
) -> ResolvedAgentRef | None:
    if _looks_like_agent_id(raw):
        by_id = _select_known_agent(find_known_agents_by_id_prefix(db_path, raw), raw, "agent id")
        if by_id is not None:
            return resolve_agent_ref(
                by_id.agent_uri,
                cwd=Path.cwd(),
                toolang_root=toolang_root,
                guest_resolver=guest_resolver,
            )

    by_name = _select_known_agent(find_known_agents_by_name(db_path, raw), raw, "agent name")
    if by_name is not None:
        return resolve_agent_ref(
            by_name.agent_uri,
            cwd=Path.cwd(),
            toolang_root=toolang_root,
            guest_resolver=guest_resolver,
        )

    if not _looks_like_agent_id(raw):
        by_id = _select_known_agent(find_known_agents_by_id_prefix(db_path, raw), raw, "agent id")
        if by_id is not None:
            return resolve_agent_ref(
                by_id.agent_uri,
                cwd=Path.cwd(),
                toolang_root=toolang_root,
                guest_resolver=guest_resolver,
            )
    return None


def _select_known_agent(
    records: list[KnownAgentRecord],
    raw: str,
    label: str,
) -> KnownAgentRecord | None:
    if not records:
        return None
    if len(records) > 1:
        matches = ", ".join(record.agent_uri for record in records)
        raise ToolangError(f"Ambiguous {label} {raw!r}: {matches}")
    return records[0]


def _remember_agent(agent: ResolvedAgentRef, *, db_path: Path) -> None:
    upsert_known_agent(
        db_path,
        KnownAgentRecord.from_resolved_agent(
            agent,
            updated_at=datetime.now(timezone.utc),
        ),
    )


def _fresh_known_agents(db_path: Path) -> list[KnownAgentSnapshot]:
    snapshots = list_known_agents(db_path)
    stale_snapshots = [
        snapshot
        for snapshot in snapshots
        if snapshot.running_status is not None
        and not sandbox_process_alive(
            sandbox_spec=snapshot.sandbox or HOST_SANDBOX,
            pid=snapshot.pid,
            agent_name=snapshot.agent_name,
            agent_id=snapshot.agent_id,
        )
    ]
    if not stale_snapshots:
        return snapshots

    for snapshot in stale_snapshots:
        delete_running_agent(db_path, snapshot.agent_uri)
        run_path = agent_run_path(Path(snapshot.agent_home), snapshot.agent_name)
        if run_path.exists():
            now = datetime.now(timezone.utc)
            run_state = AgentRunState.load(run_path)
            run_state.model_copy(update={"status": "stopped", "heartbeat_at": now}).save(
                run_path
            )
    return list_known_agents(db_path)


def _format_rows(headers: tuple[str, ...], rows: Sequence[Sequence[str]]) -> str:
    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))

    lines = [
        "  ".join(header.ljust(widths[index]) for index, header in enumerate(headers)),
        "  ".join("-" * widths[index] for index in range(len(headers))),
    ]
    for row in rows:
        lines.append("  ".join(cell.ljust(widths[index]) for index, cell in enumerate(row)))
    return "\n".join(lines)


def _init_install_note(shell: Literal["zsh", "bash", "fish"]) -> str:
    shell_file = {
        "zsh": "~/.zshrc",
        "bash": "~/.bashrc",
        "fish": "~/.config/fish/config.fish",
    }[shell]
    return (
        f"# Add the emitted block to {shell_file}.\n"
        "# Remove everything between the toolang markers to uninstall.\n"
        "#\n"
        "# Append it with:\n"
        f"#   toolang init {shell} >> {shell_file}\n"
    )


def _posix_init_script() -> str:
    return """# >>> toolang shell helpers >>>
toohome() {
  builtin cd -- "$(command toolang home "$@")"
}

tooroom() {
  builtin cd -- "$(command toolang room "$@")"
}
# <<< toolang shell helpers <<<"""


def _fish_init_script() -> str:
    return """# >>> toolang shell helpers >>>
function toohome
    cd (command toolang home $argv)
end

function tooroom
    cd (command toolang room $argv)
end
# <<< toolang shell helpers <<<"""


def _looks_like_explicit_source_selector(text: str) -> bool:
    return (
        "://" in text
        or text.startswith("guest:")
        or text.startswith(("./", "../", "/", "~"))
        or text.endswith(".too")
        or "/" in text
    )


def _looks_like_agent_id(text: str) -> bool:
    return len(text) >= 7 and all(character in "0123456789abcdef" for character in text.lower())


def _pick_free_port(host: str) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def _drop_stale_running_agent(db_path: Path, agent: ResolvedAgentRef) -> None:
    existing = get_running_agent(db_path, agent.agent_uri)
    if existing is None:
        return
    if sandbox_process_alive(
        sandbox_spec=existing.sandbox,
        pid=existing.pid,
        agent_name=agent.agent_name,
        agent_id=agent.agent_id[:12],
    ):
        return
    delete_running_agent(db_path, agent.agent_uri)
    run_path = agent_run_path(agent.agent_home, agent.agent_name)
    if run_path.exists():
        now = datetime.now(timezone.utc)
        run_state = AgentRunState.load(run_path)
        run_state.model_copy(update={"status": "stopped", "heartbeat_at": now}).save(run_path)


def _wait_for_running_agent_process(
    *,
    db_path: Path,
    agent: ResolvedAgentRef,
    process: subprocess.Popen,
    endpoint: str,
    log_path: Path,
) -> None:
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise ToolangError(
                f"Agent server exited before startup completed. See log: {log_path}"
            )
        active = get_running_agent(db_path, agent.agent_uri)
        if active is not None:
            return
        time.sleep(0.1)
    raise ToolangError(f"Timed out waiting for agent server startup at {endpoint}.")


def _wait_for_running_agent_sandbox(
    *,
    db_path: Path,
    agent: ResolvedAgentRef,
    sandbox_spec: str,
    endpoint: str,
) -> None:
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        active = get_running_agent(db_path, agent.agent_uri)
        if active is not None:
            return
        if not sandbox_process_alive(
            sandbox_spec=sandbox_spec,
            pid=None,
            agent_name=agent.agent_name,
            agent_id=agent.agent_id[:12],
        ):
            container_name = docker_container_name(agent.agent_name, agent.agent_id[:12])
            raise ToolangError(
                f"Sandboxed agent exited before startup completed: {container_name}"
            )
        time.sleep(0.1)
    raise ToolangError(f"Timed out waiting for agent server startup at {endpoint}.")


def _start_host_agent(
    *,
    prepared: PreparedAgent,
    host: str,
    port: int,
    sandbox_spec: str,
    log_path: Path,
) -> subprocess.Popen:
    command = [
        sys.executable,
        "-c",
        "from toolang.cli import main; raise SystemExit(main())",
        "serve",
        prepared.ref.agent_uri,
        "--host",
        host,
        "--port",
        str(port),
        "--sandbox",
        sandbox_spec,
    ]
    with log_path.open("ab") as log_file:
        return subprocess.Popen(
            command,
            cwd=str(prepared.ref.agent_home),
            env=os.environ.copy(),
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )


def _start_docker_agent(
    *,
    prepared: PreparedAgent,
    host: str,
    port: int,
    sandbox_image: str,
    endpoint: str,
) -> None:
    if not sandbox_image:
        raise ToolangError("docker sandbox must include an image")
    toolang_root = _toolang_root()
    key = sandbox_key(prepared.ref.agent_name, prepared.ref.agent_id[:12])
    stage_dir = sandbox_host(toolang_root, key)
    stage_dir.mkdir(parents=True, exist_ok=True)
    args_path = sandbox_args_path(toolang_root, key)
    exec_path = sandbox_exec_path(toolang_root, key)
    room_sandbox_dir = agent_room_sandbox_dir(prepared.ref.agent_home, prepared.ref.agent_name)
    room_sandbox_dir.mkdir(parents=True, exist_ok=True)
    log_path = agent_log_path(prepared.ref.agent_home, prepared.ref.agent_name)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    write_sandbox_args_file(
        args_path,
        {
            "version": 1,
            "agent_uri": prepared.ref.agent_uri,
            "agent_id": prepared.ref.agent_id[:12],
            "host": host,
            "port": port,
            "endpoint": endpoint,
            "sandbox": {
                "type": "docker",
                "container_name": docker_container_name(
                    prepared.ref.agent_name,
                    prepared.ref.agent_id[:12],
                ),
                "image_name": sandbox_image,
            },
        },
    )
    write_sandbox_exec_file(
        exec_path,
        shell_command=(
            "exec "
            + shlex.join(
                [
                    "toolang",
                    "serve",
                    prepared.ref.agent_uri,
                    "--host",
                    "0.0.0.0",
                    "--public-host",
                    host,
                    "--port",
                    str(port),
                    "--sandbox",
                    f"docker:{sandbox_image}",
                ]
            )
            + f" >> {shlex.quote(str(log_path))} 2>&1"
        ),
    )

    container_name = docker_container_name(prepared.ref.agent_name, prepared.ref.agent_id[:12])
    docker_remove_container(container_name)

    mounts = [(toolang_root, toolang_root)]
    if not _path_is_within(prepared.ref.agent_home, toolang_root):
        mounts.append((prepared.ref.agent_home, prepared.ref.agent_home))
    mounts.append((stage_dir, room_sandbox_dir))

    env_names = [name for name in forwarded_sandbox_env_names(os.environ) if name != "TOOLANG_ROOT"]
    env_values = {"TOOLANG_ROOT": str(toolang_root)}
    try:
        docker_run_detached(
            image=sandbox_image,
            container_name=container_name,
            workdir=prepared.ref.agent_home,
            command=["/bin/sh", "-lc", str(room_sandbox_dir / "exec.sh")],
            mounts=mounts,
            published_host=host,
            published_port=port,
            env_names=env_names,
            env_values=env_values,
        )
    except RuntimeError as exc:
        raise ToolangError(f"Could not start docker sandbox: {exc}") from exc


def _path_is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def _parse_sandbox_or_raise(spec: str):
    try:
        return parse_sandbox_spec(spec)
    except ValueError as exc:
        raise ToolangError(str(exc)) from exc


def _toolang_version() -> str:
    try:
        return package_version("toolang")
    except PackageNotFoundError:
        pyproject_path = Path(__file__).resolve().parents[2] / "pyproject.toml"
        project = load_toml(pyproject_path).get("project", {})
        version = project.get("version")
        if isinstance(version, str) and version:
            return version
        raise ToolangError(f"Could not determine package version from {pyproject_path}.")


skill_app.add_typer(skill_local_app, name="local", no_args_is_help=True)
service_app.add_typer(service_local_app, name="local", no_args_is_help=True)
prompt_app.add_typer(prompt_local_app, name="local", no_args_is_help=True)
psyche_app.add_typer(psyche_local_app, name="local", no_args_is_help=True)
app.add_typer(psyche_app, name="psyche", no_args_is_help=True)
app.add_typer(skill_app, name="skill", no_args_is_help=True)
app.add_typer(service_app, name="service", no_args_is_help=True)
app.add_typer(prompt_app, name="prompt", no_args_is_help=True)
app.add_typer(bus_app, name="bus", no_args_is_help=True)


def _reorder_help_entries() -> None:
    command_order = {
        "new": 0,
        "clone": 1,
        "remove": 2,
        "list": 3,
        "sync": 4,
        "invoke": 5,
        "serve": 6,
        "start": 7,
        "home": 100,
        "source": 101,
        "room": 102,
        "init": 103,
    }
    group_order = {
        "psyche": 0,
        "skill": 1,
        "service": 2,
        "prompt": 3,
        "bus": 4,
    }
    app.registered_commands.sort(
        key=lambda info: (command_order.get(info.name or "", 999), info.name or "")
    )
    app.registered_groups.sort(
        key=lambda info: (group_order.get(info.name or "", 999), info.name or "")
    )


_reorder_help_entries()
