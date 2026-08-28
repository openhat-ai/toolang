"""Small cancellable Docker CLI adapter used by the sandbox plugin."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
import subprocess

from toolang.base.types.sandbox import SandboxMount

DEFAULT_HOST_GATEWAY = "host.docker.internal"
DOCKER_DIAGNOSTIC_TAIL_LINES = 2000


def docker_container_running(container_id: str) -> bool:
    result = _docker(
        "inspect",
        "--format",
        "{{.State.Running}}",
        container_id,
        check=False,
    )
    return (
        result is not None
        and result.returncode == 0
        and result.stdout.strip() == "true"
    )


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
    command = "kill" if force else "stop"
    result = _docker(command, container_id, check=False)
    if result is None:
        raise RuntimeError("docker command not found")
    if result.returncode != 0:
        detail = result.stderr.strip()
        if "No such container" not in detail:
            raise RuntimeError(detail or f"docker {command} failed")


def docker_remove_container(container_id: str) -> None:
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
    for mount in mounts:
        suffix = ":ro" if mount.read_only else ""
        args.extend(["--volume", f"{mount.local_path}:{mount.hosted_path}{suffix}"])
    for name, value in env_values.items():
        args.extend(["--env", f"{name}={value}"])
    args.append(image)
    args.extend(command)
    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("docker command not found") from exc
    try:
        stdout, stderr = await process.communicate()
    except asyncio.CancelledError:
        await asyncio.shield(_terminate_process(process))
        raise
    if log_path is not None and stderr:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("ab") as stream:
            stream.write(stderr)
    if process.returncode != 0:
        detail = _bounded_diagnostic(stderr)
        suffix = f"; see {log_path}" if log_path is not None else ""
        reason = f": {detail}" if detail else ""
        raise RuntimeError(
            f"docker exited with code {process.returncode}{reason}{suffix}"
        )
    container_id = stdout.decode().strip()
    if not container_id:
        raise RuntimeError("docker did not return a container id")
    return container_id


def _bounded_diagnostic(content: bytes) -> str:
    text = " ".join(content.decode("utf-8", errors="replace").split())
    return text if len(text) <= 240 else text[-239:] + "…"


async def finish_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        await process.wait()
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=2.0)
    except TimeoutError:
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
) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            ("docker", *args),
            check=check,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return None
