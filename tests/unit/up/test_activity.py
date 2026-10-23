"""Exercise runtime reading policies without Top scheduling or presentation."""

from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from threading import Event

import httpx
import pytest

from tests.support.execution_fixtures import project_run_start
from tests.support.runtime_observation import NOW, binding, completed
from toolang.base.types.message import Message
from toolang.execution.schemas import LiveSnapshot, LiveTreeSnapshot, RunNode
from toolang.execution.store import RunStore
from toolang.execution.types import RunReport
from toolang.stats.errors import StatsUnavailable
from toolang.stats.schemas import CompletionCounts
from toolang.stats.types import Metrics, timestamp
from toolang.up import activity
from toolang.up.activity import ActivityReader
from toolang.up.schemas import ActivitySnapshot


@pytest.mark.parametrize("registered", [False, True])
def test_missing_history_is_zero_only_after_registration_has_departed(
    tmp_path, registered
):
    source = replace(binding(tmp_path, registered=registered), endpoint=None)
    reader = ActivityReader()
    if registered:
        with pytest.raises(StatsUnavailable):
            reader.read_usage(source, 3600)
        with pytest.raises(StatsUnavailable):
            reader.read_activity(source, 300)
    else:
        assert reader.read_usage(source, 3600).metrics == Metrics()
        assert reader.read_activity(source, 300).completed.counts.total == 0
    assert not source.layout.run_store.exists()


def test_corrupt_history_never_becomes_zero_or_gets_repaired(tmp_path):
    source = replace(binding(tmp_path, registered=True), endpoint=None)
    source.layout.run_store.parent.mkdir(parents=True, exist_ok=True)
    source.layout.run_store.write_bytes(b"not sqlite")
    reader = ActivityReader()
    with pytest.raises(StatsUnavailable):
        reader.read_usage(source, 3600)
    with pytest.raises(StatsUnavailable):
        reader.read_activity(source, 300)
    assert source.layout.run_store.read_bytes() == b"not sqlite"
    assert not source.layout.stats_store.exists()


