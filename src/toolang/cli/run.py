from __future__ import annotations

import socket
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

import typer

from toolang.agent.prepared import prepare_agent
from toolang.agent.registry import delete_running_agent, get_running_agent
from toolang.concepts.execution import RuntimeLoop
from toolang.errors import ToolangError
from toolang.concepts.layout import AgentHome, ToolangRoot
from toolang.runtime.server import run_agent
from toolang.concepts.identity import AgentRef
from toolang.concepts.persisted import ChannelsConfig
from toolang.concepts.sandbox import HOST_SANDBOX, SandboxSpec, SandboxState
from toolang.concepts.persisted.run_state import RunState
from toolang.sandbox import sandbox_alive, start_sandbox, stop_sandbox

from .support import (
    _agent_link_for_port,
    _cors_allow_origins,
    _load_runtime_channels,
    _remember_agent,
    _resolve_cli_agent,
    _resolve_runtime_cap_scopes,
    _resolve_runtime_loops,
    _toolang_root,
)


def run_command(
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
    loops: Annotated[
        list[str] | None,
        typer.Option("--loop", help="Runtime loop to enable. Repeat for multiple loops."),
    ] = None,
) -> None:
    toolang_root = _toolang_root()
    root = ToolangRoot.resolve(toolang_root)
    db_path = root.agents_db_path
    bus_db_path = root.bus_events_db_path
    parsed_sandbox = _parse_sandbox_or_raise(sandbox)
    sandbox_spec = parsed_sandbox.spec
    if parsed_sandbox.kind != "host":
        raise ToolangError("toolang run only supports host sandbox; use start for docker.")
    agent_ref = _resolve_cli_agent(agent, db_path=db_path)
    cap_scopes = _resolve_runtime_cap_scopes(
        agent_ref,
        shared_caps=shared_caps,
        global_caps=global_caps,
    )
    prepared = prepare_agent(agent_ref, cap_scopes=cap_scopes)
    _remember_agent(prepared.ref, db_path=db_path)
    runtime_loops = _with_server_loop(_resolve_runtime_loops(loops, default=("server", "pulse")))
    channels_config = _runtime_channels_for_loops(prepared.ref.home, runtime_loops)
    run_agent(
        prepared,
        agents_db_path=db_path,
        bus_db_path=bus_db_path,
        host=host,
        port=port,
        sandbox=sandbox_spec,
        public_host=public_host,
        cors_allow_origins=_cors_allow_origins(),
        runtime_loops=runtime_loops,
        channels_config=channels_config,
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
    loops: Annotated[
        list[str] | None,
        typer.Option("--loop", help="Runtime loop to enable. Repeat for multiple loops."),
    ] = None,
) -> None:
    toolang_root = _toolang_root()
    db_path = ToolangRoot.resolve(toolang_root).agents_db_path
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
        raise ToolangError(f"Agent is already running: {prepared.ref.uri}")

    parsed_sandbox = _parse_sandbox_or_raise(sandbox)
    runtime_loops = _with_server_loop(_resolve_runtime_loops(loops, default=("server", "poll", "pulse")))
    _channels_config, channel_env_names = _runtime_channels_with_env_names_for_loops(
        prepared.ref.home,
        runtime_loops,
    )
    selected_port = port if port is not None else _pick_free_port(host)
    endpoint = f"http://{host}:{selected_port}"
    agent_link = _agent_link_for_port(selected_port)
    log_path = AgentHome.resolve(prepared.ref.home).room(prepared.ref.name).log_path
    log_path.parent.mkdir(parents=True, exist_ok=True)
    started = start_sandbox(
        spec=parsed_sandbox,
        prepared=prepared,
        toolang_root=toolang_root,
        host=host,
        port=selected_port,
        endpoint=endpoint,
        log_path=log_path,
        runtime_loops=runtime_loops,
        forward_env_names=channel_env_names,
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


def _runtime_channels_for_loops(
    agent_home: Path,
    runtime_loops: tuple[RuntimeLoop, ...],
) -> ChannelsConfig:
    if "poll" not in runtime_loops:
        return ChannelsConfig()
    channels_config, _env_names = _load_runtime_channels(agent_home)
    return channels_config


def _runtime_channels_with_env_names_for_loops(
    agent_home: Path,
    runtime_loops: tuple[RuntimeLoop, ...],
) -> tuple[ChannelsConfig, tuple[str, ...]]:
    if "poll" not in runtime_loops:
        return ChannelsConfig(), ()
    return _load_runtime_channels(agent_home)


def _with_server_loop(runtime_loops: tuple[RuntimeLoop, ...]) -> tuple[RuntimeLoop, ...]:
    if "server" in runtime_loops:
        return runtime_loops
    return ("server",) + runtime_loops


def stop_command(
    agent: Annotated[str, typer.Argument(help="Agent selector")],
) -> None:
    toolang_root = _toolang_root()
    db_path = ToolangRoot.resolve(toolang_root).agents_db_path
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
    run_path = AgentHome.resolve(agent.home).room(agent.name).run_path
    if run_path.exists():
        now = datetime.now(timezone.utc)
        run_state = RunState.load(run_path)
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
            raise ToolangError(f"Agent run exited before startup completed. See log: {log_path}")
        active = get_running_agent(db_path, agent.uri)
        if active is not None:
            return
        time.sleep(0.1)
    raise ToolangError(f"Timed out waiting for agent startup at {endpoint}.")


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
    raise ToolangError(f"Timed out waiting for agent startup at {endpoint}.")


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
