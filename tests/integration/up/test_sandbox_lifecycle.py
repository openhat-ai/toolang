from __future__ import annotations

import os
from pathlib import Path
import socket
import signal
import subprocess
import sys
import time
import json

import psutil

from toolang.plugin.sandboxes.host import HOST_LAUNCH_ENV
from urllib.request import urlopen

from toolang.common.layout import AgentLayout
from toolang.up.sandbox import SandboxState


def test_host_sandbox_start_health_and_stop(tmp_path: Path) -> None:
    root = tmp_path / "toolang"
    layout = AgentLayout.resident(root, "alice")
    layout.home.mkdir(parents=True)
    layout.program.write_text("# Agent alice\n", encoding="utf-8")
    port = _available_port()
    env = {**os.environ, "TOOLANG_ROOT": str(root)}
    base = (
        sys.executable,
        "-m",
        "toolang.cli.toolang",
        "--root",
        str(root),
    )

    try:
        started = subprocess.run(
            (*base, "start", "alice", "--sandbox", "host", "--port", str(port)),
            check=False,
            capture_output=True,
            text=True,
            env=env,
            timeout=40,
        )
        assert started.returncode == 0, started.stderr
        state = SandboxState.load(layout.sandbox_state)
        assert state is not None
        assert state.sandbox == "host"
        assert state.ref.meta["signal_scope"] == "process_group"
        argv = psutil.Process(int(state.ref.runtime_id)).cmdline()
        assert "toolang.cli.toolang" in argv
        assert "_serve" in argv
        info = subprocess.run(
            (*base, "info", "alice"), capture_output=True, text=True, timeout=20
        )
        assert info.returncode == 0, info.stderr
        assert "PID" in info.stdout and state.ref.runtime_id in info.stdout
        assert "running" in info.stdout and state.ref.endpoint in info.stdout
        with urlopen(f"http://localhost:{port}/healthz", timeout=2) as response:
            assert response.status == 200

        stopped = subprocess.run(
            (*base, "stop", "alice"),
            check=False,
            capture_output=True,
            text=True,
            env=env,
            timeout=10,
        )
        assert stopped.returncode == 0, stopped.stderr
        assert SandboxState.load(layout.sandbox_state) is None
    finally:
        state = SandboxState.load(layout.sandbox_state)
        if state is not None:
            subprocess.run(
                (*base, "stop", "alice", "--force"),
                check=False,
                capture_output=True,
                text=True,
                env=env,
                timeout=10,
            )


def _available_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as stream:
        stream.bind(("127.0.0.1", 0))
        return int(stream.getsockname()[1])


def test_direct_server_registers_and_stops_without_signalling_shell(
    tmp_path: Path,
) -> None:
    root = tmp_path / "toolang"
    layout = AgentLayout.resident(root, "alice")
    layout.home.mkdir(parents=True)
    layout.program.write_text("# Agent alice\n")
    base = (sys.executable, "-m", "toolang.cli.toolang", "--root", str(root))
    port = _available_port()
    env = {key: value for key, value in os.environ.items() if key != HOST_LAUNCH_ENV}
    with (tmp_path / "server.log").open("w+") as log:
        child = subprocess.Popen(
            (*base, "_serve", "alice", "--port", str(port)),
            stdout=log,
            stderr=log,
            env=env,
        )
        try:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if child.poll() is not None:
                    log.seek(0)
                    raise AssertionError(log.read())
                if layout.runtime_status.is_file():
                    report = json.loads(layout.runtime_status.read_text())
                    if report.get("status") == "running":
                        break
                time.sleep(0.05)
            else:
                raise AssertionError("direct server did not become ready")
            state = SandboxState.load(layout.sandbox_state)
            assert state is not None and state.ref.runtime_id == str(child.pid)
            assert state.ref.meta["signal_scope"] == "process"
            assert os.getpgid(child.pid) == os.getpgrp()
            stopped = subprocess.run(
                (*base, "stop", "alice"), capture_output=True, text=True, timeout=10
            )
            assert stopped.returncode == 0, stopped.stderr
            assert child.wait(timeout=5) in {0, -signal.SIGTERM}
            assert json.loads(layout.runtime_status.read_text())["status"] == "stopped"
            assert SandboxState.load(layout.sandbox_state) is None
        finally:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)


def test_unregistered_managed_child_exits_before_preparing_agent(
    tmp_path: Path,
) -> None:
    layout = AgentLayout.resident(tmp_path, "alice")
    layout.home.mkdir(parents=True)
    layout.program.write_text("# Agent alice\n")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "toolang.cli.toolang",
            "--root",
            str(tmp_path),
            "_serve",
            "alice",
            "--port",
            str(_available_port()),
        ],
        env={**os.environ, HOST_LAUNCH_ENV: "lost-launcher", "TOOLANG_SANDBOX": "host"},
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode != 0
    assert "did not register" in result.stderr
    assert not layout.sandbox_state.exists()
    assert not layout.runtime_status.exists()
    assert not layout.agent_state.exists()
