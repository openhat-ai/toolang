"""Probe one runtime and bind its status to a local history source."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import json
from pathlib import Path
import time
from urllib.parse import urlsplit

from toolang.common.layout import AgentLayout
from .identity import verify_identity
from .instance import SandboxState, process_alive, read_instance
from .registry import registration_current
from .types import ALL_DRIVERS, AgentStatus, RuntimeInstance, RuntimeInstanceSnapshot


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


_LIVENESS_CACHE: dict = {}


class RuntimeProbe:
    """Observe one runtime without preparation or registry mutation."""

    def __init__(self, layout: AgentLayout) -> None:
        self.layout = layout

    @staticmethod
    def report(entry, reference: SandboxState | None) -> dict[str, object] | None:
        report = _runtime_report(entry)
        if report is None or reference is None:
            return report
        if report.get("launch_id") and report["launch_id"] != reference.launch_id:
            return None
        if report.get("status") in {"stopped", "failed"} and report.get("pid") is None:
            return report
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
    ) -> AgentStatus | None:
        """Inspect one authoritative snapshot without mutating it."""

        try:
            if not self.layout.home.is_dir():
                return None
        except OSError as exc:
            return AgentStatus(
                self.layout.name, "unknown", None, None, None, None, str(exc)
            )
        if check_health:
            from .sandbox import _health_ready

        endpoint = None
        sandbox = None
        message = None
        status = "stopped"
        observed = None
        alive = None
        try:
            observed = (
                snapshot
                if snapshot is not None
                else read_instance(self.layout.runtime_status, self.layout.instance)
            )
            # Resource-only records have no layout bindings. Partial identity
            # records still require validation, even if their address is missing.
            if observed is not None and any(
                key in observed["control"] for key in ("address", "home", "place")
            ):
                try:
                    verify_identity(self.layout, observed["control"])
                except ValueError:
                    observed = None
                    raise
            operation = (observed or {}).get("control", {}).get("operation")
            if operation is not None and not isinstance(operation, dict):
                raise ValueError("runtime operation must be a mapping")
            reference = SandboxState.from_instance(observed)
            report = self.report(observed, reference) or {}
            if reference is not None:
                sandbox = reference.sandbox
                terminal = (
                    report.get("status") in {"stopped", "failed"}
                    and report.get("pid") is None
                )
                alive = (
                    None if terminal else self._alive(reference, observed, cache=cache)
                )
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
                            check_health
                            and _health_ready(f"{endpoint.rstrip('/')}/healthz")
                        )
                        else "starting"
                    )
                elif alive is False:
                    if (
                        operation is not None
                        and process_alive(
                            operation.get("pid"), operation.get("created")
                        )
                        is not False
                    ):
                        status = "preparing"
                    if status != "preparing" and report.get("status") == "failed":
                        status = "failed"
                else:
                    status, message = (
                        "unknown",
                        "workload identity could not be verified",
                    )
            elif report.get("status") in {"preparing", "starting", "running"}:
                entry = observed
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
                        pass
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
            status, message = "unknown", str(exc)
        return AgentStatus(
            name=self.layout.name,
            status=status,
            endpoint=endpoint,
            api_url=_api_docs_url(endpoint) if endpoint else None,
            webui_url=webui_url(endpoint, ui_base_url=ui_base_url)
            if endpoint and status == "running"
            else None,
            sandbox=sandbox,
            message=message,
            entry=observed,
            alive=alive,
        )

    def _alive(self, reference, entry, *, cache: bool):
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
            from .sandbox import load_state_sandbox

            implementation = load_state_sandbox(self.layout, reference)
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
        except TimeoutError:
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
        from toolang.plugin.sandboxes.docker._cli import docker_reference_states

        pending = []
        now = time.monotonic()
        for owner, reference, entry in owners:
            key = owner._cache_key(reference)
            fingerprint = json.dumps(entry, sort_keys=True)
            cached = _LIVENESS_CACHE.get(key)
            if cached is None or cached[0] + 1 <= now or cached[1] != fingerprint:
                pending.append((key, fingerprint, reference.ref))
        states = docker_reference_states(tuple(ref for _, _, ref in pending))
        if len(_LIVENESS_CACHE) > 256:
            _LIVENESS_CACHE.clear()
        for (key, fingerprint, _), alive in zip(pending, states, strict=True):
            _LIVENESS_CACHE[key] = (now, fingerprint, alive)


def _runtime_report(entry) -> dict[str, object] | None:
    if entry is None:
        return None
    control = entry["control"]
    return {
        **control.get("last_successful", {}),
        **entry["workload"],
        "launch_id": entry["launch_id"],
    }


def webui_url(endpoint: str | None, *, ui_base_url: str) -> str | None:
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


def runtime_source(layout, status, *, registration=None, launch_id=None):
    entry = status.entry
    drivers = None
    diagnostic = status.message
    if status.status == "running" and entry is not None:
        raw = entry["control"].get("drivers")
        if (
            isinstance(raw, list)
            and all(isinstance(item, str) and item in ALL_DRIVERS for item in raw)
            and len(set(raw)) == len(raw)
        ):
            drivers = tuple(raw)
        else:
            diagnostic = "effective launch drivers are missing or invalid"
    instance = RuntimeInstance(
        layout.name,
        layout.placement,
        layout.instance,
        layout.address,
        status.status,
        drivers,
        str(entry["workload"].get("status")) if diagnostic and entry else None,
        diagnostic,
    )
    return RuntimeSource(
        instance,
        layout,
        status.endpoint,
        registration,
        launch_id or (entry["launch_id"] if entry else None),
        status,
    )
