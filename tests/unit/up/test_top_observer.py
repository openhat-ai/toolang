"""Observation generations, discovery retention and slow scans stay independent."""

from toolang.up.workload import ForegroundWorkload

from concurrent.futures import Future
from contextlib import closing
from threading import Event
from dataclasses import replace
import json

import httpx
import pytest

from tests.support.execution_fixtures import project_run_start
from tests.support.runtime_observation import NOW, binding, completed, usage
from toolang.base.types.message import Message

from toolang.common.layout import AgentLayout
from toolang.up.identity import allocate_roaming
from toolang.execution.schemas import LiveSnapshot, LiveTreeSnapshot, RunNode, StepNode
from toolang.execution.store import RunStore
from toolang.execution.types import RunReport
from toolang.stats.errors import StatsUnavailable
from toolang.stats.schemas import CompletionCounts
from toolang.stats.types import Metrics, timestamp
from toolang.up import activity, top
from toolang.up.activity import ActivityReader
from toolang.up.observation import RuntimeInventory
from toolang.up.schemas import ActivitySnapshot, SourceActivity
from toolang.up.types import RuntimeInstanceSnapshot


def resolved(value):
    result = Future()
    result.set_result(value)
    return result


@pytest.mark.parametrize("started", [False, True])
def test_departure_stops_endpoint_polling_and_rejects_inflight_live(
    tmp_path, monkeypatch, started
):
    source = binding(tmp_path)
    now = [NOW]
    calls = []
    monkeypatch.setattr(top.time, "time", lambda: now[0])

    def read(_reader, source, recent, **kwargs):
        calls.append(source.endpoint)
        return ActivitySnapshot(live=None, completed=completed(now[0], recent))

    monkeypatch.setattr(ActivityReader, "read_activity", read)
    with closing(
        top.TopObserver(tmp_path, agent=None, period=3600, recent=300, refresh=60)
    ) as observer:
        observer.discovery_job = resolved(
            RuntimeInventory(RuntimeInstanceSnapshot((source.instance,)), (source,))
        )
        observer._inventory(NOW)
        state = observer.sources[source.instance.identity]
        inflight = Future()
        if started:
            inflight.set_running_or_notify_cancel()
        state.activity, state.activity_recent = inflight, 300
        state.next_usage = NOW + 100
        stopped = replace(
            source,
            endpoint=None,
            registration=None,
            launch_id=None,
            instance=replace(source.instance, state="not running"),
        )
        observer.discovery_job = resolved(
            RuntimeInventory(RuntimeInstanceSnapshot((stopped.instance,)), (stopped,))
        )
        now[0] += 1
        observer._inventory(now[0])
        assert state.binding.endpoint is None
        assert observer.inventory.instances == (stopped.instance,)
        assert calls == []
        if not started:
            assert inflight.cancelled() and state.activity is None
            return
        root = RunNode("run_old", None, "agic:work", "running", timestamp(NOW - 10))
        late_live = LiveSnapshot(
            "old", timestamp(NOW), (LiveTreeSnapshot(root.id, "chat_top", (root,)),)
        )
        done = replace(root, status="succeeded", finished_at=timestamp(NOW))
        inflight.set_result(
            ActivitySnapshot(
                live=late_live,
                completed=completed().model_copy(
                    update={"counts": CompletionCounts(succeeded=1), "roots": (done,)}
                ),
            )
        )
        observer._collect(state, now[0])
        assert state.value.live is None and state.value.live_stale
        assert state.value.completed is not None
        assert state.value.completed.roots == (done,)
        assert state.next_activity <= now[0]
        observer.tick()
        assert state.activity is not None
        state.activity.result(timeout=1)
        assert calls == [None]


@pytest.mark.parametrize("departed", [False, True])
def test_rediscovery_rejects_live_results_from_earlier_bindings(
    tmp_path,
    departed,
):
    source = binding(tmp_path)
    with closing(
        top.TopObserver(tmp_path, agent=None, period=3600, recent=300, refresh=60)
    ) as observer:
        inventory = RuntimeInventory(
            RuntimeInstanceSnapshot((source.instance,)), (source,)
        )
        observer.discovery_job = resolved(inventory)
        observer._inventory(NOW)
        state = observer.sources[source.instance.identity]
        state.value.usage = usage()
        inflight = Future()
        inflight.set_running_or_notify_cancel()
        state.activity, state.activity_recent = inflight, 300
        if departed:
            observer.discovery_job = resolved(
                RuntimeInventory(
                    RuntimeInstanceSnapshot((source.instance,)),
                    (replace(source, endpoint=None),),
                )
            )
            observer._inventory(NOW + 1)
        else:
            inventory = replace(inventory, sources=(replace(source, launch_id="new"),))
        observer.discovery_job = resolved(inventory)
        observer._inventory(NOW + 2)
        assert observer.sources == {source.instance.identity: state}
        assert state.binding.endpoint == source.endpoint
        inflight.set_result(
            ActivitySnapshot(
                live=LiveSnapshot("obsolete", timestamp(NOW), ()), completed=completed()
            )
        )
        observer._collect(state, NOW + 2)
        assert state.value.live is None and state.value.live_stale
        assert state.next_activity <= NOW + 2
        assert (
            state.value.usage is not None
            and state.value.usage.metrics.model_calls == 76
        )


def test_current_membership_is_not_inferred_from_readable_source_bindings(tmp_path):
    source = binding(tmp_path)
    with closing(
        top.TopObserver(tmp_path, agent=None, period=3600, recent=300, refresh=60)
    ) as observer:
        observer.discovery_job = resolved(
            RuntimeInventory(RuntimeInstanceSnapshot((source.instance,)), ())
        )
        observer._inventory(NOW)
        assert observer.inventory.instances == (source.instance,)
        summary = observer.tick().summary
        assert summary.resident == 1 and summary.active is None
        assert summary.completed is None and summary.usage is None


def test_discovery_failure_preserves_bindings_until_successful_removal(tmp_path):
    source = binding(tmp_path)
    with closing(
        top.TopObserver(tmp_path, agent=None, period=3600, recent=300, refresh=60)
    ) as observer:
        observer.discovery_job = resolved(
            RuntimeInventory(RuntimeInstanceSnapshot((source.instance,)), (source,))
        )
        observer._inventory(NOW)
        state = observer.sources[source.instance.identity]
        failure = Future()
        failure.set_exception(OSError("registry temporarily unavailable"))
        observer.discovery_job = failure
        observer._inventory(NOW + 1)
        assert observer.inventory.instances == (source.instance,)
        assert observer.inventory.partial and state.binding.endpoint == source.endpoint
        queued = Future()
        state.activity = queued
        observer.discovery_job = resolved(
            RuntimeInventory(RuntimeInstanceSnapshot(()), ())
        )
        observer._inventory(NOW + 2)
        assert not observer.sources and queued.cancelled() and state.stopped.is_set()


