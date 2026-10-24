"""Foreground invocation ownership and launch-fenced closing publication."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import math
from pathlib import Path
from uuid import uuid4

from toolang.common.files import atomic_write_text
from toolang.common.layout import AgentLayout
from .closing import ClosingDeadline, FINALIZATION_GRACE_SEC
from .identity import identity_bindings
from .instance import control_owner, process_alive, read_instance, update_instance
from .registry import remove_registration
from .workload import management_operation, owns_preparation


class ForegroundServerOwner:
    """Bind a foreground controller's deadline to its prepared or ready launch."""

    def __init__(self, layout: AgentLayout) -> None:
        self.layout = layout
        self.launch_id: str | None = None
        self._process = control_owner()
        self.closing = ClosingDeadline(on_begin=self._begin_closing)

    def __enter__(self) -> ForegroundServerOwner:
        return self

    def __exit__(self, *exc: object) -> None:
        self.closing.close()

    def bind(self, launch_id: str) -> None:
        self.launch_id = launch_id

    def _begin_closing(self, started: float, reason: str) -> None:
        if self.launch_id is None:
            snapshot = read_instance(self.layout.runtime_status, self.layout.instance)
            operation = (snapshot or {}).get("control", {}).get("operation", {})
            if not snapshot or any(
                operation.get(key) != value for key, value in self._process.items()
            ):
                return
            self.launch_id = snapshot["launch_id"]
        _publish_closing(self.layout, self.launch_id, started, reason)


@contextmanager
def script_invocation(
    layout: AgentLayout, *, prepare: bool = True, expected_launch_id: str | None = None
) -> Iterator[ClosingDeadline | None]:
    """Claim one roaming HOME for the entire foreground CLI invocation."""
    if layout.placement != "roaming":
        yield None
        return
    import asyncio
    from .identity import read_home_identity
    from .observation import RuntimeProbe
    from .sandbox import release_stopped

    assert layout.root is not None
    owner = {"token": uuid4().hex, **control_owner()}
    initial = read_home_identity(layout, toolang_root=layout.root)
    previous = (initial or {}).get("control", {}).get("script_owner")
    if previous is not None and (
        not isinstance(previous, dict)
        or process_alive(previous.get("pid"), previous.get("created")) is not False
    ):
        raise ValueError(f"HOME is busy: {layout.home}")
    with management_operation(layout, stage="script", wait=False):
        entry = read_home_identity(layout, toolang_root=layout.root)
        assert entry is not None
        if expected_launch_id is not None and entry["launch_id"] != expected_launch_id:
            raise ValueError("selected execution owner was replaced")
        previous = entry["control"].get("script_owner")
        if previous is not None and (
            not isinstance(previous, dict)
            or process_alive(previous.get("pid"), previous.get("created")) is not False
        ):
            raise ValueError(f"HOME is busy: {layout.home}")
        observation = RuntimeProbe(layout).status(ui_base_url="")
        if not owns_preparation(layout, entry) and (
            observation is None or observation.status not in {"stopped", "failed"}
        ):
            raise ValueError(f"HOME is busy: {layout.home}")
        asyncio.run(release_stopped(layout))
        remove_registration(layout, entry["launch_id"])

        def claim(current):
            if prepare:
                operation = current["control"].get("operation")
                current["launch_id"] = uuid4().hex
                current["control"] = identity_bindings(layout)
                if operation is not None:
                    current["control"]["operation"] = operation
                current["workload"] = {
                    "status": "preparing",
                    "pid": owner["pid"],
                    "process_created": owner["created"],
                }
            current["control"]["script_owner"] = owner

        if not update_instance(
            layout.runtime_status, claim, launch_id=entry["launch_id"]
        ):
            raise ValueError("selected execution owner was replaced")
    closing = ClosingDeadline(
        on_begin=lambda started, reason: _begin_script_closing(
            layout, owner, started, reason
        )
    )
    try:
        yield closing
    finally:

        def release(current):
            if current["control"].get("script_owner") == owner:
                current["control"].pop("script_owner", None)
                if owns_preparation(layout, current):
                    current["workload"].update(
                        status="failed",
                        pid=None,
                        message="script ended before runtime startup",
                    )

        try:
            update_instance(layout.runtime_status, release)
        finally:
            closing.close()


def _begin_script_closing(
    layout: AgentLayout, owner: dict, started: float, reason: str
) -> None:
    """Publish the first deadline to this invocation's mapped workload view."""
    snapshot = read_instance(layout.runtime_status)
    if snapshot is None or snapshot["control"].get("script_owner") != owner:
        return
    _publish_closing(layout, snapshot["launch_id"], started, reason, script_owner=owner)


def _publish_closing(
    layout: AgentLayout,
    launch_id: str,
    started: float,
    reason: str,
    *,
    script_owner: dict | None = None,
) -> None:
    """Publish one deadline only to the captured foreground generation."""
    snapshot = read_instance(layout.runtime_status, layout.instance)
    if snapshot is None or snapshot["launch_id"] != launch_id:
        return
    if (
        script_owner is not None
        and snapshot["control"].get("script_owner") != script_owner
    ):
        return
    previous = snapshot["workload"].get("shutdown_started_at")
    if isinstance(previous, int | float) and math.isfinite(previous):
        started = min(started, previous)
    name = f"owner-{snapshot['launch_id']}.closing"
    if Path(name).name != name:
        raise ValueError("invalid foreground launch identity")
    atomic_write_text(
        layout.runtime / name, str(math.ceil(started + FINALIZATION_GRACE_SEC))
    )

    def begin(entry):
        if (
            script_owner is not None
            and entry["control"].get("script_owner") != script_owner
        ):
            return
        entry["workload"].setdefault("shutdown_started_at", started)
        entry["workload"].setdefault("shutdown_reason", reason)

    update_instance(
        layout.runtime_status, begin, instance=layout.instance, launch_id=launch_id
    )
