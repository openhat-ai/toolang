"""Publish and retire host-owned references to authoritative runtime status."""

from __future__ import annotations

from contextlib import contextmanager, ExitStack, nullcontext
from dataclasses import replace
import json
import time
import psutil
from pathlib import Path

from toolang.common.files import atomic_write_text, file_write_lock, file_read_snapshot
from toolang.common.layout import AgentLayout
from .identity import layout_from_control, verify_identity
from .records import RuntimeReference
from .instance import (
    LockRecovery,
    runtime_lock_owner,
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
    return registration_directory(layout, toolang_root=toolang_root) / "info.json"


def reference_address(path: Path) -> str:
    """Read the address from a current directory or a legacy filename."""
    return (
        path.parent.name
        if path.name == "info.json" and path.parent.parent.name == "active"
        else path.stem
    )


def _reference_paths(layout: AgentLayout, root: Path) -> tuple[Path, ...]:
    # Flat format-5 references remain observable during an upgrade. New owners
    # publish only in their private directory; legacy guards are never mounted.
    return (
        registration_path(layout, toolang_root=root),
        registration_directory(layout, toolang_root=root) / instance_filename(layout),
        root / ".runtime" / "active" / instance_filename(layout),
    )


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
        owner=runtime_lock_owner(),
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
        self.payload = RuntimeReference(
            layout.runtime_status, self.token, address=layout.address
        ).to_data()

    def publish(self) -> RuntimeRegistration:
        """Publish after checking identity and collisions under the registry guard."""
        legacy = (
            self.toolang_root / ".runtime" / "active" / instance_filename(self.layout)
        )
        legacy_guard = (
            _registration_guard(
                legacy.parent,
                owner=runtime_lock_owner(),
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
                    control = entry["control"]
                    sandbox = control.get("sandbox", "host")
                    self.payload = RuntimeReference(
                        self.layout.runtime_status,
                        self.token,
                        location=entry["workload"].get(
                            "location", "host" if sandbox == "host" else "guest"
                        ),
                        sandbox=sandbox,
                        source=Path(control["source_path"])
                        if control.get("source_path")
                        else None,
                        source_ref=control.get("source_ref"),
                        address=self.layout.address,
                    ).to_data()
                    atomic_write_text(self.path, json.dumps(self.payload))
                    for previous_path in _reference_paths(
                        self.layout, self.toolang_root
                    )[1:]:
                        previous = read_reference(previous_path)
                        if (
                            previous is not None
                            and previous.status == self.layout.runtime_status
                        ):
                            previous_path.unlink(missing_ok=True)
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
            owner=owner or runtime_lock_owner(),
            recovery=recovery or LockRecovery(),
            blocking=False,
        ):
            reference = read_reference(path)
            if (
                reference is not None
                and reference.launch_id == launch_id
                and reference.status == status_path
            ):
                if path.name != "info.json" or not (path.parent / "docker").exists():
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
        guard = _registration_guard(
            directory,
            owner=runtime_lock_owner(entry=observed),
            recovery=recovery,
            blocking=False,
        )
        status_guard = runtime_lock(
            layout.runtime_status,
            instance=layout.instance,
            launch_id=observed["launch_id"],
            blocking=False,
            dead_guest_launch=observed["launch_id"] if resource_dead else None,
            snapshot=observed,
            recovery=recovery,
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
                if expected is not None and (
                    path.name != "info.json" or not (path.parent / "docker").exists()
                ):
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
    try:
        entry = read_instance(layout.runtime_status, layout.instance, location=location)
    except (OSError, ValueError):
        return False
    if entry is None or entry["launch_id"] != launch_id:
        return False
    guest_report_only = location == "guest" and directory is None
    if location == "host":
        root = entry["control"].get("root")
        if not isinstance(root, str):
            return False
        directory = registration_directory(layout, toolang_root=Path(root))
        status_path = layout.runtime_status
    path = (
        directory / ("info.json" if location == "host" else instance_filename(layout))
        if directory is not None
        else None
    )
    try:
        created = psutil.Process(expected_pid).create_time()
        recovery = LockRecovery()
        guard = (
            _registration_guard(
                directory,
                owner=runtime_lock_owner(location=location, entry=entry),
                recovery=recovery,
                blocking=True,
            )
            if directory is not None
            else nullcontext()
        )
        status_guard = runtime_lock(
            layout.runtime_status,
            instance=layout.instance,
            launch_id=launch_id,
            location=location,
            blocking=False,
            snapshot=entry,
            recovery=recovery,
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
                if not guest_report_only and path is not None:
                    reference = read_reference(path)
                    if (
                        reference is not None
                        and reference.status == status_path
                        and reference.launch_id == launch_id
                        and not (path.parent / "docker").exists()
                    ):
                        path.unlink(missing_ok=True)
                return True
    except (OSError, ValueError, psutil.Error):
        return False  # Queries can later repair a dead owner's unwithdrawn record.