@pytest.mark.parametrize("placement", ["visiting", "roaming"])
def test_departure_removes_history_and_fresh_top_has_the_same_scope(
    tmp_path, monkeypatch, placement
):
    now = [NOW]
    monkeypatch.setattr(top.time, "time", lambda: now[0])
    resident = AgentLayout.resident(tmp_path, "alice")
    resident.home.mkdir(parents=True)
    layout = (
        AgentLayout.visiting("https://example.com/worker.too", "worker", root=tmp_path)
        if placement == "visiting"
        else allocate_roaming(tmp_path / "worker.too", toolang_root=tmp_path)
    )
    with (
        closing(RunStore(resident.run_store)),
        ForegroundWorkload(resident, toolang_root=tmp_path, drivers=frozenset()),
        closing(
            top.TopObserver(tmp_path, agent=None, period=3600, recent=300, refresh=60)
        ) as observer,
    ):
        with ForegroundWorkload(layout, toolang_root=tmp_path, drivers=frozenset()):
            with closing(RunStore(layout.run_store)) as store:
                project_run_start(
                    store,
                    run_id="run_done",
                    thread_id="chat_top",
                    input=Message.user("fixture"),
                    started_at=timestamp(NOW - 10),
                )
                store.finish_run(run_id="run_done", finished_at=timestamp(NOW - 1))
            first = observer.once()
            assert len(first.inventory.instances) == 2
            assert (
                first.summary.completed is not None
                and first.summary.completed.total == 1
            )
        observer.discovery_job = resolved(observer.scope_reader.read())
        now[0] += 1
        snapshot = observer.tick()
        assert len(snapshot.inventory.instances) == 1
        assert len(snapshot.sources) == 1
        assert (
            snapshot.summary.completed is not None
            and snapshot.summary.completed.total == 0
        )
        from toolang.cli.common.top_view import TopView

        assert "run_done" not in TopView().render(snapshot, width=140).plain
        observer.set_recent(60)
        now[0] += 61
        for state in observer.sources.values():
            if state.activity is not None:
                state.activity.result(timeout=1)
        observer.tick()
        for state in observer.sources.values():
            if state.activity is not None:
                state.activity.result(timeout=1)
        expired = observer.tick()
        assert (
            expired.summary.completed is not None
            and expired.summary.completed.total == 0
        )
        assert "run_done" not in TopView().render(expired, width=140).plain
        with closing(
            top.TopObserver(tmp_path, agent=None, period=3600, recent=300, refresh=60)
        ) as fresh:
            restarted = fresh.once()
            assert (
                len(restarted.sources) == 1
                and restarted.sources[0].placement == "resident"
            )
            assert (
                restarted.summary.completed is not None
                and restarted.summary.completed.total == 0
            )
        assert not layout.stats_store.exists()
        assert not (tmp_path / ".runtime" / "recent-sources").exists()


def test_slow_usage_never_blocks_live_or_marks_new_window_with_old_metrics(
    tmp_path, monkeypatch
):
    source = binding(tmp_path)
    now = [NOW]
    release = Event()
    next_release = Event()
    scans = []
    live_calls = []
    monkeypatch.setattr(top.time, "time", lambda: now[0])
    monkeypatch.setattr(
        top.ScopeReader,
        "read",
        lambda _, **kwargs: RuntimeInventory(
            RuntimeInstanceSnapshot((source.instance,)), (source,)
        ),
    )

    def slow_usage(_reader, source, period, cancelled=None):
        scans.append(period)
        assert (release if period == 3600 else next_release).wait(3)
        return usage(period)

    def live(_reader, source, recent, before=None, completed_at=None, **kwargs):
        live_calls.append(recent)
        return ActivitySnapshot(
            live=LiveSnapshot("epoch", timestamp(now[0]), ()),
            completed=completed(now[0], recent),
        )

    monkeypatch.setattr(ActivityReader, "read_usage", slow_usage)
    monkeypatch.setattr(ActivityReader, "read_activity", live)
    observer = top.TopObserver(
        tmp_path, agent=None, period=3600, recent=300, refresh=0.1
    )
    try:
        observer.tick()
        assert observer.discovery_job is not None
        observer.discovery_job.result(timeout=1)
        observer.tick()
        state = observer.sources["source"]
        assert state.activity is not None
        state.activity.result(timeout=1)
        snapshot = observer.tick()
        assert not snapshot.sources[0].live_stale
        assert snapshot.usage_total() is None
        observer.set_period(14400)
        observer.set_recent(900)
        now[0] += 1
        observer.tick()
        assert state.activity is not None
        state.activity.result(timeout=1)
        snapshot = observer.tick()
        assert snapshot.sources[0].completed is not None
        assert snapshot.recent == 900 and snapshot.sources[
            0
        ].completed.start == timestamp(now[0] - 900)
        assert state.usage is not None
        assert len(live_calls) == 2 and not state.usage.done()
        release.set()
        assert state.usage is not None
        state.usage.result(timeout=1)
        snapshot = observer.tick()
        assert snapshot.period == 14400 and snapshot.sources[0].usage is None
        now[0] += 1
        observer.tick()
        assert state.usage is not None
        next_release.set()
        state.usage.result(timeout=1)
        total = observer.tick().usage_total()
        assert total is not None and total.model_calls == 76
        assert scans == [3600, 14400]
    finally:
        release.set()
        next_release.set()
        observer.close()


def test_disconnect_freezes_known_live_epoch_replacement_and_recent_generation(
    tmp_path,
):
    source = binding(tmp_path)
    old = LiveSnapshot("old", timestamp(NOW - 5), ())
    state = top._Source(
        source,
        SourceActivity(
            identity="source",
            label="alice",
            placement="resident",
            live=old,
            live_stale=False,
        ),
    )
    observer = top.TopObserver(tmp_path, agent=None, period=3600, recent=300, refresh=1)
    observer.sources["source"] = state
    try:
        state.activity_recent = 300
        state.activity = resolved(ActivitySnapshot(live=None, completed=completed()))
        observer._collect(state, NOW)
        assert state.value.live == old and state.value.live_stale
        failure = Future()
        failure.set_exception(StatsUnavailable("offline"))
        state.activity = failure
        observer._collect(state, NOW + 1)
        assert state.value.live is not None
        assert state.value.live.observed_at == timestamp(NOW - 5)
        assert state.value.activity_error == "offline"
        observer.set_recent(900)
        state.activity = resolved(
            ActivitySnapshot(
                live=LiveSnapshot("new", timestamp(NOW), ()), completed=completed()
            )
        )
        observer._collect(state, NOW + 1)
        assert state.value.completed is None
        assert state.value.live is not None and state.value.live.epoch == "new"
        state.activity_recent = 900
        state.activity = resolved(
            ActivitySnapshot(
                live=LiveSnapshot("new", timestamp(NOW), ()),
                completed=completed(recent=900),
            )
        )
        observer._collect(state, NOW + 2)
        assert state.value.live is not None
        assert state.value.live.epoch == "new" and not state.value.live_stale
        assert state.value.activity_error is None
    finally:
        observer.close()


