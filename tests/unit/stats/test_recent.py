"""Recent observation uses original source facts, never the historical store."""

import sqlite3
from unittest.mock import Mock

import pytest

from toolang.base.types.run import ModelCall
from toolang.execution.store import RunStore
from toolang.execution.types import (
    CallCounters,
    ModelStepGiven,
    RunNoted,
    StepRef,
    TokenUsage,
)
from toolang.stats import _sql
from toolang.stats.errors import StatsUnavailable
from toolang.stats.recent import recent_source, recent_usage
from toolang.stats.records import StatsRecords
from toolang.stats.service import StatsService
from toolang.stats.types import timestamp

from .conftest import Fixture, NOW


@pytest.fixture
def source(tmp_path, monkeypatch):
    store = RunStore(tmp_path / "runs.db")
    monkeypatch.setattr(
        StatsRecords, "__init__", Mock(side_effect=AssertionError("stats.db opened"))
    )
    yield Fixture(store, Mock(spec=StatsService), [NOW])
    store.close()


@pytest.mark.parametrize("stats_state", ["missing", "corrupt", "initializing"])
@pytest.mark.parametrize("period", [3600, 14400, 28800])
def test_recent_source_ignores_statistics_store_and_settles_active_roots(
    source, tmp_path, stats_state, period
):
    stats_path = tmp_path / "stats.db"
    if stats_state != "missing":
        stats_path.write_bytes(b"not sqlite" if stats_state == "corrupt" else b"")
    before = stats_path.read_bytes() if stats_path.exists() else None
    run = source.root("active", NOW - 40000)
    source.call(run, NOW - period, NOW - 1, tokens=100)
    source.call(run, NOW - period - 1, NOW - 1, tokens=200, spend=0.2)
    source.call(run, NOW - 2, NOW, tokens=500)
    source.call(run, NOW - 5, NOW - 4, tool=True)
    source.call(run, NOW - 3, NOW - 2, invoked=False)
    source.source.begin_step(
        ref=StepRef.from_local(run, (99,)),
        kind="model",
        input=(),
        given=ModelStepGiven(
            model="test/model",
            call=ModelCall(instructions="", messages=[]),
            setup="fixture",
        ),
        started_at=timestamp(NOW - 10),
    )
    usage = recent_usage(source.source.db_path, now=NOW, period=period)
    assert usage.metrics.model_calls == 2
    assert usage.metrics.tool_calls == 1
    assert usage.metrics.input_tokens == 300
    assert usage.metrics.output_tokens == 30
    assert usage.metrics.cached_tokens == 14
    assert usage.metrics.spend_micros == 300000
    assert usage.start == timestamp(NOW - period) and usage.end == timestamp(NOW)
    assert (stats_path.read_bytes() if stats_path.exists() else None) == before


def test_unknown_usage_and_pricing_remain_unknown(source):
    run = source.root("unknown", NOW - 100)
    source.call(run, NOW - 50, NOW - 40)
    source.call(run, NOW - 20, NOW - 10, unknown=True, spend=None)
    total = recent_usage(source.source.db_path, now=NOW, period=3600).metrics
    assert total.model_calls == 2
    assert total.input_tokens is None and total.cached_tokens is None
    assert total.output_tokens is None and total.spend_micros is None


def test_completed_roots_count_statuses_current_second_and_page_without_child_runs(
    source,
):
    terminal = []
    for index in range(207):
        run = source.root(f"done{index:03}", NOW - 1000)
        state = ("succeeded", "failed", "canceled")[index % 3]
        source.source.finish_run(
            run_id=run,
            status=state,
            finished_at=timestamp(NOW - index % 2),
            noted=RunNoted(CallCounters(5, 9), TokenUsage(100, 20, 30), 0.16),
        )
        terminal.append(run)
    old = source.root("old", NOW - 2000)
    source.finish(old, NOW - 301)
    active = source.root("active", NOW - 40000)
    step = source.call(active, NOW - 10, NOW - 9, tool=True)
    child = source.root("child", NOW - 10, parent=step)
    source.finish(child, NOW)
    with recent_source(source.source.db_path) as reader:
        reader.pin()
        first = reader.completed(now=NOW, recent=300)
        assert first.counts.total == 207
        assert (first.counts.succeeded, first.counts.failed, first.counts.canceled) == (
            69,
            69,
            69,
        )
        rows = list(first.roots)
        while len(rows) < first.counts.total:
            last = rows[-1]
            assert last.finished_at is not None
            page = reader.completed(
                now=NOW, recent=300, before=(last.finished_at, last.id)
            )
            rows.extend(page.roots)
        assert len({row.id for row in rows}) == 207
        assert {row.id for row in rows} == set(terminal)
        assert rows[0].finished_at == timestamp(NOW)
        assert rows[0].calls == CallCounters(5, 9)
        assert rows[0].usage is not None
        assert rows[0].cost == 0.16 and rows[0].usage.input_tokens == 100
        assert all(row.runnable == "" for row in rows)
    with recent_source(source.source.db_path) as reader:
        reader.pin()
        assert reader.completed(now=NOW + 302, recent=300).counts.total == 0


