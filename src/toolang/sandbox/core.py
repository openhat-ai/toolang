from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

from toolang.agent.prepared import PreparedAgent
from toolang.concepts.execution import RuntimeLoop
from toolang.concepts.layout import AgentHome, ToolangRoot
from toolang.concepts.sandbox import SandboxSpec, SandboxState
from toolang.errors import ToolangError

from .docker import docker_container_running, docker_remove_container, docker_run_detached


@dataclass(frozen=True, slots=True)
class StartedSandbox:
    """Result of starting one sandboxed agent runtime."""

    state: SandboxState
    process: subprocess.Popen | None = None


def sandbox_alive(state: SandboxState) -> bool:
    """Return whether one sandbox runtime still appears to be alive."""

    if state.type == "docker":
        if not state.container_name:
            return False
        return docker_container_running(state.container_name)
    return _host_pid_exists(state.run.pid if state.run is not None else None)


def stop_sandbox(state: SandboxState, *, pid: int | None = None) -> None:
    """Stop one running sandbox."""

    if state.type == "docker":
        if state.container_name:
            docker_remove_container(state.container_name)
        return

    target_pid = pid if pid is not None else (state.run.pid if state.run is not None else None)
    if target_pid is None or target_pid <= 0:
        raise ToolangError("Running agent has no valid process id.")
    try:
        os.kill(target_pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except PermissionError as exc:
        raise ToolangError(f"Failed to stop agent process {target_pid}: {exc}") from exc


def start_sandbox(
    *,
    spec: SandboxSpec,
    prepared: PreparedAgent,
    toolang_root: Path,
    host: str,
    port: int,
    endpoint: str,
    log_path: Path,
    runtime_loops: tuple[RuntimeLoop, ...] = ("server",),
    forward_env_names: Iterable[str] = (),
) -> StartedSandbox:
    """Start one sandboxed long-lived agent runtime."""

    if spec.kind == "docker":
        state = _start_docker_sandbox(
            spec=spec,
            prepared=prepared,
            toolang_root=toolang_root,
            host=host,
            port=port,
            endpoint=endpoint,
            log_path=log_path,
            runtime_loops=runtime_loops,
            forward_env_names=forward_env_names,
        )
        return StartedSandbox(state=state, process=None)

    process = _start_host_sandbox(
        spec=spec,
        prepared=prepared,
        host=host,
        port=port,
        log_path=log_path,
        runtime_loops=runtime_loops,
    )
    return StartedSandbox(
        state=SandboxState.for_spec(
            spec,
            agent_name=prepared.ref.name,
            agent_id=prepared.ref.id,
            pid=process.pid,
            port=port,
        ),
        process=process,
    )


def _start_host_sandbox(
    *,
    spec: SandboxSpec,
    prepared: PreparedAgent,
    host: str,
    port: int,
    log_path: Path,
    runtime_loops: tuple[RuntimeLoop, ...],
) -> subprocess.Popen:
    command = [
        sys.executable,
        "-c",
        "from toolang.cli import main; raise SystemExit(main())",
        "run",
        prepared.ref.uri,
        "--host",
        host,
        "--port",
        str(port),
        "--sandbox",
        spec.spec,
        "--shared" if prepared.cap_scopes.include_shared else "--no-shared",
        "--global" if prepared.cap_scopes.include_global else "--no-global",
    ]
    for loop in runtime_loops:
        command.extend(["--loop", loop])
    with log_path.open("ab") as log_file:
        return subprocess.Popen(
            command,
            cwd=str(prepared.ref.home),
            env=os.environ.copy(),
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )


def _start_docker_sandbox(
    *,
    spec: SandboxSpec,
    prepared: PreparedAgent,
    toolang_root: Path,
    host: str,
    port: int,
    endpoint: str,
    log_path: Path,
    runtime_loops: tuple[RuntimeLoop, ...],
    forward_env_names: Iterable[str],
) -> SandboxState:
    if not spec.image:
        raise ToolangError("docker sandbox must include an image")
    key = _sandbox_key(prepared.ref.name, prepared.ref.id)
    root = ToolangRoot.resolve(toolang_root)
    stage_dir = root.sandbox_dir(key)
    stage_dir.mkdir(parents=True, exist_ok=True)
    args_path = root.sandbox_args_path(key)
    exec_path = root.sandbox_exec_path(key)
    room_sandbox_dir = AgentHome.resolve(prepared.ref.home).room(prepared.ref.name).sandbox_dir
    room_sandbox_dir.mkdir(parents=True, exist_ok=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    state = SandboxState.for_spec(
        spec,
        agent_name=prepared.ref.name,
        agent_id=prepared.ref.id,
        port=port,
    )
    _write_sandbox_args_file(
        args_path,
        {
            "version": 1,
            "agent_uri": prepared.ref.uri,
            "agent_id": prepared.ref.id[:12],
            "host": host,
            "port": port,
            "endpoint": endpoint,
            "runtime_loops": list(runtime_loops),
            "sandbox": {
                "type": state.type,
                "container_name": state.container_name,
                "image_name": state.image_name,
            },
        },
    )
    _write_sandbox_exec_file(
        exec_path,
        shell_command=(
            "exec "
            + shlex.join(
                [
                    "toolang",
                    "run",
                    prepared.ref.uri,
                    "--host",
                    "0.0.0.0",
                    "--public-host",
                    host,
                    "--port",
                    str(port),
                    "--sandbox",
                    spec.spec,
                    "--shared" if prepared.cap_scopes.include_shared else "--no-shared",
                    "--global" if prepared.cap_scopes.include_global else "--no-global",
                ]
            )
            + "".join(f" --loop {shlex.quote(loop)}" for loop in runtime_loops)
            + f" >> {shlex.quote(str(log_path))} 2>&1"
        ),
    )

    if state.container_name:
        docker_remove_container(state.container_name)

    mounts = [(toolang_root, toolang_root)]
    if not _path_is_within(prepared.ref.home, toolang_root):
        mounts.append((prepared.ref.home, prepared.ref.home))
    mounts.append((stage_dir, room_sandbox_dir))

    env_names = [
        name
        for name in _forwarded_sandbox_env_names(
            os.environ,
            extra_names=forward_env_names,
        )
        if name != "TOOLANG_ROOT"
    ]
    env_values = {"TOOLANG_ROOT": str(toolang_root)}
    try:
        docker_run_detached(
            image=spec.image,
            container_name=state.container_name or "",
            workdir=prepared.ref.home,
            command=["/bin/sh", "-lc", str(room_sandbox_dir / "exec.sh")],
            mounts=mounts,
            published_host=host,
            published_port=port,
            env_names=env_names,
            env_values=env_values,
        )
    except RuntimeError as exc:
        raise ToolangError(f"Could not start docker sandbox: {exc}") from exc
    return state


def _sandbox_key(agent_name: str, agent_id: str) -> str:
    return f"{agent_name}-{agent_id[:12]}"


def _host_pid_exists(pid: int | None) -> bool:
    if pid is None or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _write_sandbox_args_file(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(payload), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_sandbox_exec_file(
    path: Path,
    *,
    command: Iterable[str] | None = None,
    shell_command: str | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if shell_command is None:
        if command is None:
            raise ValueError("sandbox exec file requires command or shell_command")
        shell_command = "exec " + shlex.join(list(command))
    script = "#!/bin/sh\nset -eu\n" + shell_command + "\n"
    path.write_text(script, encoding="utf-8")
    path.chmod(0o755)


def _forwarded_sandbox_env_names(
    environment: Mapping[str, str],
    *,
    extra_names: Iterable[str] = (),
) -> list[str]:
    names: set[str] = set()
    for name in extra_names:
        if name in environment:
            names.add(name)
    for name in environment:
        if name.startswith("TOOLANG_"):
            names.add(name)
            continue
        if name.startswith("OPENAI_"):
            names.add(name)
            continue
        if name.endswith("_API_KEY"):
            names.add(name)
            continue
        if name in {
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "NO_PROXY",
            "ALL_PROXY",
            "http_proxy",
            "https_proxy",
            "no_proxy",
            "all_proxy",
        }:
            names.add(name)
    return sorted(names)


def _path_is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True