def test_durable_completion_retires_stale_live_tree_after_recent_expiry(tmp_path):
    source = binding(tmp_path)
    root = RunNode("run_done", None, "agic:work", "running", timestamp(NOW - 100))
    old = LiveSnapshot(
        "epoch", timestamp(NOW - 5), (LiveTreeSnapshot(root.id, "chat_top", (root,)),)
    )
    state = top._Source(
        source,
        SourceActivity(
            identity="source", label="alice", placement="resident", live=old
        ),
    )
    state.activity_recent = 300
    terminal = RunNode(
        root.id, None, "", "succeeded", root.created_at, finished_at=timestamp(NOW)
    )
    observer = top.TopObserver(tmp_path, agent=None, period=3600, recent=300, refresh=1)
    try:
        done = completed().model_copy(
            update={"counts": CompletionCounts(succeeded=1), "roots": (terminal,)}
        )
        state.activity = resolved(ActivitySnapshot(live=None, completed=done))
        observer._collect(state, NOW)
        assert state.value.live is not None and not state.value.live.trees
        assert (
            state.value.completed is not None
            and state.value.completed.counts.total == 1
        )
        state.activity = resolved(
            ActivitySnapshot(live=None, completed=completed(NOW + 301))
        )
        observer._collect(state, NOW + 301)
        assert state.value.live is not None and not state.value.live.trees
        assert state.value.completed is not None and not state.value.completed.roots
        assert state.value.live_stale
    finally:
        observer.close()


def test_inventory_removal_drops_unique_sources_immediately(tmp_path):
    source = binding(tmp_path)
    with closing(
        top.TopObserver(tmp_path, agent="alice", period=3600, recent=300, refresh=1)
    ) as observer:
        observer.discovery_job = resolved(
            RuntimeInventory(
                RuntimeInstanceSnapshot((source.instance,)), (source, source)
            )
        )
        observer._inventory(NOW)
        assert list(observer.sources) == ["source"]
        observer.discovery_job = resolved(
            RuntimeInventory(RuntimeInstanceSnapshot(()), ())
        )
        observer._inventory(NOW + 10)
        assert not observer.sources and not observer.inventory.instances


def test_obsolete_usage_response_does_not_delay_the_selected_period(tmp_path):
    source = binding(tmp_path)
    state = top._Source(
        source,
        SourceActivity(identity="source", label="alice", placement="resident"),
    )
    state.usage_period = 3600
    state.usage = resolved(usage())
    observer = top.TopObserver(
        tmp_path, agent=None, period=3600, recent=300, refresh=60
    )
    observer.sources["source"] = state
    try:
        observer.set_period(14400)
        observer._collect(state, NOW)
        assert state.value.usage is None
        assert state.next_usage <= NOW
    finally:
        observer.close()


def test_recent_change_keeps_cached_completed_rows_that_remain_eligible(
    tmp_path, monkeypatch
):
    source = binding(tmp_path)
    keep = RunNode(
        "run_keep",
        None,
        "",
        "succeeded",
        timestamp(NOW - 1000),
        finished_at=timestamp(NOW - 250),
    )
    expire = replace(keep, id="run_expire", finished_at=timestamp(NOW - 350))
    state = top._Source(
        source,
        SourceActivity(identity="source", label="alice", placement="resident"),
    )
    state.loaded = {row.id: row for row in (keep, expire)}
    observer = top.TopObserver(tmp_path, agent=None, period=3600, recent=900, refresh=1)
    observer.sources["source"] = state
    monkeypatch.setattr(top.time, "time", lambda: NOW)
    try:
        observer.set_recent(300)
        assert state.loaded == {keep.id: keep}
        assert state.value.completed is None
    finally:
        observer.close()


def test_recent_switch_keeps_fresh_live_data_and_schedules_the_new_window_now(tmp_path):
    source = binding(tmp_path)
    old = LiveSnapshot("old", timestamp(NOW - 10), ())
    fresh = LiveSnapshot("fresh", timestamp(NOW), ())
    state = top._Source(
        source,
        SourceActivity(
            identity="source", label="alice", placement="resident", live=old
        ),
    )
    state.activity_recent = 300
    state.activity = resolved(ActivitySnapshot(live=fresh, completed=completed()))
    observer = top.TopObserver(
        tmp_path, agent=None, period=3600, recent=300, refresh=60
    )
    observer.sources["source"] = state
    try:
        observer.set_recent(900)
        observer._collect(state, NOW)
        assert state.value.live == fresh and not state.value.live_stale
        assert state.value.completed is None
        assert state.next_activity <= NOW
    finally:
        observer.close()


def test_completed_paging_does_not_take_the_live_poll_slot(tmp_path, monkeypatch):
    source = binding(tmp_path)
    state = top._Source(
        source,
        SourceActivity(identity="source", label="alice", placement="resident"),
    )
    state.cursor = (timestamp(NOW), "run_100")
    state.cursor_at = NOW
    state.request_page = True
    state.next_usage = NOW + 100
    calls = []

    def live(_reader, source, recent, before=None, completed_at=None, **kwargs):
        calls.append(before)
        return ActivitySnapshot(
            live=LiveSnapshot("epoch", timestamp(NOW), ()), completed=completed()
        )

    monkeypatch.setattr(ActivityReader, "read_activity", live)
    monkeypatch.setattr(ActivityReader, "read_page", lambda *args: completed())
    monkeypatch.setattr(top.time, "time", lambda: NOW)
    observer = top.TopObserver(tmp_path, agent=None, period=3600, recent=300, refresh=1)
    observer.sources["source"] = state
    observer.next_discovery = NOW + 100
    try:
        observer.tick()
        assert state.activity is not None
        state.activity.result(timeout=1)
        assert None in calls
    finally:
        observer.close()


