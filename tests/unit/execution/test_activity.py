"""Consumption survives control mutations and has one committed observation boundary."""

from contextlib import closing
from datetime import datetime
import sqlite3

import pytest

from toolang.base.types.message import Message
from toolang.execution.activity import ActivityQuery, ActivityReader
from toolang.execution import statistics
from toolang.execution.store import RunStore
from toolang.execution.records import RunControlPayload
from tests.support.execution_fixtures import (
    project_run_start,
    project_run_end,
    project_step,
)


def at(second):
    return f"2026-10-09T10:{second // 60:02}:{second % 60:02}Z"


def clock(second):
    return datetime.fromisoformat(at(second).replace("Z", "+00:00")).timestamp()


@pytest.fixture
def store(tmp_path):
    with closing(RunStore(tmp_path / "runs.db")) as value:
        statistics.start_session(value, "one", at(0))
        yield value


def root(store, name="run_root", start=0, parent=None):
    return project_run_start(
        store,
        run_id=name,
        thread_id="term_test",
        origin="chat",
        input=Message.user("hello"),
        parent=parent,
        started_at=at(start),
        created_at=at(start),
    )


def snapshot(store, *, since="session", recent=None, now=120, text="", active=False):
    return ActivityReader(store.db_path, "agent:alice").read(
        ActivityQuery(since, recent, text, active), now=clock(now)
    )


def model(
    store, run="run_root", index=0, start=10, end=70, cost=0.5, status="succeeded"
):
    return project_step(
        store,
        run_id=run,
        index=index,
        kind="model",
        status=status,
        input=(),
        output=(),
        started_at=at(start),
        finished_at=at(end),
        detail={"tokens": {"input": 5, "output": 5}, "cost": cost},
    )


def test_boundary_cost_counts_and_duration_are_independent(store):
    root(store)
    model(store)
    project_run_end(store, run_id="run_root", finished_at=at(120))
    page = snapshot(store, since=at(60))
    assert (page.stats.model, page.stats.cost, page.stats.time) == (0, 0.5, 60)
    assert page.roots[0].total.model == 1
    assert page.roots[0].total.time == 120
    assert page.roots[0].total.estimated
    assert snapshot(store, since=at(121)).stats.cost == 0


def test_parallel_children_keep_own_statistics_and_root_duration(store):
    root(store)
    step = project_step(
        store,
        run_id="run_root",
        index=1,
        kind="par",
        status="running",
        input=(),
        output=(),
        started_at=at(1),
        finished_at=None,
    )
    root(store, "run_child", 2, step.ref)
    model(store, run="run_child", status="running", start=3)
    page = snapshot(store, now=10)
    assert page.stats.model == 1
    assert page.stats.time == 10
    assert page.roots[0].stats.time == 10
    paths = {node.id: node for node in page.paths}
    assert paths["run_child"].stats.time == 8
    assert paths["run_root.1"].stats.model == 1
    assert paths["run_child.0"].parent == "run_child"
    assert page.stats.cost is None
    assert page.stats.partial


def test_retry_preserves_attempts_and_duplicates_do_not_recount(store):
    root(store)
    model(store)
    model(store)
    project_run_end(store, run_id="run_root", status="failed", finished_at=at(80))
    control = store.get_run_control(run_id="run_root", index=0)
    assert control and isinstance(control.payload, RunControlPayload)
    payload = control.payload
    reopened, retry, _ = store.accept_retry(
        run_id="run_root",
        anchor=None,
        resources=payload.resources,
        limits=payload.limits,
        state=payload.state,
        sandbox="host",
        created_at=at(90),
        request_id=None,
    )
    assert not store.list_steps(run_id="run_root")
    page = snapshot(store)
    assert (page.stats.model, page.stats.cost, page.stats.time) == (1, 0.5, 80)
    store.begin_run(run_id="run_root", started_at=at(100), control=retry.ref)
    model(store, start=101, end=110, cost=0.2)
    page = snapshot(store, now=120)
    assert page.stats.model == 2
    assert page.stats.cost == pytest.approx(0.7)
    assert page.stats.input_tokens == 10 and page.stats.output_tokens == 10
    assert page.stats.time == 100


