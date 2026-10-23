"""Publish and retire host-owned references to authoritative runtime status."""

from __future__ import annotations

from contextlib import contextmanager, ExitStack
from dataclasses import dataclass, replace
import json
import os
import time
import psutil
from pathlib import Path
from typing import cast
from toolang.base.types.sandbox import SandboxRef

from toolang.common.files import atomic_write_text, file_write_lock, file_read_snapshot
from toolang.common.layout import AgentLayout
from .identity import layout_from_control, verify_identity
from .instance import (
    RUNTIME_STATUS_FORMAT,
    read_instance,
    runtime_lock,
    update_instance,
    process_alive,
)


def instance_filename(layout: AgentLayout) -> str:
    return f"{layout.address}.json"


def registration_directory(layout: AgentLayout, *, toolang_root: Path) -> Path:
    """One address owns its reference and guard, including across launch generations."""
    return toolang_root / ".runtime" / "active" / layout.address


def registration_path(layout: AgentLayout, *, toolang_root: Path) -> Path:
    return registration_directory(
        layout, toolang_root=toolang_root
    ) / instance_filename(layout)


def _reference_paths(layout: AgentLayout, root: Path) -> tuple[Path, ...]:
    # Flat format-5 references remain observable during an upgrade. New owners
    # publish only in their private directory; legacy guards are never mounted.
    return (
        registration_path(layout, toolang_root=root),
        root / ".runtime" / "active" / instance_filename(layout),
    )


@dataclass(frozen=True, slots=True)
class RuntimeReference:
    """A discovery locator fenced by its authoritative status path and launch."""

    status: Path
    launch_id: str

    @classmethod
    def from_data(cls, value: object) -> RuntimeReference:
        if not isinstance(value, dict):
            raise ValueError("invalid runtime reference")
        payload = cast(dict[str, object], value)
        if (
            type(payload.get("version")) is not int
            or payload["version"] != RUNTIME_STATUS_FORMAT
        ):
            raise ValueError("unsupported runtime reference version")
        status_path = payload.get("status")
        launch_id = payload.get("launch_id")
        if not isinstance(status_path, str) or not isinstance(launch_id, str):
            raise ValueError("incomplete runtime reference")
        status = Path(status_path)
        if (
            not status.is_absolute()
            or str(status) != status_path
            or ".." in status.parts
        ):
            raise ValueError(
                "runtime reference requires a normalized absolute status path"
            )
        return cls(status, launch_id)

    def to_data(self) -> dict[str, object]:
        return {
            "version": RUNTIME_STATUS_FORMAT,
            "status": str(self.status),
            "launch_id": self.launch_id,
        }


def read_reference(path: Path) -> RuntimeReference | None:
    try:
        reference = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    return RuntimeReference.from_data(reference)


def registration_current(
    path: Path, launch_id: str | None, status_path: Path | None = None
) -> bool:
    reference = read_reference(path)
    return (
        reference is not None
        and (launch_id is None or reference.launch_id == launch_id)
        and (status_path is None or reference.status == status_path)
    )


class LockRecovery:
    """One refresh's bounded recovery evidence, independent of active references."""

    def __init__(self, *, probes: int = 1) -> None:
        self.probes = probes
        self.dead_resources: set[str] = set()

    def remember(self, entry: dict, alive: bool | None) -> None:
        ref = entry["control"].get("ref")
        if alive is False and isinstance(ref, dict):
            self.dead_resources.add(
                self._resource_key(entry["control"], entry["launch_id"])
            )

    @staticmethod
    def _resource_key(control: dict, launch_id: object) -> str:
        return json.dumps(
            (control.get("sandbox"), control.get("ref"), launch_id), sort_keys=True
        )

    def dead(self, previous: dict, owner: dict) -> bool:
        if not previous:
            return owner["location"] == "host"
        location = previous.get("location")
        if (
            location == owner.get("location")
            and previous.get("pid") == owner.get("pid")
            and previous.get("created") == owner.get("created")
        ):
            return False
        if location == "host":
            return (
                owner["location"] == "host"
                and process_alive(previous.get("pid"), previous.get("created")) is False
            )
        if location != "guest":
            return False
        saved = previous.get("control", {})
        if not isinstance(saved, dict):
            return False
        ref = saved.get("ref")
        if owner["location"] == "guest":
            return (
                previous.get("launch_id") == owner.get("launch_id")
                and ref == owner.get("control", {}).get("ref")
                and process_alive(previous.get("pid"), previous.get("created")) is False
            )
        if not isinstance(ref, dict):
            return False
        key = self._resource_key(saved, previous.get("launch_id"))
        if key in self.dead_resources:
            return True
        if self.probes <= 0:
            return False
        self.probes -= 1
        try:
            from .instance import SandboxState, sandbox_alive

            layout = layout_from_control(saved)
            state = SandboxState(
                saved["sandbox"], SandboxRef.from_data(ref), previous["launch_id"]
            )
            dead = sandbox_alive(layout, state) is False
        except (ImportError, KeyError, OSError, RuntimeError, TypeError, ValueError):
            dead = False
        if dead:
            self.dead_resources.add(key)
        return dead


