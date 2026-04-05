"""First-party sandbox plugin implementations."""

from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
import sys
from pathlib import Path
from typing import Iterable, Mapping

from toolang.concepts.layout import AgentHome, ToolangRoot
from toolang.concepts.sandbox import SandboxState
from toolang.errors import ToolangError

from .contracts import SandboxPlugin, SandboxStartRequest, StartedSandbox
from .docker import docker_container_running, docker_remove_container, docker_run_detached


class HostSandboxPlugin:
    """First-party host-process sandbox plugin."""

    def start(self, request: SandboxStartRequest) -> StartedSandbox:
        process = _start_host_process(request)
        return StartedSandbox(
            state=SandboxState.for_spec(
                request.spec,
                agent_name=request.prepared.ref.name,
                agent_id=request.prepared.ref.id,
                pid=process.pid,
                port=request.port,
            ),
            process=process,
        )

    def alive(self, state: SandboxState) -> bool:
        return _host_pid_exists(state.run.pid if state.run is not None else None)

    def stop(
        self,
        state: SandboxState,
        *,
        pid: int | None = None,
        force: bool = False,
    ) -> None:
        target_pid = pid if pid is not None else (state.run.pid if state.run is not None else None)
        if target_pid is None or target_pid <= 0:
            raise ToolangError("Running agent has no valid process id.")
        try:
            os.kill(target_pid, signal.SIGKILL if force else signal.SIGTERM)
        except ProcessLookupError:
            return
        except PermissionError as exc:
            raise ToolangError(
                f"Failed to stop agent process {target_pid}: {exc}"
            ) from exc


class DockerSandboxPlugin:
    """First-party docker sandbox plugin."""

    def start(self, request: SandboxStartRequest) -> StartedSandbox:
        return StartedSandbox(
            state=_start_docker_container(request),
            process=None,
        )

    def alive(self, state: SandboxState) -> bool:
        if not state.container_name:
            return False
        return docker_container_running(state.container_name)

    def stop(
        self,
        state: SandboxState,
        *,
        pid: int | None = None,
        force: bool = False,
    ) -> None:
        del pid, force
        if state.container_name:
            docker_remove_container(state.container_name)


def create_host_sandbox_plugin(config: dict[str, object]) -> SandboxPlugin:
    """Create the first-party host sandbox plugin."""

    del config
    return HostSandboxPlugin()


def create_docker_sandbox_plugin(config: dict[str, object]) -> SandboxPlugin:
    """Create the first-party docker sandbox plugin."""

    del config
    return DockerSandboxPlugin()


def _start_host_process(request: SandboxStartRequest) -> subprocess.Popen:
    command = [
        sys.executable,
        "-c",
        "from toolang.cli import main; raise SystemExit(main())",
        "run",
        request.prepared.ref.uri,
        "--host",
        request.host,
        "--port",
        str(request.port),
        "--sandbox",
        request.spec.spec,
        "--shared"
        if request.prepared.cap_scopes.include_shared
        else "--no-shared",
        "--global"
        if request.prepared.cap_scopes.include_global
        else "--no-global",
    ]
    for loop in request.runtime_loops:
        command.extend(["--loop", loop])
    with request.log_path.open("ab") as log_file:
        return subprocess.Popen(
            command,
            cwd=str(request.prepared.ref.home),
            env=os.environ.copy(),
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )


def _start_docker_container(request: SandboxStartRequest) -> SandboxState:
    if not request.spec.image:
        raise ToolangError("docker sandbox must include an image")
    key = _sandbox_key(request.prepared.ref.name, request.prepared.ref.id)
    root = ToolangRoot.resolve(request.toolang_root)
    stage_dir = root.sandbox_dir(key)
    args_path = root.sandbox_args_path(key)
    exec_path = root.sandbox_exec_path(key)
    room_sandbox_dir = (
        AgentHome.resolve(request.prepared.ref.home)
        .room(request.prepared.ref.name)
        .sandbox_dir
    )
    stage_dir.mkdir(parents=True, exist_ok=True)
    room_sandbox_dir.mkdir(parents=True, exist_ok=True)
    request.log_path.parent.mkdir(parents=True, exist_ok=True)

    state = SandboxState.for_spec(
        request.spec,
        agent_name=request.prepared.ref.name,
        agent_id=request.prepared.ref.id,
        port=request.port,
    )
    _write_json(
        args_path,
        {
            "version": 1,
            "agent_uri": request.prepared.ref.uri,
            "agent_id": request.prepared.ref.id[:12],
            "host": request.host,
            "port": request.port,
            "endpoint": request.endpoint,
            "runtime_loops": list(request.runtime_loops),
            "sandbox": {
                "type": state.type,
                "container_name": state.container_name,
                "image_name": state.image_name,
            },
        },
    )
    _write_exec_script(
        exec_path,
        shell_command=(
            "exec "
            + shlex.join(
                [
                    "toolang",
                    "run",
                    request.prepared.ref.uri,
                    "--host",
                    "0.0.0.0",
                    "--public-host",
                    request.host,
                    "--port",
                    str(request.port),
                    "--sandbox",
                    request.spec.spec,
                    "--shared"
                    if request.prepared.cap_scopes.include_shared
                    else "--no-shared",
                    "--global"
                    if request.prepared.cap_scopes.include_global
                    else "--no-global",
                ]
            )
            + "".join(
                f" --loop {shlex.quote(loop)}" for loop in request.runtime_loops
            )
            + f" >> {shlex.quote(str(request.log_path))} 2>&1"
        ),
    )
    if state.container_name:
        docker_remove_container(state.container_name)
    mounts = [(request.toolang_root, request.toolang_root)]
    if not _path_is_within(request.prepared.ref.home, request.toolang_root):
        mounts.append((request.prepared.ref.home, request.prepared.ref.home))
    mounts.append((stage_dir, room_sandbox_dir))
    env_names = [
        name
        for name in _forwarded_env_names(os.environ, extra_names=request.forward_env_names)
        if name != "TOOLANG_ROOT"
    ]
    env_values = {"TOOLANG_ROOT": str(request.toolang_root)}
    try:
        docker_run_detached(
            image=request.spec.image,
            container_name=state.container_name or "",
            workdir=request.prepared.ref.home,
            command=["/bin/sh", "-lc", str(room_sandbox_dir / "exec.sh")],
            mounts=mounts,
            published_host=request.host,
            published_port=request.port,
            env_names=env_names,
            env_values=env_values,
        )
    except RuntimeError as exc:
        raise ToolangError(f"Could not start docker sandbox: {exc}") from exc
    return state


def _host_pid_exists(pid: int | None) -> bool:
    if pid is None or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _sandbox_key(agent_name: str, agent_id: str) -> str:
    return f"{agent_name}-{agent_id[:12]}"


def _write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(dict(payload), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _write_exec_script(path: Path, *, shell_command: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    script = "#!/bin/sh\nset -eu\n" + shell_command + "\n"
    path.write_text(script, encoding="utf-8")
    path.chmod(0o755)


def _forwarded_env_names(
    environment: Mapping[str, str],
    *,
    extra_names: Iterable[str] = (),
) -> list[str]:
    names: set[str] = set()
    for name in extra_names:
        if name in environment:
            names.add(name)
    for name in environment:
        if name.startswith("TOOLANG_") or name.startswith("OPENAI_") or name.endswith("_API_KEY"):
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
