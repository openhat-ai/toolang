"""Small cancellable Docker CLI adapter used by the sandbox plugin."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
import subprocess
import psutil

from toolang.base.types.sandbox import SandboxMount

DEFAULT_HOST_GATEWAY = "host.docker.internal"
DOCKER_DIAGNOSTIC_TAIL_LINES = 2000


def docker_container_running(container_id: str) -> bool:
    try:
        result = _docker(
            "inspect",
            "--format",
            "{{.State.Running}}",
            container_id,
            check=False,
            timeout=2.0,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("docker inspect timed out") from exc
    if result is None:
        raise RuntimeError("docker command not found")
    if result.returncode != 0:
        if "No such object:" in result.stderr or "No such container:" in result.stderr:
            if docker_launch_pending(container_id):
                raise RuntimeError("docker container creation is still pending")
            return False
        raise RuntimeError(result.stderr.strip() or "docker inspect failed")
    if result.stdout.strip() not in {"true", "false"}:
        raise RuntimeError("docker inspect returned an invalid state")
    running = result.stdout.strip() == "true"
    if not running and docker_launch_pending(container_id):
        raise RuntimeError("docker container creation is still pending")
    return running


def docker_launch_pending(container_name: str) -> bool:
    """Do not retire an intent while its orphaned native creation helper exists."""
    if not container_name.startswith("toolang-"):
        return False
    for process in psutil.process_iter(["name", "cmdline", "uids"]):
        try:
            if process.info["name"] not in {"docker", "com.docker.cli"}:
                continue
            args = process.info["cmdline"]
            if args is None:
                uids = process.info["uids"]
                if uids is not None and uids.real == os.getuid():
                    raise RuntimeError("cannot verify pending docker creation")
                continue
            if (
                len(args) > 1
                and args[1] == "run"
                and "--name" in args
                and args[args.index("--name") + 1] == container_name
                and process.status() != psutil.STATUS_ZOMBIE
            ):
                return True
        except (psutil.NoSuchProcess, IndexError):
            continue
        except psutil.AccessDenied as exc:
            raise RuntimeError("cannot verify pending docker creation") from exc
    return False


def docker_wait_container(container_id: str) -> int:
    result = _docker("wait", container_id, check=False)
    if result is None:
        raise RuntimeError("docker command not found")
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "docker wait failed")
    try:
        return int(result.stdout.strip())
    except ValueError as exc:
        raise RuntimeError("docker wait returned an invalid exit code") from exc


async def docker_follow_container_logs(
    container_id: str,
) -> asyncio.subprocess.Process:
    try:
        return await asyncio.create_subprocess_exec(
            "docker",
            "logs",
            "--follow",
            container_id,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("docker command not found") from exc


def docker_stop_container(container_id: str, *, force: bool) -> None:
    if docker_launch_pending(container_id):
        raise RuntimeError("docker container creation is still pending")
    command = "kill" if force else "stop"
    result = _docker(command, container_id, check=False)
    if result is None:
        raise RuntimeError("docker command not found")
    if result.returncode != 0:
        detail = result.stderr.strip()
        if "No such container" not in detail and docker_container_running(container_id):
            raise RuntimeError(detail or f"docker {command} failed")


def docker_remove_container(container_id: str) -> None:
    if docker_launch_pending(container_id):
        raise RuntimeError("docker container creation is still pending")
    result = _docker("rm", "--force", container_id, check=False)
    if result is None:
        raise RuntimeError("docker command not found")
    if result.returncode != 0:
        detail = result.stderr.strip()
        if "No such container" not in detail:
            raise RuntimeError(detail or "docker rm failed")


def docker_append_container_logs(container_id: str, log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.touch(mode=0o600, exist_ok=True)
    log_path.chmod(0o600)
    try:
        with log_path.open("ab") as stream:
            subprocess.run(
                (
                    "docker",
                    "logs",
                    "--tail",
                    str(DOCKER_DIAGNOSTIC_TAIL_LINES),
                    container_id,
                ),
                check=False,
                stdout=stream,
                stderr=subprocess.STDOUT,
            )
    except FileNotFoundError:
        return


async def docker_run_detached(
    *,
    image: str,
    container_name: str,
    workdir: str,
    command: list[str],
    mounts: tuple[SandboxMount, ...],
    bind_host: str,
    published_port: int,
    hosted_port: int,
    env_values: Mapping[str, str],
    log_path: Path | None,
    remove_on_exit: bool = False,
) -> str:
    args = [
        "docker",
        "run",
        "--detach",
        "--name",
        container_name,
        "--workdir",
        workdir,
        "--publish",
        f"{bind_host}:{published_port}:{hosted_port}",
        "--add-host",
        f"{DEFAULT_HOST_GATEWAY}:host-gateway",
    ]
    if remove_on_exit:
        args.append("--rm")
    for mount in mounts:
        suffix = ":ro" if mount.read_only else ""
        args.extend(["--volume", f"{mount.local_path}:{mount.hosted_path}{suffix}"])
    for name, value in env_values.items():
        args.extend(["--env", f"{name}={value}"])
    args.append(image)
    args.extend(command)
    log_stream = log_path.open("ab") if log_path is not None else None
    try:
        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=log_stream,
            )
        except FileNotFoundError as exc:
            raise RuntimeError("docker command not found") from exc
        try:
            stdout, _stderr = await process.communicate()
        except asyncio.CancelledError:
            await asyncio.shield(_terminate_process(process))
            raise
    finally:
        if log_stream is not None:
            log_stream.close()
    if process.returncode != 0:
        suffix = f"; see {log_path}" if log_path is not None else ""
        raise RuntimeError(f"docker exited with code {process.returncode}{suffix}")
    container_id = stdout.decode().strip()
    if not container_id:
        raise RuntimeError("docker did not return a container id")
    return container_id


async def finish_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        await process.wait()
        return
    await _terminate_process(process)


async def _terminate_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    with suppress(ProcessLookupError):
        process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=2.0)
    except TimeoutError:
        with suppress(ProcessLookupError):
            process.kill()
        await process.wait()


def _docker(
    *args: str,
    check: bool,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            ("docker", *args),
            check=check,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return None


def docker_container_states(identities: tuple[str, ...]) -> dict[str, bool]:
    """Batch bounded inspection; missing/unknown results never imply death."""
    if not identities:
        return {}
    try:
        result = _docker(
            "inspect",
            "--format",
            "{{.Id}} {{.Name}} {{.State.Running}}",
            *identities,
            check=False,
            timeout=2.0,
        )
    except subprocess.TimeoutExpired:
        return {}
    if result is None:
        return {}
    states = {}
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) != 3 or parts[2] not in {"true", "false"}:
            continue
        for identity in identities:
            if identity in {parts[0], parts[1].lstrip("/")}:
                running = parts[2] == "true"
                try:
                    if running or not docker_launch_pending(identity):
                        states[identity] = running
                except RuntimeError:
                    pass
    if result.returncode != 0:
        for identity in identities:
            if any(
                line.strip()
                in {
                    f"Error: No such object: {identity}",
                    f"Error: No such container: {identity}",
                }
                for line in result.stderr.splitlines()
            ):
                try:
                    if not docker_launch_pending(identity):
                        states[identity] = False
                except RuntimeError:
                    pass
    return states