def test_offline_completion_outside_first_page_replaces_the_known_live_root(
    tmp_path, monkeypatch
):
    source = replace(binding(tmp_path), endpoint=None)
    store = RunStore(source.layout.run_store)
    selected = "run_zz"
    for name in [*(f"run_{index:03}" for index in range(150)), selected]:
        project_run_start(
            store,
            run_id=name,
            thread_id="chat_top",
            input=Message.user("fixture"),
            started_at=timestamp(NOW - 10),
        )
        store.finish_run(run_id=name, finished_at=timestamp(NOW), report=RunReport())
    root = RunNode(selected, None, "agic:work", "running", timestamp(NOW - 10))
    live = LiveSnapshot(
        "epoch", timestamp(NOW - 1), (LiveTreeSnapshot(selected, "chat_top", (root,)),)
    )
    state = top._Source(
        source,
        SourceActivity(
            identity="source", label="alice", placement="resident", live=live
        ),
    )
    state.next_usage = NOW + 100
    monkeypatch.setattr(top.time, "time", lambda: NOW)
    observer = top.TopObserver(tmp_path, agent=None, period=3600, recent=300, refresh=1)
    observer.sources["source"] = state
    observer.next_discovery = NOW + 100
    try:
        from toolang.cli.common.top_view import TopView

        observer.inventory = RuntimeInstanceSnapshot((source.instance,))
        view = TopView()
        view.project(observer.tick())
        selected_key = view.selected
        assert view.rows[0].thread == "chat_top"
        assert state.activity is not None
        state.activity.result(timeout=1)
        snapshot = observer.tick()
        result = snapshot.sources[0]
        assert result.live is not None and not result.live.trees
        assert result.completed is not None
        assert result.completed.counts.total == 151
        assert selected in {node.id for node in result.completed.roots}
        assert state.cursor == (timestamp(NOW), "run_099")
        rows = view.project(snapshot)
        assert view.selected == selected_key
        assert (
            next(row for row in rows if row.key == selected_key).status == "succeeded"
        )
        assert next(row for row in rows if row.key == selected_key).thread == "chat_top"
        observer.more()
        observer.tick()
        assert state.page is not None
        state.page.result(timeout=1)
        snapshot = observer.tick()
        assert snapshot.sources[0].completed is not None
        assert len(snapshot.sources[0].completed.roots) == 151
        assert len({row.id for row in snapshot.sources[0].completed.roots}) == 151
        assert all(
            row.thread_id == "chat_top" for row in snapshot.sources[0].completed.roots
        )
    finally:
        observer.close()
        store.close()


def test_slow_page_keeps_live_polling_and_rejects_obsolete_cursor_results(
    tmp_path, monkeypatch
):
    source = binding(tmp_path)
    release = Event()
    now = [NOW]
    polls = []
    state = top._Source(
        source,
        SourceActivity(
            identity="source",
            label="alice",
            placement="resident",
            live=LiveSnapshot("epoch", timestamp(NOW), ()),
        ),
    )
    state.cursor = (timestamp(NOW), "run_old099")
    state.cursor_at = NOW
    state.request_page = True
    state.next_usage = NOW + 100
    old = RunNode(
        "run_old199",
        None,
        "",
        "succeeded",
        timestamp(NOW - 10),
        finished_at=timestamp(NOW),
    )
    new = replace(old, id="run_new099")

    def live(_reader, source, recent, **kwargs):
        polls.append(now[0])
        return ActivitySnapshot(
            live=LiveSnapshot("epoch", timestamp(now[0]), ()),
            completed=completed(now[0]).model_copy(
                update={"counts": CompletionCounts(succeeded=300), "roots": (new,)}
            ),
        )

    def page(*args):
        assert release.wait(3)
        return completed().model_copy(update={"roots": (old,)})

    monkeypatch.setattr(ActivityReader, "read_activity", live)
    monkeypatch.setattr(ActivityReader, "read_page", page)
    monkeypatch.setattr(top.time, "time", lambda: now[0])
    observer = top.TopObserver(
        tmp_path, agent=None, period=3600, recent=300, refresh=0.1
    )
    observer.sources["source"] = state
    observer.next_discovery = NOW + 100
    try:
        observer.tick()
        assert state.activity is not None and state.page is not None
        state.activity.result(timeout=1)
        now[0] += 1
        observer.tick()
        assert not state.page.done()
        observer.tick()
        now[0] += 1
        observer.tick()
        assert state.activity is not None
        state.activity.result(timeout=1)
        snapshot = observer.tick()
        assert len(polls) == 2 and snapshot.sources[0].live is not None
        assert snapshot.sources[0].live.observed_at == timestamp(now[0])
        assert state.cursor == (new.finished_at, new.id)
        release.set()
        state.page.result(timeout=1)
        observer.tick()
        assert state.cursor == (new.finished_at, new.id)
        assert old.id in state.loaded
    finally:
        release.set()
        observer.close()


def test_page_failure_keeps_fresh_scope_counts_and_reports_page_coverage(tmp_path):
    source = binding(tmp_path)
    live = LiveSnapshot("epoch", timestamp(NOW), ())
    state = top._Source(
        source,
        SourceActivity(
            identity="source",
            label="alice",
            placement="resident",
            live=live,
            live_stale=False,
            completed=completed().model_copy(
                update={"counts": CompletionCounts(succeeded=200)}
            ),
        ),
    )
    failure = Future()
    failure.set_exception(StatsUnavailable("page locked"))
    state.page = failure
    state.page_recent = 300
    state.page_epoch = "epoch"
    observer = top.TopObserver(tmp_path, agent=None, period=3600, recent=300, refresh=1)
    try:
        observer._collect(state, NOW)
        assert state.value.live == live and not state.value.live_stale
        assert (
            state.value.activity_error is None
            and state.value.page_error == "page locked"
        )
        assert (
            state.value.completed is not None
            and state.value.completed.counts.total == 200
        )
    finally:
        observer.close()


