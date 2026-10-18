"""Independent SQLite persistence and immutable-root archival."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict
import json
from pathlib import Path
import sqlite3
import tempfile

from . import _sql as sql
from .projection import project_root, project_step
from .types import (
    Contribution,
    METRIC_FIELDS,
    Metrics,
    PERIODS,
    Scope,
    TERMINAL,
    seconds,
    timestamp,
)

PAGE_SIZE = 500


def connect(path: Path, *, read_only: bool = False) -> sqlite3.Connection:
    connection = sqlite3.connect(
        path.resolve().as_uri() + ("?mode=ro" if read_only else "?mode=rwc"),
        uri=True,
        isolation_level=None,
        timeout=5,
    )
    connection.row_factory = sqlite3.Row
    return connection


class StatsRecords:
    def __init__(self, source: Path, path: Path) -> None:
        self.source = source
        self.path = path

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.writer() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("BEGIN IMMEDIATE")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                if connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table'"
                ).fetchone():
                    raise ValueError("unversioned statistics database is not empty")
                # Execute DDL individually: executescript would commit BEGIN early.
                for statement in sql.STATS_SCHEMA.split(";"):
                    if statement.strip() and "PRAGMA journal_mode" not in statement:
                        connection.execute(statement)
                connection.execute("PRAGMA user_version=1")
            elif version != 1:
                raise ValueError(f"unsupported statistics schema: {version}")
            connection.commit()

    @contextmanager
    def writer(self) -> Iterator[sqlite3.Connection]:
        connection = connect(self.path)
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def snapshot(self) -> Iterator[sqlite3.Connection]:
        connection = connect(self.path, read_only=True)
        try:
            connection.execute(
                "ATTACH DATABASE ? AS src",
                (self.source.resolve().as_uri() + "?mode=ro",),
            )
            connection.execute("BEGIN")
            connection.execute(sql.PIN_STATS).fetchone()
            connection.execute(sql.PIN_SOURCE).fetchall()
            yield connection
        finally:
            connection.close()

    def root_runs(self, connection: sqlite3.Connection, root: str) -> list[sqlite3.Row]:
        return connection.execute(sql.OWNED_RUNS, {"root_id": root}).fetchall()

    def step_page(
        self,
        connection: sqlite3.Connection,
        run: str,
        after: str,
        *,
        lo: int | None = None,
        hi: int | None = None,
    ) -> list[sqlite3.Row]:
        return connection.execute(
            sql.SOURCE_STEP_PAGE,
            {
                "run_id": run,
                "after_id": after,
                "page_size": PAGE_SIZE,
                "lo_text": timestamp(lo) if lo is not None else None,
                "hi_text": timestamp(hi) if hi is not None else None,
            },
        ).fetchall()

    def source_root(
        self,
        connection: sqlite3.Connection,
        root: str,
        *,
        scope: Scope = Scope(),
        lo: int | None = None,
        hi: int | None = None,
    ) -> Iterator[Contribution]:
        for run in self.root_runs(connection, root):
            if not scope.matches(root, run["thread"]):
                continue
            if run["id"] == root:
                yield from project_root(dict(run))
            after = ""
            while page := self.step_page(connection, run["id"], after, lo=lo, hi=hi):
                for row in page:
                    yield from project_step(dict(row), root, run["thread"])
                after = page[-1]["id"]

    def refresh(
        self, *, cutoff: int, now: int, stopped: Callable[[], bool] = lambda: False
    ) -> str | None:
        """Capture finite work, then atomically publish each still-unarchived root."""
        error = None
        with tempfile.TemporaryFile(mode="w+t") as inventory:
            with self.snapshot() as reader:
                for row in reader.execute(
                    sql.ARCHIVE_CANDIDATES, {"cutoff_text": timestamp(cutoff)}
                ):
                    if stopped():
                        return error
                    inventory.write(json.dumps(tuple(row)) + "\n")
            inventory.seek(0)
            for line in inventory:
                if stopped():
                    return error
                root, finished = json.loads(line)
                try:
                    with self.snapshot() as reader:
                        if reader.execute(
                            sql.RECEIPT_EXISTS, {"root_id": root}
                        ).fetchone():
                            continue
                    with self.collect(root, stopped) as additions:
                        self.publish(root, seconds(finished), now, additions)
                except InterruptedError:
                    return error
                except (TypeError, ValueError, sqlite3.Error) as exc:
                    error = f"{root}: {exc}"[:500]
            with self.writer() as writer:
                writer.execute("BEGIN IMMEDIATE")
                writer.execute(sql.FINISH_PASS, {"now_epoch": now})
                writer.commit()
        return error

    @contextmanager
    def collect(
        self, root: str, stopped: Callable[[], bool]
    ) -> Iterator[sqlite3.Connection]:
        """Use disposable disk storage so even a huge root has bounded memory."""
        additions = sqlite3.connect("")
        additions.row_factory = sqlite3.Row
        try:
            # Only temporary storage disables journaling; persistent stores use WAL.
            additions.execute("PRAGMA journal_mode=OFF")
            bucket_ddl = sql.STATS_SCHEMA[
                sql.STATS_SCHEMA.index("CREATE TABLE stats_buckets") :
            ]
            additions.execute(bucket_ddl.split(";")[0])
            with self.snapshot() as reader:
                runs = self.root_runs(reader, root)
                if not runs or any(
                    run["status"] not in TERMINAL or run["finished_at"] is None
                    for run in runs
                ):
                    raise ValueError("root tree is not terminal")
                for run in runs:
                    if reader.execute(
                        sql.HAS_OPEN_STEPS, {"run_id": run["id"]}
                    ).fetchone():
                        raise ValueError("root tree has open Steps")
            for run in runs:
                if run["id"] == root:
                    self.add_contributions(additions, project_root(dict(run)))
                after = ""
                while True:
                    if stopped():
                        raise InterruptedError("statistics stopped")
                    with self.snapshot() as reader:
                        page = self.step_page(reader, run["id"], after)
                    if not page:
                        break
                    for row in page:
                        self.add_contributions(
                            additions, project_step(dict(row), root, run["thread"])
                        )
                    after = page[-1]["id"]
            yield additions
        finally:
            additions.close()

    @staticmethod
    def add_contributions(
        additions: sqlite3.Connection, events: list[Contribution]
    ) -> None:
        for event in events:
            if event.metrics == Metrics():
                continue
            values = asdict(event.metrics)
            keys = (
                (scope, identity, period, event.at // width * width if width else 0)
                for scope, identity in (
                    ("agent", ""),
                    ("thread", event.thread),
                    ("root_run", event.root),
                )
                for period, width in PERIODS
            )
            additions.executemany(
                sql.UPSERT_BUCKET,
                (
                    dict(
                        values,
                        scope_kind=scope,
                        scope_id=identity,
                        period=period,
                        bucket_start=start,
                        model_ref=event.model,
                    )
                    for scope, identity, period, start in keys
                ),
            )

    def publish(
        self, root: str, finished: int, now: int, additions: sqlite3.Connection
    ) -> bool:
        with self.writer() as writer:
            writer.execute("BEGIN IMMEDIATE")
            if writer.execute(sql.RECEIPT_EXISTS, {"root_id": root}).fetchone():
                return False
            writer.executemany(
                sql.UPSERT_BUCKET,
                (dict(row) for row in additions.execute("SELECT * FROM stats_buckets")),
            )
            writer.execute(
                sql.INSERT_RECEIPT,
                {"root_id": root, "finished_epoch": finished, "now_epoch": now},
            )
            writer.execute(sql.TOUCH_STATE, {"now_epoch": now})
            writer.commit()
        return True


def metrics_from_row(row: sqlite3.Row) -> Metrics:
    return Metrics(**{name: row[name] for name in METRIC_FIELDS})
