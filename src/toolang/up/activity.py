"""Read live activity and durable usage with shared source lifecycle rules."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from threading import Lock
import time
from typing import TYPE_CHECKING

import httpx

from toolang.api.version import AGENT_API_VERSION
from toolang.execution.schemas import LiveSnapshot
from toolang.stats.errors import StatsUnavailable
from toolang.stats.schemas import CompletedRoots, CompletionCounts, RecentUsage
from toolang.stats.types import Metrics, seconds, timestamp
from toolang.stats.recent import recent_source, recent_usage

from .observation import RuntimeSource
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
    known_roots: tuple[str, ...] = (),
    check_idle: bool = False,
    cancelled: Callable[[], bool] | None = None,
) -> ActivitySnapshot:
    with recent_source(source, cancelled=cancelled) as reader:
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
        present = {node.id for node in completed.roots}
        if live is not None:
            present.update(tree.root_run_id for tree in live.trees)
        retired = reader.completed_roots(
            now=captured if completed_at is None else completed_at,
            recent=recent,
            identities=tuple(
                identity for identity in known_roots if identity not in present
            ),
        )
        idle = check_idle and not reader.has_unfinished_roots()
    return ActivitySnapshot(live=live, completed=completed, retired=retired, idle=idle)


def _read_activity(
    source: RuntimeSource,
    recent: int,
    *,
    previous: LiveSnapshot | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> ActivitySnapshot:
    live_error = None
    endpoint = source.endpoint if source.is_current() else None
    if endpoint:
        try:
            response = httpx.get(
                endpoint.rstrip("/") + f"/api/v{AGENT_API_VERSION}/top/snapshot",
                params={"recent": recent},
                timeout=3,
                trust_env=False,
            )
            response.raise_for_status()
            result = ActivitySnapshot.model_validate(response.json())
            if result.live is None:
                result.live_error = "live snapshot is unavailable"
            if (
                previous is not None
                and result.live is not None
                and previous.epoch == result.live.epoch
            ):
                present = {tree.root_run_id for tree in result.live.trees}
                present.update(node.id for node in result.completed.roots)
                missing = tuple(
                    tree.root_run_id
                    for tree in previous.trees
                    if tree.root_run_id not in present
                )
                if missing:
                    # Within one epoch, absence from live means the immutable
                    # terminal record was committed before the pinned boundary.
                    try:
                        with recent_source(
                            source.layout.run_store, cancelled=cancelled
                        ) as reader:
                            reader.pin()
                            result.retired = reader.completed_roots(
                                now=seconds(result.completed.end),
                                recent=recent,
                                identities=missing,
                            )
                    except (StatsUnavailable, OSError, ValueError, RuntimeError) as exc:
                        # Supplemental history must not invalidate a healthy sample.
                        result.page_error = str(exc)
            if source.is_current():
                return result
        except (httpx.HTTPError, ValueError) as exc:
            # Preserve durable completions when the control plane disconnects.
            live_error = str(exc)
    result = activity_snapshot(
        source.layout.run_store,
        recent=recent,
        known_roots=tuple(tree.root_run_id for tree in previous.trees)
        if previous
        else (),
        check_idle=live_error is None and source.instance.needs_live_observation,
        cancelled=cancelled,
    )
    result.live_error = live_error if source.is_current() else None
    return result


class ActivityReader:
    """Read one logical source across generations, retaining established history."""

    def __init__(self) -> None:
        self._history_seen = False
        self._history_lock = Lock()

    def _remember_history(self) -> None:
        with self._history_lock:
            self._history_seen = True

    def _empty_history(self, binding: RuntimeSource) -> bool:
        # Workers share this fact so a lost established store never becomes zero.
        with self._history_lock:
            try:
                binding.layout.run_store.stat()
            except FileNotFoundError:
                return not self._history_seen and (
                    not binding.instance.needs_live_observation
                    or not binding.is_current()
                )
            self._history_seen = True
            return False

    def read_activity(
        self,
        binding: RuntimeSource,
        recent: int,
        *,
        previous: LiveSnapshot | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> ActivitySnapshot:
        try:
            empty = self._empty_history(binding)
        except OSError:
            # A published endpoint can observe history independently of local I/O.
            if not binding.endpoint:
                raise
            empty = False
        if not empty:
            try:
                result = _read_activity(
                    binding, recent, previous=previous, cancelled=cancelled
                )
            except StatsUnavailable:
                if not self._empty_history(binding):
                    raise
            else:
                self._remember_history()
                return result
        now = int(time.time())
        return ActivitySnapshot(
            live=None,
            completed=CompletedRoots(
                observed_at=timestamp(now),
                start=timestamp(now - recent),
                end=timestamp(now),
                counts=CompletionCounts(),
                roots=(),
            ),
        )

    def read_usage(
        self,
        binding: RuntimeSource,
        period: int,
        cancelled: Callable[[], bool] | None = None,
    ) -> RecentUsage:
        if not self._empty_history(binding):
            try:
                result = recent_usage(
                    binding.layout.run_store,
                    now=int(time.time()),
                    period=period,
                    cancelled=cancelled,
                )
            except StatsUnavailable:
                if not self._empty_history(binding):
                    raise
            else:
                self._remember_history()
                return result
        now = int(time.time())
        return RecentUsage(
            observed_at=timestamp(now),
            start=timestamp(now - period),
            end=timestamp(now),
            metrics=Metrics(),
        )

    def read_page(
        self,
        source: RuntimeSource,
        recent: int,
        before: tuple[str, str],
        completed_at: int,
        cancelled: Callable[[], bool] | None = None,
    ) -> CompletedRoots:
        with recent_source(source.layout.run_store, cancelled=cancelled) as reader:
            self._remember_history()
            reader.pin()
            return reader.completed(now=completed_at, recent=recent, before=before)
