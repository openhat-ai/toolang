"""Observation generations, discovery retention and slow scans stay independent."""

from concurrent.futures import Future
from threading import Event

from toolang.common.layout import AgentLayout
from toolang.execution.schemas import LiveSnapshot, LiveTreeSnapshot, RunNode
from toolang.stats.errors import StatsUnavailable
from toolang.stats.schemas import CompletedRoots, CompletionCounts, RecentUsage
from toolang.stats.types import Metrics, timestamp
from toolang.up import top
from toolang.up.observation import RuntimeInventory, RuntimeSource
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

    def live(source, recent, before=None, completed_at=None):
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
        assert state.value.completed is None and state.value.live == old
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