def test_completion_during_recent_switch_preserves_selected_root_until_counts_load(
    tmp_path, monkeypatch
):
    from toolang.cli.common.top_view import TopView
    from toolang.up.schemas import TopSnapshot

    source = binding(tmp_path)
    root = RunNode("run_zz", None, "agic:work", "running", timestamp(NOW - 100))
    step = StepNode(
        "run_zz.0",
        root.id,
        "model",
        "model",
        "running",
        root.created_at,
        root.created_at,
    )
    live = LiveSnapshot(
        "epoch",
        timestamp(NOW - 1),
        (LiveTreeSnapshot(root.id, "chat_top", (root, step)),),
    )
    state = top._Source(
        source,
        SourceActivity(
            identity="source",
            label="alice",
            placement="resident",
            live=live,
            live_stale=False,
            completed=completed(),
        ),
    )
    view = TopView()

    def snapshot():
        return TopSnapshot(
            observed_at=timestamp(NOW),
            period=3600,
            recent=observer.recent,
            inventory=RuntimeInstanceSnapshot((source.instance,)),
            sources=(state.value,),
        )

    observer = top.TopObserver(
        tmp_path, agent=None, period=3600, recent=300, refresh=60
    )
    observer.sources["source"] = state
    monkeypatch.setattr(top.time, "time", lambda: NOW)
    terminal = replace(root, status="succeeded", finished_at=timestamp(NOW))
    try:
        view.project(snapshot())
        view.selected = view.rows[1].key
        observer.set_recent(900)
        state.activity_recent = 300
        state.activity = resolved(
            ActivitySnapshot(
                live=replace(live, trees=()), completed=completed(), retired=(terminal,)
            )
        )
        observer._collect(state, NOW)
        assert state.value.completed is None
        assert root.id in state.loaded
        assert view.project(snapshot()) == []
        assert view.selected == ("source", "epoch", root.id)
        first = tuple(replace(terminal, id=f"run_{index:03}") for index in range(100))
        state.activity_recent = 900
        state.activity = resolved(
            ActivitySnapshot(
                live=replace(live, trees=()),
                completed=completed(recent=900).model_copy(
                    update={"roots": first, "counts": CompletionCounts(succeeded=151)}
                ),
            )
        )
        observer._collect(state, NOW)
        rows = view.project(snapshot())
        assert view.selected == ("source", "epoch", root.id)
        assert (
            next(row for row in rows if row.key == view.selected).status == "succeeded"
        )
    finally:
        observer.close()


def test_page_from_a_previous_epoch_cannot_restore_old_rows_or_cursor(tmp_path):
    source = binding(tmp_path)
    old = LiveSnapshot("old", timestamp(NOW), ())
    row = RunNode(
        "run_old",
        None,
        "",
        "succeeded",
        timestamp(NOW - 10),
        finished_at=timestamp(NOW),
    )
    state = top._Source(
        source,
        SourceActivity(
            identity="source",
            label="alice",
            placement="resident",
            live=old,
            page_error="old failure",
        ),
    )
    state.loaded[row.id] = row
    state.cursor_at, state.cursor = NOW, (timestamp(NOW), row.id)
    state.activity_recent = state.page_recent = 300
    state.page_epoch = "old"
    state.page_cursor = (NOW, state.cursor)
    state.page = resolved(completed().model_copy(update={"roots": (row,)}))
    state.activity = resolved(
        ActivitySnapshot(live=replace(old, epoch="new"), completed=completed())
    )
    observer = top.TopObserver(tmp_path, agent=None, period=3600, recent=300, refresh=1)
    try:
        observer._collect(state, NOW)
        assert not state.loaded and state.cursor is None and state.cursor_at is None
        assert state.value.page_error is None
        assert state.value.completed is not None and not state.value.completed.roots
    finally:
        observer.close()


@pytest.mark.parametrize("mode", ["new", "empty", "corrupt"])
def test_unregistered_resident_history_enters_fresh_top(tmp_path, mode):
    layout = AgentLayout.resident(tmp_path, "alice")
    layout.home.mkdir(parents=True)
    if mode == "empty":
        RunStore(layout.run_store).close()
    elif mode == "corrupt":
        layout.run_store.parent.mkdir(parents=True)
        layout.run_store.write_text("not sqlite")
    with closing(
        top.TopObserver(tmp_path, agent=None, period=3600, recent=300, refresh=60)
    ) as observer:
        snapshot = observer.once()
    assert snapshot.summary.total == 1 and snapshot.summary.running == 0
    assert len(snapshot.sources) == 1
    if mode == "corrupt":
        assert snapshot.summary.usage is None
        assert snapshot.sources[0].activity_error
        assert snapshot.sources[0].usage_error
    else:
        assert snapshot.summary.usage is not None
        assert snapshot.summary.usage.model_calls == 0
    assert layout.run_store.exists() == (mode != "new")
    assert not layout.stats_store.exists()


@pytest.mark.parametrize("endpoint", [None, "http://localhost:7001"])
@pytest.mark.parametrize("unfinished", [False, True])
def test_embedded_completed_work_proves_zero_active_roots(
    tmp_path, monkeypatch, endpoint, unfinished
):
    from toolang.cli.common.top_view import TopView

    source = replace(binding(tmp_path, registered=True), endpoint=endpoint)
    with closing(RunStore(source.layout.run_store)) as store:
        project_run_start(
            store,
            run_id="run_embedded",
            thread_id="exec_top",
            input=Message.user("fixture"),
            started_at=timestamp(NOW - 1000),
        )
        if not unfinished:
            store.finish_run(run_id="run_embedded", finished_at=timestamp(NOW))

    def offline(*args, **kwargs):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "get", offline)
    monkeypatch.setattr(top.time, "time", lambda: NOW)
    monkeypatch.setattr(
        top.ScopeReader,
        "read",
        lambda *args, **kwargs: RuntimeInventory(
            RuntimeInstanceSnapshot((source.instance,)), (source,)
        ),
    )
    with closing(
        top.TopObserver(tmp_path, agent=None, period=3600, recent=300, refresh=1)
    ) as observer:
        snapshot = observer.once()
        expected = 0 if not unfinished and endpoint is None else None
        assert snapshot.summary.running == 1
        assert snapshot.summary.active == expected
        text = TopView().render(snapshot, width=160).plain
        assert ("0 active" if expected == 0 else "- active") in text
        assert ("live unavailable" in text) == (endpoint is not None)
        if expected == 0:
            source.layout.run_store.unlink()
            state = observer.sources[source.instance.identity]
            state.activity = observer.live_pool.submit(
                state.reader.read_activity,
                source,
                300,
                previous=state.value.live,
                cancelled=lambda: False,
            )
            with pytest.raises(StatsUnavailable):
                state.activity.result(timeout=1)
            assert observer.tick().summary.active is None


def test_obsolete_idle_proof_does_not_clear_current_live_rows(tmp_path):
    source = replace(binding(tmp_path), endpoint=None)
    root = RunNode("run_live", None, "agic:work", "running", timestamp(NOW))
    live = LiveSnapshot(
        "epoch", timestamp(NOW), (LiveTreeSnapshot(root.id, "chat_top", (root,)),)
    )
    state = top._Source(
        source,
        SourceActivity(
            identity="source", label="alice", placement="resident", live=live
        ),
        activity=resolved(
            ActivitySnapshot(live=None, completed=completed(), idle=True)
        ),
        activity_obsolete=True,
    )
    with closing(
        top.TopObserver(tmp_path, agent=None, period=3600, recent=300, refresh=1)
    ) as observer:
        observer._collect(state, NOW)
    assert state.value.live == live and state.value.live_stale


