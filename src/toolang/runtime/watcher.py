"""Live prepared-snapshot refresh loop."""

from __future__ import annotations

from typing import TYPE_CHECKING

WATCH_INTERVAL_SEC = 1.0

if TYPE_CHECKING:
    from .process import RuntimeProcess


def watch_runtime_process(
    process: "RuntimeProcess",
    *,
    interval_sec: float = WATCH_INTERVAL_SEC,
) -> None:
    """Refresh the live prepared snapshot until the runtime stops."""

    stop_event = process.state.require_stop_event()
    while not stop_event.wait(interval_sec):
        process.refresh_live()
