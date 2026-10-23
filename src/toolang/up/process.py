"""Control and inspect materialized agent workloads."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import time
from urllib.parse import urlsplit
from typing import TYPE_CHECKING

from toolang.common.layout import AgentLayout
from ..common.progress import ProgressSink, emit_progress
from toolang.up.instance import (
    SandboxState,
    read_instance,
)

from .types import AgentStatus as AgentStatus
from .observation import RuntimeProbe, webui_url

if TYPE_CHECKING:
    from toolang.up.sandbox import LaunchSpec, SandboxHandle


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
        return RuntimeProbe.report(entry, SandboxState.from_instance(entry))

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

    def status(
        self,
        *,
        ui_base_url: str,
        check_health: bool = False,
        cache: bool = False,
        snapshot: dict | None = None,
        read_only: bool = False,
    ) -> AgentStatus | None:
        from .watcher import runtime_status

        return runtime_status(
            self.layout,
            ui_base_url=ui_base_url,
            check_health=check_health,
            cache=cache,
            snapshot=snapshot,
            repair=not read_only,
        )

    @classmethod
    def list(cls, root: Path, *, ui_base_url: str) -> tuple[AgentStatus, ...]:
        from .watcher import RuntimeWatcher

        inventory = RuntimeWatcher(root, residents_only=True).refresh()
        return tuple(
            replace(
                source.observation,
                webui_url=webui_url(source.endpoint, ui_base_url=ui_base_url)
                if source.instance.state == "running"
                else None,
            )
            for source in inventory.sources
            if source.observation is not None
        )


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

    from .discovery import active_runtime_endpoints

    ports: set[int] = set()
    for entry in active_runtime_endpoints(toolang_root):
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

    if layout is not None or (runtime_state or {}).get("ref") is not None:
        from toolang.base.types.sandbox import SandboxRef

        try:
            state = (
                SandboxState.load(layout.runtime_status, instance=layout.instance)
                if layout is not None
                else SandboxState(
                    str((runtime_state or {}).get("sandbox", "host")),
                    SandboxRef.from_data((runtime_state or {})["ref"]),
                )
            )
        except (OSError, KeyError, ValueError):
            return None
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
