"""Small cancellable Docker CLI adapter used by the sandbox plugin."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import os
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
import subprocess
import re
import time
from typing import cast
import psutil

from toolang.base.types.sandbox import SandboxMount, SandboxRef
from toolang.common.layout import validate_agent_name
from toolang.common.place_codes import decode_place_code

DEFAULT_HOST_GATEWAY = "host.docker.internal"
DOCKER_DIAGNOSTIC_TAIL_LINES = 2000
DOCKER_REMOVE_TIMEOUT_SEC = 10.0
_OWNED_INSPECT_FORMAT = (
    "[{{json .Id}},{{json .Name}},{{json .State.Running}},{{json .Config.Labels}}]"
)
_FOREGROUND_CONTROLLERS: dict[str, subprocess.Popen[bytes]] = {}


def docker_identity_labels(identity: object) -> dict[str, str]:
    """Validate the persisted Docker identity before using it for ownership."""
    if not isinstance(identity, dict):
        raise ValueError("invalid docker identity metadata")
    data = cast(dict[str, object], identity)
    if (
        set(data) != {"version", "root", "address", "launch_id"}
        or type(data.get("version")) is not int
        or data["version"] != 1
    ):
        raise ValueError("invalid docker identity metadata")
    root_value, address, launch_id = data["root"], data["address"], data["launch_id"]
    if (
        not isinstance(root_value, str)
        or not root_value
        or not isinstance(address, str)
        or not address
        or not isinstance(launch_id, str)
        or not launch_id
    ):
        raise ValueError("invalid docker identity metadata")
    root = Path(root_value)
    if not root.is_absolute() or str(root.resolve()) != root_value:
        raise ValueError("docker identity requires a canonical Toolang root")
    agent, separator, code = address.partition("@")
    validate_agent_name(agent)
    if separator:
        decode_place_code(code)
    return {
        "io.toolang.root": root_value,
        "io.toolang.address": address,
        "io.toolang.launch-id": launch_id,
    }


@dataclass(frozen=True, slots=True)
class _Container:
    id: str
    running: bool
    labels: dict[str, str]


def _inspect_containers(identities: tuple[str, ...]) -> dict[str, _Container | None]:
    """Return only conclusive, exact-ID/name matches from one bounded inspection."""
    if not identities:
        return {}
    try:
        result = _docker(
            "inspect",
            "--type",
            "container",
            "--format",
            _OWNED_INSPECT_FORMAT,
            *identities,
            check=False,
            timeout=2.0,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("docker inspect timed out") from exc
    if result is None:
        raise RuntimeError("docker command not found")
    containers: dict[str, _Container | None] = {}
    for line in result.stdout.splitlines():
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if not isinstance(data, list) or len(data) != 4:
            continue
        container_id, name, running, labels = data
        if (
            not isinstance(container_id, str)
            or re.fullmatch(r"[0-9a-f]{64}", container_id) is None
            or not isinstance(name, str)
            or type(running) is not bool
            or not isinstance(labels, dict)
            or any(
                not isinstance(k, str) or not isinstance(v, str)
                for k, v in labels.items()
            )
        ):
            continue
        for identity in identities:
            if identity in {container_id, name.removeprefix("/")}:
                containers[identity] = _Container(container_id, running, labels)
    if result.returncode != 0:
        for identity in identities:
            if identity not in containers and _container_missing(
                result.stderr, identity
            ):
                containers[identity] = None
    return containers


def _owned_container(
    ref: SandboxRef, containers: Mapping[str, _Container | None]
) -> _Container | None:
    expected = docker_identity_labels(ref.meta["docker_identity"])
    if ref.runtime_id not in containers:
        raise RuntimeError(
            f"cannot verify docker container ownership: {ref.runtime_id}"
        )
    container = containers[ref.runtime_id]
    if container is not None and any(
        container.labels.get(k) != v for k, v in expected.items()
    ):
        raise RuntimeError(f"docker container ownership mismatch: {ref.runtime_id}")
    if (container is None or not container.running) and docker_launch_pending(
        ref.runtime_name or ref.runtime_id
    ):
        raise RuntimeError("docker container creation is still pending")
    return container


def docker_reference_id(ref: SandboxRef) -> str | None:
    """Resolve owned names once; subsequent operations must use the immutable ID."""
    if "docker_identity" not in ref.meta:
        return ref.runtime_id  # Existing references keep their original locators.
    docker_identity_labels(ref.meta["docker_identity"])
    container = _owned_container(ref, _inspect_containers((ref.runtime_id,)))
    return container.id if container is not None else None


def docker_reference_running(ref: SandboxRef) -> bool:
    if "docker_identity" not in ref.meta:
        return docker_container_running(ref.runtime_id)
    docker_identity_labels(ref.meta["docker_identity"])
    container = _owned_container(ref, _inspect_containers((ref.runtime_id,)))
    return container is not None and container.running


def docker_reference_states(refs: tuple[SandboxRef, ...]) -> tuple[bool | None, ...]:
    """Batch liveness without letting reused names satisfy stale generations."""
    legacy = docker_container_states(
        tuple(
            dict.fromkeys(
                ref.runtime_id for ref in refs if "docker_identity" not in ref.meta
            )
        )
    )
    identities = tuple(
        dict.fromkeys(ref.runtime_id for ref in refs if "docker_identity" in ref.meta)
    )
    try:
        containers = _inspect_containers(identities)
    except (OSError, RuntimeError):
        containers = {}
    states: list[bool | None] = []
    for ref in refs:
        if "docker_identity" not in ref.meta:
            states.append(legacy.get(ref.runtime_id))
            continue
        try:
            container = _owned_container(ref, containers)
            states.append(container is not None and container.running)
        except (OSError, RuntimeError, ValueError):
            states.append(None)
    return tuple(states)


def _container_missing(stderr: str, container_id: str) -> bool:
    for line in stderr.splitlines():
        detail, separator, identity = line.strip().rpartition(": ")
        if (
            separator
            and detail.lower().endswith(("no such object", "no such container"))
            and identity == container_id
        ):
            return True
    return False


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
        if _container_missing(result.stderr, container_id):
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
        if not _container_missing(detail, container_id) and docker_container_running(
            container_id
        ):
            raise RuntimeError(detail or f"docker {command} failed")


def docker_remove_container(container_id: str) -> None:
    if docker_launch_pending(container_id):
        raise RuntimeError("docker container creation is still pending")
    deadline = time.monotonic() + DOCKER_REMOVE_TIMEOUT_SEC
    while True:
        try:
            result = _docker(
                "rm",
                "--force",
                container_id,
                check=False,
                timeout=max(0.1, deadline - time.monotonic()),
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("docker rm timed out") from exc
        if result is None:
            raise RuntimeError("docker command not found")
        if result.returncode == 0 or _container_missing(result.stderr, container_id):
            return
        detail = result.stderr.strip()
        if (
            "removal of container" in detail
            and "already in progress" in detail
            and time.monotonic() < deadline
        ):
            time.sleep(0.1)
            continue
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
    labels: Mapping[str, str] | None = None,
    foreground: bool = False,
) -> str:
    args = [
        "docker",
        "run",
        "--interactive" if foreground else "--detach",
        "--name",
        container_name,
        "--workdir",
        workdir,
        "--publish",
        f"{bind_host}:{published_port}:{hosted_port}",
        "--add-host",
        f"{DEFAULT_HOST_GATEWAY}:host-gateway",
    ]
    if foreground:
        args.extend(["--sig-proxy=false", "--stop-timeout", "10"])
    if remove_on_exit:
        args.append("--rm")
    for name, value in (labels or {}).items():
        args.extend(["--label", f"{name}={value}"])
    for mount in mounts:
        suffix = ":ro" if mount.read_only else ""
        args.extend(["--volume", f"{mount.local_path}:{mount.hosted_path}{suffix}"])
    for name, value in env_values.items():
        args.extend(["--env", f"{name}={value}"])
    args.append(image)
    args.extend(command)
    if foreground:
        return await _run_foreground(args, container_name, labels or {}, log_path)
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


async def _run_foreground(
    args: list[str], name: str, labels: Mapping[str, str], log_path: Path | None
) -> str:
    """Retain a real stdin pipe across the controller's separate event loops."""
    stream = log_path.open("ab") if log_path is not None else None
    try:
        process = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=stream or subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    finally:
        if stream is not None:
            stream.close()
    _FOREGROUND_CONTROLLERS[name] = process
    try:
        while True:
            containers = await asyncio.to_thread(_inspect_containers, (name,))
            container = containers.get(name)
            if container is not None:
                if any(
                    container.labels.get(key) != value for key, value in labels.items()
                ):
                    raise RuntimeError(f"docker container ownership mismatch: {name}")
                _FOREGROUND_CONTROLLERS[container.id] = _FOREGROUND_CONTROLLERS.pop(
                    name
                )
                return container.id
            if process.poll() is not None:
                detail = ""
                if log_path is not None:
                    with log_path.open("rb") as diagnostic:
                        diagnostic.seek(0, os.SEEK_END)
                        diagnostic.seek(max(0, diagnostic.tell() - 16_384))
                        detail = diagnostic.read().decode(errors="replace").strip()
                raise RuntimeError(
                    f"foreground docker run exited before creation: {process.returncode}"
                    + (f"; {detail}" if detail else "")
                )
            await asyncio.sleep(0.05)
    except BaseException:
        _FOREGROUND_CONTROLLERS.pop(name, None)
        if process.stdin is not None:
            process.stdin.close()
        if process.poll() is None:
            process.terminate()
            await asyncio.to_thread(process.wait, timeout=2)
        raise


def docker_release_controller(container_id: str) -> None:
    """Close the owned channel only after workload shutdown, and reap its client."""
    process = _FOREGROUND_CONTROLLERS.pop(container_id, None)
    if process is None:
        return
    if process.stdin is not None:
        process.stdin.close()
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


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
            if _container_missing(result.stderr, identity):
                try:
                    if not docker_launch_pending(identity):
                        states[identity] = False
                except RuntimeError:
                    pass
    return states
