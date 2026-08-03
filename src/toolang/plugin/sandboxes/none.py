"""Hosting directly on the current machine."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
import os
import signal
import subprocess
import sys
import time
from typing import Any

from toolang.base.protocols.hosting import Hosting
from toolang.base.types.hosting import HostingPlan, HostingRef, HostingRequest


@dataclass(slots=True)
class NoneHosting:
    """Run the AgentServer as a local child process."""

    config: dict[str, Any]
    name: str = "none"
    _processes: dict[int, subprocess.Popen[bytes]] = field(
        default_factory=dict, init=False, repr=False
    )

    def prepare(self, spec: str | None, request: HostingRequest) -> HostingPlan:
        if spec is not None:
            raise ValueError("none sandbox does not accept a spec")
        return HostingPlan(
            sandbox=self.name,
            command=_local_command(request.command),
            working_directory=request.working_directory,
            log_path=request.log_path,
            endpoint=request.endpoint,
            envs=dict(request.envs),
        )

    async def launch(self, plan: HostingPlan) -> HostingRef:
        process = await asyncio.to_thread(_launch, plan)
        self._processes[process.pid] = process
        return HostingRef(
            runtime_id=str(process.pid),
            endpoint=plan.endpoint,
            meta={
                "pid": process.pid,
                "identity": _process_identity(process.pid),
            },
        )

    async def running(self, ref: HostingRef) -> bool:
        pid = _pid(ref)
        process = self._processes.get(pid)
        if process is not None:
            return process.poll() is None
        return _ref_process_running(ref, pid)

    async def wait(self, ref: HostingRef) -> int:
        pid = _pid(ref)
        process = self._processes.get(pid)
        if process is not None:
            return await asyncio.to_thread(process.wait)
        while _ref_process_running(ref, pid):
            await asyncio.sleep(0.1)
        return 0

    async def stop(self, ref: HostingRef, *, force: bool = False) -> None:
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
                raise ValueError(
                    f"local hosting reference no longer matches pid: {pid}"
                )
            stopped = await asyncio.to_thread(
                _stop_pid,
                pid,
                force=force,
            )
        if not stopped:
            raise ValueError(f"agent process did not stop: {pid}; retry with --force")

    async def release(self, ref: HostingRef) -> None:
        self._processes.pop(_pid(ref), None)


def create_hosting(config: Mapping[str, Any]) -> Hosting:
    """Create built-in local hosting."""

    return NoneHosting(dict(config))


def _launch(plan: HostingPlan) -> subprocess.Popen[bytes]:
    if not plan.command:
        raise ValueError("none hosting requires a command")
    if plan.log_path is None:
        return subprocess.Popen(
            plan.command,
            stdin=subprocess.DEVNULL,
            env=dict(plan.envs),
            cwd=str(plan.working_directory),
            start_new_session=True,
            close_fds=True,
        )
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
        raise ValueError("none hosting requires a command")
    if command[0] not in {"too", "toolang"}:
        return command
    return (sys.executable, "-m", "toolang.cli.toolang", *command[1:])


def _pid(ref: HostingRef) -> int:
    try:
        pid = int(ref.runtime_id)
    except ValueError as exc:
        raise ValueError(f"invalid local hosting pid: {ref.runtime_id}") from exc
    if pid <= 0:
        raise ValueError(f"invalid local hosting pid: {ref.runtime_id}")
    return pid


def _pid_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _ref_process_running(ref: HostingRef, pid: int) -> bool:
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
