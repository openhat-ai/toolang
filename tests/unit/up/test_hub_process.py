"""Local Hub ownership and cleanup, including actual Uvicorn SIGTERM shutdown."""

import fcntl
import json
import os
import socket
import sys
from unittest.mock import Mock

import httpx
import psutil
import pytest

from toolang.teaming.config import BackendConfig
from toolang.teaming.errors import TeamingError
from toolang.up.config import HubServerConfig
from toolang.up.hub import HubProcess, serve
from toolang.up.records import HubRecord


def record(**changes):
    return HubRecord(
        pid=os.getpid(), created=psutil.Process().create_time(), port=7001, **changes
    )


def test_private_record_has_only_process_fields(tmp_path):
    hub = HubProcess(tmp_path)
    saved = record()
    saved.save(hub.path)
    assert hub.path == tmp_path / "hub" / "hub.json"
    assert hub.path.stat().st_mode & 0o777 == 0o600
    assert set(json.loads(hub.path.read_text())) == {
        "pid",
        "created",
        "host",
        "port",
        "status",
    }
    hub.path.write_text(
        json.dumps(saved.model_dump() | {"human": "human:old", "identity": "old"})
    )
    with pytest.raises(ValueError, match="Invalid Hub process record"):
        hub.current()


def test_stale_record_never_signals_reused_pid(tmp_path, monkeypatch):
    hub = HubProcess(tmp_path)
    saved = record().model_copy(update={"created": psutil.Process().create_time() - 1})
    saved.save(hub.path)
    assert hub.current() is None
    assert not hub.stop()
    assert not hub.path.exists()


@pytest.mark.parametrize("force", [False, True])
def test_stop_cleans_verified_process_record(tmp_path, monkeypatch, force):
    hub = HubProcess(tmp_path)
    saved = record()
    saved.save(hub.path)
    process = Mock()
    process.create_time.return_value = saved.created
    process.status.return_value = psutil.STATUS_RUNNING
    if force:
        process.wait.side_effect = [psutil.TimeoutExpired(10), None]
    monkeypatch.setattr("toolang.up.hub.psutil.Process", Mock(return_value=process))
    assert hub.stop(force=force)
    process.terminate.assert_called_once()
    assert process.kill.call_count == int(force)
    assert not hub.path.exists()


@pytest.mark.parametrize("stage", ["kill", "wait"])
def test_force_stop_cleans_record_when_process_exits_after_timeout(
    tmp_path, monkeypatch, stage
):
    hub = HubProcess(tmp_path)
    saved = record()
    saved.save(hub.path)
    process = Mock()
    process.create_time.return_value = saved.created
    process.status.return_value = psutil.STATUS_RUNNING
    process.wait.side_effect = [
        psutil.TimeoutExpired(10),
        psutil.NoSuchProcess(saved.pid),
    ]
    if stage == "kill":
        process.kill.side_effect = psutil.NoSuchProcess(saved.pid)
    monkeypatch.setattr("toolang.up.hub.psutil.Process", Mock(return_value=process))
    assert hub.stop(force=True)
    assert not hub.path.exists()


def test_stop_timeout_preserves_record(tmp_path, monkeypatch):
    hub = HubProcess(tmp_path)
    saved = record()
    saved.save(hub.path)
    process = Mock()
    process.create_time.return_value = saved.created
    process.status.return_value = psutil.STATUS_RUNNING
    process.wait.side_effect = psutil.TimeoutExpired(10)
    monkeypatch.setattr("toolang.up.hub.psutil.Process", Mock(return_value=process))
    with pytest.raises(TeamingError, match="did not stop"):
        hub.stop()
    assert HubRecord.load(hub.path) == saved
    process.kill.assert_not_called()


def test_late_cleanup_preserves_replacement_record(tmp_path):
    hub = HubProcess(tmp_path)
    old = record()
    new = old.model_copy(update={"created": old.created + 1})
    new.save(hub.path)
    with hub.lock.open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        hub._clear(old)
    assert HubRecord.load(hub.path) == new


def test_start_reserves_socket_and_lifetime_lock_before_backend_access(tmp_path):
    hub = HubProcess(tmp_path)
    hub.lock.parent.mkdir()
    config = HubServerConfig(
        BackendConfig("redis://localhost", root=str(tmp_path.resolve()))
    )
    with hub.lock.open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(TeamingError, match="already running or starting"):
            serve(tmp_path, config, port=7001)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        with pytest.raises(TeamingError, match="Cannot bind"):
            serve(tmp_path, config, port=port)
    assert not hub.path.exists()


def test_backend_construction_failure_removes_starting_record(tmp_path, monkeypatch):
    hub = HubProcess(tmp_path)

    def fail(_):
        current = hub.current()
        assert current is not None and current.status == "starting"
        raise TeamingError("invalid backend")

    monkeypatch.setattr("toolang.teaming.api.create_backend", fail)
    config = HubServerConfig(
        BackendConfig("redis://localhost", root=str(tmp_path.resolve()))
    )
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    with pytest.raises(TeamingError, match="invalid backend"):
        serve(tmp_path, config, port=port)
    assert not hub.path.exists()


OFFLINE_HUB = """
from pathlib import Path
import sys
from toolang.up.hub import serve
from toolang.up.config import HubServerConfig
from toolang.teaming.config import BackendConfig
import toolang.teaming.api as api
class OfflineBackend:
    events = object()
    async def ping(self): pass
    async def initialize(self): pass
    async def due_presence(self): return []
    async def close(self): pass
api.create_backend = lambda _: OfflineBackend()
root, port = Path(sys.argv[1]), int(sys.argv[2])
raise SystemExit(serve(root, HubServerConfig(BackendConfig("redis://unused", root=str(root))), port=port))
"""


def test_graceful_sigterm_removes_actual_uvicorn_record(tmp_path):
    root = tmp_path.resolve()
    hub = HubProcess(root)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    started = hub.start([sys.executable, "-c", OFFLINE_HUB, str(root), str(port)])
    try:
        assert started.status == "running" and started.port == port
        assert hub.stop()
        assert hub.current() is None
        assert not hub.path.exists()
    finally:
        if hub.current() is not None:
            hub.stop(force=True)


def test_failed_and_timed_out_start_leave_no_child_or_owned_record(tmp_path):
    hub = HubProcess(tmp_path)
    with pytest.raises(TeamingError, match="startup failed"):
        hub.start([sys.executable, "-c", "raise SystemExit(1)"])
    script = "import os,sys,time,psutil; from pathlib import Path; from toolang.up.records import HubRecord; HubRecord(pid=os.getpid(),created=psutil.Process().create_time(),port=7001).save(Path(sys.argv[1])); time.sleep(60)"
    with pytest.raises(TeamingError, match="startup timed out"):
        hub.start([sys.executable, "-c", script, str(hub.path)], timeout=1)
    assert not hub.path.exists()


def test_local_readiness_uses_listener_without_participant_headers(monkeypatch):
    client_type = httpx.Client

    def respond(request):
        assert request.url.host == "127.0.0.1"
        assert "X-Toolang-Human" not in request.headers
        assert "X-Toolang-Backend" not in request.headers
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(
        "toolang.up.hub.httpx.Client",
        lambda **kwargs: client_type(**kwargs, transport=httpx.MockTransport(respond)),
    )
    assert HubProcess.ready(record(host="0.0.0.0"))
