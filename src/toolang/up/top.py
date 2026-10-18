"""Nonblocking local observation using the shared inventory and Stats interfaces."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
import time
from threading import Event

import httpx

from toolang.execution.schemas import RunNode
from toolang.stats.errors import StatsUnavailable
from toolang.stats.recent import recent_usage
from toolang.stats.schemas import RecentUsage
from toolang.stats.types import seconds, timestamp

from .activity import activity_snapshot
from .observation import (
    RuntimeInventory,
    RuntimeInstanceSnapshot,
    RuntimeSource,
    runtime_inventory,
)
from .schemas import ActivitySnapshot, SourceActivity, TopSnapshot


def read_activity(
    source: RuntimeSource,
    recent: int,
    before: tuple[str, str] | None = None,
    completed_at: int | None = None,
) -> ActivitySnapshot:
    if source.endpoint:
        try:
            params: dict[str, str | int] = {"recent": recent}
            if before is not None:
                params.update(before_time=before[0], after_id=before[1])
            if completed_at is not None:
                params["completed_at"] = completed_at
            response = httpx.get(
                source.endpoint.rstrip("/") + "/api/v1/top/snapshot",
                params=params,
                timeout=3,
                trust_env=False,
            )
            response.raise_for_status()
            return ActivitySnapshot.model_validate(response.json())
        except (httpx.HTTPError, ValueError):
            # Preserve durable completions when the control plane disconnects.
            pass
    return activity_snapshot(
        source.layout.run_store, recent=recent, before=before, completed_at=completed_at
    )


def read_usage(
    source: RuntimeSource, period: int, cancelled: Callable[[], bool] | None = None
) -> RecentUsage:
    # Local source reads avoid an HTTP timeout turning a busy eight-hour scan into
    # a false zero. Each scan runs independently of live snapshots and input.
    return recent_usage(
        source.layout.run_store,
        now=int(time.time()),
        period=period,
        cancelled=cancelled,
    )


@dataclass
class _Source:
    binding: RuntimeSource
    seen: float
    value: SourceActivity
    activity: Future[ActivitySnapshot] | None = None
    usage: Future[RecentUsage] | None = None
    activity_recent: int = 0
    usage_period: int = 0
    next_activity: float = 0
    next_usage: float = 0
    cursor: tuple[str, str] | None = None
    cursor_at: int | None = None
    request_page: bool = False
    page_in_flight: bool = False
    loaded: dict[str, RunNode] = field(default_factory=dict)


class TopObserver:
    """Own observation jobs; the UI thread only drains completed futures."""

    def __init__(
        self, root: Path, *, agent: str | None, period: int, recent: int, refresh: float
    ) -> None:
        self.root, self.agent = root, agent
        self.period, self.recent, self.refresh = period, recent, refresh
        self.inventory = RuntimeInstanceSnapshot(())
        self.sources: dict[str, _Source] = {}
        self.discovery = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="top-inventory"
        )
        self.live_pool = ThreadPoolExecutor(
            max_workers=8, thread_name_prefix="top-live"
        )
        self.usage_pool = ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="top-usage"
        )
        self.discovery_job: Future[RuntimeInventory] | None = None
        self.next_discovery = 0.0
        self.discovered = False
        self.stopped = Event()

    def close(self) -> None:
        self.stopped.set()
        for pool in (self.discovery, self.live_pool, self.usage_pool):
            pool.shutdown(wait=False, cancel_futures=True)

    def set_period(self, period: int) -> None:
        self.period = period
        for source in self.sources.values():
            source.value.usage = None
            source.value.usage_error = None
            source.next_usage = 0

    def set_recent(self, recent: int) -> None:
        self.recent = recent
        for source in self.sources.values():
            source.value.completed = None
            source.value.activity_error = None
            source.loaded.clear()
            source.cursor = None
            source.cursor_at = None
            source.request_page = False
            source.next_activity = 0

    def more(self) -> None:
        for source in self.sources.values():
            completed = source.value.completed
            if completed is not None and len(source.loaded) < completed.counts.total:
                source.request_page = True
                source.next_activity = 0

    def _inventory(self, now: float) -> None:
        if self.discovery_job is not None and self.discovery_job.done():
            try:
                result = self.discovery_job.result()
                unique: dict[str, RuntimeSource] = {}
                for item in result.sources:
                    if self.agent is None or (
                        item.instance.placement == "resident"
                        and item.instance.name == self.agent
                    ):
                        unique.setdefault(item.instance.identity, item)
                sources = tuple(unique.values())
                self.inventory = RuntimeInstanceSnapshot(
                    tuple(item.instance for item in sources),
                    result.snapshot.diagnostics,
                )
                for binding in sources:
                    identity = binding.instance.identity
                    previous = self.sources.get(identity)
                    if previous is None:
                        self.sources[identity] = _Source(
                            binding,
                            now,
                            SourceActivity(
                                identity=identity,
                                label=binding.instance.label,
                                placement=binding.instance.placement,
                            ),
                        )
                    else:
                        previous.binding, previous.seen = binding, now
                for identity, source in list(self.sources.items()):
                    if now - source.seen > 8 * 3600:
                        del self.sources[identity]
                self.discovered = True
            except (OSError, ValueError, RuntimeError) as exc:
                self.inventory = RuntimeInstanceSnapshot(
                    self.inventory.instances, (str(exc),)
                )
            self.discovery_job = None
            self.next_discovery = now + 5
        if self.discovery_job is None and now >= self.next_discovery:
            self.discovery_job = self.discovery.submit(runtime_inventory, self.root)

    def _collect(self, source: _Source, now: float) -> None:
        if source.activity is not None and source.activity.done():
            try:
                result = source.activity.result()
                if source.activity_recent == self.recent:
                    if not source.page_in_flight:
                        if result.live is not None:
                            if (
                                source.value.live is not None
                                and source.value.live.epoch != result.live.epoch
                            ):
                                source.loaded.clear()
                            source.value.live = result.live
                        source.value.live_stale = result.live is None
                        source.value.activity_error = None
                    if source.value.live is not None:
                        finished = {row.id for row in result.completed.roots}
                        source.value.live = replace(
                            source.value.live,
                            trees=tuple(
                                tree
                                for tree in source.value.live.trees
                                if tree.root_run_id not in finished
                            ),
                        )
                    page = result.completed.roots
                    reset_cursor = bool(page) and page[-1].id not in source.loaded
                    source.loaded.update(
                        (row.id, row) for row in result.completed.roots
                    )
                    cutoff = timestamp(int(now) - self.recent)
                    source.loaded = {
                        key: row
                        for key, row in source.loaded.items()
                        if row.finished_at is not None and row.finished_at >= cutoff
                    }
                    # First-page refreshes update counts. Additional pages extend
                    # the viewport without replacing a newer live baseline.
                    if not source.page_in_flight or source.value.completed is None:
                        source.value.completed = result.completed
                    source.value.completed = source.value.completed.model_copy(
                        update={"roots": tuple(source.loaded.values())}
                    )
                    if page and (
                        source.page_in_flight or source.cursor is None or reset_cursor
                    ):
                        source.cursor = (page[-1].finished_at or "", page[-1].id)
                        if not source.page_in_flight:
                            source.cursor_at = seconds(result.completed.end)
            except (StatsUnavailable, OSError, ValueError, RuntimeError) as exc:
                if source.activity_recent == self.recent:
                    if not source.page_in_flight:
                        source.value.live_stale = True
                    source.value.activity_error = str(exc)
            source.activity = None
            source.next_activity = now + self.refresh
        if source.usage is not None and source.usage.done():
            try:
                result = source.usage.result()
                if source.usage_period == self.period:
                    source.value.usage = result
                    source.value.usage_error = None
            except (StatsUnavailable, OSError, ValueError, RuntimeError) as exc:
                if source.usage_period == self.period:
                    source.value.usage_error = str(exc)
            source.usage = None
            source.next_usage = now + self.refresh

    def tick(self) -> TopSnapshot:
        now = time.time()
        self._inventory(now)
        for source in self.sources.values():
            self._collect(source, now)
            if source.activity is None and now >= source.next_activity:
                source.activity_recent = self.recent
                source.page_in_flight = (
                    source.request_page and source.cursor is not None
                )
                source.request_page = False
                source.activity = self.live_pool.submit(
                    read_activity,
                    source.binding,
                    self.recent,
                    source.cursor if source.page_in_flight else None,
                    source.cursor_at if source.page_in_flight else None,
                )
            if source.usage is None and now >= source.next_usage:
                source.usage_period = self.period
                source.usage = self.usage_pool.submit(
                    read_usage, source.binding, self.period, self.stopped.is_set
                )
        return TopSnapshot(
            observed_at=timestamp(int(now)),
            agent=self.agent,
            period=self.period,
            recent=self.recent,
            inventory=self.inventory,
            inventory_updating=not self.discovered,
            sources=tuple(
                source.value.model_copy(deep=True) for source in self.sources.values()
            ),
        )

    def once(self) -> TopSnapshot:
        # One snapshot waits for each first result, unlike interactive refreshes.
        self.tick()
        assert self.discovery_job is not None
        self.discovery_job.result()
        self.tick()
        for source in self.sources.values():
            for job in (source.activity, source.usage):
                if job is not None:
                    try:
                        job.result()
                    except (StatsUnavailable, OSError, ValueError, RuntimeError):
                        pass
        return self.tick()