def _claim_owner(*, location: str = "host", entry: dict | None = None) -> dict:
    return {
        "location": location,
        "pid": os.getpid(),
        "created": psutil.Process().create_time(),
        "acquired_at": time.time(),
        "launch_id": (entry or {}).get("launch_id", ""),
        "control": (entry or {}).get("control", {}),
    }


def _registration_guard(
    directory: Path, *, owner: dict, recovery: LockRecovery, blocking: bool
):
    """Prepare recovery evidence before a caller acquires any mutation guard."""
    path = directory / "register.lock"
    deadline = time.monotonic() + (2 if blocking else 0)
    try:
        with file_read_snapshot(path) as stream:
            content = stream.read()
        claim = json.loads(content) if content.strip() else {}
        dead = isinstance(claim, dict) and recovery.dead(claim, owner)
    except FileNotFoundError:
        claim, dead = None, False
    except (OSError, ValueError):
        claim, dead = None, False

    @contextmanager
    def acquire():
        stack = ExitStack()
        while True:
            try:
                stack.enter_context(
                    file_write_lock(
                        path,
                        shared_owner=owner,
                        inherit_owner=True,
                        blocking=False,
                        owner_dead=lambda current: dead and current == claim,
                    )
                )
                break
            except (BlockingIOError, FileNotFoundError):
                if not blocking or time.monotonic() >= deadline:
                    stack.close()
                    raise
                time.sleep(min(0.02, max(0, deadline - time.monotonic())))
        try:
            with stack:
                yield
        finally:
            if owner["location"] == "host":
                try:
                    directory.rmdir()
                except OSError:
                    pass

    return acquire()


def registration_lock(toolang_root: Path, layout: AgentLayout):
    """Prepare one bounded publication guard without acquiring it yet."""
    return _registration_guard(
        registration_directory(layout, toolang_root=toolang_root),
        owner=_claim_owner(),
        recovery=LockRecovery(),
        blocking=True,
    )


def verify_registration(
    layout: AgentLayout, *, toolang_root: Path, require_absent: bool = False
) -> None:
    """Reject an occupied name before attempting this caller's publication."""
    for path in _reference_paths(layout, toolang_root):
        reference = read_reference(path)
        if reference is not None and (
            require_absent or reference.status != layout.runtime_status
        ):
            raise ValueError(
                f"place code collision in runtime registry: {layout.address}"
            )


class RuntimeRegistration:
    """Publish only a discovery reference to an already initialized workload."""

    def __init__(
        self,
        layout: AgentLayout,
        *,
        toolang_root: Path,
        launch_id: str,
    ) -> None:
        self.token = launch_id
        self.toolang_root = toolang_root.expanduser().resolve()
        self.layout = replace(layout, root=self.toolang_root)
        self.path = registration_path(layout, toolang_root=self.toolang_root)
        self.payload = RuntimeReference(layout.runtime_status, self.token).to_data()

    def publish(self) -> RuntimeRegistration:
        """Publish after checking identity and collisions under the registry guard."""
        legacy = (
            self.toolang_root / ".runtime" / "active" / instance_filename(self.layout)
        )
        legacy_guard = (
            _registration_guard(
                legacy.parent,
                owner=_claim_owner(),
                recovery=LockRecovery(probes=0),
                blocking=False,
            )
            if legacy.is_file()
            else None
        )
        guard = registration_lock(self.toolang_root, self.layout)
        status_guard = runtime_lock(
            self.layout.runtime_status,
            instance=self.layout.instance,
            launch_id=self.token,
            blocking=False,
        )
        with ExitStack() as guards:
            if legacy_guard is not None:
                guards.enter_context(legacy_guard)
            guards.enter_context(guard)
            with status_guard:
                verify_registration(self.layout, toolang_root=self.toolang_root)
                entry = read_instance(self.layout.runtime_status, self.layout.instance)
                if (
                    entry is None
                    or entry["launch_id"] != self.token
                    or entry["workload"].get("status")
                    not in {"preparing", "starting", "running"}
                ):
                    raise ValueError(
                        "runtime publication requires matching instance identity"
                    )
                verify_identity(self.layout, entry["control"])
                try:
                    atomic_write_text(self.path, json.dumps(self.payload))
                    previous = read_reference(legacy)
                    if (
                        previous is not None
                        and previous.status == self.layout.runtime_status
                    ):
                        legacy.unlink(missing_ok=True)
                except BaseException:
                    self.close()
                    raise
        return self

    def __enter__(self) -> RuntimeRegistration:
        return self.publish()

    def close(self) -> None:
        _remove_reference(self.path, self.token, self.layout.runtime_status)

    def __exit__(self, *exc: object) -> None:
        self.close()


