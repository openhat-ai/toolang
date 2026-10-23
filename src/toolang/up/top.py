"""Nonblocking local observation using the shared inventory and Stats interfaces."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path
import time
from threading import Event

from toolang.execution.schemas import RunNode
from toolang.stats.errors import StatsUnavailable
from toolang.stats.schemas import CompletedRoots, RecentUsage
from toolang.stats.types import seconds, timestamp

from .activity import ActivityReader
from .observation import (
    RuntimeInventory,
    RuntimeInstanceSnapshot,
    RuntimeSource,
)
from .schemas import ActivitySnapshot, SourceActivity, TopSnapshot
from .watcher import RuntimeWatcher


@dataclass
class _Source:
    binding: RuntimeSource
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
    reader: ActivityReader = field(default_factory=ActivityReader)
    stopped: Event = field(default_factory=Event)

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
        self.runtime_watcher = RuntimeWatcher(root, agent=agent)
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

    def close(self) -> None:
        for source in self.sources.values():
            source.stopped.set()
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
                current = {item.identity: item for item in result.snapshot.instances}
                unique: dict[str, RuntimeSource] = {}
                for item in result.sources:
                    if item.instance.identity in current:
                        unique.setdefault(item.instance.identity, item)
                self.inventory = RuntimeInstanceSnapshot(
                    tuple(current.values()),
                    result.snapshot.diagnostics,
                    result.snapshot.membership_complete,
                )
                for binding in unique.values():
                    identity = binding.instance.identity
                    previous = self.sources.get(identity)
                    if previous is None:
                        self.sources[identity] = _Source(
                            binding,
                            SourceActivity(
                                identity=identity,
                                label=binding.instance.label,
                                placement=binding.instance.placement,
                            ),
                        )
                    else:
                        if previous.binding != binding:
                            previous.rebind(binding)
                for identity in self.sources.keys() - current.keys():
                    source = self.sources.pop(identity)
                    source.stopped.set()
                    for job in (source.activity, source.usage, source.page):
                        if job is not None:
                            job.cancel()
                self.discovered = True
            except (OSError, ValueError, RuntimeError) as exc:
                self.inventory = RuntimeInstanceSnapshot(
                    self.inventory.instances, (str(exc),), False
                )
            self.discovery_job = None
            self.next_discovery = now + 5
        if self.discovery_job is None and now >= self.next_discovery:
            self.discovery_job = self.discovery.submit(self.runtime_watcher.refresh)

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
                if (
                    source.activity_recent == self.recent
                    and not source.activity_obsolete
                ):
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
                    source.reader.read_activity,
                    source.binding,
                    self.recent,
                    previous=source.value.live,
                    cancelled=source.stopped.is_set,
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
                    source.reader.read_page,
                    source.binding,
                    self.recent,
                    source.cursor,
                    source.page_cursor[0],
                    source.stopped.is_set,
                )
            if source.usage is None and now >= source.next_usage:
                source.usage_period = self.period
                source.usage = self.usage_pool.submit(
                    source.reader.read_usage,
                    source.binding,
                    self.period,
                    source.stopped.is_set,
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
