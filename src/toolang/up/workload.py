"""Workload state transitions shared by every placement and execution host."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
import os
from pathlib import Path
import time
from uuid import uuid4

import psutil

from toolang.common.layout import AgentLayout
from .identity import identity_bindings, visiting_home_guard
from .instance import (
    control_owner,
    initialize_instance,
    patch_workload,
    process_alive,
    read_instance,
    remove_finished_instance,
    update_instance,
)
from .registry import RuntimeRegistration, remove_registration, finalize_runtime
from .types import ALL_DRIVERS, ExecutionDrivers


def prepare_workload(
    layout: AgentLayout,
    *,
    launch_id: str,
    requested_at: float,
    owner: Mapping[str, int | float],
) -> None:
    """Initialize status only after allocation exclusively creates a new place."""
    initialize_instance(
        layout.runtime_status,
        launch_id=launch_id,
        control=identity_bindings(layout),
        workload={
            "status": "preparing",
            "requested_at": requested_at,
            "pid": owner["pid"],
            "process_created": owner["created"],
        },
    )


def fail_workload(
    layout: AgentLayout,
    launch_id: str,
    message: str,
    *,
    updated_at: str | None = None,
    location: str = "host",
) -> None:
    """Record failure only for the caller's launch, preserving recovery references."""
    fields: dict[str, object] = {"status": "failed", "pid": None, "message": message}
    if updated_at is not None:
        fields["updated_at"] = updated_at
    patch_workload(
        layout.runtime_status,
        launch_id,
        fields,
        instance=layout.instance,
        location=location,
    )