def _remove_reference(
    path: Path,
    launch_id: str,
    status_path: Path,
    *,
    owner: dict | None = None,
    recovery: LockRecovery | None = None,
) -> None:
    try:
        if not path.is_file():
            return
        with _registration_guard(
            path.parent,
            owner=owner or _claim_owner(),
            recovery=recovery or LockRecovery(),
            blocking=False,
        ):
            reference = read_reference(path)
            if (
                reference is not None
                and reference.launch_id == launch_id
                and reference.status == status_path
            ):
                path.unlink(missing_ok=True)
    except (OSError, ValueError):
        pass


def remove_registration(layout: AgentLayout, launch_id: str) -> None:
    entry = read_instance(layout.runtime_status, layout.instance)
    if entry is None or entry["launch_id"] != launch_id:
        return
    root = entry["control"].get("root")
    if isinstance(root, str):
        for path in _reference_paths(layout_from_control(entry["control"]), Path(root)):
            _remove_reference(path, launch_id, layout.runtime_status)
        try:
            registration_directory(layout, toolang_root=Path(root)).rmdir()
        except OSError:
            pass


def repair_registration(
    layout: AgentLayout,
    observed: dict,
    *,
    resource_dead: bool,
    recovery: LockRecovery,
    reference_path: Path | None = None,
) -> str:
    """Repair only the probed snapshot; registry precedes the status guard."""
    root = observed["control"].get("root")
    if not isinstance(root, str):
        return "deferred"
    path = reference_path or next(
        (path for path in _reference_paths(layout, Path(root)) if path.is_file()),
        registration_path(layout, toolang_root=Path(root)),
    )
    directory = path.parent
    operation = observed["control"].get("operation")
    if (
        operation
        and process_alive(operation.get("pid"), operation.get("created")) is not False
    ):
        return "deferred"
    try:
        expected = read_reference(path)
        status_guard = runtime_lock(
            layout.runtime_status,
            instance=layout.instance,
            launch_id=observed["launch_id"],
            blocking=False,
            dead_guest_launch=observed["launch_id"] if resource_dead else None,
            snapshot=observed,
        )
        guard = _registration_guard(
            directory,
            owner=_claim_owner(entry=observed),
            recovery=recovery,
            blocking=False,
        )
        with guard:
            with status_guard:
                current = read_instance(layout.runtime_status, layout.instance)
                if current != observed or read_reference(path) != expected:
                    return "changed"
                if expected is not None and expected.status != layout.runtime_status:
                    return "changed"
                if expected is None or expected.launch_id == observed["launch_id"]:

                    def finish(entry):
                        if entry["workload"].get("status") in {
                            "preparing",
                            "starting",
                            "running",
                        }:
                            entry["workload"].update(
                                status="stopped",
                                pid=None,
                                observed_dead_at=time.strftime(
                                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
                                ),
                            )

                    update_instance(
                        layout.runtime_status,
                        finish,
                        instance=layout.instance,
                        launch_id=observed["launch_id"],
                    )
                if expected is not None:
                    path.unlink(missing_ok=True)
                return "retired"
    except (OSError, ValueError):
        return "deferred"


def finalize_runtime(
    layout: AgentLayout,
    launch_id: str,
    *,
    expected_pid: int,
    failure: str | None = None,
    location: str = "host",
    directory: Path | None = None,
    status_path: Path | None = None,
) -> bool:
    """An owner terminates its launch and withdraws its discovery reference together."""
    entry = read_instance(layout.runtime_status, layout.instance, location=location)
    if entry is None or entry["launch_id"] != launch_id:
        return False
    if location == "guest":
        if directory is None or status_path is None:
            raise ValueError(
                "guest finalization requires its explicit registry binding"
            )
    else:
        root = entry["control"].get("root")
        if not isinstance(root, str):
            return False
        directory = registration_directory(layout, toolang_root=Path(root))
        status_path = layout.runtime_status
    path = directory / instance_filename(layout)
    try:
        created = psutil.Process(expected_pid).create_time()
        status_guard = runtime_lock(
            layout.runtime_status,
            instance=layout.instance,
            launch_id=launch_id,
            location=location,
            blocking=False,
            snapshot=entry,
        )
        guard = _registration_guard(
            directory,
            owner=_claim_owner(location=location, entry=entry),
            recovery=LockRecovery(),
            blocking=True,
        )
        with guard:
            with status_guard:
                current = read_instance(
                    layout.runtime_status, layout.instance, location=location
                )
                if current is None or current["launch_id"] != launch_id:
                    return False
                workload = current["workload"]
                if (
                    workload.get("pid") != expected_pid
                    or workload.get("process_created") != created
                ):
                    return False

                def finish(value):
                    value["workload"].update(
                        status="failed" if failure else "stopped",
                        pid=None,
                        updated_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    )
                    if failure:
                        value["workload"]["message"] = failure

                update_instance(
                    layout.runtime_status,
                    finish,
                    instance=layout.instance,
                    launch_id=launch_id,
                    location=location,
                    blocking=False,
                )
                reference = read_reference(path)
                if reference == RuntimeReference(status_path, launch_id):
                    path.unlink(missing_ok=True)
                return True
    except (OSError, ValueError, psutil.Error):
        return False  # Queries can later repair a dead owner's unwithdrawn record.
