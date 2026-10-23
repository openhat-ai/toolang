"""Observe agent runtime status through authoritative files and discovery references."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import re
from pathlib import Path
from typing import Any, TypeGuard, cast


from toolang.common import layout as paths
from toolang.common.layout import AgentLayout
from toolang.common.place_codes import root_code
from toolang.up.identity import layout_from_control, verify_identity
from toolang.up.instance import (
    read_document,
    read_instance,
)
from .registry import (
    instance_filename,
    read_reference,
    registration_current,
)
from toolang.up.types import (
    AgentStatus,
    RuntimeInstance as RuntimeInstance,
    RuntimeInstanceSnapshot as RuntimeInstanceSnapshot,
)


def instance_identity(layout: AgentLayout) -> str:
    return layout.address


def running_instances(root: Path) -> tuple[dict, ...]:
    """Return running endpoints from the shared runtime inventory."""
    from .watcher import RuntimeWatcher

    return tuple(
        {
            **source.observation.entry["control"],
            "agent": f"agent:{source.instance.label}",
            "instance": source.instance.identity,
            "endpoint": source.endpoint,
        }
        for source in RuntimeWatcher(root).refresh().sources
        if source.instance.state == "running"
        and source.observation is not None
        and source.observation.entry is not None
        and source.registration is not None
        and source.is_current()
    )


def active_layout(root: Path, address: str) -> tuple[AgentLayout, str]:
    """Resolve one exact active address without a historical or cross-root scan."""
    root = root.expanduser().resolve()
    path = root / ".runtime" / "active" / address / f"{address}.json"
    if not path.is_file():
        path = root / ".runtime" / "active" / f"{address}.json"
    reference = read_reference(path)
    if reference is None:
        raise FileNotFoundError(
            f"active instance not found: {address}; use a source path for offline access"
        )
    status_path = reference.status
    entry = read_instance(status_path)
    if entry is None or entry["launch_id"] != reference.launch_id:
        raise ValueError("selected execution owner was replaced")
    layout = _runtime_layout(entry["control"], status_path, root)
    if layout.address != address:
        raise ValueError("active instance does not match the selected agent")
    if layout.placement != "resident":
        from .private_paths import verify_root_namespace, verify_home_paths

        verify_root_namespace(layout.place.parent, root)
        verify_home_paths(layout.place, layout.name)
    return layout, entry["launch_id"]


@dataclass(frozen=True, slots=True)
class RuntimeSource:
    """Validated local source and optional running control endpoint."""

    instance: RuntimeInstance
    layout: AgentLayout
    endpoint: str | None
    registration: Path | None
    launch_id: str | None
    observation: AgentStatus | None = None

    def validate_history_identity(self) -> None:
        """Residents have fixed homes; nonresidents require full source ownership."""
        if self.layout.placement == "resident":
            return
        entry = read_instance(self.layout.runtime_status, self.layout.instance)
        if entry is None:
            raise ValueError("runtime source ownership record is unavailable")
        verify_identity(self.layout, entry["control"])

    def is_current(self) -> bool:
        return self.registration is not None and registration_current(
            self.registration, self.launch_id, self.layout.runtime_status
        )


@dataclass(frozen=True, slots=True)
class RuntimeInventory:
    snapshot: RuntimeInstanceSnapshot
    sources: tuple[RuntimeSource, ...]
    registrations: frozenset[Path] | None = frozenset()


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
    r"(?P<name>[a-z][a-z0-9_]{0,63})(?:@(?P<instance>t[0-9abcdefghjkmnpqrstvwxyz]{10}|s[0-9abcdefghjkmnpqrstvwxyz]{17}))?\.json\Z"
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
    place_code = match.group("instance")
    placement = (
        "resident"
        if place_code is None
        else "visiting"
        if place_code.startswith("t")
        else "roaming"
    )
    layout = AgentLayout(
        status_path.parents[3],
        name,
        placement,
        root=toolang_root,
        place_code=place_code,
    )
    if placement == "resident" and layout.place != toolang_root:
        return None
    if placement == "visiting" and layout.place != paths.VISITING_BASE / root_code(
        str(toolang_root)
    ) / str(layout.place_code):
        return None
    if placement == "roaming" and (
        layout.place.parent.name != root_code(str(toolang_root))
        or layout.place.parent.parent.name != ".toolang"
        or layout.place.name != layout.place_code
    ):
        return None
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
    layout = layout_from_control(control)
    recorded_root_path = control.get("root")
    if not isinstance(recorded_root_path, str):
        raise ValueError("runtime identity has no Toolang root")
    recorded_root = Path(recorded_root_path).expanduser()
    if (
        recorded_root.is_absolute()
        and _resolved(recorded_root) != _resolved(toolang_root)
    ) or _resolved(layout.runtime_status) != _resolved(status_path):
        raise ValueError("runtime identity does not match its registration")
    if layout.placement == "resident" and _resolved(layout.place) != _resolved(
        toolang_root
    ):
        raise ValueError("resident runtime is outside the selected Toolang root")
    return layout


def runtime_snapshot(root: Path) -> RuntimeInstanceSnapshot:
    return runtime_inventory(root).snapshot


def runtime_inventory(root: Path, *, agent: str | None = None) -> RuntimeInventory:
    """Collect the same scope used by the interactive runtime monitors."""
    from .watcher import RuntimeWatcher

    return RuntimeWatcher(root, agent=agent).refresh()


def _registered_inventory(root: Path, *, agent: str | None = None) -> RuntimeInventory:
    """Resolve and probe registered entries once; mutations belong to the watcher."""
    from .watcher import _RuntimeProbe, _runtime_source

    root = _resolved(root)
    selected_status = (
        _resolved(AgentLayout.resident(root, agent).runtime_status)
        if agent is not None
        else None
    )
    diagnostics: list[str] = []
    membership_complete = True
    enumerated = True
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

    instance_dir = root / ".runtime" / "active"
    try:
        reference_paths = sorted(
            (
                path
                for child in instance_dir.iterdir()
                for path in (
                    (child,)
                    if child.suffix == ".json"
                    else (child / f"{child.name}.json",)
                    if child.is_dir() and not child.is_symlink()
                    else ()
                )
                if path.is_file() and (agent is None or path.stem == agent)
            ),
            key=lambda path: str(path),
        )
    except FileNotFoundError:
        reference_paths = []
    except OSError as exc:
        reference_paths = []
        enumerated = membership_complete = False
        diagnostics.append(
            f"runtime registry: {str(exc).strip() or type(exc).__name__}"
        )

    registrations = set(reference_paths)
    for reference_path in reference_paths:
        fallback: AgentLayout | None = None
        try:
            reference = read_reference(reference_path)
            if reference is None:
                registrations.discard(reference_path)
                continue
            status_path = root / reference.status.expanduser()
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
            launch_id = reference.launch_id
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
            membership_complete = False
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
    docker = []
    from .instance import SandboxState

    for candidate in candidates_list:
        entry, layout = candidate.entry, candidate.layout
        if entry is None or layout is None:
            continue
        if str(entry["control"].get("sandbox")).partition(":")[0] == "docker":
            try:
                reference = SandboxState.from_instance(entry)
                if reference is not None:
                    docker.append((_RuntimeProbe(layout), reference, entry))
            except (TypeError, ValueError):
                pass
    try:
        _RuntimeProbe.cache_docker_states(docker)
    except (OSError, RuntimeError, ValueError):
        pass  # Individual probes preserve uncertainty when the batch is unavailable.

    def inspect(candidate: _SnapshotCandidate) -> RuntimeSource | None:
        layout = candidate.layout
        if layout is None:
            return None
        if candidate.diagnostic is not None or candidate.entry is None:
            status = AgentStatus(
                layout.name,
                "unknown",
                None,
                None,
                None,
                None,
                candidate.diagnostic or "runtime record is unavailable",
            )
        else:
            try:
                status = _RuntimeProbe(layout).status(
                    ui_base_url="", cache=True, snapshot=candidate.entry
                )
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                status = AgentStatus(
                    layout.name,
                    "unknown",
                    None,
                    None,
                    None,
                    None,
                    f"inspection failed: {exc}",
                )
            if status is None:
                status = AgentStatus(
                    layout.name,
                    "unknown",
                    None,
                    None,
                    None,
                    None,
                    "runtime home disappeared",
                )
        return _runtime_source(
            layout,
            status,
            registration=candidate.registration,
            launch_id=candidate.launch_id,
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        inspected = tuple(executor.map(inspect, candidates_list))
    sources = []
    for candidate, source in zip(candidates_list, inspected, strict=True):
        if source is None:
            membership_complete = False
            diagnostics.append(
                f"runtime registry {candidate.registration.name}: {candidate.diagnostic or 'runtime identity is unavailable'}"
            )
            continue
        try:
            if not source.is_current():
                reference = read_reference(source.registration)
                if reference is None:
                    registrations.discard(source.registration)
                elif reference.launch_id == source.launch_id:
                    diagnostics.append(
                        f"runtime registry {source.registration.name}: status path does not match the agent home"
                    )
                continue
        except (OSError, ValueError, TypeError) as exc:
            diagnostics.append(f"runtime registry {source.instance.label}: {exc}")
            membership_complete = False
            continue
        if (
            source.instance.placement != "resident"
            and source.instance.state == "unknown"
        ):
            membership_complete = False
        sources.append(source)
    return RuntimeInventory(
        RuntimeInstanceSnapshot(
            tuple(source.instance for source in sources),
            tuple(sorted(set(diagnostics))),
            membership_complete,
        ),
        tuple(sources),
        frozenset(registrations) if enumerated else None,
    )
