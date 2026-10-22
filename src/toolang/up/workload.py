"""Workload state transitions shared by every placement and execution host."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import nullcontext
import os
from pathlib import Path
from uuid import uuid4

import psutil

from toolang.common.layout import AgentLayout
from .identity import identity_bindings, visiting_home_guard
from .instance import (
    control_owner,
    patch_workload,
    read_instance,
    remove_finished_instance,
    update_instance,
)
from .observation import RuntimeRegistration, remove_registration
from .types import ExecutionDrivers


def owns_preparation(layout: AgentLayout, entry: dict) -> bool:
    """Recognize an unstarted identity published by this exact caller process."""
    if layout.instance_id is None:
        return False
    control = entry.get("control", {})
    workload = entry.get("workload", {})
    owner = control_owner()
    return (
        control.get("instance_id") == layout.instance_id
        and "startup_contract" not in control
        and control.get("ref") is None
        and workload.get("status") == "preparing"
        and workload.get("pid") == owner["pid"]
        and workload.get("process_created") == owner["created"]
    )


def begin_workload(
    layout: AgentLayout,
    *,
    launch_id: str,
    toolang_root: Path,
    sandbox: str,
    endpoint: str,
    drivers: ExecutionDrivers,
    workload: Mapping[str, object],
    startup_contract: Mapping[str, str] | None = None,
) -> None:
    """Bind one generation to its identity before any execution is admitted."""

    def begin(entry):
        entry["launch_id"] = launch_id
        entry["control"].update(identity_bindings(layout))
        entry["control"].update(
            runtime_instance=layout.instance,
            toolang_root=str(toolang_root),
            sandbox=sandbox,
            endpoint=endpoint,
            drivers=sorted(drivers),
        )
        if startup_contract is not None:
            entry["control"]["startup_contract"] = dict(startup_contract)
        if layout.instance is not None:
            entry["control"]["owner"] = control_owner()
        entry["workload"] = dict(workload)

    if layout.instance_id is not None and layout.toolang_root != toolang_root.resolve():
        raise ValueError("instance belongs to a different Toolang root")
    guard = (
        visiting_home_guard(layout) if layout.placement == "visiting" else nullcontext()
    )
    with guard:
        update_instance(layout.runtime_status, begin, instance=layout.instance)


class ForegroundWorkload:
    """Own an embedded workload; discovery publication has a separate lifetime."""

    def __init__(
        self,
        layout: AgentLayout,
        *,
        toolang_root: Path,
        drivers: ExecutionDrivers,
        endpoint: str = "",
    ) -> None:
        self.layout = layout
        self.token = uuid4().hex
        self.toolang_root = toolang_root
        self.drivers = drivers
        self.endpoint = endpoint
        self._registration = RuntimeRegistration(
            layout, toolang_root=toolang_root, launch_id=self.token
        )

    @property
    def path(self) -> Path:
        return self._registration.path

    def __enter__(self) -> ForegroundWorkload:
        begin_workload(
            self.layout,
            launch_id=self.token,
            toolang_root=self.toolang_root,
            sandbox="host",
            endpoint=self.endpoint,
            drivers=self.drivers,
            workload={
                "pid": os.getpid(),
                "process_created": psutil.Process().create_time(),
                "status": "running",
                "endpoint": self.endpoint,
            },
        )
        try:
            self._registration.__enter__()
        except BaseException:
            self.close()
            raise
        return self

    def close(self) -> None:
        from .process import stop_runtime_state

        stop_runtime_state(self.layout, expected_pid=os.getpid(), launch_id=self.token)
        self._registration.close()
        remove_finished_instance(self.layout, self.token)

    def __exit__(self, *exc: object) -> None:
        self.close()


def finish_preparation(layout: AgentLayout, message: str) -> None:
    """Finalize an owned command that returned without starting its workload."""
    entry = read_instance(layout.runtime_status)
    if entry is not None and entry["workload"].get("status") in {
        "preparing",
        "starting",
    }:
        fail_preparation(layout, RuntimeError(message))


def fail_preparation(layout: AgentLayout, error: BaseException) -> None:
    """Retain failed identities without leaving an unowned active reference."""
    entry = read_instance(layout.runtime_status)
    if entry is not None and not entry["control"].get("ref"):
        patch_workload(
            layout.runtime_status,
            entry["launch_id"],
            {"status": "failed", "pid": None, "message": str(error)},
        )
        remove_registration(layout, entry["launch_id"])
