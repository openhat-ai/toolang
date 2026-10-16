"""Hub process ownership, discovery, and startup cleanup without a live backend."""

from dataclasses import replace
import fcntl
import json
import os
import socket
import subprocess
import sys
import time
from unittest.mock import Mock

import httpx
import psutil
import pytest

from toolang.teaming.config import BackendConfig, TeamingRootConfig
from toolang.teaming.errors import TeamingError
from toolang.cli.toolang import main as cli
from toolang.up.hub import HubProcess, serve
from toolang.up.records import HubRecord


def record(**changes):
    values = dict(
        pid=os.getpid(),
        created=psutil.Process().create_time(),
        port=7000,
        human="human:owner",
        identity="backend",
    )
    return HubRecord.model_validate(values | changes)


def test_hub_artifacts_live_under_root_hub(tmp_path):
    hub = HubProcess(tmp_path)
    assert hub.path == tmp_path / "hub" / "hub.json"
    assert hub.lock == tmp_path / "hub" / "hub.lock"
    assert hub.log == tmp_path / "hub" / "hub.log"
    assert not (tmp_path / ".runtime").exists()


def test_hub_version_uses_recorded_endpoint_and_identity(monkeypatch):
    import toolang.up.hub as module

    saved = record(port=7123, human="human:owner")
    client_type = httpx.Client

    def respond(request):
        assert str(request.url) == "http://127.0.0.1:7123/info"
        assert request.headers["X-Toolang-Backend"] == saved.identity
        assert request.headers["X-Toolang-Human"] == "human%3Aowner"
        assert request.extensions["timeout"] == dict.fromkeys(
            ["connect", "read", "write", "pool"], 1
        )
        return httpx.Response(200, json={"version": "0.4.0a5-3-g12345678*"})

    def client(**kwargs):
        assert kwargs == {"trust_env": False, "timeout": 1}
        return client_type(**kwargs, transport=httpx.MockTransport(respond))

    monkeypatch.setattr(module.httpx, "Client", client)
    assert HubProcess.version(saved) == "0.4.0a5-3-g12345678*"


@pytest.mark.parametrize(
    "status,body",
    [
        (404, b"{}"),
        (409, b"{}"),
        (503, b"{}"),
        (200, b"not-json"),
        (200, b"{}"),
        (200, b"null"),
        (200, b'{"version": 123}'),
        (200, b'{"version": ""}'),
        (200, b'{"version": " invalid "}'),
        (200, b'{"version": "bad\\nlabel"}'),
    ],
)
def test_hub_version_unavailable_metadata_is_unknown(monkeypatch, status, body):
    import toolang.up.hub as module

    client_type = httpx.Client
    transport = httpx.MockTransport(
        lambda request: httpx.Response(status, content=body)
    )
    monkeypatch.setattr(
        module.httpx,
        "Client",
        lambda **kwargs: client_type(**kwargs, transport=transport),
    )
    assert HubProcess.version(record()) == "unknown"


@pytest.mark.parametrize("error", [httpx.ConnectError, httpx.ReadTimeout])
def test_hub_version_transport_failure_is_unknown(monkeypatch, error):
    import toolang.up.hub as module

    client_type = httpx.Client

    def fail(request):
        raise error("unavailable", request=request)

    monkeypatch.setattr(
        module.httpx,
        "Client",
        lambda **kwargs: client_type(**kwargs, transport=httpx.MockTransport(fail)),
    )
    assert HubProcess.version(record()) == "unknown"


def test_starting_hub_version_skips_http(monkeypatch):
    import toolang.up.hub as module

    client = Mock(side_effect=AssertionError("Starting Hub must not query metadata"))
    monkeypatch.setattr(module.httpx, "Client", client)
    assert HubProcess.version(record(status="starting")) == "unknown"
    client.assert_not_called()


def test_private_discovery_uses_actual_endpoint_and_ignores_stale_pid(
    tmp_path, monkeypatch
):
    hub = HubProcess(tmp_path)
    saved = record(port=7123)
    saved.save(hub.path)
    assert hub.path.stat().st_mode & 0o777 == 0o600
    assert "token" not in json.loads(hub.path.read_text())
    monkeypatch.setenv("TOOLANG_HUB_PORT", "invalid")
    assert hub.connection().endpoint == "http://127.0.0.1:7123"
    assert hub.connection().human == "human:owner"
    record(created=saved.created - 10).save(hub.path)
    assert hub.current() is None
    assert hub.stop() is False
    with pytest.raises(TeamingError, match="too hub start"):
        hub.connection()


