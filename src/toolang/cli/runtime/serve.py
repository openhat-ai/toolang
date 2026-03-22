from __future__ import annotations

import socket
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

import typer

from toolang.agent.prepared import prepare_agent
from toolang.agent.registry import delete_running_agent, get_running_agent
from toolang.errors import ToolangError
from toolang.http import agent_link_for_port
from toolang.layout import (
    agent_log_path,
    agent_run_path,
    agents_db_path,
    bus_events_db_path,
)
from toolang.runtime.server import serve_agent
from toolang.concepts.identity import AgentRef
from toolang.concepts.sandbox import HOST_SANDBOX, SandboxSpec, SandboxState
from toolang.concepts.persisted.activation_state import ActivationState
from toolang.sandbox import sandbox_alive, start_sandbox, stop_sandbox

from ..support import (
    _cors_allow_origins,
    _remember_agent,
    _resolve_cli_agent,
    _resolve_runtime_cap_scopes,
    _toolang_root,
)


def serve_command(
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
    db_path = agents_db_path(toolang_root)
    bus_db_path = bus_events_db_path(toolang_root)
    parsed_sandbox = _parse_sandbox_or_raise(sandbox)
    sandbox_spec = parsed_sandbox.spec
    if parsed_sandbox.kind != "host":
        raise ToolangError("toolang serve only supports host sandbox; use start for docker.")
    agent_ref = _resolve_cli_agent(agent, db_path=db_path)
    cap_scopes = _resolve_runtime_cap_scopes(
        agent_ref,
        shared_caps=shared_caps,
        global_caps=global_caps,
    )
    prepared = prepare_agent(agent_ref, cap_scopes=cap_scopes)
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


def start_command(
    agent: Annotated[str, typer.Argument(help="Agent selector")],
    host: Annotated[str, typer.Option(help="Host interface to bind")] = "127.0.0.1",
    port: Annotated[
        int | None,
        typer.Option(help="Port to bind; chooses a free port by default"),
    ] = None,
    sandbox: Annotated[
        str,
        typer.Option(help="Sandbox to use: host or docker:<image>"),
    ] = HOST_SANDBOX,
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
    db_path = agents_db_path(toolang_root)
    agent_ref = _resolve_cli_agent(agent, db_path=db_path)
    cap_scopes = _resolve_runtime_cap_scopes(
        agent_ref,
        shared_caps=shared_caps,
        global_caps=global_caps,
    )
    prepared = prepare_agent(agent_ref, cap_scopes=cap_scopes)
    _remember_agent(prepared.ref, db_path=db_path)
    _drop_stale_running_agent(db_path, prepared.ref)

    active = get_running_agent(db_path, prepared.ref.uri)
    if active is not None:
        raise ToolangError(f"Agent is already being served: {prepared.ref.uri}")

    parsed_sandbox = _parse_sandbox_or_raise(sandbox)
    selected_port = port if port is not None else _pick_free_port(host)
    endpoint = f"http://{host}:{selected_port}"
    agent_link = agent_link_for_port(selected_port)
    log_path = agent_log_path(prepared.ref.home, prepared.ref.name)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    started = start_sandbox(
        spec=parsed_sandbox,
        prepared=prepared,
        toolang_root=toolang_root,
        host=host,
        port=selected_port,
        endpoint=endpoint,
        log_path=log_path,
    )
    if parsed_sandbox.kind == "docker":
        _wait_for_running_agent_sandbox(
            db_path=db_path,
            agent=prepared.ref,
            sandbox_state=started.state,
            endpoint=endpoint,
        )
    else:
        process = _start_host_agent(
            started=started,
        )
        _wait_for_running_agent_process(
            db_path=db_path,
            agent=prepared.ref,
            process=process,
            endpoint=endpoint,
            log_path=log_path,
        )
    typer.echo(f"started {prepared.ref.id[:12]} {agent_link}")


def stop_command(
    agent: Annotated[str, typer.Argument(help="Agent selector")],
) -> None:
    toolang_root = _toolang_root()
    db_path = agents_db_path(toolang_root)
    agent_ref = _resolve_cli_agent(agent, db_path=db_path)
    _drop_stale_running_agent(db_path, agent_ref)

    active = get_running_agent(db_path, agent_ref.uri)
    if active is None:
        raise ToolangError(f"Agent is not running: {agent_ref.uri}")

    _stop_running_agent_process(
        sandbox_state=SandboxState.for_spec(
            SandboxSpec.parse(active.sandbox),
            agent_name=agent_ref.name,
            agent_id=agent_ref.id[:12],
            pid=active.pid,
        ),
        pid=active.pid,
    )
    _wait_for_running_agent_stop(db_path=db_path, agent=agent_ref)
    typer.echo(f"stopped {agent_ref.id[:12]}")


def _pick_free_port(host: str) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def _stop_running_agent_process(
    *,
    sandbox_state: SandboxState,
    pid: int | None,
) -> None:
    stop_sandbox(sandbox_state, pid=pid)


def _drop_stale_running_agent(db_path: Path, agent: AgentRef) -> None:
    existing = get_running_agent(db_path, agent.uri)
    if existing is None:
        return
    if sandbox_alive(
        SandboxState.for_spec(
            SandboxSpec.parse(existing.sandbox),
            agent_name=agent.name,
            agent_id=agent.id[:12],
            pid=existing.pid,
        )
    ):
        return
    delete_running_agent(db_path, agent.uri)
    run_path = agent_run_path(agent.home, agent.name)
    if run_path.exists():
        now = datetime.now(timezone.utc)
        run_state = ActivationState.load(run_path)
        run_state.model_copy(update={"status": "stopped", "heartbeat_at": now}).save(run_path)


def _wait_for_running_agent_stop(
    *,
    db_path: Path,
    agent: AgentRef,
) -> None:
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        _drop_stale_running_agent(db_path, agent)
        if get_running_agent(db_path, agent.uri) is None:
            return
        time.sleep(0.1)
    raise ToolangError(f"Timed out waiting for agent stop: {agent.uri}")


def _wait_for_running_agent_process(
    *,
    db_path: Path,
    agent: AgentRef,
    process,
    endpoint: str,
    log_path: Path,
) -> None:
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise ToolangError(
                f"Agent server exited before startup completed. See log: {log_path}"
            )
        active = get_running_agent(db_path, agent.uri)
        if active is not None:
            return
        time.sleep(0.1)
    raise ToolangError(f"Timed out waiting for agent server startup at {endpoint}.")


def _wait_for_running_agent_sandbox(
    *,
    db_path: Path,
    agent: AgentRef,
    sandbox_state: SandboxState,
    endpoint: str,
) -> None:
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        active = get_running_agent(db_path, agent.uri)
        if active is not None:
            return
        if not sandbox_alive(sandbox_state):
            container_name = sandbox_state.container_name or "<unknown>"
            raise ToolangError(
                f"Sandboxed agent exited before startup completed: {container_name}"
            )
        time.sleep(0.1)
    raise ToolangError(f"Timed out waiting for agent server startup at {endpoint}.")


def _start_host_agent(
    *,
    started,
):
    return started.process


def _parse_sandbox_or_raise(spec: str):
    try:
        return SandboxSpec.parse(spec)
    except ValueError as exc:
        raise ToolangError(str(exc)) from exc
