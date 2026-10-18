from datetime import date
import sqlite3
from unittest.mock import Mock

import pytest

from toolang.stats import query, records
from toolang.stats.service import midnight
from toolang.stats.types import Scope
from zoneinfo import ZoneInfo

from .conftest import DAY, NOW


def populate(fixture):
    old = fixture.root("old", NOW - 50 * DAY)
    fixture.call(old, NOW - 20 * DAY + 11, NOW - 20 * DAY + 13, model="test/a")
    fixture.call(old, NOW - 3 * DAY + 5, NOW - 3 * DAY + 9, model="test/b", spend=0.2)
    ref = fixture.call(old, NOW - 5 * DAY, NOW - 5 * DAY + 1, tool=True)
    child = fixture.root("child", NOW - 4 * DAY, parent=ref, thread="chat_stats")
    fixture.call(child, NOW - 3 * DAY, NOW - 3 * DAY + 1, model="test/a")
    fixture.finish(child, NOW - 2 * DAY - 1)
    fixture.finish(old, NOW - 2 * DAY)
    active = fixture.root("active", NOW - 5 * DAY)
    fixture.call(active, NOW - 4 * DAY, NOW - 4 * DAY + 1, model="test/a")
    fixture.call(active, NOW - 200, NOW - 100, model="test/b", cached=None, spend=None)
    young = fixture.root("young", NOW - 1000, thread="chat_young")
    fixture.call(young, NOW - 120, NOW - 60, model="test/free", spend=0, cached=0)
    fixture.finish(young, NOW - 30)
    return old, active, young


@pytest.mark.parametrize("scope_kind", ["agent", "thread", "root_run"])
@pytest.mark.parametrize("model", [None, "test/a", "test/b", "test/missing"])
def test_dual_store_summary_models_and_daily_equal_raw_results(
    fixture, scope_kind, model
):
    old, _, _ = populate(fixture)
    scope = Scope(
        scope_kind, {"agent": "", "thread": "chat_stats", "root_run": old}[scope_kind]
    )
    summary = fixture.stats.summary(scope, model).data
    models = fixture.stats.models("all", scope).data
    daily = fixture.stats.daily(
        date(2026, 9, 1), date(2026, 10, 19), "Asia/Shanghai", scope, model
    ).data
    assert fixture.refresh() is None
    assert fixture.stats.summary(scope, model).data == summary
    assert fixture.stats.models("all", scope).data == models
    assert (
        fixture.stats.daily(
            date(2026, 9, 1), date(2026, 10, 19), "Asia/Shanghai", scope, model
        ).data
        == daily
    )
    with fixture.stats.records.snapshot() as connection:
        assert connection.execute("SELECT COUNT(*) FROM roots").fetchone()[0] == 1
    if scope_kind == "agent" and model is None:
        assert summary[0].in_ == 600 and summary[0].model_calls == 6
        assert summary[0].tool_calls == 1 and summary[0].root_runs == 3
        assert summary[0].cached is None and summary[0].spend is None


def test_archived_partial_minute_edges_deduplicate_steps_and_split_event_times(fixture):
    root = fixture.root("edge", NOW - 5 * DAY)
    start, end = NOW - 1800 - 2 * DAY, NOW - 2 * DAY
    fixture.call(root, start - 1, start + 1, spend=0.1)
    fixture.call(root, end - 20, end - 10, spend=0.2)
    fixture.finish(root, NOW - DAY - 1)
    assert fixture.refresh() is None
    with fixture.stats.records.snapshot() as connection:
        from toolang.stats.query import StatsReader

        groups = StatsReader(fixture.stats.records, connection).query(
            [(start, end)], Scope(), None, recent=False
        )[0]
    result = groups["test/model"]
    assert result.model_calls == 1 and result.input_tokens == 200
    assert result.spend_micros == 300000


