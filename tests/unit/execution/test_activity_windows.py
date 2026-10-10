"""Relative statistics age committed facts without new execution events."""

from contextlib import closing

import pytest

from toolang.execution import statistics
from toolang.execution.activity import ActivityQuery, ActivityReader
from toolang.execution.store import RunStore
from tests.support.execution_fixtures import project_run_end
from tests.unit.execution.test_activity import at, clock, model, root


def test_rolling_window_expires_settled_usage_and_clips_closed_time(tmp_path):
    with closing(RunStore(tmp_path / "runs.db")) as store:
        statistics.start_session(store, "one", at(0))
        root(store)
        model(store, start=10, end=70)
        project_run_end(store, run_id="run_root", finished_at=at(120))
        statistics.checkpoint(store, "one", at(120), end=True)
        reader = ActivityReader(store.db_path, "agent:alice")
        query = ActivityQuery("60s", None)
        first = reader.pages(query, now=clock(120))[0]
        later = reader.pages(query, now=clock(135))[0]
        assert first.revision == later.revision
        assert first.since == later.since == "60s"
        assert (first.stats.model, first.stats.cost, first.stats.time) == (0, 0.5, 60)
        assert (later.stats.model, later.stats.cost, later.stats.time) == (0, 0, 45)
        assert first.stats.input_tokens == 5 and later.stats.input_tokens == 0
        assert (
            later.total.model == 1
            and later.total.cost == 0.5
            and later.total.time == 120
        )
        # Offline history still ages Activity; observation does not freeze the clock.
        assert reader.pages(ActivityQuery("session", 30), now=clock(151))[0].roots == []
        assert (
            reader.pages(ActivityQuery("session", None), now=clock(151))[0].stats.time
            == 120
        )


def test_open_duration_plateaus_at_window_and_freezes_at_checkpoint_offline(tmp_path):
    with closing(RunStore(tmp_path / "runs.db")) as store:
        statistics.start_session(store, "one", at(0))
        root(store)
        statistics.checkpoint(store, "one", at(40))
        reader = ActivityReader(store.db_path, "agent:alice")
        query = ActivityQuery("30s", None)
        for now in (31, 40, 90):
            assert reader.pages(query, now=clock(now), live=True)[0].stats.time == 30
        offline = reader.pages(query, now=clock(60), live=False)[0]
        assert offline.stats.time == 10 and offline.presence == "offline"
        assert offline.stale and offline.paths == []
        assert reader.pages(query, now=clock(80), live=False)[0].stats.time == 0
        assert (
            reader.pages(ActivityQuery("all", None), now=clock(80), live=False)[
                0
            ].stats.time
            == 40
        )


@pytest.mark.parametrize("value", ["1h", "1d", "1w", "0.5m"])
def test_relative_query_identity_is_stable(value):
    query = ActivityQuery(value)
    later, earlier = query.start(clock(120)), query.start(clock(60))
    assert later is not None and earlier is not None
    assert later - earlier == 60
    assert query.since == value


def test_rolling_ticks_reuse_structure_and_bound_duration_queries(
    tmp_path, monkeypatch
):
    from toolang.execution import activity

    with closing(RunStore(tmp_path / "runs.db")) as store:
        statistics.start_session(store, "one", at(0))
        root(store)
        model(store)
        project_run_end(store, run_id="run_root", finished_at=at(120))
        reader = ActivityReader(store.db_path, "agent:alice")
        query = ActivityQuery("60s", None)
        reader.pages(query, now=clock(120))

        def rebuild(*args, **kwargs):
            raise AssertionError("A window tick must not rebuild unchanged activity")

        monkeypatch.setattr(activity, "_read", rebuild)
        assert reader.pages(query, now=clock(135))[0].stats.time == 45
        # An old, large history must not turn a recent duration query into a
        # scan of every attempt owned by the agent.
        conn = store._conn
        conn.executemany(
            "INSERT INTO activity_attempts(id,ref,session,kind,started,finished) VALUES (?,?,'one','run',?,?)",
            [(i, f"run_old{i}", clock(0), clock(1)) for i in range(1000, 11000)],
        )
        conn.executemany(
            "INSERT INTO activity_owners(scope,attempt,duration) VALUES ('@agent',?,1)",
            [(i,) for i in range(1000, 11000)],
        )
        budget = 20

        def progress():
            nonlocal budget
            budget -= 1
            return int(budget <= 0)

        conn.set_progress_handler(progress, 1000)
        try:
            metrics = activity._metrics(
                conn,
                "@agent",
                "60s",
                conn.execute("SELECT * FROM activity_meta").fetchone(),
                clock(135),
            )
            assert metrics.time == 45
        finally:
            conn.set_progress_handler(None, 0)
