from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from toolang.common.config_sources import ConfigSource
from toolang.stats.config import StatsConfig, stats_config
from toolang.stats.errors import StatsUnavailable
from toolang.stats import service as stats_service
from toolang.stats.service import StatsService

from .conftest import DAY, NOW


def test_initial_history_waits_for_worker_but_recent_reads_use_time_indexes(
    fixture, monkeypatch, tmp_path
):
    root = fixture.root("recent", NOW - 3 * DAY)
    fixture.call(root, NOW - 2 * 3600, NOW - 10)
    service = StatsService(
        fixture.source.db_path,
        tmp_path / "fresh.db",
        config=StatsConfig(),
        clock=lambda: NOW,
    )
    wake = Mock()
    monkeypatch.setattr(service, "request_refresh", wake)
    monkeypatch.setattr(
        service.records,
        "source_root",
        Mock(side_effect=AssertionError("old trees were scanned")),
    )
    try:
        service.start()
        assert service._thread is None
        historical = service.summary()
        assert historical.data is None and historical.snapshot.initializing
        wake.assert_called_once()
        recent = service.models("1h")
        assert recent.data is not None
        assert recent.data[0].in_ == 100 and recent.data[0].model_calls == 0
        assert wake.call_count == 1
    finally:
        service.close()


def test_worker_is_lazy_coalesces_requests_and_stops_before_another_pass(
    fixture, monkeypatch, tmp_path
):
    service = StatsService(
        fixture.source.db_path,
        tmp_path / "worker.db",
        config=StatsConfig(),
        clock=lambda: NOW,
    )
    entered, release, done = Event(), Event(), Event()
    original = service.records.refresh

    def refresh(**kwargs):
        entered.set()
        assert release.wait(timeout=10)
        result = original(**kwargs)
        done.set()
        return result

    refresh_mock = Mock(side_effect=refresh)
    monkeypatch.setattr(service.records, "refresh", refresh_mock)
    try:
        service.start()
        refresh_mock.assert_not_called()
        assert service.summary().data is None
        assert entered.wait(timeout=10)
        for _ in range(10):
            assert service.summary().data is None
        assert refresh_mock.call_count == 1
        assert service._wake.is_set()
        release.set()
        assert done.wait(timeout=10)
    finally:
        release.set()
        service.close()
    assert service._thread is not None and not service._thread.is_alive()
    assert refresh_mock.call_count == 1


def test_statistics_failure_does_not_prevent_source_execution(fixture, tmp_path):
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory")
    service = StatsService(
        fixture.source.db_path, blocked / "stats.db", config=StatsConfig()
    )
    service.start()
    with pytest.raises(StatsUnavailable):
        service.summary()
    root = fixture.root("independent", NOW - 10)
    fixture.call(root, NOW - 9, NOW - 8)
    fixture.finish(root, NOW - 7)
    assert fixture.source.get_run(run_id=root).status == "succeeded"
    service.close()


def test_worker_coalesces_requests_arriving_during_throttle(
    fixture, monkeypatch, tmp_path
):
    service = StatsService(
        fixture.source.db_path,
        tmp_path / "throttle.db",
        config=StatsConfig(),
        clock=lambda: NOW,
    )
    first_pass, throttled, release, idle = Event(), Event(), Event(), Event()
    idle_wakes = []
    original_wake_wait = service._wake.wait
    waits = 0

    def wake_wait():
        nonlocal waits
        waits += 1
        if waits == 3:
            idle_wakes.append(service._wake.is_set())
            idle.set()
        return original_wake_wait()

    def stop_wait(delay):
        if delay:
            assert delay == 5
            throttled.set()
            assert release.wait(timeout=10)
        return service._stop.is_set()

    refresh = Mock(side_effect=lambda **_: first_pass.set())
    monkeypatch.setattr(service.records, "refresh", refresh)
    monkeypatch.setattr(service._wake, "wait", wake_wait)
    monkeypatch.setattr(service._stop, "wait", stop_wait)
    monkeypatch.setattr(stats_service, "time", SimpleNamespace(monotonic=lambda: 1.0))
    try:
        service.start()
        service.request_refresh()
        assert first_pass.wait(timeout=10)
        service.request_refresh()
        assert throttled.wait(timeout=10)
        for _ in range(10):
            service.request_refresh()
        release.set()
        assert idle.wait(timeout=10)
        assert idle_wakes == [False]
        assert refresh.call_count == 2
    finally:
        release.set()
        service.close()


@pytest.mark.parametrize("value", [True, 1, 59, 1.5, "86400", None])
def test_archive_wait_requires_home_scoped_integer_seconds(value):
    home = Path("/agent/config.toml")
    with pytest.raises(ValueError, match="integer >=60"):
        stats_config(
            [ConfigSource(home, {"stats": {"archive_after_seconds": value}})], home=home
        )


def test_statistics_configuration_has_explicit_home_scope():
    home = Path("/agent/config.toml")
    assert stats_config([], home=home).archive_after_seconds == DAY
    assert (
        stats_config(
            [ConfigSource(home, {"stats": {"archive_after_seconds": 3600}})], home=home
        ).archive_after_seconds
        == 3600
    )
    with pytest.raises(ValueError, match="home scope"):
        stats_config(
            [ConfigSource(Path("/root/config.toml"), {"stats": {}})], home=home
        )
    with pytest.raises(ValueError, match="only accepts"):
        stats_config([ConfigSource(home, {"stats": {"poll_interval": 5}})], home=home)
