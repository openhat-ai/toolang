"""Read recent source statistics without opening a statistics database."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3

from toolang.execution.records import run_noted_from_data
from toolang.execution.schemas import RunNode
from toolang.execution.types import CallCounters

from . import _sql as sql
from .errors import StatsUnavailable
from .projection import project_step
from .query import SourceReader
from .records import PAGE_SIZE
from .schemas import CompletedRoots, CompletionCounts, RecentUsage
from .types import Metrics, timestamp


class RecentReader(SourceReader):
    """Consume a pinned runs.db view; no archive ownership filtering applies."""

    def pin(self) -> None:
        self.connection.execute("BEGIN")
        self.connection.execute(sql.PIN_SOURCE).fetchall()

    def usage(self, *, now: int, period: int) -> RecentUsage:
        if not 0 < period <= 8 * 3600:
            raise ValueError("period must be between 1 and 28800 seconds")
        start, end = timestamp(now - period), timestamp(now)
        cursor = self.connection.execute(
            sql.TIME_STEP_IDS, {"lo_text": start, "hi_text": end}
        )
        total = Metrics()
        while page := cursor.fetchmany(PAGE_SIZE):
            self.load_runs([row["run"] for row in page])
            owners = {row["run"]: self.owner(row["run"]) for row in page}
            for row in self.connection.execute(
                sql.STEPS_BY_IDS, {"ids_json": json.dumps([r["id"] for r in page])}
            ):
                root, thread = owners[row["run"]]
                for fact in project_step(dict(row), root, thread):
                    if now - period <= fact.at < now:
                        total += fact.metrics
        return RecentUsage(observed_at=end, start=start, end=end, metrics=total)

    def completed(
        self,
        *,
        now: int,
        recent: int,
        limit: int = 100,
        before: tuple[str, str] | None = None,
    ) -> CompletedRoots:
        if not 0 < recent <= 15 * 60 or not 1 <= limit <= 500:
            raise ValueError("recent must be 1-900 seconds; limit must be 1-500")
        start, end = timestamp(now - recent), timestamp(now)
        params = {"lo_text": start, "hi_text": end, "limit": limit}
        counts = CompletionCounts()
        for row in self.connection.execute(sql.RECENT_ROOT_COUNTS, params):
            setattr(counts, row["status"], row["count"])
        roots = []
        for row in self.connection.execute(
            sql.RECENT_ROOT_PAGE,
            dict(
                params,
                before_time=before[0] if before else None,
                after_id=before[1] if before else "",
            ),
        ):
            noted = run_noted_from_data(
                json.loads(row["noted"]) if row["noted"] else None
            )
            roots.append(
                RunNode(
                    row["id"],
                    None,
                    "",
                    row["status"],
                    row["created_at"],
                    started_at=row["started_at"],
                    finished_at=row["finished_at"],
                    calls=noted.calls if noted else CallCounters(),
                    usage=noted.usage if noted else None,
                    cost=noted.cost if noted else None,
                    cost_approximate=noted.cost_approximate if noted else False,
                )
            )
        return CompletedRoots(
            observed_at=end, start=start, end=end, counts=counts, roots=tuple(roots)
        )


@contextmanager
def recent_source(
    source: Path, *, cancelled: Callable[[], bool] | None = None
) -> Iterator[RecentReader]:
    """Open only the supplied source, read-only. The caller chooses the pin boundary."""
    connection = sqlite3.connect(":memory:", isolation_level=None, timeout=1, uri=True)
    connection.row_factory = sqlite3.Row
    if cancelled is not None:
        connection.set_progress_handler(lambda: int(cancelled()), 1000)
    try:
        connection.execute(
            "ATTACH DATABASE ? AS src", (source.resolve().as_uri() + "?mode=ro",)
        )
        connection.execute("PRAGMA query_only=ON")
        yield RecentReader(connection)
    except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
        raise StatsUnavailable(
            f"recent statistics source is unavailable: {exc}"
        ) from exc
    finally:
        connection.close()


def recent_usage(
    source: Path, *, now: int, period: int, cancelled: Callable[[], bool] | None = None
) -> RecentUsage:
    with recent_source(source, cancelled=cancelled) as reader:
        reader.pin()
        return reader.usage(now=now, period=period)
