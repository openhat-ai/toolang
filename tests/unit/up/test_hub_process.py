"""Hub process ownership, discovery, and startup cleanup without a live backend."""

from dataclasses import replace
import fcntl
import os
import socket
import sys
from unittest.mock import Mock

import psutil
import pytest

from toolang.teaming.config import BackendConfig, TeamingRootConfig
from toolang.teaming.errors import TeamingError
from toolang.up.hub import HubProcess, serve
from toolang.up.records import HubRecord


def record(**changes):
    values = dict(
        pid=os.getpid(),
        created=psutil.Process().create_time(),
        port=7000,
        token="x" * 32,
        human="human:owner",
        identity="backend",
    )
    return HubRecord.model_validate(values | changes)


def test_private_discovery_uses_actual_endpoint_and_ignores_stale_pid(
    tmp_path, monkeypatch
):
    hub = HubProcess(tmp_path)
    saved = record(port=7123)
    saved.save(hub.path)
    assert hub.path.stat().st_mode & 0o777 == 0o600
    monkeypatch.setenv("TOOLANG_HUB_PORT", "invalid")
    assert hub.connection().endpoint == "http://127.0.0.1:7123"
    assert hub.connection().human == "human:owner"
    record(created=saved.created - 10).save(hub.path)
    assert hub.current() is None
    assert hub.stop() is False
    with pytest.raises(TeamingError, match="too hub start"):
        hub.connection()


def test_stop_signals_only_the_verified_hub_process(tmp_path, monkeypatch):
    import toolang.up.hub as module

    saved = record()
    saved.save(HubProcess(tmp_path).path)
    process = Mock()
    process.create_time.return_value = saved.created
    process.status.return_value = psutil.STATUS_RUNNING
    monkeypatch.setattr(module.psutil, "Process", Mock(return_value=process))
    assert HubProcess(tmp_path).stop()
    process.terminate.assert_called_once_with()
    process.wait.assert_called_once_with(timeout=10)
    process.kill.assert_not_called()


def test_lifetime_lock_rejects_concurrent_serve_before_connecting(tmp_path):
    hub = HubProcess(tmp_path)
    hub.lock.parent.mkdir()
    with hub.lock.open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(TeamingError, match="already running or starting"):
            serve(
                tmp_path,
                TeamingRootConfig(
                    "human:owner", BackendConfig("redis://localhost:6379/0"), 7000
                ),
                port=7000,
            )


def test_busy_hub_port_fails_without_fallback_or_backend_access(tmp_path):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        with pytest.raises(TeamingError, match=f"Cannot bind Hub port {port}"):
            serve(
                tmp_path,
                TeamingRootConfig(
                    "human:owner", BackendConfig("redis://localhost:6379/0"), 7000
                ),
                port=port,
            )
    assert HubProcess(tmp_path).current() is None


def test_unavailable_backend_exits_and_does_not_publish_ready(tmp_path):
    config = replace(
        TeamingRootConfig(
            "human:owner", BackendConfig("redis://localhost:6379/0"), 7000
        ),
        backend=BackendConfig(f"unix://{tmp_path}/absent.sock"),
    )
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    assert serve(tmp_path, config, port=port) != 0
    assert HubProcess(tmp_path).current() is None


def test_failed_and_timed_out_start_leave_no_child(tmp_path):
    hub = HubProcess(tmp_path)
    with pytest.raises(TeamingError, match="startup failed"):
        hub.start([sys.executable, "-c", "raise SystemExit(1)"])
    pidfile = tmp_path / "child.pid"
    script = f"import os,time; from pathlib import Path; Path({str(pidfile)!r}).write_text(str(os.getpid())); time.sleep(30)"
    with pytest.raises(TeamingError, match="startup timed out"):
        hub.start([sys.executable, "-c", script], timeout=0.5)
    assert not psutil.pid_exists(int(pidfile.read_text()))
