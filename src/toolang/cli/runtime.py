from __future__ import annotations

import os
import shlex
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

import typer

from toolang.agent_refs import ResolvedAgentRef
from toolang.agent_registry import delete_running_agent, get_running_agent
from toolang.errors import ToolangError
from toolang.files.agent_run import AgentRunState
from toolang.layout import (
    agent_log_path,
    agent_room_sandbox_dir,
    agent_run_path,
    agents_db_path,
    bus_events_db_path,
    sandbox_args_path,
    sandbox_exec_path,
    sandbox_host,
)
from toolang.prepared import PreparedAgent, prepare_agent
from toolang.runtime.invoke import invoke_prepared_agent
from toolang.runtime.server import serve_agent
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

from .support import (
    _append_agent_updated,
    _cors_allow_origins,
    _remember_agent,
    _resolve_cli_agent,
    _resolve_runtime_cap_scopes,
    _toolang_root,
)

bus_app = typer.Typer(
    help="Bus commands",
    add_completion=False,
    no_args_is_help=True,
    pretty_exceptions_enable=False,
    pretty_exceptions_show_locals=False,
)


def register_runtime_commands(app: typer.Typer) -> None:
    @app.command("invoke", help="Run one non-interactive agent turn.", no_args_is_help=True)
    def invoke(
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
        db_path = agents_db_path(toolang_root)
        bus_db_path = bus_events_db_path(toolang_root)
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
        sandbox_spec = normalize_sandbox_spec(sandbox)
        parsed_sandbox = _parse_sandbox_or_raise(sandbox_spec)
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

    @app.command("start", help="Start serving one agent in the background.", no_args_is_help=True)
    def start(
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
        from toolang.bus.app import serve_bus_app

        serve_bus_app(
            bus_events_db_path(toolang_root),
            host=host,
            port=port,
            cors_allow_origins=_cors_allow_origins(),
        )

    app.add_typer(bus_app, name="bus", no_args_is_help=True)


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
        "--shared" if prepared.cap_scopes.include_shared else "--no-shared",
        "--global" if prepared.cap_scopes.include_global else "--no-global",
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
                    "--shared" if prepared.cap_scopes.include_shared else "--no-shared",
                    "--global" if prepared.cap_scopes.include_global else "--no-global",
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

    env_names = [
        name for name in forwarded_sandbox_env_names(os.environ) if name != "TOOLANG_ROOT"
    ]
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
