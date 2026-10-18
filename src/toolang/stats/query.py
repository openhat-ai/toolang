"""Merge disjoint cached buckets with source facts under one pinned receipt set."""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Iterator
import json
import sqlite3

from toolang.execution.types import StepRef

from . import _sql as sql
from .projection import project_root, project_step
from .records import PAGE_SIZE, StatsRecords, metrics_from_row
from .types import Contribution, Metrics, Scope, slices, timestamp

Interval = tuple[int | None, int | None]


class _Intervals:
    def __init__(self, intervals: list[Interval]) -> None:
        self.intervals = intervals
        self.disjoint = all(
            a[1] is not None and b[0] is not None and a[1] <= b[0]
            for a, b in zip(intervals, intervals[1:])
        ) and all(lo is not None and hi is not None for lo, hi in intervals)
        self.starts = [lo for lo, _ in intervals if lo is not None]

    def containing(self, at: int) -> Iterator[int]:
        if self.disjoint:
            index = bisect_right(self.starts, at) - 1
            if index >= 0:
                hi = self.intervals[index][1]
                if hi is not None and at < hi:
                    yield index
        else:
            for index, (lo, hi) in enumerate(self.intervals):
                if lo is None or (hi is not None and lo <= at < hi):
                    yield index


class StatsReader:
    def __init__(self, records: StatsRecords, connection: sqlite3.Connection) -> None:
        self.records = records
        self.connection = connection
        self.runs: dict[str, sqlite3.Row] = {}
        self.roots: dict[str, str] = {}

    def validate_scope(self, scope: Scope) -> None:
        if scope.kind == "thread":
            if not self.connection.execute(
                "SELECT 1 FROM src.threads WHERE id=?", (scope.id,)
            ).fetchone():
                raise LookupError(f"thread not found: {scope.id}")
        elif scope.kind == "root_run":
            self.load_runs([scope.id])
            row = self.runs.get(scope.id)
            if row is None:
                raise LookupError(f"root run not found: {scope.id}")
            if row["parent"] is not None:
                raise ValueError("root_run_id must identify a physical root")

    def load_runs(self, identities: list[str]) -> None:
        for offset in range(0, len(identities), PAGE_SIZE):
            missing = [
                identity
                for identity in identities[offset : offset + PAGE_SIZE]
                if identity not in self.runs
            ]
            if missing:
                for row in self.connection.execute(
                    sql.RUNS_BY_IDS, {"ids_json": json.dumps(missing)}
                ):
                    self.runs[row["id"]] = row

    def owner(self, run: str) -> tuple[str, str]:
        path: list[str] = []
        current = run
        while current not in self.roots:
            if current in path:
                raise ValueError("cycle in Run ownership")
            self.load_runs([current])
            row = self.runs.get(current)
            if row is None:
                raise ValueError(f"missing owning Run: {current}")
            path.append(current)
            if row["parent"] is None:
                self.roots[current] = current
                break
            current = StepRef.parse(row["parent"]).run_id
        root = self.roots[current]
        for identity in path:
            self.roots[identity] = root
        return root, self.runs[run]["thread"]

    def archived(self, roots: list[str]) -> set[str]:
        return {
            row[0]
            for row in self.connection.execute(
                sql.ARCHIVED_ROOT_IDS, {"ids_json": json.dumps(roots)}
            )
        }

    def time_data(self, lo: int, hi: int, archived: bool) -> Iterator[Contribution]:
        params = {"lo_text": timestamp(lo), "hi_text": timestamp(hi)}
        cursor = self.connection.execute(sql.TIME_STEP_IDS, params)
        while page := cursor.fetchmany(PAGE_SIZE):
            self.load_runs([row["run"] for row in page])
            owners = {row["run"]: self.owner(row["run"]) for row in page}
            receipts = self.archived(list({owner[0] for owner in owners.values()}))
            ids = [
                row["id"]
                for row in page
                if (owners[row["run"]][0] in receipts) == archived
            ]
            for row in self.connection.execute(
                sql.STEPS_BY_IDS, {"ids_json": json.dumps(ids)}
            ):
                root, thread = owners[row["run"]]
                for event in project_step(dict(row), root, thread):
                    if lo <= event.at < hi:
                        yield event
        for row in self.connection.execute(
            sql.ROOT_STARTS_BY_TIME, dict(params, archived=int(archived))
        ):
            yield from project_root(dict(row))

    def raw_data(
        self, scope: Scope, lo: int | None = None, hi: int | None = None
    ) -> Iterator[Contribution]:
        if scope.kind == "root_run":
            if scope.id not in self.archived([scope.id]):
                yield from self.records.source_root(
                    self.connection, scope.id, lo=lo, hi=hi
                )
        else:
            for row in self.connection.execute(sql.UNARCHIVED_ROOTS):
                yield from self.records.source_root(
                    self.connection, row["id"], lo=lo, hi=hi
                )

    def query(
        self,
        intervals: list[Interval],
        scope: Scope,
        model: str | None,
        *,
        recent: bool,
    ) -> list[dict[str, Metrics]]:
        totals: list[dict[str, Metrics]] = [{} for _ in intervals]
        routing = _Intervals(intervals)

        def accumulate(event: Contribution, indices: Iterator[int]) -> None:
            if model is not None and event.model != model:
                return
            if scope.kind == "thread" and scope.id != event.thread:
                return
            if scope.kind == "root_run" and scope.id != event.root:
                return
            for index in indices:
                totals[index][event.model] = (
                    totals[index].get(event.model, Metrics()) + event.metrics
                )

        for index, (lo, hi) in enumerate(intervals):
            parts = (
                [("all", 0, 1)]
                if lo is None
                else slices(lo, hi if hi is not None else lo)
            )
            for period, start, end in parts:
                if period == "edge":
                    for event in self.time_data(start, end, True):
                        accumulate(event, iter((index,)))
                else:
                    params = {
                        "scope_kind": scope.kind,
                        "scope_id": scope.id,
                        "period": period,
                        "lo": start,
                        "hi": end,
                        "model_ref": model,
                    }
                    for row in self.connection.execute(sql.BUCKET_TOTALS, params):
                        ref = row["model_ref"]
                        totals[index][ref] = totals[index].get(
                            ref, Metrics()
                        ) + metrics_from_row(row)
        if all(lo is not None and hi is not None for lo, hi in intervals):
            bounds = [
                (lo, hi)
                for lo, hi in intervals
                if lo is not None and hi is not None and lo < hi
            ]
            events = iter(())
            if bounds:
                lo, hi = min(lo for lo, _ in bounds), max(hi for _, hi in bounds)
                events = (
                    self.time_data(lo, hi, False)
                    if recent
                    else self.raw_data(scope, lo, hi)
                )
        else:
            events = self.raw_data(scope)
        for event in events:
            accumulate(event, routing.containing(event.at))
        return totals