def test_pinned_source_is_stable_across_completion(source):
    run = source.root("handoff", NOW - 10)
    with recent_source(source.source.db_path) as reader:
        reader.pin()
        source.finish(run, NOW)
        assert reader.completed(now=NOW, recent=300).counts.total == 0
    with recent_source(source.source.db_path) as reader:
        reader.pin()
        assert [row.id for row in reader.completed(now=NOW, recent=300).roots] == [run]


def test_archived_source_contributions_are_read_once(fixture):
    run = fixture.root("archived", NOW - 40000)
    fixture.call(run, NOW - 4000, NOW - 3900, tokens=123)
    fixture.finish(run, NOW - 3800)
    assert fixture.refresh(cutoff=NOW - 3600) is None
    usage = recent_usage(fixture.source.db_path, now=NOW, period=28800).metrics
    assert usage.model_calls == 1 and usage.input_tokens == 123
    assert usage.spend_micros == 100000


def test_busy_eight_hour_source_uses_bounded_indexed_reads(source, monkeypatch):
    run = source.root("busy", NOW - 40000)
    source.call(run, NOW - 20000, NOW - 19999)
    with sqlite3.connect(source.source.db_path) as connection:
        connection.row_factory = sqlite3.Row
        row = dict(connection.execute("SELECT * FROM steps").fetchone())
        columns = ",".join('"' + key + '"' for key in row)
        query = f"INSERT INTO steps ({columns}) VALUES ({','.join('?' for _ in row)})"
        connection.executemany(
            query,
            [
                tuple(dict(row, id=f"{run}.{index}", path=str(index)).values())
                for index in range(1, 2501)
            ],
        )
    with recent_source(source.source.db_path) as reader:
        reader.pin()
        sizes = []
        original = reader.load_runs

        def load(identities):
            sizes.append(len(identities))
            return original(identities)

        monkeypatch.setattr(reader, "load_runs", load)
        total = reader.usage(now=NOW, period=28800).metrics
        assert total.model_calls == 2501 and total.input_tokens == 250100
        assert max(sizes) <= 500 and len(sizes) >= 6
        plan = " ".join(
            str(tuple(row))
            for row in reader.connection.execute(
                "EXPLAIN QUERY PLAN " + _sql.TIME_STEP_IDS,
                {"lo_text": timestamp(NOW - 28800), "hi_text": timestamp(NOW)},
            )
        )
        assert "idx_steps" in plan and "SCAN src.steps" not in plan
        plan = " ".join(
            str(tuple(row))
            for row in reader.connection.execute(
                "EXPLAIN QUERY PLAN " + _sql.RECENT_ROOT_COUNTS,
                {"lo_text": timestamp(NOW - 900), "hi_text": timestamp(NOW)},
            )
        )
        assert "idx_runs_closed_roots" in plan
    with pytest.raises(StatsUnavailable, match="interrupted"):
        with recent_source(source.source.db_path, cancelled=lambda: True) as reader:
            reader.pin()
            reader.usage(now=NOW, period=28800)


def test_missing_and_invalid_source_never_create_or_repair_files(tmp_path):
    path = tmp_path / "runs.db"
    with pytest.raises(StatsUnavailable):
        recent_usage(path, now=NOW, period=3600)
    assert not path.exists()
    path.write_text("corrupt")
    with pytest.raises(StatsUnavailable):
        recent_usage(path, now=NOW, period=3600)
    assert path.read_text() == "corrupt"
    assert not (tmp_path / "stats.db").exists()
