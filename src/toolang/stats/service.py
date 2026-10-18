"""Agent-server-owned, demand-driven statistics worker and query facade."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, time as day_time, timedelta
import logging
from pathlib import Path
import sqlite3
import threading
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from toolang.execution.types import valid_run_id, valid_thread_id

from . import _sql as sql
from .config import StatsConfig
from .errors import StatsUnavailable
from .query import Interval, StatsReader
from .records import StatsRecords
from .schemas import (
    DailyRow,
    ModelRow,
    ModelsResponse,
    Quantities,
    Snapshot,
    StatsResponse,
    WindowRow,
)
from .types import Metrics, Scope, WINDOWS, Window, timestamp

logger = logging.getLogger(__name__)


def scope_for(thread_id: str | None = None, root_run_id: str | None = None) -> Scope:
    if thread_id is not None and root_run_id is not None:
        raise ValueError("thread_id and root_run_id are mutually exclusive")
    if thread_id is not None:
        if not valid_thread_id(thread_id):
            raise ValueError("invalid thread_id")
        return Scope("thread", thread_id)
    if root_run_id is not None:
        if not valid_run_id(root_run_id):
            raise ValueError("invalid root_run_id")
        return Scope("root_run", root_run_id)
    return Scope()


def quantities(metrics: Metrics, *, model: bool = False) -> Quantities:
    return Quantities(
        in_=metrics.input_tokens,
        out=metrics.output_tokens,
        cached=metrics.cached_tokens,
        spend=metrics.spend_micros / 1_000_000
        if metrics.spend_micros is not None
        else None,
        model_calls=metrics.model_calls,
        tool_calls=metrics.tool_calls,
        root_runs=None if model else metrics.root_runs,
        spend_approximate=metrics.spend_approximate,
    )


def midnight(day: date, zone: ZoneInfo) -> int:
    """Resolve midnight to its earliest occurrence, or forward across a gap."""
    wall = datetime.combine(day, day_time())
    candidates = sorted(
        {int(wall.replace(tzinfo=zone, fold=fold).timestamp()) for fold in (0, 1)}
    )
    exact = [
        at
        for at in candidates
        if datetime.fromtimestamp(at, zone).replace(tzinfo=None) == wall
    ]
    if exact:
        return min(exact)
    left, right = candidates[0], candidates[-1]
    while right - left > 1:
        middle = (left + right) // 2
        if datetime.fromtimestamp(middle, zone).replace(tzinfo=None) >= wall:
            right = middle
        else:
            left = middle
    return right


class StatsService:
    def __init__(
        self,
        source: Path,
        path: Path,
        *,
        config: StatsConfig,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.records = StatsRecords(source, path)
        self.config = config
        self.clock = clock
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._refreshing = False
        self._available = False
        self._error: str | None = None

    def start(self) -> None:
        try:
            self.records.initialize()
            self._available = True
        except (OSError, ValueError, sqlite3.Error) as exc:
            self._error = str(exc)[:500]
            logger.exception("Statistics store is unavailable")

    def close(self) -> None:
        with self._lock:
            self._stop.set()
            self._wake.set()
            worker = self._thread
        if worker is not None:
            worker.join()

    def request_refresh(self) -> None:
        with self._lock:
            if self._stop.is_set() or not self._available:
                return
            self._wake.set()
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._work, name="toolang-stats", daemon=True
                )
                self._thread.start()

    def _work(self) -> None:
        next_start = 0.0
        while True:
            self._wake.wait()
            self._wake.clear()
            if self._stop.wait(max(0.0, next_start - time.monotonic())):
                return
            next_start = time.monotonic() + 5.0
            with self._lock:
                self._refreshing = True
            try:
                now = int(self.clock())
                error = self.records.refresh(
                    cutoff=now - self.config.archive_after_seconds,
                    now=now,
                    stopped=self._stop.is_set,
                )
            except Exception as exc:
                error = str(exc)[:500]
                logger.exception("Statistics refresh failed")
            with self._lock:
                self._error = error
                self._refreshing = False
            if self._stop.is_set():
                return

    def _query(
        self, intervals: list[Interval], scope: Scope, model: str | None, now: int
    ) -> tuple[Snapshot, list[dict[str, Metrics]] | None]:
        if not self._available or self._stop.is_set():
            raise StatsUnavailable(self._error or "statistics service is unavailable")
        if model is not None and not model.strip():
            raise ValueError("model_ref must not be empty")
        historical = any(
            lo is None or lo < now - self.config.archive_after_seconds
            for lo, _ in intervals
        )
        try:
            with self.records.snapshot() as connection:
                reader = StatsReader(self.records, connection)
                reader.validate_scope(scope)
                state = connection.execute(sql.PIN_STATS).fetchone()
                work = historical and (
                    not state["initialized"]
                    or connection.execute(
                        sql.HAS_ARCHIVE_WORK,
                        {
                            "cutoff_text": timestamp(
                                now - self.config.archive_after_seconds
                            )
                        },
                    ).fetchone()
                    is not None
                )
                initializing = not bool(state["initialized"])
                if initializing and historical:
                    totals = None
                else:
                    try:
                        totals = reader.query(
                            intervals, scope, model, recent=not historical
                        )
                    except ValueError as exc:
                        raise StatsUnavailable(
                            f"invalid statistics source: {exc}"
                        ) from exc
        except (OSError, sqlite3.Error) as exc:
            raise StatsUnavailable(str(exc)) from exc
        if work:
            self.request_refresh()
        with self._lock:
            snapshot = Snapshot(
                queried_at=timestamp(now),
                computed_at=timestamp(state["updated_at"])
                if state["updated_at"] is not None
                else None,
                initializing=initializing,
                stale=initializing or bool(work),
                refreshing=self._refreshing or self._wake.is_set(),
                refresh_error=self._error,
            )
        return snapshot, totals

    def summary(
        self, scope: Scope = Scope(), model: str | None = None
    ) -> StatsResponse[list[WindowRow]]:
        now = int(self.clock())
        intervals: list[Interval] = [
            (now - width, now) if width is not None else (None, None)
            for width in WINDOWS.values()
        ]
        snapshot, totals = self._query(intervals, scope, model, now)
        rows = None
        if totals is not None:
            rows = []
            for window, (lo, hi), groups in zip(WINDOWS, intervals, totals):
                total = sum(groups.values(), Metrics())
                rows.append(
                    WindowRow(
                        **quantities(total, model=model is not None).model_dump(),
                        window=window,
                        start=timestamp(lo) if lo is not None else None,
                        end=timestamp(hi) if hi is not None else None,
                    )
                )
        return StatsResponse(scope=scope, snapshot=snapshot, data=rows)

    def models(self, window: Window = "1mo", scope: Scope = Scope()) -> ModelsResponse:
        if window not in WINDOWS:
            raise ValueError("invalid statistics window")
        now = int(self.clock())
        width = WINDOWS[window]
        interval = (now - width, now) if width is not None else (None, None)
        snapshot, totals = self._query([interval], scope, None, now)
        rows = (
            None
            if totals is None
            else [
                ModelRow(**quantities(value, model=True).model_dump(), model_ref=model)
                for model, value in sorted(totals[0].items())
                if model and value != Metrics()
            ]
        )
        return ModelsResponse(
            scope=scope,
            snapshot=snapshot,
            data=rows,
            window=window,
            start=timestamp(interval[0]) if interval[0] is not None else None,
            end=timestamp(interval[1]) if interval[1] is not None else None,
        )

    def daily(
        self,
        start: date,
        end: date,
        timezone: str,
        scope: Scope = Scope(),
        model: str | None = None,
    ) -> StatsResponse[list[DailyRow]]:
        try:
            zone = ZoneInfo(timezone)
        except (ValueError, ZoneInfoNotFoundError) as exc:
            raise ValueError("invalid IANA timezone") from exc
        now = int(self.clock())
        today = datetime.fromtimestamp(now, zone).date()
        if not 1 <= (end - start).days <= 366 or end > today + timedelta(days=1):
            raise ValueError("daily range requires 1-366 dates through today")
        dates = [start + timedelta(days=index) for index in range((end - start).days)]
        intervals: list[Interval] = [
            (midnight(day, zone), min(now, midnight(day + timedelta(days=1), zone)))
            for day in dates
        ]
        snapshot, totals = self._query(intervals, scope, model, now)
        rows = (
            None
            if totals is None
            else [
                DailyRow(
                    **quantities(
                        sum(groups.values(), Metrics()), model=model is not None
                    ).model_dump(),
                    date=day,
                    start=timestamp(lo),
                    end=timestamp(hi),
                    partial=day == today,
                )
                for day, (lo, hi), groups in zip(dates, intervals, totals)
            ]
        )
        return StatsResponse(scope=scope, snapshot=snapshot, data=rows)
