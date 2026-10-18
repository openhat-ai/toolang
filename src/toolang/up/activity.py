"""Compose source statistics and resident LiveTree at one publication boundary."""

from __future__ import annotations

from pathlib import Path
import time
from typing import TYPE_CHECKING

from toolang.stats.recent import recent_source

from .schemas import ActivitySnapshot

if TYPE_CHECKING:
    from .core import AgentCore


def activity_snapshot(
    source: Path,
    *,
    recent: int,
    core: AgentCore | None = None,
    now: int | None = None,
    before: tuple[str, str] | None = None,
    completed_at: int | None = None,
) -> ActivitySnapshot:
    with recent_source(source) as reader:
        if core is None:
            reader.pin()
            captured = int(time.time()) if now is None else now
            live = None
        else:
            with core.executor.stream.gate():
                reader.pin()
                captured = int(time.time()) if now is None else now
                live = core.executor.live_trees()
        # Neither completion scans nor serialization hold the publication gate.
        completed = reader.completed(
            now=captured if completed_at is None else completed_at,
            recent=recent,
            before=before,
        )
    return ActivitySnapshot(live=live, completed=completed)
