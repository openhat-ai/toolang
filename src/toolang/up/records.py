"""Atomic, generation-checked control and workload records."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from contextlib import contextmanager
from contextvars import ContextVar
import os
import time
from collections.abc import Iterator
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
from toolang.teaming.records import HubRecord as HubRecord

RUNTIME_STATUS_VERSION = 2


def read_document(path: Path, *, location: str = "host") -> dict[str, Any] | None:
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
    return payload


def read_record(
    path: Path, instance: str | None = None, *, location: str = "host"
) -> dict[str, Any] | None:
    payload = read_document(path, location=location)
    if payload is None:
        return None
    if payload.get("version") != RUNTIME_STATUS_VERSION:
        if "version" in payload or "status" not in payload:
            raise ValueError(f"unsupported runtime status version: {path}")
        # Legacy reports remain readable, but cannot be adopted while active.
        return {
            "launch_id": "",
            "control": {
                "last_successful": {
                    "endpoint": payload.get("endpoint"),
                    "sandbox": payload.get("sandbox"),
                }
                if payload.get("status") == "stopped"
                else {}
            },
            "workload": payload,
        }
    instances = payload.get("instances")
    if not isinstance(instances, dict):
        raise ValueError(f"invalid runtime instances: {path}")
    entry = instances.get(instance or "default")
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


@contextmanager
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
        or read_record(path, instance, location=location)
        or {
            "control": {},
            "launch_id": "",
        }
    )
    control = entry["control"]
    if not isinstance(control, dict):
        raise ValueError(f"invalid runtime control: {path}")
    owner = {
        "location": location,
        "pid": os.getpid(),
        "created": psutil.Process().create_time(),
        "launch_id": launch_id or entry["launch_id"],
        "acquired_at": time.time(),
        "control": {
            key: control[key]
            for key in ("root", "name", "placement", "sandbox", "ref")
            if key in control
        },
        "instance": instance,
    }

    def dead(previous):
        if not previous:
            return location == "host"  # Upgrade an idle legacy flock guard.
        previous_location = previous.get("location")
        if previous_location == location and (
            location == "host" or previous.get("launch_id") == launch_id
        ):
            return process_alive(previous.get("pid"), previous.get("created")) is False
        if location != "host" or previous_location != "guest":
            return False  # Guests cannot inspect a different PID namespace.
        if (
            dead_guest_launch is not None
            and previous.get("launch_id") == dead_guest_launch
        ):
            return True  # The query already proved this guest launch absent.
        acquired_at = previous.get("acquired_at")
        if not isinstance(acquired_at, (int, float)):
            return False
        if not blocking or 0 <= time.time() - acquired_at < 1:
            return False
        try:
            saved = previous["control"]
            ref = SandboxState(
                saved["sandbox"],
                SandboxRef.from_data(saved["ref"]),
                previous["launch_id"],
            )
            layout = AgentLayout(
                Path(saved["root"]),
                saved["name"],
                saved["placement"],
                instance=previous.get("instance"),
            )
            from toolang.up.sandbox import load_state_sandbox
            import asyncio

            from concurrent.futures import ThreadPoolExecutor

            async def probe():
                return await asyncio.wait_for(
                    load_state_sandbox(layout, ref).running(ref.ref), timeout=2
                )

            with ThreadPoolExecutor(max_workers=1) as pool:
                return pool.submit(asyncio.run, probe()).result(timeout=3) is False
        except (KeyError, OSError, RuntimeError, TypeError, ValueError):
            return False

    with file_write_lock(
        file_lock_path(path),
        inherit_owner=True,
        blocking=blocking,
        on_wait=on_wait,
        shared_owner=owner,
        owner_dead=dead,
    ):
        yield


def update_record(
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
    snapshot = read_record(path, instance, location=location)
    if launch_id is not None and (
        snapshot is None or snapshot["launch_id"] != launch_id
    ):
        return False
    # Reject live legacy reports before changing their old lock protocol.
    if (
        snapshot
        and not snapshot["launch_id"]
        and snapshot["workload"].get("status") in {"running", "starting", "preparing"}
    ):
        raise ValueError(
            "stop the legacy workload with the previous Toolang version before migration"
        )
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
            payload = {"version": RUNTIME_STATUS_VERSION, "instances": {}}
        elif payload.get("version") != RUNTIME_STATUS_VERSION:
            if "version" in payload or "status" not in payload:
                raise ValueError(f"unsupported runtime status version: {path}")
            if payload.get("status") in {"running", "starting", "preparing"}:
                raise ValueError(
                    "stop the legacy workload with the previous Toolang version before migration"
                )
            old = payload
            payload = {
                "version": RUNTIME_STATUS_VERSION,
                "instances": {
                    "default": {
                        "launch_id": "",
                        "control": {
                            "last_successful": {
                                "endpoint": old.get("endpoint"),
                                "sandbox": old.get("sandbox"),
                            }
                        },
                        "workload": old,
                    }
                },
            }
        entries = payload["instances"]
        entry = entries.setdefault(
            instance or "default", {"launch_id": "", "control": {}, "workload": {}}
        )
        if launch_id is not None and entry["launch_id"] != launch_id:
            return False
        before = json.dumps(payload, sort_keys=True)
        update(entry)
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
    return update_record(
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
    snapshot = read_record(path, layout.instance)
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
            if not entry["launch_id"]:
                entry["launch_id"] = self.launch_id or str(
                    self.ref.meta.get("launch_id") or uuid4().hex
                )
            entry["control"].update(sandbox=self.sandbox, ref=self.ref.to_data())

        current = read_record(path, instance)
        if not update_record(
            path,
            bind,
            instance=instance,
            launch_id=self.launch_id if current and current["launch_id"] else None,
        ):
            raise ValueError("runtime launch ownership changed")

    @classmethod
    def load(cls, path: Path, *, instance: str | None = None) -> SandboxState | None:
        return cls.from_record(read_record(path, instance))

    @classmethod
    def from_record(cls, entry: dict[str, Any] | None) -> SandboxState | None:
        """Decode a resource from the same snapshot as its workload report."""
        if entry is None or entry["control"].get("ref") is None:
            return None
        control = entry["control"]
        return cls(
            sandbox=control["sandbox"],
            ref=SandboxRef.from_data(control["ref"]),
            launch_id=entry["launch_id"],
        )


# Durable ownership replaces locks held across process creation/readiness/shutdown.
_OPERATIONS: ContextVar[tuple[tuple[Path, str | None], ...]] = ContextVar(
    "runtime_operations", default=()
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


@contextmanager
def management_operation(
    layout: AgentLayout, *, stage: str, on_wait: Callable[[], None] | None = None
) -> Iterator[None]:
    key = (layout.runtime_status, layout.instance)
    held = _OPERATIONS.get()
    if key in held:
        yield
        return
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

        update_record(
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
            update_record(layout.runtime_status, release, instance=layout.instance)
            if completed_launch is not None:
                remove_finished_instance(layout, completed_launch)


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
