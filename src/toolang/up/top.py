"""Nonblocking local observation using the shared inventory and Stats interfaces."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
import time
from threading import Event, Lock

import httpx

from toolang.api.version import AGENT_API_VERSION
from toolang.execution.schemas import LiveSnapshot, RunNode
from toolang.stats.errors import StatsUnavailable
from toolang.stats.recent import recent_source, recent_usage
from toolang.stats.schemas import CompletedRoots, CompletionCounts, RecentUsage
from toolang.stats.types import Metrics, seconds, timestamp

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
    *,
    previous: LiveSnapshot | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> ActivitySnapshot:
    live_error = source.observation_error
    if source.endpoint:
        try:
            response = httpx.get(
                source.endpoint.rstrip("/") + f"/api/v{AGENT_API_VERSION}/top/snapshot",
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
        check_idle=not source.endpoint
        and live_error is None
        and source.instance.needs_live_observation,
        cancelled=cancelled,
    )
    result.live_error = live_error
    return result


def read_page(
    source: RuntimeSource,
    recent: int,
    before: tuple[str, str],
    completed_at: int,
    cancelled: Callable[[], bool] | None = None,
) -> CompletedRoots:
    with recent_source(source.layout.run_store, cancelled=cancelled) as reader:
        reader.pin()
        return reader.completed(now=completed_at, recent=recent, before=before)


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
    page: Future[CompletedRoots] | None = None
    page_recent: int = 0
    page_epoch: str | None = None
    page_cursor: tuple[int, tuple[str, str]] | None = None
    activity_recent: int = 0
    activity_obsolete: bool = False
    usage_period: int = 0
    next_activity: float = 0
    next_usage: float = 0
    cursor: tuple[str, str] | None = None
    cursor_at: int | None = None
    request_page: bool = False
    loaded: dict[str, RunNode] = field(default_factory=dict)
    history_seen: bool = False
    history_lock: Lock = field(default_factory=Lock, repr=False)

    def empty_history(self, binding: RuntimeSource) -> bool:
        # Workers share this fact so a lost established store never becomes zero.
        with self.history_lock:
            try:
                binding.layout.run_store.stat()
            except FileNotFoundError:
                return (
                    not self.history_seen
                    and not binding.instance.needs_live_observation
                )
            self.history_seen = True
            return False

    def read_activity(
        self,
        binding: RuntimeSource,
        recent: int,
        *,
        previous: LiveSnapshot | None,
        cancelled: Callable[[], bool],
    ) -> ActivitySnapshot:
        try:
            empty = self.empty_history(binding)
        except OSError:
            # A published endpoint can observe history independently of local I/O.
            if not binding.endpoint:
                raise
            empty = False
        if empty:
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
        result = read_activity(binding, recent, previous=previous, cancelled=cancelled)
        with self.history_lock:
            self.history_seen = True
        return result

    def read_usage(
        self, binding: RuntimeSource, period: int, cancelled: Callable[[], bool]
    ) -> RecentUsage:
        if self.empty_history(binding):
            now = int(time.time())
            return RecentUsage(
                observed_at=timestamp(now),
                start=timestamp(now - period),
                end=timestamp(now),
                metrics=Metrics(),
            )
        return read_usage(binding, period, cancelled)

    def rebind(self, binding: RuntimeSource) -> None:
        self.binding = binding
        # A running request may still supply durable terminal facts, but its
        # live sample belongs to the previous binding, even after rediscovery.
        self.activity_obsolete = True
        self.value.live_stale = True
        self.value.live_error = None
        self.next_activity = 0
        if self.activity is not None and self.activity.cancel():
            self.activity = None


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
        self.page_pool = ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="top-page"
        )
        self.discovery_job: Future[RuntimeInventory] | None = None
        self.next_discovery = 0.0
        self.discovered = False
        self.stopped = Event()

    def close(self) -> None:
        self.stopped.set()
        for pool in (self.discovery, self.live_pool, self.usage_pool, self.page_pool):
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
            source.value.page_error = None
            source.value.activity_error = None
            self._merge_roots(source, (), time.time())
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
                previous_current = {item.identity for item in self.inventory.instances}
                current = {item.identity: item for item in result.snapshot.instances}
                unique: dict[str, RuntimeSource] = {}
                for item in result.sources:
                    if item.instance.identity in current:
                        unique.setdefault(item.instance.identity, item)
                self.inventory = RuntimeInstanceSnapshot(
                    tuple(current.values()),
                    result.snapshot.diagnostics,
                )
                for binding in unique.values():
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
                        if (
                            identity not in previous_current
                            or previous.binding != binding
                        ):
                            previous.rebind(binding)
                        previous.seen = now
                for identity in previous_current - unique.keys():
                    if source := self.sources.get(identity):
                        source.rebind(replace(source.binding, endpoint=None))
                self.discovered = True
            except (OSError, ValueError, RuntimeError) as exc:
                self.inventory = RuntimeInstanceSnapshot(
                    self.inventory.instances, (str(exc),)
                )
            self.discovery_job = None
            self.next_discovery = now + 5
        if self.discovery_job is None and now >= self.next_discovery:
            self.discovery_job = self.discovery.submit(
                runtime_inventory, self.root, agent=self.agent
            )
        current = {item.identity for item in self.inventory.instances}
        for identity, source in list(self.sources.items()):
            if identity not in current and now - source.seen > 8 * 3600:
                for job in (source.activity, source.usage, source.page):
                    if job is not None:
                        job.cancel()
                del self.sources[identity]

    def _merge_roots(
        self, source: _Source, roots: tuple[RunNode, ...], now: float
    ) -> None:
        self._retire_live(source, roots)
        source.loaded.update((row.id, row) for row in roots)
        cutoff = timestamp(int(now) - self.recent)
        source.loaded = {
            identity: row
            for identity, row in source.loaded.items()
            if row.finished_at is not None and row.finished_at >= cutoff
        }
        if source.value.completed is not None:
            source.value.completed = source.value.completed.model_copy(
                update={"roots": tuple(source.loaded.values())}
            )

    @staticmethod
    def _retire_live(source: _Source, roots: tuple[RunNode, ...]) -> None:
        if source.value.live is not None:
            finished = {row.id for row in roots}
            source.value.live = replace(
                source.value.live,
                trees=tuple(
                    tree
                    for tree in source.value.live.trees
                    if tree.root_run_id not in finished
                ),
            )

    def _collect(self, source: _Source, now: float) -> None:
        self._collect_activity(source, now)
        self._collect_page(source, now)
        self._collect_usage(source, now)

    def _collect_activity(self, source: _Source, now: float) -> None:
        if source.activity is not None and source.activity.done():
            try:
                result = source.activity.result()
                idle = (
                    result.idle
                    and not source.binding.endpoint
                    and result.live_error is None
                )
                # Live samples have no Recent filter. Never discard a fresh live
                # baseline just because the completion window changed in flight.
                if result.live is not None and not source.activity_obsolete:
                    if (
                        source.value.live is not None
                        and source.value.live.epoch != result.live.epoch
                    ):
                        source.loaded.clear()
                        source.cursor = source.cursor_at = None
                        source.request_page = False
                        source.value.page_error = None
                    source.value.live = result.live
                elif (
                    idle
                    and not source.activity_obsolete
                    and source.value.live is not None
                ):
                    source.value.live = replace(source.value.live, trees=())
                source.value.live_stale = source.activity_obsolete or (
                    result.live is None and not idle
                )
                if not source.activity_obsolete:
                    source.value.live_error = result.live_error
                self._retire_live(source, (*result.completed.roots, *result.retired))
                page = result.completed.roots
                reset_cursor = bool(page) and page[-1].id not in source.loaded
                if source.activity_recent == self.recent:
                    source.value.activity_error = None
                    source.value.completed = result.completed
                    if result.page_error and not source.activity_obsolete:
                        source.value.page_error = result.page_error
                    if page and (source.cursor is None or reset_cursor):
                        source.cursor = (page[-1].finished_at or "", page[-1].id)
                        source.cursor_at = seconds(result.completed.end)
                # Terminal facts stay true when Recent changes; retain eligible
                # rows while rejecting only the obsolete window's totals.
                self._merge_roots(source, (*page, *result.retired), now)
            except (StatsUnavailable, OSError, ValueError, RuntimeError) as exc:
                source.value.live_stale = True
                if source.activity_recent == self.recent:
                    source.value.activity_error = str(exc)
            source.activity = None
            source.next_activity = (
                now + self.refresh
                if source.activity_recent == self.recent
                and not source.activity_obsolete
                else 0
            )

    def _collect_page(self, source: _Source, now: float) -> None:
        if source.page is not None and source.page.done():
            current_page = source.page_recent == self.recent and source.page_epoch == (
                source.value.live.epoch if source.value.live else None
            )
            try:
                result = source.page.result()
                if current_page:
                    self._merge_roots(source, result.roots, now)
                    # A newer first page may have reset the cursor after a burst
                    # of completions. Old pages may fill rows, never skip that gap.
                    if source.page_cursor == (source.cursor_at, source.cursor):
                        if result.roots:
                            last = result.roots[-1]
                            source.cursor = (last.finished_at or "", last.id)
                        else:
                            source.cursor = None
                    source.value.page_error = None
            except (StatsUnavailable, OSError, ValueError, RuntimeError) as exc:
                if current_page:
                    source.value.page_error = str(exc)
            source.page = None

    def _collect_usage(self, source: _Source, now: float) -> None:
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
            source.next_usage = (
                now + self.refresh if source.usage_period == self.period else 0
            )

    def tick(self) -> TopSnapshot:
        now = time.time()
        self._inventory(now)
        for source in self.sources.values():
            self._collect(source, now)
            if source.activity is None and now >= source.next_activity:
                source.activity_recent = self.recent
                source.activity_obsolete = False
                source.activity = self.live_pool.submit(
                    source.read_activity,
                    source.binding,
                    self.recent,
                    previous=source.value.live,
                    cancelled=self.stopped.is_set,
                )
            if (
                source.request_page
                and source.page is None
                and source.cursor is not None
            ):
                source.request_page = False
                source.page_recent = self.recent
                source.page_epoch = (
                    source.value.live.epoch if source.value.live else None
                )
                source.page_cursor = (source.cursor_at or int(now), source.cursor)
                source.page = self.page_pool.submit(
                    read_page,
                    source.binding,
                    self.recent,
                    source.cursor,
                    source.page_cursor[0],
                    self.stopped.is_set,
                )
            if source.usage is None and now >= source.next_usage:
                source.usage_period = self.period
                source.usage = self.usage_pool.submit(
                    source.read_usage, source.binding, self.period, self.stopped.is_set
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
        try:
            self.discovery_job.result()
        except (OSError, ValueError, RuntimeError):
            pass  # _inventory preserves discovery diagnostics in the snapshot.
        self.tick()
        for source in self.sources.values():
            for job in (source.activity, source.usage):
                if job is not None:
                    try:
                        job.result()
                    except (StatsUnavailable, OSError, ValueError, RuntimeError):
                        pass
        return self.tick()