def test_rollback_is_invisible_to_usage_and_revision(store):
    root(store)
    before = snapshot(store)
    with pytest.raises(RuntimeError):
        with store.write_transaction():
            model(store)
            raise RuntimeError("rollback")
    after = snapshot(store)
    assert after.revision == before.revision
    assert after.stats == before.stats


def test_restart_closes_crashed_intervals_at_checkpoint(store):
    root(store)
    model(store, status="running")
    statistics.checkpoint(store, "one", at(20))
    statistics.start_session(store, "two", at(100))
    page = snapshot(store, now=120)
    assert page.stats.time == 0
    assert page.roots[0].stale
    assert not page.paths
    assert page.total.time == 20
    assert not page.total.complete
    assert snapshot(store, since=at(15)).stats.time == 5


def test_recent_filter_and_cache_do_not_change_statistics(store):
    root(store)
    model(store)
    project_run_end(store, run_id="run_root", finished_at=at(80))
    store.create_thread(thread_id="term_empty", origin="chat", created_at=at(100))
    page = snapshot(store, recent=30, now=120)
    assert not page.roots
    assert [node.id for node in page.threads] == ["term_empty"]
    assert page.stats.model == 1
    assert snapshot(store, text="run_root.0").roots[0].matches == ["run_root.0"]
    assert not snapshot(store, active=True).roots


def test_cached_clock_uses_open_intervals_without_reading_history(store, monkeypatch):
    root(store)
    reader = ActivityReader(store.db_path, "agent:alice")
    initial = reader.read(ActivityQuery(), now=clock(10))

    def forbidden(*args, **kwargs):
        raise AssertionError("History reread on clock tick")

    monkeypatch.setattr("toolang.execution.activity._metrics", forbidden)
    later = reader.read(ActivityQuery(), now=clock(20))
    assert initial.stats.time is not None
    assert initial.roots[0].stats.time is not None
    assert later.stats.time == initial.stats.time + 10
    assert later.roots[0].stats.time == initial.roots[0].stats.time + 10


def test_pagination_keeps_all_roots_and_revision_in_one_snapshot(store):
    for index in range(205):
        root(store, f"run_{index}")
    reader = ActivityReader(store.db_path, "agent:alice")
    pages = reader.pages(ActivityQuery(), now=clock(10))
    assert [len(page.roots) for page in pages] == [200, 5]
    assert {page.revision for page in pages} == {pages[0].revision}
    assert pages[0].available == 205
    assert pages[0].next_offset == 200 and pages[1].next_offset is None
    assert pages[0].stats.time == 2050