def test_once_reports_initial_discovery_failure_without_fabricating_zero(
    tmp_path, monkeypatch
):
    def denied(_, **kwargs):
        raise PermissionError("permission denied")

    monkeypatch.setattr(top.ScopeReader, "read", denied)
    with closing(
        top.TopObserver(tmp_path, agent=None, period=3600, recent=300, refresh=60)
    ) as observer:
        snapshot = observer.once()
        assert snapshot.summary.total is None and snapshot.summary.running is None
        assert snapshot.inventory.diagnostics == ("permission denied",)


def test_live_error_recovers_and_obsolete_failure_cannot_restore_it(tmp_path):
    source = binding(tmp_path)
    state = top._Source(
        source,
        SourceActivity(identity="source", label="alice", placement="resident"),
    )
    state.activity_recent = 300
    with closing(
        top.TopObserver(tmp_path, agent=None, period=3600, recent=300, refresh=60)
    ) as observer:
        state.activity = resolved(
            ActivitySnapshot(
                live=None, completed=completed(), live_error="connection refused"
            )
        )
        observer._collect(state, NOW)
        assert state.value.live_error == "connection refused" and state.value.live_stale
        state.activity = resolved(
            ActivitySnapshot(
                live=LiveSnapshot("epoch", timestamp(NOW), ()), completed=completed()
            )
        )
        observer._collect(state, NOW)
        assert state.value.live_error is None and not state.value.live_stale
        state.activity = Future()
        state.activity.set_running_or_notify_cancel()
        state.rebind(replace(source, endpoint=None))
        state.activity.set_result(
            ActivitySnapshot(
                live=None,
                completed=completed(),
                live_error="obsolete failure",
                page_error="obsolete lookup failure",
            )
        )
        observer._collect(state, NOW)
        assert state.value.live_error is None
        assert state.value.page_error is None


@pytest.mark.parametrize("recovered", [False, True])
def test_failed_docker_batch_keeps_membership_and_readable_history(
    tmp_path, monkeypatch, recovered
):
    from toolang.base.types.sandbox import SandboxRef
    from toolang.up.instance import SandboxState
    from toolang.up.process import AgentProcess, AgentStatus

    layout = AgentLayout.resident(tmp_path, "alice")
    layout.home.mkdir(parents=True)
    with (
        closing(RunStore(layout.run_store)),
        ForegroundWorkload(
            layout, toolang_root=tmp_path, drivers=frozenset()
        ) as registration,
    ):
        SandboxState(
            "docker:image",
            SandboxRef("container", "http://localhost:7001", runtime_kind="container"),
            registration.token,
        ).save(layout.runtime_status)

        def failed_batch(cls, owners):
            assert len(owners) == 1
            raise OSError("Docker inspection failed")

        def inspect(self, **kwargs):
            if not recovered:
                raise OSError("container inspection failed")
            return AgentStatus(
                self.layout.name, "running", None, None, None, "docker:image"
            )

        monkeypatch.setattr(
            AgentProcess, "cache_docker_states", classmethod(failed_batch)
        )
        monkeypatch.setattr(AgentProcess, "status", inspect)
        with closing(
            top.TopObserver(tmp_path, agent=None, period=3600, recent=300, refresh=60)
        ) as observer:
            snapshot = observer.once()
    assert snapshot.summary.total == snapshot.summary.resident == 1
    assert snapshot.summary.running == (1 if recovered else None)
    assert snapshot.summary.completed == CompletionCounts()
    assert snapshot.summary.usage == Metrics()
    assert not snapshot.inventory.diagnostics
    assert bool(snapshot.inventory.instances[0].diagnostic) == (not recovered)


@pytest.mark.parametrize("agent", [None, "alice"])
@pytest.mark.parametrize(
    "fault",
    [
        "peer",
        "registry",
        "enumeration",
        "own_metadata",
        "own_reference",
        "same_name_roaming",
    ],
)
def test_agent_scope_isolates_discovery_but_preserves_its_own_failures(
    tmp_path, monkeypatch, agent, fault
):
    from contextlib import ExitStack

    with ExitStack() as stack:
        for name in ("alice", "bob"):
            layout = AgentLayout.resident(tmp_path, name)
            stack.enter_context(closing(RunStore(layout.run_store)))
            registration = stack.enter_context(
                ForegroundWorkload(layout, toolang_root=tmp_path, drivers=frozenset())
            )
            if name == "alice":
                own_registration = registration
        alice, bob = (
            AgentLayout.resident(tmp_path, "alice"),
            AgentLayout.resident(tmp_path, "bob"),
        )
        if fault in {"peer", "own_metadata"}:
            target = bob.runtime_status if fault == "peer" else alice.runtime_status
            original = target.read_bytes()
            target.write_text("not json")
            stack.callback(target.write_bytes, original)
        elif fault == "registry":
            (tmp_path / ".runtime" / "active" / "broken.json").write_text("invalid")
        elif fault == "own_reference":
            reference = json.loads(own_registration.path.read_text())
            reference["status"] = str(bob.runtime_status)
            own_registration.path.write_text(json.dumps(reference))
        elif fault == "same_name_roaming":
            roaming = allocate_roaming(tmp_path / "alice.too", toolang_root=tmp_path)
            registration = stack.enter_context(
                ForegroundWorkload(roaming, toolang_root=tmp_path, drivers=frozenset())
            )
            registration.path.write_text("invalid")
        elif fault == "enumeration":
            original = type(tmp_path).iterdir

            def denied(path):
                if path == tmp_path / "agents":
                    raise PermissionError("no resident scan allowed")
                return original(path)

            monkeypatch.setattr(type(tmp_path), "iterdir", denied)
        with closing(
            top.TopObserver(tmp_path, agent=agent, period=3600, recent=300, refresh=60)
        ) as observer:
            snapshot = observer.once()
        partial = fault in {"own_metadata", "own_reference"} or (
            agent is None
            and fault in {"peer", "registry", "same_name_roaming", "enumeration"}
        )
        assert snapshot.inventory.partial == partial
        if not partial:
            assert snapshot.summary.running == (1 if agent else 2)
            assert {source.label for source in snapshot.sources} == (
                {"alice"} if agent else {"alice", "bob"}
            )