@pytest.mark.parametrize("following_read", ["activity", "usage"])
def test_usage_remembers_history_created_after_the_initial_existence_check(
    tmp_path, monkeypatch, following_read
):
    source = replace(binding(tmp_path, registered=True), endpoint=None)
    reader = ActivityReader()
    original = activity.recent_usage

    def appeared(*args, **kwargs):
        with closing(RunStore(source.layout.run_store)):
            pass
        return original(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(activity, "recent_usage", appeared)
        assert reader.read_usage(source, 3600).metrics == Metrics()
    source.layout.run_store.unlink()
    source.registration.unlink()
    read = reader.read_activity if following_read == "activity" else reader.read_usage
    with pytest.raises(StatsUnavailable):
        read(source, 300)
    assert not source.layout.run_store.exists()
    assert not source.layout.stats_store.exists()


def test_activity_and_usage_workers_share_established_history(tmp_path, monkeypatch):
    source = binding(tmp_path, registered=True)
    with closing(RunStore(source.layout.run_store)):
        pass
    reader = ActivityReader()
    entered, release = Event(), Event()

    def pending(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return httpx.Response(
            200,
            json=ActivitySnapshot(
                live=LiveSnapshot("epoch", timestamp(NOW), ()), completed=completed()
            ).model_dump(mode="json"),
            request=httpx.Request("GET", source.endpoint),
        )

    monkeypatch.setattr(httpx, "get", pending)
    with ThreadPoolExecutor(max_workers=2) as pool:
        live = pool.submit(reader.read_activity, source, 300)
        try:
            assert entered.wait(2)
            source.layout.run_store.unlink()
            source.registration.unlink()
            usage = pool.submit(reader.read_usage, source, 3600)
            with pytest.raises(StatsUnavailable):
                usage.result(timeout=2)
        finally:
            release.set()
        with pytest.raises(StatsUnavailable):
            live.result(timeout=2)
    assert not source.layout.run_store.exists()


def test_completed_page_keeps_the_captured_upper_bound_and_cursor(
    tmp_path, monkeypatch
):
    source = binding(tmp_path, registered=True)
    with closing(RunStore(source.layout.run_store)) as store:
        for identity, finished in (("run_a", NOW), ("run_b", NOW), ("run_c", NOW + 1)):
            project_run_start(
                store,
                run_id=identity,
                thread_id="chat_top",
                input=Message.user("fixture"),
                started_at=timestamp(NOW - 10),
            )
            store.finish_run(run_id=identity, finished_at=timestamp(finished))
        monkeypatch.setattr(activity.time, "time", lambda: NOW + 3600)
        reader = ActivityReader()
        result = reader.read_page(source, 300, (timestamp(NOW), "run_a"), NOW)
        assert [row.id for row in result.roots] == ["run_b"]
        assert result.counts.succeeded == 2
        assert result.end == timestamp(NOW)
    source.layout.run_store.unlink()
    source.registration.unlink()
    with pytest.raises(StatsUnavailable):
        reader.read_usage(source, 3600)


@pytest.mark.parametrize("epoch", ["epoch", "restarted"])
def test_online_completion_lookup_keeps_the_first_page_cursor_and_checks_epoch(
    tmp_path, monkeypatch, epoch
):
    source = binding(tmp_path, registered=True)
    store = RunStore(source.layout.run_store)
    selected = "run_zz"
    for name in [*(f"run_{index:03}" for index in range(101)), selected]:
        project_run_start(
            store,
            run_id=name,
            thread_id="chat_top",
            input=Message.user("fixture"),
            started_at=timestamp(NOW - 10),
        )
        store.finish_run(run_id=name, finished_at=timestamp(NOW), report=RunReport())
    root = RunNode(selected, None, "agic:work", "running", timestamp(NOW - 10))
    previous = LiveSnapshot(
        "epoch", timestamp(NOW - 1), (LiveTreeSnapshot(selected, "chat_top", (root,)),)
    )
    try:
        with activity.recent_source(source.layout.run_store) as reader:
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
        monkeypatch.setattr(activity.httpx, "get", lambda *args, **kwargs: response)
        result = ActivityReader().read_activity(source, 300, previous=previous)
        assert (
            len(result.completed.roots) == 100
            and result.completed.roots[-1].id == "run_099"
        )
        assert [row.id for row in result.retired] == (
            [selected] if epoch == previous.epoch else []
        )
    finally:
        store.close()


def test_lost_history_never_becomes_empty_after_a_successful_read(tmp_path):
    source = replace(
        binding(tmp_path, registered=True),
        instance=replace(
            binding(tmp_path, registered=True).instance, state="not running"
        ),
        endpoint=None,
    )
    with closing(RunStore(source.layout.run_store)):
        pass
    reader = ActivityReader()
    assert reader.read_usage(source, 3600, lambda: False).metrics == Metrics()
    source.layout.run_store.unlink()
    with pytest.raises(StatsUnavailable):
        reader.read_activity(source, 300, previous=None, cancelled=lambda: False)
    with pytest.raises(StatsUnavailable):
        reader.read_usage(source, 3600, lambda: False)
    assert not source.layout.run_store.exists()


def test_published_endpoint_failure_is_distinct_from_an_embedded_runtime(
    tmp_path, monkeypatch
):
    source = binding(tmp_path, registered=True)
    with closing(RunStore(source.layout.run_store)):
        pass
    calls = []

    def offline(*args, **kwargs):
        calls.append(args)
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(activity.httpx, "get", offline)
    result = ActivityReader().read_activity(replace(source, endpoint=None), 300)
    assert result.live is None and result.live_error is None and not calls
    result = ActivityReader().read_activity(source, 300)
    assert result.live is None and result.live_error == "connection refused"
    assert result.completed.counts.total == 0 and len(calls) == 1
    response = httpx.Response(
        200,
        json=ActivitySnapshot(
            live=LiveSnapshot("epoch", timestamp(NOW), ()), completed=completed()
        ).model_dump(mode="json"),
        request=httpx.Request("GET", source.endpoint),
    )
    monkeypatch.setattr(activity.httpx, "get", lambda *args, **kwargs: response)
    result = ActivityReader().read_activity(source, 300)
    assert result.live is not None and result.live_error is None


@pytest.mark.parametrize("replacement", [False, True])
@pytest.mark.parametrize("response", ["disconnected", "stale", "history"])
def test_departure_during_live_read_keeps_history_without_reporting_a_live_failure(
    tmp_path, monkeypatch, replacement, response
):
    source = binding(tmp_path, registered=True)
    with closing(RunStore(source.layout.run_store)) as store:
        project_run_start(
            store,
            run_id="run_done",
            thread_id="chat_top",
            input=Message.user("fixture"),
            started_at=timestamp(NOW - 10),
        )
        store.finish_run(run_id="run_done", finished_at=timestamp(NOW - 1))
    monkeypatch.setattr(activity.time, "time", lambda: NOW)
    calls = []

    def depart():
        if replacement:
            reference = json.loads(source.registration.read_text())
            source.registration.write_text(json.dumps(dict(reference, launch_id="new")))
        else:
            source.registration.unlink(missing_ok=True)

    def departed(*args, **kwargs):
        calls.append(args)
        if response != "history":
            depart()
        if response != "stale":
            raise httpx.ConnectError("old endpoint closed")
        return httpx.Response(
            200,
            json=ActivitySnapshot(
                live=LiveSnapshot("obsolete", timestamp(NOW), ()),
                completed=completed(),
            ).model_dump(mode="json"),
            request=httpx.Request("GET", source.endpoint),
        )

    monkeypatch.setattr(activity.httpx, "get", departed)
    if response == "history":
        original = activity.activity_snapshot

        def history(*args, **kwargs):
            depart()
            return original(*args, **kwargs)

        monkeypatch.setattr(activity, "activity_snapshot", history)
    for _ in range(2):
        result = ActivityReader().read_activity(source, 300)
        assert result.live is None and result.live_error is None
        assert result.completed.counts.succeeded == 1
        assert [row.id for row in result.completed.roots] == ["run_done"]
    assert len(calls) == 1
    assert source.layout.run_store.exists()


@pytest.mark.parametrize("operation", ["activity", "usage"])
@pytest.mark.parametrize("established", [False, True])
def test_departure_during_first_history_read_only_zeros_a_never_created_store(
    tmp_path, monkeypatch, operation, established
):
    source = binding(tmp_path, registered=True)
    if established:
        with closing(RunStore(source.layout.run_store)):
            pass
    reader = ActivityReader()

    def departed(*args, **kwargs):
        source.registration.unlink()
        source.layout.run_store.unlink(missing_ok=True)
        raise StatsUnavailable("store is absent")

    monkeypatch.setattr(
        activity,
        "activity_snapshot" if operation == "activity" else "recent_usage",
        departed,
    )

    def read():
        if operation == "activity":
            return reader.read_activity(
                source, 300, previous=None, cancelled=lambda: False
            )
        return reader.read_usage(source, 3600, lambda: False)

    if established:
        with pytest.raises(StatsUnavailable, match="store is absent"):
            read()
    else:
        result = read()
        if operation == "activity":
            assert result.completed.counts.total == 0 and result.live_error is None
        else:
            assert result.metrics == Metrics()
    assert not source.layout.run_store.exists()


def test_unreadable_store_metadata_cannot_be_treated_as_new_history(
    tmp_path, monkeypatch
):
    source = replace(
        binding(tmp_path, registered=True),
        instance=replace(
            binding(tmp_path, registered=True).instance, state="not running"
        ),
        endpoint=None,
    )
    reader = ActivityReader()
    original = type(source.layout.run_store).stat

    def denied(path, *args, **kwargs):
        if path == source.layout.run_store:
            raise PermissionError("permission denied")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(type(source.layout.run_store), "stat", denied)
    with pytest.raises(PermissionError):
        reader.read_usage(source, 3600, lambda: False)


def test_healthy_endpoint_does_not_require_readable_local_store_metadata(
    tmp_path, monkeypatch
):
    source = binding(tmp_path, registered=True)
    reader = ActivityReader()
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
    monkeypatch.setattr(activity.httpx, "get", lambda *args, **kwargs: response)
    assert (
        reader.read_activity(source, 300, previous=None, cancelled=lambda: False)
        == expected
    )


def test_http_history_cannot_become_uninitialized_after_agent_stops(
    tmp_path, monkeypatch
):
    source = binding(tmp_path, registered=True)
    reader = ActivityReader()
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
    monkeypatch.setattr(activity.httpx, "get", lambda *args, **kwargs: response)
    assert (
        reader.read_activity(source, 300, previous=None, cancelled=lambda: False)
        == expected
    )
    stopped = replace(
        source, instance=replace(source.instance, state="not running"), endpoint=None
    )
    with pytest.raises(StatsUnavailable):
        reader.read_activity(stopped, 300, previous=None, cancelled=lambda: False)
    with pytest.raises(StatsUnavailable):
        reader.read_usage(stopped, 3600, lambda: False)
    assert not source.layout.run_store.exists()


@pytest.mark.parametrize("departed", [False, True])
def test_http_rejection_is_a_live_error_only_for_a_current_binding(
    tmp_path, monkeypatch, departed
):
    source = binding(tmp_path, registered=True)
    with closing(RunStore(source.layout.run_store)):
        pass

    def rejected(*args, **kwargs):
        if departed:
            source.registration.unlink()
        return httpx.Response(401, request=httpx.Request("GET", source.endpoint))

    monkeypatch.setattr(httpx, "get", rejected)
    result = ActivityReader().read_activity(source, 300)
    assert result.completed.counts.total == 0
    assert (result.live_error is None) == departed
    if not departed:
        assert result.live_error is not None and "401" in result.live_error


@pytest.mark.parametrize("placement", ["visiting", "roaming"])
@pytest.mark.parametrize("read_kind", ["activity", "usage", "page"])
def test_nonresident_history_requires_full_ownership_before_any_data_read(
    tmp_path, monkeypatch, placement, read_kind
):
    import json
    from toolang.common.layout import AgentLayout
    from toolang.up.allocation import allocate_roaming
    from toolang.up.observation import runtime_inventory
    from toolang.up.workload import ForegroundWorkload
    from toolang.up.activity import ActivityReader
    from toolang.up import activity

    layout = (
        AgentLayout.visiting("https://example.com/alice.too", "alice", root=tmp_path)
        if placement == "visiting"
        else allocate_roaming(tmp_path / "alice.too", toolang_root=tmp_path)
    )
    with ForegroundWorkload(layout, toolang_root=tmp_path, drivers=frozenset()):
        source = runtime_inventory(tmp_path).sources[0]
        document = json.loads(layout.runtime_status.read_text())
        field = "source_ref" if placement == "visiting" else "source_path"
        document["instances"]["default"]["control"][field] = (
            "https://foreign.example/alice.too"
            if placement == "visiting"
            else str(tmp_path / "other.too")
        )
        original = layout.runtime_status.read_bytes()
        layout.runtime_status.write_text(json.dumps(document))
        before = layout.runtime_status.read_bytes()
        monkeypatch.setattr(
            activity,
            "recent_source",
            lambda *_a, **_k: pytest.fail("read unverified history"),
        )
        monkeypatch.setattr(
            activity,
            "recent_usage",
            lambda *_a, **_k: pytest.fail("read unverified usage"),
        )
        reader = ActivityReader()
        try:
            with pytest.raises(ValueError, match="identity|ownership"):
                if read_kind == "activity":
                    reader.read_activity(source, 300)
                elif read_kind == "usage":
                    reader.read_usage(source, 3600)
                else:
                    reader.read_page(source, 300, ("2026-10-21T00:00:00Z", "run"), 0)
            assert layout.runtime_status.read_bytes() == before
        finally:
            layout.runtime_status.write_bytes(original)