def test_migration_resumes_batches_without_recounting(tmp_path, monkeypatch):
    path = tmp_path / "runs.db"
    with closing(RunStore(path)) as original:
        root(original)
        for index in range(300):
            model(original, index=index)
        project_run_end(original, run_id="run_root", finished_at=at(100))
        # Restore a v53-shaped database; no observation metadata existed there.
        connection = original._conn
        for kind, name in connection.execute(
            "SELECT type,name FROM sqlite_master WHERE type IN ('trigger','table') AND name LIKE 'activity_%' ORDER BY type DESC"
        ).fetchall():
            connection.execute(f'DROP {kind} "{name}"')
        connection.execute("PRAGMA user_version=53")
        connection.commit()
    original_flush = statistics.flush
    calls = 0

    def interrupt(store, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("migration interrupted")
        original_flush(store, **kwargs)

    monkeypatch.setattr(statistics, "flush", interrupt)
    with pytest.raises(RuntimeError, match="interrupted"):
        RunStore(path)
    monkeypatch.setattr(statistics, "flush", original_flush)
    with closing(RunStore(path)) as migrated:
        page = snapshot(migrated, since="all")
        assert page.stats.model == 300
        assert page.stats.cost == 150
        assert not page.stats.complete
        assert page.complete
        assert (
            migrated._conn.execute("SELECT COUNT(*) FROM activity_dirty").fetchone()[0]
            == 0
        )
    with closing(RunStore(path)) as reopened:
        assert snapshot(reopened, since="all").stats.cost == 150


def test_async_completed_launch_stays_only_as_running_ancestor(store):
    root(store)
    launch = project_step(
        store,
        run_id="run_root",
        index=1,
        kind="run",
        status="succeeded",
        input=(),
        output=(),
        started_at=at(1),
        finished_at=at(2),
    )
    root(store, "run_child", 3, launch.ref)
    model(store, run="run_child", status="running", start=4)
    project_step(
        store,
        run_id="run_root",
        index=2,
        kind="value",
        status="succeeded",
        input=(),
        output=(),
        started_at=at(5),
        finished_at=at(6),
    )
    paths = {node.id: node for node in snapshot(store).paths}
    assert paths[launch.id].status == "succeeded"
    assert "run_root.2" not in paths
    project_run_end(store, run_id="run_child", finished_at=at(70))
    project_run_end(store, run_id="run_root", finished_at=at(80))
    assert not snapshot(store).paths


def test_fork_and_rewind_keep_physical_consumption_in_original_thread(store):
    root(store)
    model(store)
    project_run_end(store, run_id="run_root", finished_at=at(80))
    store.fork_thread(
        thread_id="term_fork",
        source="term_test",
        anchor="run_root",
        request_id=None,
        created_at=at(90),
    )
    store.rewind_thread(
        thread_id="term_test",
        anchor="run_root",
        request_id=None,
        expected_head=store.thread_view("term_test").head,
        created_at=at(100),
    )
    page = snapshot(store, recent=30, now=120)
    assert page.stats.cost == 0.5
    threads = {node.id: node for node in page.threads}
    assert threads["term_test"].stats.model == 1
    assert threads["term_fork"].stats.model == 0
    assert threads["term_test"].changed == clock(100)


def test_map_cardinality_is_compact_metadata(store):
    root(store)
    step = project_step(
        store,
        run_id="run_root",
        index=1,
        kind="par",
        status="running",
        input=(),
        output=(),
        started_at=at(1),
        finished_at=None,
    )
    project_run_start(
        store,
        run_id="run_child",
        thread_id="term_test",
        origin="chat",
        input=Message.user("child"),
        parent=step.ref,
        created_at=at(2),
        started_at=at(2),
        context={"occurrence": {"item": 0, "items": 8}},
    )
    model(store, run="run_child", status="running", start=3)
    paths = {node.id: node for node in snapshot(store).paths}
    assert paths[step.id].children == 8


def test_model_preview_uses_only_bounded_existing_content(store):
    from toolang.base.types.run import ModelCall
    from toolang.execution.types import ModelStepGiven, StepRef

    root(store)
    store.begin_step(
        ref=StepRef.parse("run_root.0"),
        kind="model",
        input=(),
        given=ModelStepGiven(
            "test/model",
            ModelCall(
                "system text", [Message.user("Analyze configuration " + "x" * 1000)]
            ),
            setup="test-setup",
        ),
        started_at=at(10),
    )
    page = snapshot(store)
    assert page.paths[0].summary.startswith("preview: Analyze configuration")
    assert len(page.paths[0].summary) <= 240


def test_retry_freezes_removed_unfinished_attempt_before_reusing_ref(store):
    root(store)
    model(store, status="running")
    project_run_end(store, run_id="run_root", status="failed", finished_at=at(80))
    statistics.checkpoint(store, "one", at(80))
    control = store.get_run_control(run_id="run_root", index=0)
    assert control and isinstance(control.payload, RunControlPayload)
    payload = control.payload
    _, retry, _ = store.accept_retry(
        run_id="run_root",
        anchor=None,
        resources=payload.resources,
        limits=payload.limits,
        state=payload.state,
        sandbox="host",
        created_at=at(90),
        request_id=None,
    )
    store.begin_run(run_id="run_root", started_at=at(100), control=retry.ref)
    model(store, start=101, status="running")
    step = next(
        node for node in snapshot(store, now=120).paths if node.id == "run_root.0"
    )
    assert step.stats.time == 89  # 70 interrupted seconds + 19 on the retry.
    assert step.stats.model == 2
    assert not step.stats.complete
    assert snapshot(store, now=130).paths[0].stats.time == 99


def test_session_end_freezes_unfinished_intervals_before_restart(store):
    root(store)
    model(store, status="running")
    statistics.checkpoint(store, "one", at(20), end=True)
    assert snapshot(store, now=90).total.time == 20
    statistics.start_session(store, "two", at(100))
    page = snapshot(store, now=120)
    assert page.total.time == 20
    assert not page.total.complete
    assert page.stats.time == 0


def test_thread_limit_applies_after_recent_selection(store, monkeypatch):
    monkeypatch.setattr("toolang.execution.activity.THREAD_LIMIT", 2)
    for index in range(3):
        store.create_thread(
            thread_id=f"term_old{index}", origin="chat", created_at=at(0)
        )
    root(store, start=100)
    page = snapshot(store, recent=30, now=120)
    assert [node.id for node in page.threads] == ["term_test"]
    assert page.thread_count == 4
    assert page.complete


def test_page_size_does_not_multiply_history_reads(store, monkeypatch):
    for index in range(12):
        root(store, f"run_{index}")
    statements = []
    connect = sqlite3.connect

    def traced(*args, **kwargs):
        conn = connect(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(sqlite3, "connect", traced)
    query = ActivityQuery("all", None, "run_")
    whole = ActivityReader(store.db_path, "agent:alice").pages(query, now=clock(100))
    whole_reads = len(statements)
    statements.clear()
    monkeypatch.setattr("toolang.execution.activity.PAGE_SIZE", 1)
    paged = ActivityReader(store.db_path, "agent:alice").pages(query, now=clock(100))
    assert [node for page in paged for node in page.roots] == whole[0].roots
    assert all(page.stats == whole[0].stats for page in paged)
    assert len(statements) <= whole_reads + 10
    assert sum(len(page.threads) for page in paged) == len(whole[0].threads)


def test_thread_counts_describe_recent_eligibility_before_filters_and_limits(
    store, monkeypatch
):
    root(store)
    root(store, "run_second")
    store.create_thread(thread_id="term_empty", origin="chat", created_at=at(100))
    store.create_thread(thread_id="term_old", origin="chat", created_at=at(0))
    page = snapshot(store, recent=30, text="run_root")
    assert page.thread_count == 3
    assert page.thread_eligible == 2
    assert page.thread_matched == 1
    assert page.eligible == 2 and page.matched == 1
    monkeypatch.setattr("toolang.execution.activity.THREAD_LIMIT", 1)
    limited = snapshot(store, recent=30)
    assert limited.thread_matched == limited.thread_eligible == 2
    assert len(limited.threads) == 1
    assert not limited.complete


def test_model_completion_reuses_the_captured_preview(store, monkeypatch):
    from toolang.base.types.run import ModelCall
    from toolang.execution.types import ModelStepGiven, ModelStepNoted, StepRef

    root(store)
    ref = StepRef.parse("run_root.0")
    store.begin_step(
        ref=ref,
        kind="model",
        input=(),
        given=ModelStepGiven(
            "test/model",
            ModelCall("instructions", [Message.user("Review configuration")]),
            setup="test-setup",
        ),
        started_at=at(10),
    )

    def forbidden(*args, **kwargs):
        raise AssertionError(
            "Completion must not reload the prompt to recapture a preview"
        )

    monkeypatch.setattr(store, "get_content", forbidden)
    store.finish_step(
        ref=ref,
        kind="model",
        status="canceled",
        output=None,
        noted=ModelStepNoted(),
        error=None,
        finished_at=at(20),
    )
    assert snapshot(store, text="Review configuration").roots[0].matches == [str(ref)]