def test_discovery_rejects_unsupported_token_record(tmp_path):
    hub = HubProcess(tmp_path)
    saved = record()
    saved.save(hub.path)
    hub.path.write_text(json.dumps(saved.model_dump() | {"token": "old-token"}))
    with pytest.raises(ValueError, match="Invalid Hub record"):
        hub.current()


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


def test_client_construction_failure_removes_starting_record(tmp_path, monkeypatch):
    import toolang.up.hub as module

    hub = HubProcess(tmp_path)

    def fail(*args, **kwargs):
        current = hub.current()
        assert current is not None and current.status == "starting"
        raise TeamingError("Invalid backend")

    monkeypatch.setattr(module, "MessagingClient", fail)
    config = TeamingRootConfig("human:owner", BackendConfig("redis://localhost"), 7000)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    with pytest.raises(TeamingError, match="Invalid backend"):
        serve(tmp_path, config, port=port)
    assert not hub.path.exists()
    assert hub.lock.is_file()
    assert not (tmp_path / ".runtime").exists()


def test_starting_hub_reserves_port_before_backend_access(tmp_path, monkeypatch):
    import toolang.up.hub as module

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]

    def check_reservation(*args, **kwargs):
        with socket.socket() as competitor:
            competitor.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            with pytest.raises(OSError):
                competitor.bind(("127.0.0.1", port))
                competitor.listen()
        raise TeamingError("Stop after checking reservation")

    monkeypatch.setattr(module, "MessagingClient", check_reservation)
    config = TeamingRootConfig("human:owner", BackendConfig("redis://localhost"), port)
    with pytest.raises(TeamingError, match="Stop after checking reservation"):
        serve(tmp_path, config, port=port)
    assert not HubProcess(tmp_path).path.exists()


def test_failed_and_timed_out_start_leave_no_child(tmp_path):
    hub = HubProcess(tmp_path)
    with pytest.raises(TeamingError, match="startup failed"):
        hub.start([sys.executable, "-c", "raise SystemExit(1)"])
    pidfile = tmp_path / "child.pid"
    script = f"import os,time; from pathlib import Path; Path({str(pidfile)!r}).write_text(str(os.getpid())); time.sleep(30)"
    with pytest.raises(TeamingError, match="startup timed out"):
        hub.start([sys.executable, "-c", script], timeout=0.5)
    assert not psutil.pid_exists(int(pidfile.read_text()))
    assert hub.log.is_file()
    assert not (tmp_path / ".runtime").exists()


def test_starting_hub_is_visible_and_force_stoppable(tmp_path, capsys):
    # Hold registration before readiness, just as a slow backend connection would.
    script = """
import asyncio
import sys
from pathlib import Path
import toolang.up.hub as module
from toolang.teaming.config import BackendConfig, TeamingRootConfig
root, port = Path(sys.argv[1]), int(sys.argv[2])
class SlowClient(module.MessagingClient):
    async def check_backend(self):
        (root / "starting").touch()
        await asyncio.sleep(60)
    async def close(self): pass
module.MessagingClient = SlowClient
module.serve(root, TeamingRootConfig("human:owner", BackendConfig("redis://localhost"), port), port=port)
"""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    with (tmp_path / "child.log").open("w") as log:
        process = subprocess.Popen(
            [sys.executable, "-c", script, str(tmp_path), str(port)],
            stdout=log,
            stderr=log,
        )
        try:
            deadline = time.monotonic() + 10
            while not (tmp_path / "starting").exists() and time.monotonic() < deadline:
                assert process.poll() is None, (tmp_path / "child.log").read_text()
                time.sleep(0.02)
            assert (tmp_path / "starting").exists()
            hub = HubProcess(tmp_path)
            current = hub.current()
            assert current is not None and current.pid == process.pid
            assert current.status == "starting" and not hub.ready(current)
            with pytest.raises(TeamingError, match="Hub is starting"):
                hub.connection()
            assert cli.main(["--root", str(tmp_path), "hub"]) == 0
            assert "Hub starting" in capsys.readouterr().out
            assert cli.main(["--root", str(tmp_path), "hub", "stop", "--force"]) == 0
            assert process.poll() is not None
            assert hub.current() is None
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