def owns_preparation(layout: AgentLayout, entry: dict) -> bool:
    """Recognize an unstarted identity published by this exact caller process."""
    if layout.place_code is None:
        return False
    control = entry.get("control", {})
    workload = entry.get("workload", {})
    owner = control_owner()
    return (
        control.get("place_code") == layout.place_code
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
    root = toolang_root.expanduser().resolve()
    if layout.root != root and (
        layout.root is not None or layout.place_code is not None
    ):
        raise ValueError("instance belongs to a different Toolang root")
    if layout.placement == "resident" and layout.place != root:
        raise ValueError("resident agent root does not match its Toolang root")

    def begin(entry):
        entry["launch_id"] = launch_id
        entry["control"].update(identity_bindings(layout))
        entry["control"].update(
            runtime_instance=layout.instance,
            root=str(root),
            sandbox=sandbox,
            endpoint=endpoint,
            drivers=sorted(drivers),
        )
        if startup_contract is not None:
            entry["control"]["startup_contract"] = dict(startup_contract)
        if layout.instance is not None:
            entry["control"]["owner"] = control_owner()
        entry["workload"] = dict(workload)

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
            self._registration.publish()
        except BaseException:
            self.close()
            raise
        return self

    def close(self) -> None:
        finalize_runtime(self.layout, self.token, expected_pid=os.getpid())
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


def fail_preparation(
    layout: AgentLayout, error: BaseException, *, expected_launch_id: str | None = None
) -> None:
    """Retain failed identities without leaving an unowned active reference."""
    entry = read_instance(layout.runtime_status)
    if (
        entry is not None
        and owns_preparation(layout, entry)
        and (expected_launch_id is None or entry["launch_id"] == expected_launch_id)
    ):
        patch_workload(
            layout.runtime_status,
            entry["launch_id"],
            {"status": "failed", "pid": None, "message": str(error)},
        )
        remove_registration(layout, entry["launch_id"])


# Durable ownership replaces locks held across process creation/readiness/shutdown.
_OPERATIONS: ContextVar[tuple[tuple[Path, str | None], ...]] = ContextVar(
    "runtime_operations", default=()
)


@contextmanager
def management_operation(
    layout: AgentLayout, *, stage: str, on_wait: Callable[[], None] | None = None
) -> Iterator[None]:
    key = (layout.runtime_status, layout.instance)
    held = _OPERATIONS.get()
    if key in held:
        yield
        return
    if layout.placement == "visiting":
        with visiting_home_guard(layout):
            pass
    owner = {
        "token": uuid4().hex,
        **control_owner(),
        "stage": stage,
    }
    claimed = False
    while not claimed:

        def claim(entry):
            nonlocal claimed
            previous = entry["control"].get("operation")
            if previous is not None:
                alive = process_alive(previous.get("pid"), previous.get("created"))
                if alive is None:
                    raise ValueError("cannot verify the runtime control owner")
                if alive:
                    return
            entry["control"]["operation"] = owner
            claimed = True

        update_instance(
            layout.runtime_status, claim, instance=layout.instance, on_wait=on_wait
        )
        if not claimed:
            if on_wait is not None:
                on_wait()
            time.sleep(0.05)
    token = _OPERATIONS.set((*held, key))
    try:
        yield
    finally:
        _OPERATIONS.reset(token)
        completed_launch: str | None = None

        def release(entry):
            nonlocal completed_launch
            if entry["control"].get("operation") == owner:
                completed_launch = entry["launch_id"]
                entry["control"].pop("operation", None)

        if layout.runtime_status.exists():
            update_instance(layout.runtime_status, release, instance=layout.instance)
            if completed_launch is not None:
                remove_finished_instance(layout, completed_launch)


def write_runtime_state(
    layout: AgentLayout,
    *,
    endpoint: str,
    started_at: str,
    pid: int | None,
    sandbox: str = "host",
    process_created: object = None,
    sandbox_description: str | None = None,
    sandbox_instance: str | None = None,
    models: Sequence[str] | None = None,
    drivers: Sequence[str] | None = None,
    workspace_additions: Mapping[str, str] | None = None,
    status: str = "running",
    message: str | None = None,
    launch_id: str | None = None,
    location: str = "host",
    lifetime: str | None = None,
    api_version: int | None = None,
    resource_counts: Mapping[str, object] | None = None,
) -> Path:
    """Persist one minimal runtime state file for a running agent."""

    path = layout.runtime_status
    payload: dict[str, object] = {
        "agent": layout.name,
        "status": status,
        "endpoint": endpoint,
        "started_at": started_at,
        "updated_at": started_at,
        "pid": pid,
        "process_created": process_created,
        "sandbox": sandbox,
        "models": list(models or ()),
        "drivers": sorted(drivers if drivers is not None else ALL_DRIVERS),
        "workspace_additions": dict(workspace_additions or {}),
        "message": message,
    }
    if resource_counts is not None:
        payload["resource_counts"] = dict(resource_counts)
    if lifetime is not None:
        payload["lifetime"] = lifetime
        payload["api_version"] = api_version
    if sandbox_instance is not None:
        payload["sandbox_instance"] = sandbox_instance
    if sandbox_description is not None:
        payload["sandbox_description"] = sandbox_description
    entry = read_instance(path, layout.instance, location=location)
    identity = launch_id or (entry or {}).get("launch_id")
    if not identity:
        identity = uuid4().hex
        update_instance(
            path,
            lambda entry: entry.update(launch_id=identity),
            instance=layout.instance,
            location=location,
        )
    patch_workload(path, identity, payload, instance=layout.instance, location=location)
    return path


def stop_runtime_state(
    layout: AgentLayout,
    *,
    expected_pid: int | None = None,
    expected_started_at: str | None = None,
    launch_id: str | None = None,
    location: str = "host",
) -> bool:
    """Mark one runtime state as stopped while keeping the last endpoint."""

    path = layout.runtime_status
    changed = False

    def finish(entry):
        nonlocal changed
        state = entry["workload"]
        if expected_pid is not None and state.get("pid") != expected_pid:
            return
        if (
            expected_started_at is not None
            and state.get("started_at") != expected_started_at
        ):
            return
        if state.get("status") not in {"running", "starting", "preparing"}:
            return
        state.update(
            status="stopped",
            pid=None,
            updated_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )
        changed = True

    update_instance(
        path, finish, instance=layout.instance, launch_id=launch_id, location=location
    )
    return changed