@pytest.mark.parametrize("agent", [None, "alice"])
@pytest.mark.parametrize(
    "state", ["stopped", "missing_document", "missing_entry", "linked_stopped"]
)
def test_scopes_agree_on_registered_residents_when_status_changes(
    tmp_path, agent, state
):
    from toolang.up.instance import update_instance

    layout = AgentLayout.resident(tmp_path, "alice")
    with (
        closing(RunStore(layout.run_store)),
        ForegroundWorkload(layout, toolang_root=tmp_path, drivers=frozenset()),
    ):
        original = layout.runtime_status.read_bytes()
        try:
            if state in {"stopped", "linked_stopped"}:
                update_instance(
                    layout.runtime_status,
                    lambda entry: entry["workload"].update(status="stopped", pid=None),
                )
                if state == "linked_stopped":
                    target = layout.runtime_status.with_name("linked-status.json")
                    layout.runtime_status.rename(target)
                    layout.runtime_status.symlink_to(target)
            elif state == "missing_document":
                layout.runtime_status.unlink()
            else:
                document = json.loads(original)
                document["instances"].pop("default")
                layout.runtime_status.write_text(json.dumps(document))
            with closing(
                top.TopObserver(
                    tmp_path, agent=agent, period=3600, recent=300, refresh=60
                )
            ) as observer:
                snapshot = observer.once()
            if state in {"stopped", "linked_stopped"}:
                assert snapshot.summary.total == 1 and snapshot.summary.running == 0
                assert len(snapshot.sources) == 1
            else:
                assert snapshot.inventory.partial
                assert snapshot.summary.total == 1
                assert {source.label for source in snapshot.sources} == {"alice"}
        finally:
            layout.runtime_status.write_bytes(original)


@pytest.mark.parametrize("agent", [None, "alice"])
def test_resident_scope_excludes_roaming_instances_with_the_same_name(tmp_path, agent):
    root = tmp_path / ".toolang"
    resident = AgentLayout.resident(root, "alice")
    roaming = allocate_roaming(tmp_path / "alice.too", toolang_root=root)
    assert resident.home != roaming.home
    with (
        closing(RunStore(resident.run_store)),
        closing(RunStore(roaming.run_store)),
        ForegroundWorkload(resident, toolang_root=root, drivers=frozenset()),
        ForegroundWorkload(roaming, toolang_root=root, drivers=frozenset()),
        closing(
            top.TopObserver(root, agent=agent, period=3600, recent=300, refresh=60)
        ) as observer,
    ):
        snapshot = observer.once()
    assert (
        snapshot.summary.total
        == snapshot.summary.running
        == (2 if agent is None else 1)
    )
    assert {source.placement for source in snapshot.sources} == (
        {"resident", "roaming"} if agent is None else {"resident"}
    )


@pytest.mark.parametrize("agent", [None, "alice"])
@pytest.mark.parametrize("error", [StatsUnavailable, PermissionError])
def test_failed_retired_lookup_preserves_http_snapshot_and_retries_as_paging(
    tmp_path, monkeypatch, agent, error
):
    from toolang.cli.common.top_view import TopView

    source = binding(tmp_path, registered=True)
    with closing(RunStore(source.layout.run_store)) as store:
        for identity in [*(f"run_{index:03}" for index in range(100)), "run_zz"]:
            project_run_start(
                store,
                run_id=identity,
                thread_id="chat_fixture",
                input=Message.user("fixture"),
                started_at=timestamp(NOW - 10),
            )
            store.finish_run(
                run_id=identity, finished_at=timestamp(NOW), report=RunReport()
            )
        with activity.recent_source(source.layout.run_store) as reader:
            reader.pin()
            first = reader.completed(now=NOW, recent=300)
        root = RunNode("run_zz", None, "agic:work", "running", timestamp(NOW - 10))
        previous = LiveSnapshot(
            "epoch", timestamp(NOW - 1), (LiveTreeSnapshot(root.id, "chat", (root,)),)
        )
        fresh = LiveSnapshot("epoch", timestamp(NOW), ())
        response = httpx.Response(
            200,
            json=ActivitySnapshot(live=fresh, completed=first).model_dump(mode="json"),
            request=httpx.Request("GET", source.endpoint),
        )
        monkeypatch.setattr(httpx, "get", lambda *args, **kwargs: response)
        monkeypatch.setattr(top.time, "time", lambda: NOW)
        state = top._Source(
            source,
            SourceActivity(
                identity="source", label="alice", placement="resident", live=previous
            ),
        )
        state.next_usage = NOW + 3600
        state.activity_recent = 300
        with closing(
            top.TopObserver(tmp_path, agent=agent, period=3600, recent=300, refresh=60)
        ) as observer:
            observer.sources["source"] = state
            observer.inventory = RuntimeInstanceSnapshot((source.instance,))
            observer.discovered = True
            observer.next_discovery = NOW + 3600
            with monkeypatch.context() as denied:

                def unavailable(*args, **kwargs):
                    raise error("retired lookup unavailable")

                denied.setattr(activity, "recent_source", unavailable)
                state.activity = resolved(
                    state.reader.read_activity(
                        source, 300, previous=previous, cancelled=lambda: False
                    )
                )
            snapshot = observer.tick()
            assert snapshot.summary.active == 0
            assert (
                snapshot.summary.completed is not None
                and snapshot.summary.completed.total == 101
            )
            assert (
                snapshot.sources[0].live == fresh and not snapshot.sources[0].live_stale
            )
            assert snapshot.sources[0].activity_error is None
            assert snapshot.sources[0].page_error == "retired lookup unavailable"
            assert not TopView().notices(snapshot)
            observer.more()
            observer.tick()
            assert state.page is not None
            state.page.result(timeout=2)
            recovered = observer.tick()
            assert recovered.sources[0].completed is not None
            assert {node.id for node in recovered.sources[0].completed.roots} == {
                node.id for node in first.roots
            } | {root.id}
            assert recovered.sources[0].page_error is None