@pytest.mark.parametrize("scope_kind", ["agent", "thread", "root_run"])
def test_finite_history_does_not_project_steps_outside_its_time_range(
    fixture, monkeypatch, scope_kind
):
    root = fixture.root("long_active", NOW - 60 * DAY)
    fixture.call(root, NOW - 40 * DAY, NOW - 40 * DAY + 1)
    # Include both kinds of boundary crossing: count only, and usage only.
    fixture.call(root, NOW - 31 * DAY, NOW - 29 * DAY, tokens=200)
    fixture.call(root, NOW - DAY, NOW + 1, tokens=300)
    fixture.call(root, NOW - 10, NOW - 1, tokens=400)
    project = Mock(wraps=records.project_step)
    monkeypatch.setattr(records, "project_step", project)
    scope = Scope(
        scope_kind, {"agent": "", "thread": "chat_stats", "root_run": root}[scope_kind]
    )

    result = fixture.stats.models("1mo", scope).data

    assert result is not None and len(result) == 1
    assert result[0].model_calls == 2 and result[0].in_ == 600
    assert result[0].spend == 0.2
    assert project.call_count == 3
    assert fixture.stats.models("all", scope).data[0].in_ == 1000


@pytest.mark.parametrize("scope_kind", ["thread", "root_run"])
@pytest.mark.parametrize("mode", ["all", "recent", "archived_edge"])
def test_scoped_queries_do_not_decode_unrelated_steps(
    fixture, monkeypatch, scope_kind, mode
):
    at = NOW - 20 if mode == "recent" else NOW - 2 * DAY - 20
    selected = fixture.root("selected", NOW - 5 * DAY)
    unrelated = fixture.root("unrelated", NOW - 5 * DAY, thread="chat_unrelated")
    for root in (selected, unrelated):
        fixture.call(root, at, at + 10)
        if mode == "archived_edge":
            fixture.finish(root, NOW - 2 * DAY - 1)
    if mode == "archived_edge":
        assert fixture.refresh() is None
    project = Mock(wraps=records.project_step)
    monkeypatch.setattr(records, "project_step", project)
    monkeypatch.setattr(query, "project_step", project)
    interval = {
        "all": (None, None),
        "recent": (NOW - 1800, NOW),
        "archived_edge": (NOW - 4 * DAY, NOW - 2 * DAY),
    }[mode]
    scope = Scope(scope_kind, "chat_stats" if scope_kind == "thread" else selected)

    with fixture.stats.records.snapshot() as connection:
        result = query.StatsReader(fixture.stats.records, connection).query(
            [interval], scope, None, recent=mode == "recent"
        )[0]["test/model"]

    assert result.model_calls == 1 and result.input_tokens == 100
    assert project.call_count == 1


def test_publication_between_snapshot_pins_does_not_lose_or_duplicate_root(
    fixture, monkeypatch
):
    root = fixture.root("race", NOW - 4 * DAY)
    fixture.call(root, NOW - 3 * DAY, NOW - 3 * DAY + 1)
    fixture.finish(root, NOW - 2 * DAY)
    expected = fixture.stats.summary().data
    original = records.connect
    with fixture.stats.records.collect(root, lambda: False) as additions:
        pending = True
        pins = []
        published = []

        def connect(path, **kwargs):
            connection = original(path, **kwargs)

            def trace(statement):
                nonlocal pending
                if "SELECT initialized,updated_at FROM main.state" in statement:
                    pins.append("stats")
                if "SELECT id FROM src.runs LIMIT 1" in statement:
                    pins.append("source")
                if pending and "SELECT id FROM src.runs LIMIT 1" in statement:
                    pending = False
                    published.append(
                        fixture.stats.records.publish(
                            root, NOW - 2 * DAY, NOW, additions
                        )
                    )

            connection.set_trace_callback(trace)
            return connection

        monkeypatch.setattr(records, "connect", connect)
        assert fixture.stats.summary().data == expected
        assert not pending
        assert pins[:2] == ["stats", "source"]
        assert published == [True]
    assert fixture.stats.summary().data == expected


