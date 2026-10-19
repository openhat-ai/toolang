"""Observation generations, discovery retention and slow scans stay independent."""

from concurrent.futures import Future
from contextlib import closing
from threading import Event
from dataclasses import replace

import httpx
import pytest

from tests.support.execution_fixtures import project_run_start
from toolang.base.types.message import Message

from toolang.common.layout import AgentLayout
from toolang.execution.schemas import LiveSnapshot, LiveTreeSnapshot, RunNode, StepNode
from toolang.execution.store import RunStore
from toolang.execution.types import RunReport
from toolang.stats.errors import StatsUnavailable
from toolang.stats.schemas import CompletedRoots, CompletionCounts, RecentUsage
from toolang.stats.types import Metrics, timestamp
from toolang.up import top
from toolang.up.observation import RuntimeInventory, RuntimeRegistration, RuntimeSource
from toolang.up.schemas import ActivitySnapshot, SourceActivity
from toolang.up.types import RuntimeInstance, RuntimeInstanceSnapshot

NOW = 1792326877


def completed(at=NOW, recent=300):
    return CompletedRoots(
        observed_at=timestamp(at),
        start=timestamp(at - recent),
        end=timestamp(at),
        counts=CompletionCounts(),
        roots=(),
    )


def usage(period=3600):
    return RecentUsage(
        observed_at=timestamp(NOW),
        start=timestamp(NOW - period),
        end=timestamp(NOW),
        metrics=Metrics(model_calls=76),
    )


def binding(tmp_path):
    return RuntimeSource(
        RuntimeInstance("alice", "resident", None, "source", "running", ()),
        AgentLayout.resident(tmp_path, "alice"),
        "http://localhost:7001",
    )


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

    def read(source, recent, **kwargs):
        calls.append(source.endpoint)
        return ActivitySnapshot(live=None, completed=completed(now[0], recent))

    monkeypatch.setattr(top, "read_activity", read)
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
        observer.discovery_job = resolved(
            RuntimeInventory(RuntimeInstanceSnapshot(()), ())
        )
        now[0] += 1
        observer._inventory(now[0])
        assert state.binding.endpoint is None
        assert not observer.inventory.instances
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