@pytest.mark.parametrize("agent", [None, "alice"])
@pytest.mark.parametrize("count", [1, 8])
def test_slow_completion_pages_cannot_starve_live_polls(
    tmp_path, monkeypatch, agent, count
):
    release, started = Event(), Event()
    now = [NOW]

    def page(*args):
        started.set()
        assert release.wait(5)
        return completed()

    def activity(*args, **kwargs):
        return ActivitySnapshot(
            live=LiveSnapshot("epoch", timestamp(now[0]), ()), completed=completed()
        )

    monkeypatch.setattr(ActivityReader, "read_page", page)
    monkeypatch.setattr(ActivityReader, "read_activity", activity)
    monkeypatch.setattr(top.time, "time", lambda: now[0])
    with closing(
        top.TopObserver(tmp_path, agent=agent, period=3600, recent=300, refresh=60)
    ) as observer:
        observer.next_discovery = NOW + 3600
        for index in range(count):
            source = binding(tmp_path)
            source = replace(
                source,
                instance=replace(
                    source.instance,
                    identity=f"source{index}",
                    runtime_instance=f"slot{index}",
                ),
                layout=replace(source.layout, instance=f"slot{index}"),
            )
            state = top._Source(
                source,
                SourceActivity(
                    identity=source.instance.identity,
                    label=source.instance.label,
                    placement="resident",
                    live=LiveSnapshot("epoch", timestamp(NOW), ()),
                ),
            )
            state.next_activity = state.next_usage = NOW + 3600
            state.cursor, state.cursor_at = (timestamp(NOW), "run_099"), NOW
            state.request_page = True
            observer.sources[source.instance.identity] = state
        try:
            observer.tick()
            assert started.wait(2)
            now[0] += 1
            for state in observer.sources.values():
                state.next_activity = 0
            observer.tick()
            for state in observer.sources.values():
                assert state.activity is not None
                assert state.activity.result(timeout=2).live is not None
            snapshot = observer.tick()
            assert all(
                source.live is not None and source.live.observed_at == timestamp(now[0])
                for source in snapshot.sources
            )
            assert any(
                state.page is not None and not state.page.done()
                for state in observer.sources.values()
            )
        finally:
            release.set()
            for state in observer.sources.values():
                for job in (state.page, state.activity):
                    if job is not None:
                        job.result(timeout=2)


@pytest.mark.parametrize("placement", ["visiting", "roaming"])
@pytest.mark.parametrize("failed", [False, True])
def test_removed_source_jobs_cannot_restore_facts_or_errors_after_rediscovery(
    tmp_path, placement, failed
):
    source = binding(tmp_path)
    source = replace(source, instance=replace(source.instance, placement=placement))
    with closing(
        top.TopObserver(tmp_path, agent=None, period=3600, recent=300, refresh=60)
    ) as observer:
        roster = RuntimeInventory(
            RuntimeInstanceSnapshot((source.instance,)), (source,)
        )
        observer.discovery_job = resolved(roster)
        observer._inventory(NOW)
        old = observer.sources[source.instance.identity]
        jobs = [Future() for _ in range(3)]
        for job in jobs:
            job.set_running_or_notify_cancel()
        old.activity, old.usage, old.page = jobs
        old.value.usage = usage()
        observer.discovery_job = resolved(
            RuntimeInventory(RuntimeInstanceSnapshot(()), ())
        )
        observer._inventory(NOW + 1)
        assert not observer.sources and old.stopped.is_set()
        for job, result in zip(
            jobs,
            (ActivitySnapshot(live=None, completed=completed()), usage(), completed()),
            strict=True,
        ):
            if failed:
                job.set_exception(StatsUnavailable("obsolete source"))
            else:
                job.set_result(result)
        snapshot = observer.tick()
        assert snapshot.sources == ()
        assert (
            snapshot.summary.usage is not None
            and snapshot.summary.usage.model_calls == 0
        )
        assert (
            snapshot.summary.completed is not None
            and snapshot.summary.completed.total == 0
        )
        observer.discovery_job = resolved(roster)
        observer._inventory(NOW + 2)
        current = observer.sources[source.instance.identity]
        assert current is not old and not current.stopped.is_set()
        assert current.value.usage is None and current.value.activity_error is None


def test_stopped_resident_homes_are_included_without_creating_history(tmp_path):
    layouts = [AgentLayout.resident(tmp_path, name) for name in ("alice", "bob")]
    for layout in layouts:
        layout.home.mkdir(parents=True)
    with closing(
        top.TopObserver(tmp_path, agent=None, period=3600, recent=300, refresh=1)
    ) as observer:
        snapshot = observer.once()
    assert snapshot.summary.resident == 2
    assert snapshot.summary.running == 0
    assert snapshot.summary.active == 0
    assert snapshot.summary.completed is not None
    assert snapshot.summary.completed.total == 0
    assert snapshot.summary.usage is not None
    assert snapshot.summary.usage.model_calls == 0
    assert all(not layout.run_store.exists() for layout in layouts)


def test_stopped_resident_keeps_durable_usage_and_runs_when_top_reopens(
    tmp_path, monkeypatch
):
    from tests.support.execution_fixtures import project_run_start
    from tests.support.runtime_observation import NOW
    from toolang.base.types.message import Message
    from toolang.execution.store import RunStore
    from toolang.execution.types import (
        ModelStepSpec,
        ModelStepReport,
        StepRef,
        TokenUsage,
    )
    from toolang.base.types.run import ModelCall
    from toolang.stats.types import timestamp
    from toolang.up import top
    from toolang.up.workload import ForegroundWorkload

    layout = AgentLayout.resident(tmp_path, "alice")
    with closing(RunStore(layout.run_store)) as store:
        project_run_start(
            store,
            run_id="run_done",
            thread_id="chat_top",
            input=Message.user("fixture"),
            started_at=timestamp(NOW - 10),
        )
        ref = StepRef.from_local("run_done", (0,))
        store.begin_step(
            ref=ref,
            kind="model",
            input=(),
            spec=ModelStepSpec(
                model="test/model",
                call=ModelCall(instructions="", messages=[]),
                setup="fixture",
            ),
            started_at=timestamp(NOW - 9),
        )
        store.finish_step(
            ref=ref,
            kind="model",
            status="succeeded",
            output=None,
            error=None,
            report=ModelStepReport(invoked=True, usage=TokenUsage(100, 10, 7)),
            finished_at=timestamp(NOW - 2),
        )
        store.finish_run(run_id="run_done", finished_at=timestamp(NOW - 1))
    monkeypatch.setattr(top.time, "time", lambda: NOW)
    with closing(
        top.TopObserver(tmp_path, agent=None, period=3600, recent=300, refresh=60)
    ) as observer:
        with ForegroundWorkload(layout, toolang_root=tmp_path, drivers=frozenset()):
            first = observer.once()
            state = observer.sources[layout.address]
        observer.discovery_job = Future()
        observer.discovery_job.set_result(observer.scope_reader.read())
        stopped = observer.tick()
        assert observer.sources[layout.address] is state
        assert stopped.summary.running == 0 and stopped.summary.total == 1
        assert stopped.summary.usage == first.summary.usage
        with closing(
            top.TopObserver(tmp_path, agent=None, period=3600, recent=300, refresh=60)
        ) as fresh:
            reopened = fresh.once()
        assert reopened.summary.usage == stopped.summary.usage
        assert reopened.summary.completed == stopped.summary.completed
        assert reopened.summary.usage is not None
        assert reopened.summary.usage.model_calls == 1
        assert reopened.summary.usage.input_tokens == 100
        assert reopened.sources[0].completed is not None
        assert reopened.sources[0].completed.roots[0].id == "run_done"
        assert state.reader is observer.sources[layout.address].reader
