"""Observe runtime membership and repair stale registrations without executing agents."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from pathlib import Path
import time
from urllib.parse import urlsplit

from toolang.common.layout import AgentLayout
from .instance import SandboxState, process_alive, read_instance
from .types import ALL_DRIVERS, AgentStatus, RuntimeInstance, RuntimeInstanceSnapshot
from .observation import RuntimeInventory, RuntimeSource

_LIVENESS_CACHE: dict = {}


class _RuntimeProbe:
    def __init__(self, layout: AgentLayout) -> None:
        self.layout = layout

    def _report(
        self, entry, reference: SandboxState | None
    ) -> dict[str, object] | None:
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
        from toolang.up.sandbox import _health_ready

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
            reference = SandboxState.from_instance(observed)
            report = self._report(observed, reference) or {}
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
                    operation = (observed or {}).get("control", {}).get("operation")
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
            webui_url=_webui_url(endpoint, ui_base_url=ui_base_url)
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


def _runtime_source(layout, status, *, registration=None, launch_id=None):
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


def _repairable(status: AgentStatus) -> bool:
    return bool(status.entry and status.entry["launch_id"]) and status.status in {
        "stopped",
        "failed",
    }


def _repair_status(layout, status, recovery, reference_path=None):
    from .registry import repair_registration

    result = repair_registration(
        layout,
        status.entry,
        resource_dead=status.alive is False,
        recovery=recovery,
        reference_path=reference_path,
    )
    if result == "changed":
        # A launcher changed the full entry after the probe. Observe the new
        # generation once, then let the next refresh perform any further repair.
        try:
            refreshed = _RuntimeProbe(layout).status(ui_base_url="", cache=False)
            if (
                refreshed is not None
                and read_instance(layout.runtime_status, layout.instance)
                == refreshed.entry
            ):
                return refreshed
        except (OSError, ValueError):
            pass
        return replace(
            status,
            status="unknown",
            endpoint=None,
            api_url=None,
            webui_url=None,
            message="runtime changed during inspection",
        )
    return status


def runtime_status(
    layout: AgentLayout,
    *,
    ui_base_url: str = "",
    check_health: bool = False,
    cache: bool = True,
    snapshot: dict | None = None,
    repair: bool = True,
) -> AgentStatus | None:
    """Inspect one selected home without enumerating other agents."""
    from .registry import LockRecovery

    status = _RuntimeProbe(layout).status(
        ui_base_url=ui_base_url,
        check_health=check_health,
        cache=cache,
        snapshot=snapshot,
    )
    if repair and status is not None and _repairable(status):
        recovery = LockRecovery()
        assert status.entry is not None
        recovery.remember(status.entry, status.alive)
        status = _repair_status(layout, status, recovery)
        if status.endpoint:
            status = replace(
                status, webui_url=_webui_url(status.endpoint, ui_base_url=ui_base_url)
            )
    return status


class RuntimeWatcher:
    """Observe resident homes and active registered visitors as one inventory."""

    def __init__(
        self, root: Path, *, agent: str | None = None, residents_only: bool = False
    ) -> None:
        self.root = root.expanduser().resolve()
        self.agent = agent
        self.residents_only = residents_only
        self._homes: tuple[AgentLayout, ...] = ()
        self._registered: dict[str, RuntimeSource] = {}
        self._repair_offset = 0

    @staticmethod
    def status(layout: AgentLayout, **kwargs) -> AgentStatus | None:
        return runtime_status(layout, **kwargs)

    def refresh(self) -> RuntimeInventory:
        from .discovery import agent_layouts
        from .observation import _registered_inventory
        from .registry import LockRecovery

        diagnostics = []
        complete = True
        try:
            self._homes = agent_layouts(self.root, agent=self.agent)
        except OSError as exc:
            diagnostics.append(f"resident homes: {exc}")
            complete = False
        homes_complete = complete
        try:
            runtime = _registered_inventory(self.root, agent=self.agent)
        except (OSError, RuntimeError, ValueError) as exc:
            runtime = RuntimeInventory(
                RuntimeInstanceSnapshot((), (f"runtime registry: {exc}",), False),
                (),
                None,
            )
        diagnostics.extend(runtime.snapshot.diagnostics)
        known_homes = {f"{layout.address}.json" for layout in self._homes}
        resolved = {source.registration for source in runtime.sources}
        complete = (
            complete
            and runtime.registrations is not None
            and all(
                path in resolved or path.name in known_homes
                for path in (runtime.registrations or ())
            )
            and all(
                source.layout.placement == "resident"
                or source.instance.state != "unknown"
                for source in runtime.sources
            )
        )
        registered = {source.instance.identity: source for source in runtime.sources}
        # Preserve only unresolved references that still exist, or all previous
        # bindings if enumeration itself failed. One bad peer cannot freeze others.
        for identity, old in self._registered.items():
            if (
                old.observation is not None
                and identity not in registered
                and (
                    runtime.registrations is None
                    or old.registration in runtime.registrations
                )
            ):
                status = replace(
                    old.observation,
                    status="unknown",
                    endpoint=None,
                    message="runtime registration could not be resolved",
                )
                registered[identity] = _runtime_source(
                    old.layout,
                    status,
                    registration=old.registration,
                    launch_id=old.launch_id,
                )
        self._registered = registered
        scoped = dict(registered)
        pending = [layout for layout in self._homes if layout.address not in scoped]

        def inspect_home(layout):
            return layout, _RuntimeProbe(layout).status(ui_base_url="", cache=True)

        with ThreadPoolExecutor(max_workers=8) as pool:
            for layout, status in pool.map(inspect_home, pending):
                if status is not None:
                    scoped[layout.address] = _runtime_source(layout, status)
        recovery = LockRecovery()
        stale = []
        for source in scoped.values():
            status = source.observation
            if status is not None and status.entry is not None:
                recovery.remember(status.entry, status.alive)
                if _repairable(status) and (
                    source.registration is not None
                    or status.entry["workload"].get("status")
                    in {"preparing", "starting", "running"}
                ):
                    stale.append(source)
        # Repair is bounded independently of membership. Rotate attempts so busy
        # early entries cannot starve later stale registrations in a monitor.
        if stale:
            stale.sort(key=lambda source: source.instance.identity)
            offset = self._repair_offset % len(stale)
            ordered = stale[offset:] + stale[:offset]
            for source in ordered[:8]:
                status = _repair_status(
                    source.layout, source.observation, recovery, source.registration
                )
                scoped[source.instance.identity] = _runtime_source(
                    source.layout,
                    status,
                    registration=source.registration,
                    launch_id=status.entry["launch_id"]
                    if status.entry
                    else source.launch_id,
                )
            self._repair_offset = (offset + min(8, len(stale))) % len(stale)
        sources = tuple(
            sorted(
                (
                    source
                    for source in scoped.values()
                    if (
                        not self.residents_only or source.layout.placement == "resident"
                    )
                    and (
                        source.layout.placement == "resident"
                        or source.instance.state not in {"stopped", "failed"}
                    )
                ),
                key=lambda source: (
                    source.instance.label.casefold(),
                    source.instance.identity,
                ),
            )
        )
        if self.residents_only:
            complete = homes_complete
        return RuntimeInventory(
            RuntimeInstanceSnapshot(
                tuple(source.instance for source in sources),
                tuple(diagnostics),
                complete,
            ),
            sources,
            runtime.registrations,
        )
