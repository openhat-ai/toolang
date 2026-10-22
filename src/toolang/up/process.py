"""Control and inspect materialized agent workloads."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
from pathlib import Path
import time
from urllib.parse import urlsplit
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

from toolang.common.layout import AgentLayout
from ..common.progress import ProgressSink, emit_progress
from toolang.up.instance import (
    SandboxState,
    read_instance,
    update_instance,
    process_alive,
    recover_control_write_lock,
)

_LIVENESS_CACHE: dict = {}

if TYPE_CHECKING:
    from toolang.up.sandbox import LaunchSpec, SandboxHandle


@dataclass(frozen=True, slots=True)
class AgentStatus:
    """One listed agent status row."""

    name: str
    status: str
    endpoint: str | None
    api_url: str | None
    webui_url: str | None
    sandbox: str | None
    message: str | None = None

    @property
    def port(self) -> int | None:
        """Return the runtime endpoint port when one is known."""

        if self.endpoint is None:
            return None
        try:
            return urlsplit(self.endpoint).port
        except ValueError:
            return None


class AgentProcess:
    """Control and inspect one materialized AgentServer process."""

    def __init__(self, layout: AgentLayout) -> None:
        self.layout = layout

    async def start(
        self, spec: LaunchSpec, *, progress: ProgressSink | None = None
    ) -> SandboxHandle:
        from toolang.up.sandbox import launch

        return await launch(spec, progress=progress)

    async def stop(
        self,
        *,
        force: bool = False,
        progress: ProgressSink | None = None,
        expected_launch_id: str | None = None,
    ) -> bool:
        from toolang.up.sandbox import stop

        return await stop(
            self.layout,
            force=force,
            progress=progress,
            **(
                {"expected_launch_id": expected_launch_id}
                if expected_launch_id is not None
                else {}
            ),
        )

    @property
    def reference(self) -> SandboxState | None:
        return SandboxState.load(
            self.layout.runtime_status, instance=self.layout.instance
        )

    def state(self) -> dict[str, object] | None:
        """Return descriptive data only for the referenced workload."""

        entry = read_instance(self.layout.runtime_status, self.layout.instance)
        return self._report(entry, SandboxState.from_instance(entry))

    def wait_ready(
        self,
        status: AgentStatus,
        *,
        ui_base_url: str,
        timeout: float,
        progress: ProgressSink | None = None,
    ) -> AgentStatus:
        """Wait for an existing launch, reporting the blocking readiness check."""
        item_id = f"runtime:{self.layout.name}:ready"
        emit_progress(
            progress,
            id=item_id,
            kind="runtime",
            stage="start",
            label="Waiting for agent API...",
            status="running",
            detail=self.layout.name,
        )
        current: AgentStatus | None = status
        deadline = time.monotonic() + timeout
        try:
            while current is not None and current.status in {"preparing", "starting"}:
                if time.monotonic() >= deadline:
                    raise ValueError(
                        f"agent {self.layout.name} did not become ready; see {self.layout.runtime_log}"
                    )
                time.sleep(0.1)
                current = self.status(ui_base_url=ui_base_url, check_health=True)
            if current is None or current.status != "running":
                raise ValueError(
                    f"agent {self.layout.name} stopped before becoming ready; see {self.layout.runtime_log}"
                )
        except BaseException:
            emit_progress(
                progress,
                id=item_id,
                kind="runtime",
                stage="start",
                label="Agent did not become ready",
                status="failed",
            )
            raise
        emit_progress(
            progress,
            id=item_id,
            kind="runtime",
            stage="start",
            label="Agent API ready",
            status="ok",
        )
        return current

    def _report(
        self, entry, reference: SandboxState | None
    ) -> dict[str, object] | None:
        report = _runtime_report(entry)
        if report is None or reference is None:
            return report
        if report.get("launch_id") and report["launch_id"] != reference.launch_id:
            return None
        ref = reference.ref
        if ref.runtime_kind == "process":
            matches = (
                (
                    str(report.get("pid")) == ref.runtime_id
                    or (
                        report.get("pid") is None
                        and report.get("status") in {"stopped", "failed"}
                    )
                )
                and report.get("process_created") == ref.meta.get("created")
                and isinstance(ref.meta.get("created"), (int, float))
            )
        else:
            if report.get("launch_id"):
                return report
            instance = report.get("sandbox_instance")
            matches = isinstance(instance, str) and instance in {
                ref.runtime_id,
                ref.runtime_name,
            }
            if reference.sandbox.partition(":")[0] == "docker" and isinstance(
                instance, str
            ):
                # Docker's guest bootstrap reports its default short hostname.
                matches = matches or (
                    len(instance) == 12
                    and all(char in "0123456789abcdef" for char in instance)
                    and ref.runtime_id.startswith(instance)
                )
        return report if matches else None

    def status(
        self,
        *,
        ui_base_url: str,
        check_health: bool = False,
        cache: bool = False,
        snapshot: dict | None = None,
        read_only: bool = False,
    ) -> AgentStatus | None:
        """Inspect liveness; read-only probes skip lock recovery and state repair."""

        if not self.layout.home.is_dir():
            return None
        if not read_only:
            recover_control_write_lock(self.layout.runtime_status)
        from toolang.up.sandbox import load_state_sandbox, _health_ready

        endpoint = None
        sandbox = None
        message = None
        status = "stopped"
        try:
            observed = (
                snapshot
                if snapshot is not None
                else read_instance(self.layout.runtime_status, self.layout.instance)
            )
            reference = SandboxState.from_instance(observed)
            report = self._report(observed, reference) or {}
            if reference is not None:
                sandbox = reference.sandbox
                implementation = load_state_sandbox(self.layout, reference)
                alive = self._alive(reference, implementation, observed, cache=cache)
                if (
                    report.get("status") in {"stopped", "failed"}
                    and report.get("pid") is None
                ):
                    status = str(report["status"])
                elif alive:
                    endpoint = reference.ref.endpoint
                    status = (
                        "running"
                        if (not check_health and report.get("status") == "running")
                        or (
                            (check_health or not cache)
                            and _health_ready(f"{endpoint.rstrip('/')}/healthz")
                        )
                        else "starting"
                    )
                elif alive is False:
                    operation = (observed or {}).get("control", {}).get("operation")
                    if (
                        operation is not None
                        and process_alive(
                            operation.get("pid"), operation.get("created")
                        )
                        is not False
                    ):
                        status = "preparing"
                    elif not read_only:
                        self.reconcile(observed)
                    if status != "preparing" and report.get("status") == "failed":
                        status = "failed"
                else:
                    status, message = (
                        "unknown",
                        "workload identity could not be verified",
                    )
            elif report.get("status") in {"preparing", "starting", "running"}:
                entry = observed
                operation = (entry or {}).get("control", {}).get("operation")
                native = (entry or {}).get("control", {}).get(
                    "sandbox", "host"
                ) == "host"
                alive = (
                    process_alive(report.get("pid"), report.get("process_created"))
                    if native
                    else None
                )
                if (
                    operation is not None
                    and process_alive(operation.get("pid"), operation.get("created"))
                    is not False
                    and alive is not True
                ):
                    status = "preparing"
                elif not native:
                    # A guest PID has no meaning in this host namespace. Without
                    # its resource reference there is no valid liveness probe.
                    status = "unknown"
                else:
                    if alive is False:
                        if not read_only:
                            self.reconcile(entry)
                    elif alive is True:
                        status = str(report.get("status"))
                        live_endpoint = report.get("endpoint")
                        if (
                            report.get("api_version") is not None
                            and isinstance(live_endpoint, str)
                            and live_endpoint.strip()
                        ):
                            endpoint = live_endpoint.strip()
                            sandbox = "host"
                            if check_health and not _health_ready(
                                f"{endpoint}/healthz"
                            ):
                                status = "starting"
                    else:
                        status = "unknown"
            elif report.get("status") == "failed":
                status = "failed"
            raw_message = report.get("message")
            if isinstance(raw_message, str):
                message = raw_message
            if self.layout.legacy_sandbox_state.is_file():
                raise ValueError("legacy sandbox state requires manual cleanup")
        except (ImportError, OSError, RuntimeError, TypeError, ValueError) as exc:
            status, message = "failed", str(exc)
        return AgentStatus(
            name=self.layout.name,
            status=status,
            endpoint=endpoint,
            api_url=_api_docs_url(endpoint) if endpoint else None,
            webui_url=_webui_url(endpoint, ui_base_url=ui_base_url)
            if endpoint and status == "running"
            else None,
            sandbox=sandbox,
            message=message,
        )

    def _alive(self, reference, implementation, entry, *, cache: bool):
        key = self._cache_key(reference)
        now = time.monotonic()
        fingerprint = json.dumps(entry, sort_keys=True)
        previous = _LIVENESS_CACHE.get(key) if cache else None
        if (
            previous is not None
            and previous[0] + 1 > now
            and previous[1] == fingerprint
        ):
            return previous[2]

        async def probe():
            return await asyncio.wait_for(
                implementation.running(reference.ref), timeout=2
            )

        try:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                alive = asyncio.run(probe())
            else:
                # Synchronous status reads also run during async launch resolution.
                with ThreadPoolExecutor(max_workers=1) as pool:
                    alive = pool.submit(lambda: asyncio.run(probe())).result()
        except (OSError, RuntimeError, ValueError, TimeoutError):
            alive = None
        if cache:
            if len(_LIVENESS_CACHE) > 256:
                _LIVENESS_CACHE.clear()
            _LIVENESS_CACHE[key] = (now, fingerprint, alive)
        return alive

    def _cache_key(self, reference):
        return (
            self.layout.runtime_status,
            self.layout.instance,
            reference.launch_id,
            repr(reference.ref.to_data()),
        )

    @classmethod
    def cache_docker_states(cls, owners):
        from toolang.plugin.sandboxes.docker._cli import docker_container_states

        pending = []
        now = time.monotonic()
        for owner, reference, entry in owners:
            key = owner._cache_key(reference)
            fingerprint = json.dumps(entry, sort_keys=True)
            cached = _LIVENESS_CACHE.get(key)
            if cached is None or cached[0] + 1 <= now or cached[1] != fingerprint:
                pending.append((key, fingerprint, reference.ref.runtime_id))
        states = docker_container_states(
            tuple(dict.fromkeys(identity for _, _, identity in pending))
        )
        if len(_LIVENESS_CACHE) > 256:
            _LIVENESS_CACHE.clear()
        for key, fingerprint, identity in pending:
            _LIVENESS_CACHE[key] = (now, fingerprint, states.get(identity))

    def reconcile(self, observed) -> bool:
        """Compare the probe snapshot under a nonblocking lock; retain recovery refs."""
        if observed is None or observed["workload"].get("status") not in {
            "running",
            "starting",
            "preparing",
        }:
            return False
        changed = False

        def correct(entry):
            nonlocal changed
            if entry != observed:
                return
            operation = entry["control"].get("operation")
            if (
                operation is not None
                and process_alive(operation.get("pid"), operation.get("created"))
                is not False
            ):
                return
            workload = entry["workload"]
            if workload.get("status") not in {"running", "starting", "preparing"}:
                return
            workload.update(
                status="stopped",
                pid=None,
                observed_dead_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            )
            changed = True

        try:
            update_instance(
                self.layout.runtime_status,
                correct,
                instance=self.layout.instance,
                launch_id=observed["launch_id"],
                blocking=False,
                dead_guest_launch=observed["launch_id"]
                if observed["control"].get("ref", {}).get("runtime_kind") != "process"
                else None,
            )
        except (OSError, ValueError):
            pass
        return changed

    @classmethod
    def list(cls, root: Path, *, ui_base_url: str) -> tuple[AgentStatus, ...]:
        from .discovery import agent_layouts

        layouts = tuple(agent_layouts(root))
        if not layouts:
            return ()
        with ThreadPoolExecutor(max_workers=min(8, len(layouts))) as pool:
            statuses = pool.map(
                lambda layout: cls(layout).status(ui_base_url=ui_base_url), layouts
            )
            return tuple(status for status in statuses if status is not None)


def preferred_runtime_port(layout: AgentLayout) -> int | None:
    """Return one previously used port for an agent when available."""

    entry = read_instance(layout.runtime_status, layout.instance)
    remembered = (entry or {}).get("control", {}).get("last_successful", {})
    return _runtime_state_port(remembered)


def assigned_runtime_ports(
    toolang_root: Path,
    *,
    exclude_agent: str | None = None,
) -> set[int]:
    """Return peer ports reserved by active global registrations."""

    from .observation import running_instances

    ports: set[int] = set()
    for entry in running_instances(toolang_root):
        if entry.get("address", entry.get("name")) == exclude_agent:
            continue
        port = _runtime_state_port(entry)
        if port is not None:
            ports.add(port)
    return ports


def _runtime_state_port(runtime_state: dict[str, object] | None) -> int | None:
    """Return one parsed port from runtime state when available."""

    raw_endpoint = runtime_state.get("endpoint") if runtime_state else None
    if not isinstance(raw_endpoint, str) or not raw_endpoint.strip():
        return None
    try:
        return urlsplit(raw_endpoint).port
    except ValueError:
        return None


def runtime_identity_row(
    runtime_state: dict[str, object] | None,
    *,
    layout: AgentLayout | None = None,
) -> tuple[str, str] | None:
    """Return one human-readable workload identity for runtime info output."""

    if layout is not None:
        state = SandboxState.load(layout.runtime_status, instance=layout.instance)
        if state is not None:
            ref = state.ref
            if ref.runtime_kind == "process":
                return "PID", ref.runtime_id
            if (
                ref.runtime_kind == "container"
                or state.sandbox.partition(":")[0] == "docker"
            ):
                short_id = (
                    ref.runtime_id[:12]
                    if len(ref.runtime_id) >= 12
                    and all(
                        char.casefold() in "0123456789abcdef" for char in ref.runtime_id
                    )
                    else ref.runtime_id
                )
                runtime_name = ref.runtime_name
                value = (
                    f"{runtime_name} ({short_id})"
                    if runtime_name is not None and runtime_name != short_id
                    else short_id
                )
                return "Container", value
            return "Runtime", f"{ref.runtime_kind}:{ref.runtime_id}"
        return None
    pid = (runtime_state or {}).get("pid")
    if isinstance(pid, int) and pid > 0:
        return "PID", str(pid)
    return None


def _load_runtime_state(
    path: Path, *, instance: str | None = None
) -> dict[str, object] | None:
    return _runtime_report(read_instance(path, instance))


def _runtime_report(entry) -> dict[str, object] | None:
    if entry is None:
        return None
    control = entry["control"]
    return {
        **control.get("last_successful", {}),
        **entry["workload"],
        "launch_id": entry["launch_id"],
    }


def _webui_url(endpoint: str | None, *, ui_base_url: str) -> str | None:
    if endpoint is None or not endpoint.strip():
        return None
    try:
        port = urlsplit(endpoint).port
    except ValueError:
        return None
    if port is None:
        return None
    return f"{ui_base_url.rstrip('/')}/{port}"


def _api_docs_url(endpoint: str | None) -> str | None:
    if endpoint is None or not endpoint.strip():
        return None
    return f"{endpoint.rstrip('/')}/docs"
