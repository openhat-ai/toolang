"""Sandbox directly on the current machine."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import cache
import os
from pathlib import Path
import platform
import signal
import subprocess
import sys
import time
from typing import Any

from toolang.base.errors import SandboxLaunchError
from toolang.base.protocols.sandbox import Sandbox
from toolang.base.types.sandbox import (
    SandboxLocation,
    SandboxPlan,
    SandboxRef,
    SandboxRequest,
)

HOST_SANDBOX_DESCRIPTION_ENV = "TOOLANG_SANDBOX_DESCRIPTION"


@dataclass(slots=True)
class HostSandbox:
    """Run the AgentServer as a local child process."""

    config: dict[str, Any]
    name: str = "host"
    location: SandboxLocation = "host"
    _processes: dict[int, subprocess.Popen[bytes]] = field(
        default_factory=dict, init=False, repr=False
    )

    def runtime_root(self, local_root: Path) -> Path:
        """Use the caller's Toolang root without path translation."""

        return local_root

    def prepare(self, spec: str | None, request: SandboxRequest) -> SandboxPlan:
        if spec is not None:
            raise ValueError("host sandbox does not accept a spec")
        envs = dict(request.envs)
        envs[HOST_SANDBOX_DESCRIPTION_ENV] = host_sandbox_description()
        return SandboxPlan(
            sandbox=self.name,
            command=_local_command(request.command),
            working_directory=request.working_directory,
            output=request.output,
            log_path=request.log_path,
            endpoint=request.endpoint,
            envs=envs,
        )

    async def launch(self, plan: SandboxPlan) -> SandboxRef:
        worker = asyncio.create_task(asyncio.to_thread(_launch, plan))
        try:
            process = await asyncio.shield(worker)
        except asyncio.CancelledError as exc:
            process = await worker
            ref = _process_ref(process, plan.endpoint)
            try:
                stopped = await asyncio.to_thread(_stop_process, process, force=True)
                if not stopped:
                    raise RuntimeError(f"agent process did not stop: {process.pid}")
            except BaseException as cleanup_exc:
                self._processes[process.pid] = process
                raise SandboxLaunchError(
                    "Could not cancel host sandbox launch and stop its workload "
                    f"{process.pid}: {cleanup_exc}",
                    ref=ref,
                ) from exc
            raise
        self._processes[process.pid] = process
        return _process_ref(process, plan.endpoint)

    async def attach(
        self,
        plan: SandboxPlan,
        ref: SandboxRef,
    ) -> None:
        """Keep inherited host streams attached by the launched process itself."""

        del plan, ref

    async def detach(self, ref: SandboxRef) -> None:
        """Keep inherited host streams attached to their launched process."""

        del ref

    async def running(self, ref: SandboxRef) -> bool:
        pid = _pid(ref)
        process = self._processes.get(pid)
        if process is not None:
            return process.poll() is None
        return _ref_process_running(ref, pid)

    async def wait(self, ref: SandboxRef) -> int:
        pid = _pid(ref)
        process = self._processes.get(pid)
        if process is not None:
            return await asyncio.to_thread(process.wait)
        while _ref_process_running(ref, pid):
            await asyncio.sleep(0.1)
        return 0

    async def stop(self, ref: SandboxRef, *, force: bool = False) -> None:
        pid = _pid(ref)
        process = self._processes.get(pid)
        if process is not None:
            stopped = await asyncio.to_thread(
                _stop_process,
                process,
                force=force,
            )
        else:
            if _pid_running(pid) and not _ref_process_running(ref, pid):
                raise ValueError(f"host sandbox reference no longer matches pid: {pid}")
            stopped = await asyncio.to_thread(
                _stop_pid,
                pid,
                force=force,
            )
        if not stopped:
            raise ValueError(f"agent process did not stop: {pid}; retry with --force")

    async def release(self, ref: SandboxRef) -> None:
        self._processes.pop(_pid(ref), None)


def create_sandbox(config: Mapping[str, Any]) -> Sandbox:
    """Create built-in local sandbox."""

    return HostSandbox(dict(config))