def test_rediscovery_refreshes_binding_without_accepting_a_predeparture_live_result(
    tmp_path,
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
        observer.discovery_job = resolved(
            RuntimeInventory(RuntimeInstanceSnapshot(()), ())
        )
        observer._inventory(NOW + 1)
        observer.discovery_job = resolved(inventory)
        observer._inventory(NOW + 2)
        assert observer.sources == {source.instance.identity: state}
        assert state.binding.endpoint == source.endpoint and state.seen == NOW + 2
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


def test_discovery_failure_preserves_current_bindings_and_departed_sources_expire(
    tmp_path,
):
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
        observer.discovery_job = resolved(
            RuntimeInventory(RuntimeInstanceSnapshot(()), ())
        )
        observer._inventory(NOW + 2)
        queued = Future()
        state.activity = queued
        observer.next_discovery = NOW + 30000
        observer._inventory(NOW + 28800)
        assert observer.sources[source.instance.identity] is state
        observer._inventory(NOW + 28801)
        assert not observer.sources and queued.cancelled()


@pytest.mark.parametrize("placement", ["visiting", "roaming"])
def test_session_retains_departed_history_but_fresh_top_discovers_only_current_sources(
    tmp_path, monkeypatch, placement
):
    now = [NOW]
    monkeypatch.setattr(top.time, "time", lambda: now[0])
    resident = AgentLayout.resident(tmp_path, "alice")
    resident.home.mkdir(parents=True)
    layout = (
        AgentLayout.visiting(
            "https://example.com/worker.too", "worker", toolang_root=tmp_path
        )
        if placement == "visiting"
        else replace(AgentLayout.roaming(tmp_path / "worker.too"), instance="one")
    )
    with (
        closing(RunStore(resident.run_store)),
        closing(
            top.TopObserver(tmp_path, agent=None, period=3600, recent=300, refresh=60)
        ) as observer,
    ):
        with RuntimeRegistration(layout, toolang_root=tmp_path, drivers=frozenset()):
            with closing(RunStore(layout.run_store)) as store:
                project_run_start(
                    store,
                    run_id="run_done",
                    thread_id="chat_top",
                    origin="chat",
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
        observer.discovery_job = resolved(top.runtime_inventory(tmp_path))
        now[0] += 1
        snapshot = observer.tick()
        assert len(snapshot.inventory.instances) == 1
        assert len(snapshot.sources) == 2
        assert (
            snapshot.summary.completed is not None
            and snapshot.summary.completed.total == 1
        )
        from toolang.cli.common.top_view import TopView

        assert "run_done" in TopView().render(snapshot, width=140).plain
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
    scans = []
    live_calls = []
    monkeypatch.setattr(top.time, "time", lambda: now[0])
    monkeypatch.setattr(
        top,
        "runtime_inventory",
        lambda _: RuntimeInventory(
            RuntimeInstanceSnapshot((source.instance,)), (source,)
        ),
    )

    def slow_usage(source, period, cancelled=None):
        scans.append(period)
        assert release.wait(3)
        return usage(period)

    def live(source, recent, before=None, completed_at=None, **kwargs):
        live_calls.append(recent)
        return ActivitySnapshot(
            live=LiveSnapshot("epoch", timestamp(now[0]), ()),
            completed=completed(now[0], recent),
        )

    monkeypatch.setattr(top, "read_usage", slow_usage)
    monkeypatch.setattr(top, "read_activity", live)
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
        state.usage.result(timeout=1)
        total = observer.tick().usage_total()
        assert total is not None and total.model_calls == 76
        assert scans == [3600, 14400]
    finally:
        release.set()
        observer.close()


def test_disconnect_freezes_known_live_epoch_replacement_and_recent_generation(
    tmp_path,
):
    source = binding(tmp_path)
    old = LiveSnapshot("old", timestamp(NOW - 5), ())
    state = top._Source(
        source,
        NOW,
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
        NOW,
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


def test_inventory_removal_keeps_known_sources_for_windows_and_single_scope_filters(
    tmp_path,
):
    source = binding(tmp_path)
    observer = top.TopObserver(
        tmp_path, agent="alice", period=3600, recent=300, refresh=1
    )
    visitor = RuntimeSource(
        RuntimeInstance("alice", "visiting", None, "visitor", "running", ()),
        source.layout,
        None,
    )
    try:
        observer.discovery_job = resolved(
            RuntimeInventory(
                RuntimeInstanceSnapshot((source.instance, visitor.instance)),
                (source, source, visitor),
            )
        )
        observer._inventory(NOW)
        assert list(observer.sources) == ["source"]
        assert observer.inventory.instances == (source.instance,)
        observer.discovery_job = resolved(
            RuntimeInventory(RuntimeInstanceSnapshot(()), ())
        )
        observer._inventory(NOW + 10)
        assert list(observer.sources) == ["source"] and not observer.inventory.instances
        observer.discovery_job = resolved(
            RuntimeInventory(RuntimeInstanceSnapshot(()), ())
        )
        observer._inventory(NOW + 28801)
        assert not observer.sources
    finally:
        observer.close()


def test_obsolete_usage_response_does_not_delay_the_selected_period(tmp_path):
    source = binding(tmp_path)
    state = top._Source(
        source,
        NOW,
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
        NOW,
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
        NOW,
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
        NOW,
        SourceActivity(identity="source", label="alice", placement="resident"),
    )
    state.cursor = (timestamp(NOW), "run_100")
    state.cursor_at = NOW
    state.request_page = True
    state.next_usage = NOW + 100
    calls = []

    def live(source, recent, before=None, completed_at=None, **kwargs):
        calls.append(before)
        return ActivitySnapshot(
            live=LiveSnapshot("epoch", timestamp(NOW), ()), completed=completed()
        )

    monkeypatch.setattr(top, "read_activity", live)
    monkeypatch.setattr(top, "read_page", lambda *args: completed(), raising=False)
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
            origin="chat",
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
        NOW,
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
        observer.more()
        observer.tick()
        assert state.page is not None
        state.page.result(timeout=1)
        snapshot = observer.tick()
        assert snapshot.sources[0].completed is not None
        assert len(snapshot.sources[0].completed.roots) == 151
        assert len({row.id for row in snapshot.sources[0].completed.roots}) == 151
    finally:
        observer.close()
        store.close()


@pytest.mark.parametrize("epoch", ["epoch", "restarted"])
def test_online_completion_lookup_keeps_the_first_page_cursor_and_checks_epoch(
    tmp_path, monkeypatch, epoch
):
    source = binding(tmp_path)
    store = RunStore(source.layout.run_store)
    selected = "run_zz"
    for name in [*(f"run_{index:03}" for index in range(101)), selected]:
        project_run_start(
            store,
            run_id=name,
            thread_id="chat_top",
            origin="chat",
            input=Message.user("fixture"),
            started_at=timestamp(NOW - 10),
        )
        store.finish_run(run_id=name, finished_at=timestamp(NOW), report=RunReport())
    root = RunNode(selected, None, "agic:work", "running", timestamp(NOW - 10))
    previous = LiveSnapshot(
        "epoch", timestamp(NOW - 1), (LiveTreeSnapshot(selected, "chat_top", (root,)),)
    )
    try:
        with top.recent_source(source.layout.run_store) as reader:
            reader.pin()
            first = reader.completed(now=NOW, recent=300)
        payload = ActivitySnapshot(
            live=LiveSnapshot(epoch, timestamp(NOW), ()), completed=first
        )
        assert source.endpoint is not None
        response = httpx.Response(
            200,
            json=payload.model_dump(mode="json"),
            request=httpx.Request("GET", source.endpoint),
        )
        monkeypatch.setattr(top.httpx, "get", lambda *args, **kwargs: response)
        result = top.read_activity(source, 300, previous=previous)
        assert (
            len(result.completed.roots) == 100
            and result.completed.roots[-1].id == "run_099"
        )
        assert [row.id for row in result.retired] == (
            [selected] if epoch == previous.epoch else []
        )
    finally:
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
        NOW,
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

    def live(source, recent, **kwargs):
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

    monkeypatch.setattr(top, "read_activity", live)
    monkeypatch.setattr(top, "read_page", page)
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
        NOW,
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
        NOW,
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
        NOW,
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
def test_inactive_agent_history_is_empty_only_when_uninitialized(tmp_path, mode):
    layout = AgentLayout.resident(tmp_path, "alice")
    layout.home.mkdir(parents=True)
    if mode == "empty":
        with closing(RunStore(layout.run_store)):
            pass
    elif mode == "corrupt":
        layout.run_store.parent.mkdir(parents=True)
        layout.run_store.write_text("not sqlite")
    with closing(
        top.TopObserver(tmp_path, agent=None, period=3600, recent=300, refresh=60)
    ) as observer:
        snapshot = observer.once()
        summary = snapshot.summary
        assert summary.running == 0 and summary.total == 1 and summary.active == 0
        if mode == "corrupt":
            assert summary.completed is None and summary.usage is None
            assert (
                snapshot.sources[0].activity_error and snapshot.sources[0].usage_error
            )
        else:
            assert summary.completed is not None and summary.completed.total == 0
            assert summary.usage == Metrics()
            assert (
                not snapshot.sources[0].activity_error
                and not snapshot.sources[0].usage_error
            )
        assert layout.run_store.exists() == (mode != "new")
        assert not layout.stats_store.exists()


def test_lost_history_never_becomes_empty_after_a_successful_read(tmp_path):
    source = replace(
        binding(tmp_path),
        instance=replace(binding(tmp_path).instance, state="not running"),
        endpoint=None,
    )
    with closing(RunStore(source.layout.run_store)):
        pass
    state = top._Source(
        source,
        NOW,
        SourceActivity(identity="source", label="alice", placement="resident"),
    )
    assert state.read_usage(source, 3600, lambda: False).metrics == Metrics()
    source.layout.run_store.unlink()
    with pytest.raises(StatsUnavailable):
        state.read_activity(source, 300, previous=None, cancelled=lambda: False)
    with pytest.raises(StatsUnavailable):
        state.read_usage(source, 3600, lambda: False)
    assert not source.layout.run_store.exists()


def test_published_endpoint_failure_is_distinct_from_an_embedded_runtime(
    tmp_path, monkeypatch
):
    source = binding(tmp_path)
    with closing(RunStore(source.layout.run_store)):
        pass
    calls = []

    def offline(*args, **kwargs):
        calls.append(args)
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(top.httpx, "get", offline)
    result = top.read_activity(replace(source, endpoint=None), 300)
    assert result.live is None and result.live_error is None and not calls
    result = top.read_activity(source, 300)
    assert result.live is None and result.live_error == "connection refused"
    assert result.completed.counts.total == 0 and len(calls) == 1
    response = httpx.Response(
        200,
        json=ActivitySnapshot(
            live=LiveSnapshot("epoch", timestamp(NOW), ()), completed=completed()
        ).model_dump(mode="json"),
        request=httpx.Request("GET", source.endpoint),
    )
    monkeypatch.setattr(top.httpx, "get", lambda *args, **kwargs: response)
    result = top.read_activity(source, 300)
    assert result.live is not None and result.live_error is None


def test_once_reports_initial_discovery_failure_without_fabricating_zero(
    tmp_path, monkeypatch
):
    def denied(_):
        raise PermissionError("permission denied")

    monkeypatch.setattr(top, "runtime_inventory", denied)
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
        NOW,
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
                live=None, completed=completed(), live_error="obsolete failure"
            )
        )
        observer._collect(state, NOW)
        assert state.value.live_error is None


def test_unreadable_store_metadata_cannot_be_treated_as_new_history(
    tmp_path, monkeypatch
):
    source = replace(
        binding(tmp_path),
        instance=replace(binding(tmp_path).instance, state="not running"),
        endpoint=None,
    )
    state = top._Source(
        source,
        NOW,
        SourceActivity(identity="source", label="alice", placement="resident"),
    )
    original = type(source.layout.run_store).stat

    def denied(path, *args, **kwargs):
        if path == source.layout.run_store:
            raise PermissionError("permission denied")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(type(source.layout.run_store), "stat", denied)
    with pytest.raises(PermissionError):
        state.read_usage(source, 3600, lambda: False)


def test_healthy_endpoint_does_not_require_readable_local_store_metadata(
    tmp_path, monkeypatch
):
    source = binding(tmp_path)
    state = top._Source(
        source,
        NOW,
        SourceActivity(identity="source", label="alice", placement="resident"),
    )
    original = type(source.layout.run_store).stat

    def denied(path, *args, **kwargs):
        if path == source.layout.run_store:
            raise PermissionError("permission denied")
        return original(path, *args, **kwargs)

    expected = ActivitySnapshot(
        live=LiveSnapshot("epoch", timestamp(NOW), ()), completed=completed()
    )
    response = httpx.Response(
        200,
        json=expected.model_dump(mode="json"),
        request=httpx.Request("GET", source.endpoint),
    )
    monkeypatch.setattr(type(source.layout.run_store), "stat", denied)
    monkeypatch.setattr(top.httpx, "get", lambda *args, **kwargs: response)
    assert (
        state.read_activity(source, 300, previous=None, cancelled=lambda: False)
        == expected
    )


def test_http_history_cannot_become_uninitialized_after_agent_stops(
    tmp_path, monkeypatch
):
    source = binding(tmp_path)
    state = top._Source(
        source,
        NOW,
        SourceActivity(identity="source", label="alice", placement="resident"),
    )
    expected = ActivitySnapshot(
        live=LiveSnapshot("epoch", timestamp(NOW), ()),
        completed=completed().model_copy(
            update={"counts": CompletionCounts(succeeded=3)}
        ),
    )
    response = httpx.Response(
        200,
        json=expected.model_dump(mode="json"),
        request=httpx.Request("GET", source.endpoint),
    )
    monkeypatch.setattr(top.httpx, "get", lambda *args, **kwargs: response)
    assert (
        state.read_activity(source, 300, previous=None, cancelled=lambda: False)
        == expected
    )
    stopped = replace(
        source, instance=replace(source.instance, state="not running"), endpoint=None
    )
    state.rebind(stopped)
    with pytest.raises(StatsUnavailable):
        state.read_activity(stopped, 300, previous=None, cancelled=lambda: False)
    with pytest.raises(StatsUnavailable):
        state.read_usage(stopped, 3600, lambda: False)
    assert not source.layout.run_store.exists()


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
        RuntimeRegistration(
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
