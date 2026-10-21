"""Concurrent real Chat processes share and reclaim a visiting server offline."""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import os
import socket
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit

import httpx
import pytest

from tests.support.chat_tui_pty import ChatTuiPtySession
from toolang.up.instance import update_instance
from toolang.up.identity import visiting_home_guard
from toolang.up.process import AgentProcess
from toolang.up.source import visiting_layout


def _wait(predicate, timeout=40):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("lifecycle condition did not become true")


@contextmanager
def _visiting_chat(tmp_path: Path, *, source_available: threading.Event | None = None):
    requests = []

    class Source(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            if source_available is not None and not source_available.is_set():
                self.send_error(503)
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"flow:\n  pass\n")

        def log_message(self, format: str, *args: object) -> None:
            pass

    source = ThreadingHTTPServer(("127.0.0.1", 0), Source)
    thread = threading.Thread(target=source.serve_forever, daemon=True)
    thread.start()
    root = tmp_path / "root"
    root.mkdir()
    (root / "config.toml").write_text("[teaming]\nenabled = false\n")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        agent_port = listener.getsockname()[1]
    url = f"http://127.0.0.1:{source.server_port}/dev.too"
    layout = visiting_layout(url, toolang_root=root)
    with visiting_home_guard(layout):
        pass
    update_instance(
        layout.runtime_status,
        lambda entry: entry["control"].update(
            last_successful={
                "endpoint": f"http://localhost:{agent_port}",
                "sandbox": "host",
            }
        ),
    )
    base = [sys.executable, "-m", "toolang.cli.toolang", "--root", str(root)]
    clients = []
    outputs = []

    def chat(*, wait=True):
        log = tmp_path / f"chat-{len(clients)}.log"
        output = log.open("w")
        outputs.append(output)
        process = subprocess.Popen(
            [*base, url, "chat", "--sandbox", "host"],
            stdin=subprocess.PIPE,
            stdout=output,
            stderr=subprocess.STDOUT,
            text=True,
            env={
                **os.environ,
                "TOOLANG_ROOT": str(root),
            },
        )
        clients.append(process)

        def ready():
            if process.poll() is not None:
                raise AssertionError(log.read_text())
            return "> " in log.read_text()

        if wait:
            _wait(ready)
            state = AgentProcess(layout).state()
            assert state is not None
            endpoint = state["endpoint"]
            assert isinstance(endpoint, str)
            assert urlsplit(endpoint).port == agent_port
        return process

    def command(*args, success=True):
        result = subprocess.run(
            [*base, *args], capture_output=True, text=True, timeout=40
        )
        assert result.returncode == (0 if success else 1), result.stderr
        return result

    try:
        yield layout, url, chat, command, requests
    finally:
        for client in clients:
            if client.poll() is None:
                client.kill()
            client.wait(timeout=10)
        subprocess.run([*base, "stop", url, "--force"], capture_output=True, timeout=20)
        for output in outputs:
            output.close()
        source.shutdown()
        source.server_close()
        thread.join()


@pytest.mark.parametrize("killed", [False, True])
def test_visiting_clients_share_until_the_final_client_exits(tmp_path, killed):
    with _visiting_chat(tmp_path) as (layout, _url, chat, _command, _requests):
        first = chat(wait=False)
        second = chat()
        _wait(lambda: "> " in (tmp_path / "chat-0.log").read_text())
        original = AgentProcess(layout).state()
        assert original is not None
        current = AgentProcess(layout).state()
        assert current is not None and current["launch_id"] == original["launch_id"]
        assert original["drivers"] == ["chat"]
        assert original["lifetime"] == "automatic"
        if not killed:
            # Input is idle longer than the lease TTL; renewal cannot depend on SSE or typing.
            time.sleep(16)
        first.stdin.close()
        assert first.wait(timeout=10) == 0
        status = AgentProcess(layout).status(ui_base_url="", check_health=True)
        assert status is not None and status.status == "running"
        if killed:
            second.kill()
            second.wait(timeout=10)
        else:
            second.stdin.close()
            assert second.wait(timeout=10) == 0
        _wait(
            lambda: (
                (status := AgentProcess(layout).status(ui_base_url="")) is not None
                and status.status == "stopped"
            ),
            timeout=25,
        )
        assert layout.run_store.exists()


