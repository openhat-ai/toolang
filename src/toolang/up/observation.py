"""Host-owned discovery references to authoritative agent runtime records."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import json
import re
from pathlib import Path
from typing import Any, TypeGuard, cast


from toolang.common.files import atomic_write_text, file_write_lock
from toolang.common.layout import AgentLayout
from toolang.up.instance import (
    AGENT_INSTANCE_FORMAT,
    read_document,
    read_instance,
)
from toolang.up.types import (
    ALL_DRIVERS,
    RuntimeInstance as RuntimeInstance,
    RuntimeInstanceSnapshot as RuntimeInstanceSnapshot,
)


def _registered_layout(control: dict) -> AgentLayout:
    """Recover discovery identity from control bindings, never workload policy."""
    return AgentLayout(
        Path(control["root"]),
        control["name"],
        control["placement"],
        instance=control["runtime_instance"],
        project_inputs=control.get("project_inputs"),
        managed_membership=control.get("managed_membership"),
        source_id=control.get("source_id"),
        instance_id=control.get("instance_id"),
        source_path=Path(control["source_path"])
        if control.get("source_path")
        else None,
        source_ref=control.get("source_ref"),
        request_order_ns=control.get("request_order_ns"),
        source_edit_lock=Path(control["source_edit_lock"])
        if control.get("source_edit_lock")
        else None,
    )


def instance_identity(layout: AgentLayout) -> str:
    return layout.instance_name


def instance_filename(layout: AgentLayout) -> str:
    return f"{layout.instance_name}.json"


def _read_reference(path: Path) -> dict[str, Any] | None:
    try:
        reference = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    if (
        not isinstance(reference, dict)
        or type(reference.get("version")) is not int
        or reference["version"] != AGENT_INSTANCE_FORMAT
    ):
        raise ValueError("invalid runtime reference")
    if not isinstance(reference.get("status"), str) or not isinstance(
        reference.get("launch_id"), str
    ):
        raise ValueError("incomplete runtime reference")
    return reference


def _registration_current(path: Path, launch_id: str | None) -> bool:
    reference = _read_reference(path)
    return reference is not None and (
        launch_id is None or reference["launch_id"] == launch_id
    )


def _register_lock(path: Path) -> Path:
    return path.parent / "register.lock"


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
        self.path = toolang_root / ".runtime" / "instances" / instance_filename(layout)
        self.payload = {
            "version": AGENT_INSTANCE_FORMAT,
            "status": str(layout.runtime_status),
            "launch_id": self.token,
        }

    def __enter__(self) -> RuntimeRegistration:
        try:
            with file_write_lock(_register_lock(self.path)):
                atomic_write_text(self.path, json.dumps(self.payload))
        except BaseException:
            self.close()
            raise
        return self

    def close(self) -> None:
        _remove_reference(self.path, self.token)

    def __exit__(self, *exc: object) -> None:
        self.close()


def _remove_reference(path: Path, launch_id: str) -> None:
    try:
        if not path.is_file():
            return
        with file_write_lock(_register_lock(path)):
            reference = json.loads(path.read_text())
            if isinstance(reference, dict) and reference.get("launch_id") == launch_id:
                path.unlink(missing_ok=True)
    except (OSError, ValueError):
        pass


def remove_registration(layout: AgentLayout, launch_id: str) -> None:
    entry = read_instance(layout.runtime_status, layout.instance)
    if entry is None or entry["launch_id"] != launch_id:
        return
    root = entry["control"].get("toolang_root")
    if isinstance(root, str):
        _remove_reference(
            Path(root)
            / ".runtime"
            / "instances"
            / instance_filename(_registered_layout(entry["control"])),
            launch_id,
        )


def running_instances(root: Path) -> tuple[dict, ...]:
    """Follow references and reconcile only definitively dead workload identities."""
    from toolang.up.process import AgentProcess

    candidates = []
    records: dict[Path, dict] = {}
    for path in sorted((root / ".runtime" / "instances").glob("*.json")):
        try:
            reference = _read_reference(path)
            if reference is None:
                continue
            status_path = Path(reference["status"])
            if status_path not in records:
                document = read_document(status_path)
                records[status_path] = (
                    {
                        entry["launch_id"]: entry
                        for entry in document["instances"].values()
                        if isinstance(entry, dict)
                        and isinstance(entry.get("launch_id"), str)
                        and isinstance(entry.get("control"), dict)
                        and isinstance(entry.get("workload"), dict)
                    }
                    if document is not None
                    else {}
                )
            entry = records[status_path].get(reference["launch_id"])
            if entry is None:
                continue
            control = entry["control"]
            layout = _registered_layout(control)
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
            from toolang.up.instance import SandboxState

            reference = SandboxState.from_instance(entry)
            if reference is not None:
                docker.append((AgentProcess(layout), reference, entry))
    AgentProcess.cache_docker_states(docker)
    instances = []
    for path, entry, layout in candidates:
        try:
            control = entry["control"]
            status = AgentProcess(layout).status(
                ui_base_url="", check_health=False, cache=True, snapshot=entry
            )
            if status is None or status.status != "running":
                continue
            if not _registration_current(path, entry["launch_id"]):
                continue
            agent = f"agent:{layout.name}"
            if (
                layout.placement != "resident"
                or layout.instance is not None
                or root != layout.root
            ):
                agent = f"agent:{layout.instance_name}"
            instances.append(
                {
                    **control,
                    "agent": agent,
                    "instance": instance_identity(layout),
                    "drivers": control.get("drivers", []),
                    "endpoint": status.endpoint,
                }
            )
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return tuple(instances)


@dataclass(frozen=True, slots=True)
class RuntimeSource:
    """Validated local source and optional running control endpoint."""

    instance: RuntimeInstance
    layout: AgentLayout
    endpoint: str | None
    registration: Path | None
    launch_id: str | None

    def is_current(self) -> bool:
        return self.registration is not None and _registration_current(
            self.registration, self.launch_id
        )


@dataclass(frozen=True, slots=True)
class RuntimeInventory:
    snapshot: RuntimeInstanceSnapshot
    sources: tuple[RuntimeSource, ...]


@dataclass(slots=True)
class _SnapshotCandidate:
    registration: Path
    status_path: Path
    instance_key: str
    launch_id: str | None
    layout: AgentLayout | None
    entry: dict[str, Any] | None
    diagnostic: str | None = None


_REFERENCE_NAME = re.compile(
    r"(?P<name>[a-z][a-z0-9_]{0,63})(?:@(?P<instance>[vr][0-9a-f]+))?\.json\Z"
)


def _resolved(path: Path) -> Path:
    path = path.expanduser()
    try:
        return path.resolve(strict=False)
    except (OSError, RuntimeError):
        return path.absolute()


def _fallback_layout(
    reference_path: Path, status_path: Path, toolang_root: Path
) -> AgentLayout | None:
    """Recover display identity from our stable registration filename."""
    match = _REFERENCE_NAME.fullmatch(reference_path.name)
    if match is None or len(status_path.parents) < 4:
        return None
    name = match.group("name")
    instance_id = match.group("instance")
    placement = (
        "resident"
        if instance_id is None
        else "visiting"
        if instance_id.startswith("v")
        else "roaming"
    )
    layout = AgentLayout(
        status_path.parents[3],
        name,
        placement,
        toolang_root=toolang_root,
        instance_id=instance_id,
    )
    return (
        layout if _resolved(layout.runtime_status) == _resolved(status_path) else None
    )


def _snapshot_entry(value: object) -> TypeGuard[dict[str, Any]]:
    if not isinstance(value, dict):
        return False
    entry = cast(dict[str, Any], value)
    return (
        isinstance(entry.get("launch_id"), str)
        and isinstance(entry.get("control"), dict)
        and isinstance(entry.get("workload"), dict)
    )


def _runtime_layout(
    control: dict, status_path: Path, toolang_root: Path
) -> AgentLayout:
    """Validate persisted identity before probing its runtime."""
    layout = _registered_layout(control)
    recorded_toolang_root = control.get("toolang_root")
    if not isinstance(recorded_toolang_root, str):
        raise ValueError("runtime identity has no Toolang root")
    recorded_root = Path(recorded_toolang_root).expanduser()
    if (
        recorded_root.is_absolute()
        and _resolved(recorded_root) != _resolved(toolang_root)
    ) or _resolved(layout.runtime_status) != _resolved(status_path):
        raise ValueError("runtime identity does not match its registration")
    if layout.placement == "resident" and _resolved(layout.root) != _resolved(
        toolang_root
    ):
        raise ValueError("resident runtime is outside the selected Toolang root")
    if layout.placement == "visiting" and (
        _resolved(layout.root.parent) != _resolved(Path("/tmp"))
        or layout.root.name != layout.instance_name
    ):
        raise ValueError("visiting runtime path does not match its source identity")
    if layout.placement == "roaming" and layout.root.name != layout.instance_name:
        raise ValueError("roaming runtime path is invalid")
    return layout


def runtime_snapshot(root: Path) -> RuntimeInstanceSnapshot:
    """Return the existing public roster without exposing local source bindings."""
    return runtime_inventory(root).snapshot


def runtime_inventory(
    root: Path, *, agent: str | None = None, read_only: bool = False
) -> RuntimeInventory:
    """Discover registrations; read-only consumers retain stopped references."""
    from toolang.up.process import AgentProcess, AgentStatus

    root = _resolved(root)
    selected_status = (
        _resolved(AgentLayout.resident(root, agent).runtime_status)
        if agent is not None
        else None
    )
    diagnostics: list[str] = []
    candidates: dict[tuple[Path, str, str | None], _SnapshotCandidate] = {}
    documents: dict[Path, tuple[dict | None, str | None]] = {}

    def read_once(path: Path) -> tuple[dict[str, Any] | None, str | None]:
        resolved = _resolved(path)
        if resolved not in documents:
            try:
                documents[resolved] = (read_document(resolved), None)
            except (OSError, ValueError, TypeError) as exc:
                detail = str(exc).strip() or type(exc).__name__
                documents[resolved] = (None, detail[:180])
        return documents[resolved]

    def add(candidate: _SnapshotCandidate) -> None:
        if (
            agent is not None
            and candidate.layout is not None
            and (
                candidate.layout.placement != "resident"
                or candidate.layout.name != agent
            )
        ):
            return
        status_path = _resolved(candidate.status_path)
        if candidate.launch_id is not None:
            placeholder_key = (status_path, candidate.instance_key, None)
            placeholder = candidates.pop(placeholder_key, None)
            if placeholder is not None and placeholder.entry is None:
                if candidate.layout is None:
                    candidate.layout = placeholder.layout
                if candidate.entry is None:
                    candidate.entry = placeholder.entry
                if candidate.diagnostic is None:
                    candidate.diagnostic = placeholder.diagnostic
        key = (status_path, candidate.instance_key, candidate.launch_id)
        previous = candidates.get(key)
        if previous is None:
            candidates[key] = candidate
            return
        if previous.layout is None and candidate.layout is not None:
            previous.layout = candidate.layout
        if previous.entry is None and candidate.entry is not None:
            previous.entry = candidate.entry
        if previous.diagnostic is None:
            previous.diagnostic = candidate.diagnostic

    instance_dir = root / ".runtime" / "instances"
    try:
        reference_paths = sorted(
            (
                path
                for path in instance_dir.iterdir()
                if path.suffix == ".json"
                and (agent is None or path.stem == agent)
                and path.is_file()
            ),
            key=lambda path: path.name,
        )
    except FileNotFoundError:
        reference_paths = []
    except OSError as exc:
        reference_paths = []
        diagnostics.append(
            f"runtime registry: {str(exc).strip() or type(exc).__name__}"
        )

    for reference_path in reference_paths:
        fallback: AgentLayout | None = None
        try:
            reference = _read_reference(reference_path)
            if reference is None:
                continue
            status_path = root / Path(reference["status"]).expanduser()
            if (
                selected_status is not None
                and status_path is not None
                and _resolved(status_path) != selected_status
            ):
                name = _REFERENCE_NAME.fullmatch(reference_path.name)
                if name is None or name.group("instance") is None:
                    diagnostics.append(
                        f"runtime registry {reference_path.name}: resident status does not match the selected agent"
                    )
                continue
            launch_id = reference["launch_id"]
            fallback = _fallback_layout(reference_path, status_path, root)
            document, error = read_once(status_path)
            if error is not None:
                add(
                    _SnapshotCandidate(
                        reference_path,
                        status_path,
                        fallback.instance
                        if fallback and fallback.instance
                        else "default",
                        launch_id,
                        fallback,
                        None,
                        f"runtime status could not be read: {error}",
                    )
                )
                continue
            if document is None:
                add(
                    _SnapshotCandidate(
                        reference_path,
                        status_path,
                        fallback.instance
                        if fallback and fallback.instance
                        else "default",
                        launch_id,
                        fallback,
                        None,
                        "runtime status record is missing or invalid",
                    )
                )
                continue
            entries = document["instances"]
            matches = [
                (key, value)
                for key, value in entries.items()
                if _snapshot_entry(value) and value["launch_id"] == launch_id
            ]
            if len(matches) != 1:
                add(
                    _SnapshotCandidate(
                        reference_path,
                        status_path,
                        fallback.instance
                        if fallback and fallback.instance
                        else "default",
                        launch_id,
                        fallback,
                        None,
                        "runtime launch could not be matched",
                    )
                )
                continue
            instance_key, entry = matches[0]
            try:
                layout = _runtime_layout(entry["control"], status_path, root)
                if (
                    not isinstance(instance_key, str)
                    or layout.instance
                    != (None if instance_key == "default" else instance_key)
                    or instance_filename(layout) != reference_path.name
                ):
                    raise ValueError("runtime identity does not match its registration")
            except (KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
                add(
                    _SnapshotCandidate(
                        reference_path,
                        status_path,
                        instance_key if isinstance(instance_key, str) else "default",
                        launch_id,
                        fallback,
                        None,
                        str(exc)[:180],
                    )
                )
                continue
            add(
                _SnapshotCandidate(
                    reference_path,
                    status_path,
                    instance_key,
                    launch_id,
                    layout,
                    entry,
                )
            )
        except (OSError, ValueError, TypeError) as exc:
            diagnostics.append(
                f"runtime registry {reference_path.name}: {str(exc).strip() or type(exc).__name__}"
            )
            if fallback is not None:
                add(
                    _SnapshotCandidate(
                        reference_path,
                        fallback.runtime_status,
                        fallback.instance or "default",
                        None,
                        fallback,
                        None,
                        "runtime registration could not be read",
                    )
                )

    candidates_list = list(candidates.values())
    probes: list[tuple[_SnapshotCandidate, AgentLayout, dict[str, Any]]] = []
    result: list[RuntimeInstance] = []
    for candidate in candidates_list:
        if candidate.layout is None:
            continue
        if candidate.diagnostic is not None:
            result.append(
                RuntimeInstance(
                    candidate.layout.name,
                    candidate.layout.placement,
                    candidate.layout.instance,
                    instance_identity(candidate.layout),
                    "unknown",
                    None,
                    None,
                    candidate.diagnostic,
                )
            )
            continue
        entry = candidate.entry
        if entry is None:
            if candidate.layout.placement == "resident":
                result.append(
                    RuntimeInstance(
                        candidate.layout.name,
                        candidate.layout.placement,
                        candidate.layout.instance,
                        instance_identity(candidate.layout),
                        "not running",
                        None,
                    )
                )
            else:
                result.append(
                    RuntimeInstance(
                        candidate.layout.name,
                        candidate.layout.placement,
                        candidate.layout.instance,
                        instance_identity(candidate.layout),
                        "unknown",
                        None,
                        "unresolved",
                        "registered instance has no matching runtime record",
                    )
                )
            continue
        workload = entry["workload"]
        state = workload.get("status")
        if (
            isinstance(state, str)
            and state in {"stopped", "failed"}
            and workload.get("pid") is None
            and entry["control"].get("operation") is None
            and entry["control"].get("ref") is None
        ):
            if not read_only:
                remove_registration(candidate.layout, entry["launch_id"])
            else:
                result.append(
                    RuntimeInstance(
                        candidate.layout.name,
                        candidate.layout.placement,
                        candidate.layout.instance,
                        instance_identity(candidate.layout),
                        "not running",
                        None,
                    )
                )
            continue
        probes.append((candidate, candidate.layout, entry))

    docker_owners = []
    from toolang.up.instance import SandboxState

    for candidate, layout, entry in probes:
        sandbox_name = entry["control"].get("sandbox")
        if (
            entry["control"].get("ref") is not None
            and isinstance(sandbox_name, str)
            and sandbox_name.partition(":")[0] == "docker"
        ):
            try:
                reference = SandboxState.from_instance(entry)
            except (TypeError, ValueError):
                reference = None
            if reference is not None:
                docker_owners.append((AgentProcess(layout), reference, entry))
    try:
        AgentProcess.cache_docker_states(docker_owners)
    except Exception:
        # Individual probes below report failures without invalidating membership.
        pass

    def inspect(
        item: tuple[_SnapshotCandidate, AgentLayout, dict[str, Any]],
    ) -> tuple[
        _SnapshotCandidate, AgentLayout, dict[str, Any], AgentStatus | None, str | None
    ]:
        candidate, layout, entry = item
        try:
            status = AgentProcess(layout).status(
                ui_base_url="",
                check_health=False,
                cache=True,
                snapshot=entry,
                read_only=read_only,
            )
            return candidate, layout, entry, status, None
        except Exception as exc:
            return (
                candidate,
                layout,
                entry,
                None,
                str(exc).strip() or type(exc).__name__,
            )

    with ThreadPoolExecutor(max_workers=8) as executor:
        inspected = tuple(executor.map(inspect, probes))

    for candidate, layout, entry, status, error in inspected:
        if error is not None or status is None:
            result.append(
                RuntimeInstance(
                    layout.name,
                    layout.placement,
                    layout.instance,
                    instance_identity(layout),
                    "unknown",
                    None,
                    str(entry["workload"].get("status") or "unknown"),
                    f"inspection failed: {(error or 'agent is unavailable')[:180]}",
                )
            )
            continue
        workload_state = entry["workload"].get("status")
        reported_state = status.status
        diagnostic = None
        drivers: tuple[str, ...] | None = None
        if reported_state == "running":
            raw_drivers = entry["control"].get("drivers")
            if (
                isinstance(raw_drivers, list)
                and all(
                    isinstance(driver, str) and driver in ALL_DRIVERS
                    for driver in raw_drivers
                )
                and len(raw_drivers) == len(set(raw_drivers))
            ):
                drivers = tuple(raw_drivers)
                normalized_state = "running"
            else:
                normalized_state = "running"
                diagnostic = "effective launch drivers are missing or invalid"
        elif reported_state == "stopped" or (
            reported_state == "failed" and workload_state == "failed"
        ):
            normalized_state = "not running"
        else:
            normalized_state = "unknown"
            observed = str(workload_state or reported_state)
            detail = status.message or f"observed {observed}"
            diagnostic = f"{detail[:180]}"
        if normalized_state == "not running" and not read_only:
            if entry["control"].get("ref") is None:
                remove_registration(layout, entry["launch_id"])
                continue
            normalized_state = "unknown"
            diagnostic = "runtime resources have not been released"
        if diagnostic is not None:
            observed_state = str(workload_state or reported_state)
        else:
            observed_state = None
        result.append(
            RuntimeInstance(
                layout.name,
                layout.placement,
                layout.instance,
                instance_identity(layout),
                normalized_state,
                drivers,
                observed_state,
                diagnostic,
            )
        )

    def sort_key(item: RuntimeInstance) -> tuple[int, str, str]:
        if item.state == "running":
            priority = (
                0
                if item.drivers is not None and "talk" in item.drivers
                else 1
                if item.drivers is not None
                else 2
            )
        elif item.state == "unknown":
            priority = 3
        else:
            priority = 4
        return priority, item.label.casefold(), item.identity

    # Discovery references are observations, not leases. A completed probe or
    # read failure cannot speak for a registration that departed or was replaced.
    current: dict[str, _SnapshotCandidate] = {}
    for candidate in candidates_list:
        try:
            if not _registration_current(candidate.registration, candidate.launch_id):
                continue
        except (OSError, ValueError, TypeError) as exc:
            diagnostics.append(f"runtime registry {candidate.registration.name}: {exc}")
        if candidate.layout is None:
            diagnostics.append(
                candidate.diagnostic or "runtime identity is unavailable"
            )
        else:
            current[instance_identity(candidate.layout)] = candidate
    result = [item for item in result if item.identity in current]
    result.sort(key=sort_key)
    diagnostics = sorted(set(diagnostics), key=str.casefold)
    layouts = {
        instance_identity(candidate.layout): candidate.layout
        for candidate in candidates_list
        if candidate.layout is not None
    }
    endpoints = {
        instance_identity(layout): status.endpoint
        for _, layout, _, status, _ in inspected
        if status is not None and status.status == "running"
    }
    return RuntimeInventory(
        RuntimeInstanceSnapshot(tuple(result), tuple(diagnostics)),
        tuple(
            RuntimeSource(
                item,
                layouts[item.identity],
                endpoints.get(item.identity),
                current[item.identity].registration,
                current[item.identity].launch_id,
            )
            for item in result
        ),
    )
