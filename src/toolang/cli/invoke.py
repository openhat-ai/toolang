from __future__ import annotations

import sys
from typing import Annotated

import typer

from toolang.agent.prepared import prepare_agent
from toolang.caps import sync_agent
from toolang.concepts.layout import ToolangRoot
from toolang.runtime.invoke import invoke_prepared_agent
from toolang.concepts.sandbox import HOST_SANDBOX

from .support import (
    _append_agent_updated,
    _remember_agent,
    _resolve_cli_agent,
    _resolve_runtime_cap_scopes,
    _toolang_root,
)


def invoke_command(
    agent: Annotated[str, typer.Argument(help="Agent selector")],
    thunk: Annotated[str | None, typer.Option(help="Thunk name to invoke")] = None,
    user_input: Annotated[
        str | None,
        typer.Option("--input", help="User input for a thunk(user) entrypoint"),
    ] = None,
    model: Annotated[str | None, typer.Option(help="Override model selection")] = None,
    shared_caps: Annotated[
        bool | None,
        typer.Option(
            "--shared/--no-shared",
            help="Enable or disable shared caps. Defaults to on for resident and roaming agents.",
        ),
    ] = None,
    global_caps: Annotated[
        bool | None,
        typer.Option(
            "--global/--no-global",
            help="Enable or disable global caps. Defaults to on only for resident agents.",
        ),
    ] = None,
) -> None:
    toolang_root = _toolang_root()
    root = ToolangRoot.resolve(toolang_root)
    db_path = root.agents_db_path
    bus_db_path = root.bus_events_db_path
    agent_ref = _resolve_cli_agent(agent, db_path=db_path)
    cap_scopes = _resolve_runtime_cap_scopes(
        agent_ref,
        shared_caps=shared_caps,
        global_caps=global_caps,
    )
    prepared = prepare_agent(agent_ref, cap_scopes=cap_scopes)
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
        sandbox=HOST_SANDBOX,
    )
    typer.echo(result.output)


def sync_command(
    agent: Annotated[str, typer.Argument(help="Agent selector")],
) -> None:
    toolang_root = _toolang_root()
    db_path = ToolangRoot.resolve(toolang_root).agents_db_path
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