def test_unknowns_free_calls_and_failed_or_skipped_invocations(fixture):
    root = fixture.root("facts", NOW - 4 * DAY)
    fixture.call(
        root,
        NOW - 3 * DAY,
        NOW - 3 * DAY + 1,
        model="test/free",
        spend=0,
        cached=0,
        cost_source="zero",
    )
    fixture.call(
        root,
        NOW - 3 * DAY,
        NOW - 3 * DAY + 1,
        model="test/unknown",
        unknown=True,
        spend=None,
        status="failed",
    )
    fixture.call(root, NOW - 3 * DAY, NOW - 3 * DAY + 1, invoked=False)
    fixture.call(root, NOW - 3 * DAY, NOW - 3 * DAY + 1, tool=True, status="canceled")
    fixture.finish(root, NOW - 2 * DAY)
    assert fixture.refresh() is None
    rows = {row.model_ref: row for row in fixture.stats.models("all").data}
    assert set(rows) == {"test/free", "test/unknown"}
    assert rows["test/free"].spend == 0 and rows["test/free"].cached == 0
    assert rows["test/unknown"].in_ is None and rows["test/unknown"].spend is None
    assert (
        rows["test/unknown"].model_calls == 1 and rows["test/unknown"].root_runs is None
    )
    total = fixture.stats.summary().data[0]
    assert total.model_calls == 2 and total.tool_calls == 1


@pytest.mark.parametrize(
    "zone,day,hours",
    [
        ("America/New_York", date(2026, 3, 8), 23),
        ("America/New_York", date(2026, 11, 1), 25),
        ("Australia/Lord_Howe", date(2026, 10, 4), 23.5),
        ("Pacific/Apia", date(2011, 12, 30), 0),
    ],
)
def test_local_date_boundaries(zone, day, hours):
    from datetime import timedelta

    assert (
        midnight(day + timedelta(days=1), ZoneInfo(zone))
        - midnight(day, ZoneInfo(zone))
        == hours * 3600
    )


def test_source_and_query_connections_cannot_write_persistent_databases(fixture):
    with fixture.stats.records.snapshot() as connection:
        for statement in (
            "CREATE TABLE src.forbidden(x)",
            "DELETE FROM main.roots",
        ):
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                connection.execute(statement)


def test_call_counts_and_usage_belong_to_different_local_dates(fixture):
    from datetime import datetime, timezone

    midnight_utc = NOW // DAY * DAY
    root = fixture.root("midnight", midnight_utc - 10)
    fixture.call(root, midnight_utc - 1, midnight_utc + 1)
    today = datetime.fromtimestamp(NOW, timezone.utc).date()
    from datetime import timedelta

    result = fixture.stats.daily(
        today - timedelta(days=1), today + timedelta(days=1), "UTC"
    ).data
    assert (
        result[0].model_calls == 1 and result[0].in_ == 0 and result[0].root_runs == 1
    )
    assert (
        result[1].model_calls == 0 and result[1].in_ == 100 and result[1].root_runs == 0
    )


def test_candidate_paging_time_and_bucket_queries_use_indexes(fixture):
    from toolang.stats import _sql as sql
    from toolang.stats.types import timestamp

    with fixture.stats.records.snapshot() as connection:
        parameters = {
            "root_id": "run_old",
            "run_id": "run_old",
            "after_id": "run_old.9",
            "lo_text": timestamp(NOW - 1800),
            "hi_text": timestamp(NOW),
            "cutoff_text": timestamp(NOW - DAY),
            "page_size": 500,
            "scope_kind": "agent",
            "scope_id": "",
            "period": "day",
            "lo": NOW - 365 * DAY,
            "hi": NOW,
            "model_ref": None,
        }
        queries = [
            (sql.ARCHIVE_CANDIDATES, "idx_runs_closed_roots"),
            (sql.UNARCHIVED_ROOTS, "idx_runs_roots"),
            (sql.OWNED_RUNS, "idx_runs_parent"),
            (sql.SOURCE_STEP_PAGE, "idx_steps_run"),
            (sql.TIME_STEP_IDS, "idx_steps_call_started"),
            (sql.TIME_STEP_IDS, "idx_steps_call_finished"),
            (sql.BUCKET_TOTALS, "PRIMARY KEY"),
        ]
        for statement, expected in queries:
            plan = str(
                [
                    tuple(row)
                    for row in connection.execute(
                        "EXPLAIN QUERY PLAN " + statement, parameters
                    )
                ]
            )
            assert expected in plan
            if statement == sql.SOURCE_STEP_PAGE:
                assert "id>?" in plan and "ORDER BY" not in plan
