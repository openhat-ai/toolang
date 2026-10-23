"""Atomic, generation-checked agent instance files."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import os
import psutil
from toolang.common.layout import AgentLayout
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from toolang.base.types.sandbox import SandboxRef
from toolang.common.files import (
    atomic_write_text,
    file_lock_path,
    file_write_lock,
    file_read_snapshot,
    reap_file_lock,
)

RUNTIME_STATUS_FORMAT = 5


def initialize_instance(
    path: Path,
    *,
    launch_id: str,
    control: Mapping[str, object],
    workload: Mapping[str, object],
) -> None:
    """Write the first status document beneath an exclusively allocated new home."""
    payload = {
        "version": RUNTIME_STATUS_FORMAT,
        "instances": {
            "default": {
                "launch_id": launch_id,
                "control": dict(control),
                "workload": dict(workload),
            }
        },
    }
    atomic_write_text(path, json.dumps(payload))


def read_document(path: Path, *, location: str = "host") -> dict[str, Any] | None:
    """Read a status document with a validated format and instances container."""
    try:
        if location == "guest":
            with file_read_snapshot(path) as stream:
                payload = json.load(stream)
        else:
            payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise ValueError(f"invalid runtime status: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"invalid runtime status: {path}")
    if (
        type(payload.get("version")) is not int
        or payload["version"] != RUNTIME_STATUS_FORMAT
    ):
        raise ValueError(f"unsupported runtime status version: {path}")
    if not isinstance(payload.get("instances"), dict):
        raise ValueError(f"invalid runtime instances: {path}")
    return payload


def read_instance(
    path: Path, instance: str | None = None, *, location: str = "host"
) -> dict[str, Any] | None:
    payload = read_document(path, location=location)
    if payload is None:
        return None
    entry = payload["instances"].get(instance or "default")
    if entry is None:
        return None
    if (
        not isinstance(entry, dict)
        or not isinstance(entry.get("control"), dict)
        or not isinstance(entry.get("workload"), dict)
        or not isinstance(entry.get("launch_id"), str)
    ):
        raise ValueError(f"invalid runtime entry: {path}")
    return entry


def runtime_lock(
    path: Path,
    *,
    instance: str | None = None,
    launch_id: str | None = None,
    location: str = "host",
    blocking: bool = True,
    on_wait=None,
    dead_guest_launch: str | None = None,
    snapshot: dict[str, Any] | None = None,
):
    entry: dict[str, Any] = (
        snapshot
        or read_instance(path, instance, location=location)
        or {
            "control": {},
            "launch_id": "",
        }
    )
    from .registry import LockRecovery, _claim_owner

    owner = _claim_owner(location=location, entry=entry)
    owner["launch_id"] = launch_id or entry["launch_id"]
    recovery = LockRecovery(probes=1 if blocking else 0)
    if dead_guest_launch == entry["launch_id"]:
        recovery.remember(entry, False)
    claim = file_lock_path(path)
    try:
        with file_read_snapshot(claim) as stream:
            content = stream.read()
        previous = json.loads(content) if content.strip() else {}
    except FileNotFoundError:
        previous = None
    except (OSError, ValueError):
        previous = {}  # Legacy empty guards can only be recovered by the host.
    known_dead = isinstance(previous, dict) and recovery.dead(previous, owner)

    def dead(current):
        return (
            recovery.dead(current, owner)
            if blocking
            else known_dead and current == previous
        )

    return file_write_lock(
        file_lock_path(path),
        inherit_owner=True,
        blocking=blocking,
        on_wait=on_wait,
        shared_owner=owner,
        owner_dead=dead,
    )


def update_instance(
    path: Path,
    update: Callable[[dict[str, Any]], None],
    *,
    instance: str | None = None,
    launch_id: str | None = None,
    blocking: bool = True,
    on_wait: Callable[[], None] | None = None,
    location: str = "host",
    dead_guest_launch: str | None = None,
) -> bool:
    """Reread under the shared lock and replace only when data changed."""
    snapshot = read_instance(path, instance, location=location)
    if launch_id is not None and (
        snapshot is None or snapshot["launch_id"] != launch_id
    ):
        return False
    with runtime_lock(
        path,
        instance=instance,
        launch_id=launch_id,
        location=location,
        blocking=blocking,
        on_wait=on_wait,
        dead_guest_launch=dead_guest_launch,
        snapshot=snapshot,
    ):
        payload: dict[str, Any] | None = read_document(path, location=location)
        if payload is None:
            payload = {"version": RUNTIME_STATUS_FORMAT, "instances": {}}
        entries = payload["instances"]
        entry = entries.setdefault(
            instance or "default", {"launch_id": "", "control": {}, "workload": {}}
        )
        if launch_id is not None and entry["launch_id"] != launch_id:
            return False
        before = json.dumps(payload, sort_keys=True)
        previous_launch = entry["launch_id"]
        previous_phase = entry["workload"].get("status")
        update(entry)
        if (
            previous_launch
            and entry["launch_id"] == previous_launch
            and previous_phase in {"stopped", "failed"}
            and entry["workload"].get("status") in {"preparing", "starting", "running"}
        ):
            raise ValueError("terminal runtime launch cannot be reactivated")
        if json.dumps(payload, sort_keys=True) != before or not path.exists():
            atomic_write_text(
                path,
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
                + "\n",
                inherit_owner=True,
            )
        return True


def patch_workload(
    path: Path,
    launch_id: str,
    fields: dict[str, Any],
    *,
    instance: str | None = None,
    location: str = "host",
) -> bool:
    return update_instance(
        path,
        lambda entry: entry["workload"].update(fields),
        instance=instance,
        launch_id=launch_id,
        location=location,
    )


def remove_finished_instance(layout: AgentLayout, launch_id: str) -> bool:
    """Retire temporary control metadata after release; preserve database/log history."""
    if layout.instance is None:
        return False  # Persistent agent settings survive stop.
    path = layout.runtime_status
    snapshot = read_instance(path, layout.instance)
    if snapshot is None or snapshot["launch_id"] != launch_id:
        return False
    with runtime_lock(
        path, instance=layout.instance, launch_id=launch_id, snapshot=snapshot
    ):
        document = read_document(path)
        if document is None:
            return False
        entry = document["instances"].get(layout.instance)
        if (
            entry is None
            or entry["launch_id"] != launch_id
            or entry["control"].get("ref") is not None
            or entry["control"].get("operation") is not None
            or not (
                entry["workload"].get("status") in {"stopped", "failed"}
                or (not entry["launch_id"] and not entry["workload"])
            )
            or entry["workload"].get("pid") is not None
        ):
            return False
        del document["instances"][layout.instance]
        atomic_write_text(
            path,
            json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            inherit_owner=True,
        )
    return True


@dataclass(frozen=True, slots=True)
class SandboxState:
    """One control-owned resource reference bound to a startup generation."""

    sandbox: str
    ref: SandboxRef
    launch_id: str = ""

    def __post_init__(self) -> None:
        sandbox = self.sandbox.strip()
        if not sandbox:
            raise ValueError("sandbox state requires sandbox")
        object.__setattr__(self, "sandbox", sandbox)

    def save(self, path: Path, *, instance: str | None = None) -> None:
        def bind(entry: dict[str, Any]) -> None:
            if self.launch_id and entry["launch_id"] not in {"", self.launch_id}:
                raise ValueError("runtime launch ownership changed")
            if entry["workload"].get("status") in {"stopped", "failed"}:
                raise ValueError("terminal runtime launch cannot bind a resource")
            if not entry["launch_id"]:
                entry["launch_id"] = self.launch_id or str(
                    self.ref.meta.get("launch_id") or uuid4().hex
                )
            entry["control"].update(sandbox=self.sandbox, ref=self.ref.to_data())

        current = read_instance(path, instance)
        if not update_instance(
            path,
            bind,
            instance=instance,
            launch_id=self.launch_id if current and current["launch_id"] else None,
        ):
            raise ValueError("runtime launch ownership changed")

    @classmethod
    def load(cls, path: Path, *, instance: str | None = None) -> SandboxState | None:
        return cls.from_instance(read_instance(path, instance))

    @classmethod
    def from_instance(cls, entry: dict[str, Any] | None) -> SandboxState | None:
        """Decode a resource from the same snapshot as its workload report."""
        if entry is None or entry["control"].get("ref") is None:
            return None
        control = entry["control"]
        return cls(
            sandbox=control["sandbox"],
            ref=SandboxRef.from_data(control["ref"]),
            launch_id=entry["launch_id"],
        )


def process_alive(pid: object, created: object) -> bool | None:
    """Return None when identity cannot be verified; never infer death then."""
    if not isinstance(pid, int) or not isinstance(created, (int, float)):
        return None
    try:
        process = psutil.Process(pid)
        return (
            process.create_time() == created
            and process.status() != psutil.STATUS_ZOMBIE
        )
    except psutil.NoSuchProcess:
        return False
    except psutil.Error:
        return None


def control_owner() -> dict[str, int | float]:
    """Host identity for a control operation or a temporary caller's lifetime."""
    return {"pid": os.getpid(), "created": psutil.Process().create_time()}


def recover_control_write_lock(path: Path) -> None:
    """Queries may unblock a guest waiting on a killed host writer, without state edits."""
    lock = file_lock_path(path)
    if not lock.exists() or lock.is_symlink():
        return
    try:
        reap_file_lock(
            lock,
            lambda owner: (
                owner.get("location") == "host"
                and process_alive(owner.get("pid"), owner.get("created")) is False
            ),
        )
    except (OSError, ValueError):
        pass


def sandbox_alive(layout: AgentLayout, state: SandboxState) -> bool | None:
    """Bound namespace liveness probes, including synchronous callers in a loop."""
    import asyncio
    from concurrent.futures import ThreadPoolExecutor
    from .sandbox import load_state_sandbox

    async def probe():
        return await asyncio.wait_for(
            load_state_sandbox(layout, state).running(state.ref), timeout=2
        )

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(lambda: asyncio.run(probe())).result()
    except (OSError, RuntimeError, ValueError, TimeoutError):
        return None