@pytest.mark.parametrize("explicit_drivers", [False, True])
def test_start_promotes_same_generation_without_fetch_or_driver_changes(
    tmp_path, explicit_drivers
):
    with _visiting_chat(tmp_path) as (layout, url, chat, command, requests):
        client = chat()
        original = AgentProcess(layout).state()
        assert original is not None
        fetched = len(requests)
        conflict = command(url, "start", "--enable", "chat,talk", success=False)
        assert "drivers" in conflict.stderr
        wrong_port = command(url, "start", "--port", "1", success=False)
        assert "port" in wrong_port.stderr
        promoted = command(
            url, "start", *(["--enable", "chat"] if explicit_drivers else [])
        )
        expected = f"Agent {layout.name} running: {original['endpoint']}"
        assert promoted.stdout.strip() == expected
        assert len(requests) == fetched
        current = AgentProcess(layout).state()
        assert current is not None and current["launch_id"] == original["launch_id"]
        assert current["drivers"] == original["drivers"]
        assert current["lifetime"] == "persistent"
        assert command("start", url, "--enable", "chat").stdout.strip() == expected
        client.stdin.close()
        assert client.wait(timeout=10) == 0
        endpoint = original["endpoint"]
        assert httpx.get(f"{endpoint}/healthz", timeout=3).is_success
        command("stop", url)
        assert len(requests) == fetched
        status = AgentProcess(layout).status(ui_base_url="")
        assert status is not None and status.status == "stopped"


def test_chat_reuses_default_hosted_drivers_without_relaunch(tmp_path):
    with _visiting_chat(tmp_path) as (layout, url, chat, command, requests):
        command(url, "start")
        original = AgentProcess(layout).state()
        assert original is not None and original["drivers"] == ["chat", "talk"]
        fetched = len(requests)
        client = chat()
        client.stdin.close()
        assert client.wait(timeout=10) == 0
        current = AgentProcess(layout).state()
        assert current is not None
        assert current["launch_id"] == original["launch_id"]
        assert current["drivers"] == original["drivers"]
        assert len(requests) == fetched


def test_visiting_reuse_ignores_caller_startup_defaults_and_unavailable_source(
    tmp_path, monkeypatch
):
    available = threading.Event()
    available.set()
    with _visiting_chat(tmp_path, source_available=available) as (
        layout,
        url,
        chat,
        command,
        requests,
    ):
        first = chat()
        original = AgentProcess(layout).state()
        assert original is not None
        fetched = len(requests)
        available.clear()
        monkeypatch.setenv("PWD", "/another/directory")
        monkeypatch.setenv("TERM", "another-terminal")
        monkeypatch.setenv("TOOLANG_LIMIT_TOKENS", "invalid-startup-value")
        assert layout.toolang_root is not None
        (layout.toolang_root / "config.toml").write_text(
            '[teaming]\nenabled = false\n[sandbox]\ndriver = "docker"\n'
        )
        assert command(url, "start").stdout.strip() == (
            f"Agent {layout.name} running: {original['endpoint']}"
        )
        second = chat()
        current = AgentProcess(layout).state()
        assert current is not None
        assert current["launch_id"] == original["launch_id"]
        assert current["drivers"] == original["drivers"]
        assert current["lifetime"] == "persistent"
        assert len(requests) == fetched
        for client in (first, second):
            client.stdin.close()
            assert client.wait(timeout=10) == 0


def test_lease_loss_ends_an_idle_client_without_relaunch(tmp_path):
    with _visiting_chat(tmp_path) as (layout, url, chat, command, _requests):
        client = chat()
        command(url, "stop")
        assert client.wait(timeout=25) == 1
        output = (tmp_path / "chat-0.log").read_text()
        assert "retention lease was lost" in output
        assert "Traceback" not in output
        status = AgentProcess(layout).status(ui_base_url="")
        assert status is not None and status.status == "stopped"


def test_lease_loss_ends_a_terminal_client_without_relaunch(tmp_path):
    with _visiting_chat(tmp_path) as (layout, url, _chat, command, _requests):
        session = ChatTuiPtySession.start(
            "toolang.cli.toolang",
            "--root",
            tmp_path / "root",
            url,
            "chat",
            "--sandbox",
            "host",
        )
        try:
            session.wait_for("Describe your task", timeout=40)
            command(url, "stop")
            assert session.wait_for_exit(timeout=25) == 1
            assert "retention lease was lost" in session.output
            assert "Traceback" not in session.output
            status = AgentProcess(layout).status(ui_base_url="")
            assert status is not None and status.status == "stopped"
        finally:
            session.close()