@cache
def host_sandbox_description() -> str:
    """Return one compact, human-readable description of the current host."""

    system = _fact(platform.system()) or "Unknown"
    machine = _fact(platform.machine()) or "unknown"
    if system == "Darwin":
        release, _version_info, detected_machine = platform.mac_ver()
        return _description(
            "macOS",
            _fact(release),
            machine if machine != "unknown" else _fact(detected_machine),
        )
    if system == "Linux":
        try:
            release_info = platform.freedesktop_os_release()
        except OSError:
            name = "Linux"
            release = _fact(platform.release())
        else:
            name = _fact(release_info.get("NAME", "Linux")) or "Linux"
            release = _fact(
                release_info.get("VERSION_ID", "") or release_info.get("VERSION", "")
            )
        return _description(name, release, machine)
    if system == "Windows":
        release, _version, _service_pack, _product_type = platform.win32_ver()
        return _description("Windows", _fact(release), machine)
    return _description(system, _fact(platform.release()), machine)


def _description(name: str, version: str, machine: str) -> str:
    return " ".join(value for value in (name, version, machine) if value)


def _fact(value: str) -> str:
    return " ".join(value.split())


def _launch(plan: SandboxPlan) -> subprocess.Popen[bytes]:
    if not plan.command:
        raise ValueError("host sandbox requires a command")
    if plan.output == "inherit":
        return subprocess.Popen(
            plan.command,
            stdin=subprocess.DEVNULL,
            env=dict(plan.envs),
            cwd=str(plan.working_directory),
            start_new_session=True,
            close_fds=True,
        )
    if plan.output != "file" or plan.log_path is None:
        raise ValueError("host file output requires a log path")
    plan.log_path.parent.mkdir(parents=True, exist_ok=True)
    with plan.log_path.open("ab") as stream:
        return subprocess.Popen(
            plan.command,
            stdin=subprocess.DEVNULL,
            stdout=stream,
            stderr=stream,
            env=dict(plan.envs),
            cwd=str(plan.working_directory),
            start_new_session=True,
            close_fds=True,
        )


def _local_command(command: tuple[str, ...]) -> tuple[str, ...]:
    if not command:
        raise ValueError("host sandbox requires a command")
    if command[0] not in {"too", "toolang"}:
        return command
    return (sys.executable, "-m", "toolang.cli.toolang", *command[1:])


def _process_ref(process: subprocess.Popen[bytes], endpoint: str) -> SandboxRef:
    identity = _process_identity(process.pid)
    return SandboxRef(
        runtime_id=str(process.pid),
        endpoint=endpoint,
        runtime_kind="process",
        meta={
            "pid": process.pid,
            **({"identity": identity} if identity is not None else {}),
        },
    )


def _pid(ref: SandboxRef) -> int:
    try:
        pid = int(ref.runtime_id)
    except ValueError as exc:
        raise ValueError(f"invalid host sandbox pid: {ref.runtime_id}") from exc
    if pid <= 0:
        raise ValueError(f"invalid host sandbox pid: {ref.runtime_id}")
    return pid


def _pid_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _ref_process_running(ref: SandboxRef, pid: int) -> bool:
    if not _pid_running(pid):
        return False
    expected = ref.meta.get("identity")
    if not isinstance(expected, str) or not expected:
        return True
    return _process_identity(pid) == expected


def _process_identity(pid: int) -> str | None:
    try:
        result = subprocess.run(
            ("ps", "-p", str(pid), "-o", "lstart="),
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return None
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def _stop_pid(pid: int, *, force: bool) -> bool:
    if not _pid_running(pid):
        return True
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except PermissionError as exc:
        raise ValueError(f"permission denied while stopping pid {pid}") from exc
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if not _pid_running(pid):
            return True
        time.sleep(0.05)
    if not force:
        return False
    try:
        os.killpg(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
    except ProcessLookupError:
        return True
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if not _pid_running(pid):
            return True
        time.sleep(0.05)
    return not _pid_running(pid)


def _stop_process(
    process: subprocess.Popen[bytes],
    *,
    force: bool,
) -> bool:
    if process.poll() is not None:
        return True
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=2)
        return True
    except ProcessLookupError:
        return True
    except PermissionError as exc:
        raise ValueError(f"permission denied while stopping pid {process.pid}") from exc
    except subprocess.TimeoutExpired:
        if not force:
            return False
    try:
        os.killpg(
            process.pid,
            getattr(signal, "SIGKILL", signal.SIGTERM),
        )
        process.wait(timeout=2)
        return True
    except PermissionError as exc:
        raise ValueError(
            f"permission denied while force-stopping pid {process.pid}"
        ) from exc
    except (ProcessLookupError, subprocess.TimeoutExpired):
        return process.poll() is not None
