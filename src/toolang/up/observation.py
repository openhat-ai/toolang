"""Host-owned discovery references to authoritative agent runtime records."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

import psutil

from toolang.common.files import atomic_write_text, file_write_lock
from toolang.common.layout import AgentLayout
from toolang.up.records import read_document, read_record, update_record
from toolang.up.types import ExecutionDrivers


def instance_identity(layout: AgentLayout) -> str:
    return hashlib.sha256(str(layout.run_store).encode()).hexdigest()[:16]


def instance_filename(layout: AgentLayout) -> str:
    """Keep the stable ID while making references recognizable in a directory."""
    return f"{layout.name}-{layout.placement}-{instance_identity(layout)}.json"


def _registry_lock(path: Path) -> Path:
    return path.parent / "registry.lock"


class RuntimeRegistration:
    """Publish references; embedded execution also publishes its workload identity."""

    def __init__(
        self,
        layout: AgentLayout,
        *,
        roots: tuple[Path, ...],
        drivers: ExecutionDrivers,
        endpoint: str = "",
        sandbox_runtime: str | None = None,
        launch_id: str | None = None,
    ) -> None:
        self.layout = layout
        self.instance = instance_identity(layout)
        self.token = launch_id or uuid4().hex
        self.embedded = launch_id is None
        self.roots = tuple(dict.fromkeys(roots))
        self.paths = tuple(
            root / ".runtime" / "instances" / instance_filename(layout)
            for root in self.roots
        )
        self.payload = {
            "version": 1,
            "status": str(layout.runtime_status),
            "launch_id": self.token,
        }
        self.drivers = drivers
        self.endpoint = endpoint

    def __enter__(self) -> RuntimeRegistration:
        if self.embedded:

            def begin(entry):
                entry["launch_id"] = self.token
                entry["control"].update(
                    root=str(self.layout.root),
                    name=self.layout.name,
                    placement=self.layout.placement,
                    runtime_instance=self.layout.instance,
                    home=str(self.layout.home),
                    store=str(self.layout.run_store),
                    drivers=sorted(self.drivers),
                    toolang_root=str(self.roots[0]),
                )
                entry["workload"] = {
                    "pid": os.getpid(),
                    "process_created": psutil.Process().create_time(),
                    "status": "running",
                    "endpoint": self.endpoint,
                }

            update_record(
                self.layout.runtime_status, begin, instance=self.layout.instance
            )
        try:
            for path in self.paths:
                with file_write_lock(_registry_lock(path)):
                    atomic_write_text(path, json.dumps(self.payload))
        except BaseException:
            self.close()
            raise
        return self

    def close(self) -> None:
        if self.embedded:
            from toolang.up.process import stop_runtime_state

            stop_runtime_state(
                self.layout, expected_pid=os.getpid(), launch_id=self.token
            )
        for path in self.paths:
            _remove_reference(path, self.token)

    def __exit__(self, *exc: object) -> None:
        self.close()


def _remove_reference(path: Path, launch_id: str) -> None:
    try:
        with file_write_lock(_registry_lock(path), create=False):
            if json.loads(path.read_text()).get("launch_id") == launch_id:
                path.unlink(missing_ok=True)
    except (OSError, ValueError):
        pass


def remove_registration(layout: AgentLayout, launch_id: str) -> None:
    entry = read_record(layout.runtime_status, layout.instance)
    if entry is None or entry["launch_id"] != launch_id:
        return
    root = entry["control"].get("toolang_root")
    if isinstance(root, str):
        _remove_reference(
            Path(root) / ".runtime" / "instances" / instance_filename(layout),
            launch_id,
        )


def running_instances(root: Path) -> tuple[dict, ...]:
    """Follow references and reconcile only definitively dead workload identities."""
    from toolang.up.process import AgentProcess

    candidates = []
    for path in sorted((root / ".runtime" / "instances").glob("*.json")):
        try:
            reference = json.loads(path.read_text())
            if reference.get("version") != 1:
                continue  # Never adopt legacy registrations.
            status_path = Path(reference["status"])
            document = read_document(status_path)
            if document is None or document.get("version") != 2:
                continue
            for entry in document["instances"].values():
                if entry["launch_id"] != reference["launch_id"]:
                    continue
                control = entry["control"]
                layout = AgentLayout(
                    Path(control["root"]),
                    control["name"],
                    control["placement"],
                    instance=control["runtime_instance"],
                )
                if (
                    layout.runtime_status != status_path
                    or instance_filename(layout) != path.name
                ):
                    continue
                candidates.append((path, entry, layout))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    docker = []
    for _, entry, layout in candidates:
        control = entry["control"]
        if (
            control.get("ref") is not None
            and control.get("sandbox", "").partition(":")[0] == "docker"
        ):
            from toolang.up.records import SandboxState

            reference = SandboxState.snapshot(
                layout.runtime_status, instance=layout.instance
            )
            if reference is not None:
                docker.append((AgentProcess(layout), reference, entry))
    AgentProcess.cache_docker_states(docker)
    instances = []
    for path, entry, layout in candidates:
        try:
            control = entry["control"]
            status = AgentProcess(layout).status(
                ui_base_url="", check_health=False, cache=True
            )
            if status is None or status.status != "running":
                continue
            agent = f"agent:{layout.name}"
            if (
                layout.placement != "resident"
                or layout.instance is not None
                or root != layout.root
            ):
                agent += f"@{instance_identity(layout)[:8]}"
            instances.append(
                {
                    **control,
                    "agent": agent,
                    "instance": instance_identity(layout),
                    "drivers": control.get("drivers", []),
                }
            )
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return tuple(instances)
