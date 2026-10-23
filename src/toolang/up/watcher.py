"""Select runtime membership and conditionally repair expired registrations."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

from toolang.common.layout import AgentLayout
from .instance import LockRecovery, read_instance, recover_control_write_lock
from .observation import (
    RuntimeInventory,
    RuntimeProbe,
    RuntimeSource,
    runtime_source,
    webui_url,
)
from .types import AgentStatus, RuntimeInstanceSnapshot


def _repairable(status: AgentStatus) -> bool:
    return bool(status.entry and status.entry["launch_id"]) and status.status in {
        "stopped",
        "failed",
    }


def _recheck_status(layout, status, *, changed=False):
    """Deferred writes cannot turn an obsolete death probe into a departure."""
    try:
        if (
            not changed
            and read_instance(layout.runtime_status, layout.instance) == status.entry
        ):
            return status
        refreshed = RuntimeProbe(layout).status(ui_base_url="", cache=False)
        if (
            refreshed is not None
            and refreshed.entry is not None
            and read_instance(layout.runtime_status, layout.instance) == refreshed.entry
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


def _repair_status(layout, status, recovery, reference_path=None):
    from .registry import repair_registration

    result = repair_registration(
        layout,
        status.entry,
        resource_dead=status.alive is False,
        recovery=recovery,
        reference_path=reference_path,
    )
    return (
        status
        if result == "retired"
        else _recheck_status(layout, status, changed=result == "changed")
    )


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
    if repair:
        recover_control_write_lock(layout.runtime_status)
    status = RuntimeProbe(layout).status(
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
                status, webui_url=webui_url(status.endpoint, ui_base_url=ui_base_url)
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

    status = staticmethod(runtime_status)

    def refresh(self) -> RuntimeInventory:
        from .discovery import agent_layouts, registered_inventory

        diagnostics = []
        complete = True
        try:
            self._homes = agent_layouts(self.root, agent=self.agent)
        except OSError as exc:
            diagnostics.append(f"resident homes: {exc}")
            complete = False
        homes_complete = complete
        try:
            runtime = registered_inventory(
                self.root, agent=self.agent, residents_only=self.residents_only
            )
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
                registered[identity] = runtime_source(
                    old.layout,
                    status,
                    registration=old.registration,
                    launch_id=old.launch_id,
                )
        if homes_complete:
            for identity, source in tuple(registered.items()):
                if (
                    source.layout.placement == "resident"
                    and f"{source.layout.address}.json" not in known_homes
                ):
                    del registered[identity]
                    diagnostics.append(
                        f"runtime registry {identity}: resident home is absent"
                    )
        self._registered = registered
        scoped = dict(registered)
        pending = [layout for layout in self._homes if layout.address not in scoped]

        def inspect_home(layout):
            return layout, RuntimeProbe(layout).status(ui_base_url="", cache=True)

        with ThreadPoolExecutor(max_workers=8) as pool:
            for layout, status in pool.map(inspect_home, pending):
                if status is not None:
                    scoped[layout.address] = runtime_source(layout, status)
        recovery = LockRecovery()
        stale = []
        for source in scoped.values():
            # Live guests can also be waiting on a dead host writer.
            recover_control_write_lock(source.layout.runtime_status)
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
            for index, source in enumerate(ordered):
                status = (
                    _repair_status(
                        source.layout,
                        source.observation,
                        recovery,
                        source.registration,
                    )
                    if index < 8
                    else _recheck_status(source.layout, source.observation)
                )
                scoped[source.instance.identity] = runtime_source(
                    source.layout,
                    status,
                    registration=source.registration,
                    launch_id=status.entry["launch_id"]
                    if status.entry
                    else source.launch_id,
                )
            # The first candidate gets the exceptional probe budget. Rotate it
            # even when every candidate fits inside the eight repair attempts.
            self._repair_offset = (offset + 1) % len(stale)
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
        else:
            complete = complete and all(
                source.layout.placement == "resident"
                or source.instance.state != "unknown"
                for source in sources
            )
        return RuntimeInventory(
            RuntimeInstanceSnapshot(
                tuple(source.instance for source in sources),
                tuple(diagnostics),
                complete,
            ),
            sources,
            runtime.registrations,
        )


def runtime_snapshot(root: Path) -> RuntimeInstanceSnapshot:
    return runtime_inventory(root).snapshot


def runtime_inventory(root: Path, *, agent: str | None = None) -> RuntimeInventory:
    """Collect the same scope used by the interactive runtime monitors."""
    return RuntimeWatcher(root, agent=agent).refresh()
