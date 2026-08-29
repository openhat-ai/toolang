"""Opt-in Docker sandbox bootstrap and lifecycle checks.

Run explicitly with:

    TOOLANG_LIVE_DOCKER=1 uv run pytest -q \
      tests/system/cli/test_docker_sandbox_live.py
"""

from __future__ import annotations

import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time

import pytest

from toolang.common.layout import AgentLayout
from toolang.up.records import SandboxState


pytestmark = pytest.mark.skipif(
    os.environ.get("TOOLANG_LIVE_DOCKER") != "1",
    reason="set TOOLANG_LIVE_DOCKER=1 to run Docker lifecycle checks",
)


@pytest.fixture(scope="module")
def toolang_wheel(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build one current wheel for the Docker guest checks."""

    available = subprocess.run(
        ("docker", "info"),
        check=False,
        capture_output=True,
        text=True,
    )
    if available.returncode != 0:
        pytest.skip(available.stderr.strip() or "Docker Engine is unavailable")
    destination = tmp_path_factory.mktemp("docker-wheel")
    subprocess.run(
        ("uv", "build", "--wheel", "--out-dir", str(destination)),
        check=True,
    )
    wheels = sorted(destination.glob("toolang-*.whl"))
    assert len(wheels) == 1
    return wheels[0]


def test_docker_background_start_detaches_and_preserves_logs(
    tmp_path: Path,
    toolang_wheel: Path,
) -> None:
    root = tmp_path / "root"
    layout = _create_agent(root)
    port = _available_port()

    started = _run_cli(
        root,
        "start",
        "alice",
        "--sandbox",
        "docker",
        "--dev",
        str(toolang_wheel),
        "--port",
        str(port),
    )

    assert started.returncode == 0, started.stderr
    assert _ordered(
        started.stderr,
        "Preparing sandbox",
        "Starting workload",
        "Installing Toolang...",
        "Installed Toolang ·",
        "Starting agent...",
        "Agent started",
    )
    state = SandboxState.load(layout.sandbox_state)
    assert state is not None
    assert _container_running(state.ref.runtime_id)
    assert not tuple(layout.runtime.glob("sandbox-bootstrap-*.log"))

    stopped = _run_cli(root, "stop", "alice")

    assert stopped.returncode == 0, stopped.stderr
    assert SandboxState.load(layout.sandbox_state) is None
    assert not _container_exists(state.ref.runtime_id)
    assert "Starting agent..." in layout.runtime_log.read_text(encoding="utf-8")


def test_docker_foreground_interrupt_releases_the_container(
    tmp_path: Path,
    toolang_wheel: Path,
) -> None:
    root = tmp_path / "root"
    layout = _create_agent(root)
    output_path = tmp_path / "run.log"
    port = _available_port()
    command = _cli_command(
        root,
        "run",
        "alice",
        "--sandbox",
        "docker",
        "--dev",
        str(toolang_wheel),
        "--port",
        str(port),
    )
    with output_path.open("w+b") as output:
        process = subprocess.Popen(command, stdout=output, stderr=subprocess.STDOUT)
        try:
            _wait_for_output(process, output_path, "Running agent alice:", timeout=90)
            state = SandboxState.load(layout.sandbox_state)
            assert state is not None
            process.send_signal(signal.SIGINT)
            assert process.wait(timeout=30) == 130
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=10)
            remaining = SandboxState.load(layout.sandbox_state)
            if remaining is not None:
                _run_cli(root, "stop", "alice", "--force")

    assert SandboxState.load(layout.sandbox_state) is None
    assert not _container_exists(state.ref.runtime_id)


def _create_agent(root: Path) -> AgentLayout:
    layout = AgentLayout.resident(root, "alice")
    layout.home.mkdir(parents=True)
    layout.program.write_text("agent alice\n", encoding="utf-8")
    return layout


def _run_cli(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        _cli_command(root, *args),
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _cli_command(root: Path, *args: str) -> tuple[str, ...]:
    return (
        sys.executable,
        "-m",
        "toolang.cli.toolang.main",
        "--root",
        str(root),
        *args,
    )


def _available_port() -> int:
    with socket.socket() as selected:
        selected.bind(("127.0.0.1", 0))
        return int(selected.getsockname()[1])


def _ordered(text: str, *values: str) -> bool:
    positions = tuple(text.index(value) for value in values)
    return positions == tuple(sorted(positions))


def _container_running(container_id: str) -> bool:
    inspected = subprocess.run(
        ("docker", "inspect", "--format", "{{.State.Running}}", container_id),
        check=False,
        capture_output=True,
        text=True,
    )
    return inspected.returncode == 0 and inspected.stdout.strip() == "true"


def _container_exists(container_id: str) -> bool:
    inspected = subprocess.run(
        ("docker", "inspect", container_id),
        check=False,
        capture_output=True,
    )
    return inspected.returncode == 0


def _wait_for_output(
    process: subprocess.Popen[bytes],
    path: Path,
    expected: str,
    *,
    timeout: float,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        content = path.read_text(encoding="utf-8")
        if expected in content:
            return
        if process.poll() is not None:
            pytest.fail(f"Docker foreground run exited early:\n{content}")
        time.sleep(0.1)
    pytest.fail(f"Docker foreground run did not become ready:\n{path.read_text()}")
