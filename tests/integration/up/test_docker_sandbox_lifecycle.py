from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from typing import Any, cast
from urllib.request import urlopen

import pytest

from toolang.common.layout import AgentLayout
from toolang.up.sandbox import SandboxState


pytestmark = [
    pytest.mark.live_docker,
    pytest.mark.skipif(
        os.environ.get("TOOLANG_TEST_DOCKER") != "1",
        reason="set TOOLANG_TEST_DOCKER=1 to run live Docker sandbox checks",
    ),
]


@dataclass(frozen=True, slots=True)
class _RunningDockerAgent:
    layout: AgentLayout
    endpoint: str
    container_id: str
    started: subprocess.CompletedProcess[str]


@pytest.fixture(scope="module")
def docker_wheel_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    _require_docker()
    workspace = tmp_path_factory.mktemp("docker-sandbox-wheel")
    wheel_dir = workspace / "wheel"
    built = subprocess.run(
        ("uv", "build", "--wheel", "--out-dir", str(wheel_dir)),
        cwd=_repository(),
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert built.returncode == 0, built.stderr
    return wheel_dir


@pytest.fixture(scope="module")
def running_docker_agent(
    tmp_path_factory: pytest.TempPathFactory,
    docker_wheel_dir: Path,
) -> Iterator[_RunningDockerAgent]:
    workspace = tmp_path_factory.mktemp("docker-sandbox-lifecycle")
    root = workspace / "toolang"
    layout = AgentLayout.resident(root, "alice")
    layout.home.mkdir(parents=True)
    layout.program.write_text("agent alice\n", encoding="utf-8")
    provider_port = _available_port()
    layout.root_env.write_text(
        f"OLLAMA_HOST=http://127.0.0.1:{provider_port}\n"
        f"LLAMA_CPP_HOST=http://localhost:{provider_port}\n",
        encoding="utf-8",
    )
    provider = ThreadingHTTPServer(
        ("0.0.0.0", provider_port),
        _LocalModelHandler,
    )
    provider_thread = threading.Thread(target=provider.serve_forever, daemon=True)
    provider_thread.start()

    agent_port = _available_port()
    endpoint = f"http://localhost:{agent_port}"
    environ = dict(os.environ)
    environ.pop("OLLAMA_HOST", None)
    environ.pop("LLAMA_CPP_HOST", None)
    environ["TOOLANG_ROOT"] = str(root)
    base = (
        sys.executable,
        "-m",
        "toolang.cli.toolang",
        "--root",
        str(root),
        "alice",
    )

    started: subprocess.CompletedProcess[str] | None = None
    container_id: str | None = None
    try:
        started = subprocess.run(
            (
                *base,
                "start",
                "--sandbox",
                "docker",
                "--dev",
                str(docker_wheel_dir),
                "--port",
                str(agent_port),
            ),
            cwd=_repository(),
            check=False,
            capture_output=True,
            text=True,
            env=environ,
            timeout=180,
        )
        assert started.returncode == 0, started.stderr
        state = SandboxState.load(layout.sandbox_state)
        assert state is not None
        container_id = state.ref.runtime_id
        yield _RunningDockerAgent(
            layout=layout,
            endpoint=endpoint,
            container_id=container_id,
            started=started,
        )
    finally:
        try:
            if SandboxState.load(layout.sandbox_state) is not None:
                stopped = subprocess.run(
                    (*base, "stop", "--force"),
                    cwd=_repository(),
                    check=False,
                    capture_output=True,
                    text=True,
                    env=environ,
                    timeout=30,
                )
                assert stopped.returncode == 0, stopped.stderr
            assert SandboxState.load(layout.sandbox_state) is None
            if container_id is not None:
                inspected = subprocess.run(
                    ("docker", "inspect", container_id),
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                assert inspected.returncode != 0
        finally:
            provider.shutdown()
            provider.server_close()
            provider_thread.join(timeout=2)


def test_docker_agent_server_start_exposes_canonical_runtime_identity(
    running_docker_agent: _RunningDockerAgent,
) -> None:
    agent = running_docker_agent

    with urlopen(f"{agent.endpoint}/healthz", timeout=2) as response:
        assert response.status == 200
    profile = _get_json(f"{agent.endpoint}/api/v1/profile")
    state = SandboxState.load(agent.layout.sandbox_state)

    assert state is not None
    assert state.sandbox == "docker:python:3.13-slim"
    assert re.fullmatch(r"[0-9a-f]{64}", agent.container_id)
    assert state.ref.runtime_id == agent.container_id
    assert profile["runtime"]["sandbox"]["driver"] == "docker"
    assert profile["runtime"]["sandbox"]["selector"] == ("docker:python:3.13-slim")
    assert profile["runtime"]["sandbox"]["instance"] == agent.container_id[:12]
    assert "Agent alice started:" in agent.started.stdout
    assert "Connected to the agent API" in agent.started.stderr


def test_docker_agent_server_discovers_host_ollama_and_llama_cpp(
    running_docker_agent: _RunningDockerAgent,
) -> None:
    payload = _get_json(f"{running_docker_agent.endpoint}/api/v1/models")
    discovered = {
        (item["provider"], item["model"])
        for item in payload["items"]
        if item["provider"] in {"ollama", "llama_cpp"}
    }

    assert discovered == {
        ("ollama", "test-ollama:latest"),
        ("llama_cpp", "test-llama"),
    }


def test_docker_agent_run_interrupt_stops_and_removes_container(
    docker_wheel_dir: Path,
    tmp_path: Path,
) -> None:
    _require_docker()
    root = tmp_path / "toolang"
    layout = AgentLayout.resident(root, "bob")
    layout.home.mkdir(parents=True)
    layout.program.write_text("agent bob\n", encoding="utf-8")
    unavailable_provider_port = _available_port()
    layout.root_env.write_text(
        f"OLLAMA_HOST=http://127.0.0.1:{unavailable_provider_port}\n"
        f"LLAMA_CPP_HOST=http://localhost:{unavailable_provider_port}\n",
        encoding="utf-8",
    )
    agent_port = _available_port()
    endpoint = f"http://localhost:{agent_port}"
    environ = dict(os.environ)
    environ.pop("OLLAMA_HOST", None)
    environ.pop("LLAMA_CPP_HOST", None)
    environ["TOOLANG_ROOT"] = str(root)
    base = (
        sys.executable,
        "-m",
        "toolang.cli.toolang",
        "--root",
        str(root),
        "bob",
    )
    process = subprocess.Popen(
        (
            *base,
            "run",
            "--sandbox",
            "docker",
            "--dev",
            str(docker_wheel_dir),
            "--port",
            str(agent_port),
        ),
        cwd=_repository(),
        env=environ,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    container_id: str | None = None
    try:
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            state = SandboxState.load(layout.sandbox_state)
            if state is not None and _endpoint_ready(endpoint):
                container_id = state.ref.runtime_id
                break
            if process.poll() is not None:
                stdout, stderr = process.communicate()
                pytest.fail(
                    f"foreground agent exited before readiness\n{stdout}\n{stderr}"
                )
            time.sleep(0.1)
        assert container_id is not None, "foreground agent did not become ready"

        process.send_signal(signal.SIGINT)
        stdout, stderr = process.communicate(timeout=30)

        assert process.returncode == 130, (stdout, stderr)
        assert SandboxState.load(layout.sandbox_state) is None
        inspected = subprocess.run(
            ("docker", "inspect", container_id),
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert inspected.returncode != 0
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGINT)
            try:
                process.communicate(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=10)
        if SandboxState.load(layout.sandbox_state) is not None:
            subprocess.run(
                (*base, "stop", "--force"),
                cwd=_repository(),
                check=False,
                capture_output=True,
                text=True,
                env=environ,
                timeout=30,
            )


class _LocalModelHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/api/tags":
            self._write_json(
                {
                    "models": [
                        {
                            "name": "test-ollama:latest",
                            "details": {
                                "family": "test",
                                "parameter_size": "1B",
                            },
                        }
                    ]
                }
            )
            return
        if self.path == "/v1/models":
            self._write_json(
                {
                    "data": [
                        {
                            "id": "test-llama",
                            "meta": {"n_ctx_train": 4096},
                        }
                    ]
                }
            )
            return
        if self.path == "/props":
            self._write_json(
                {
                    "model_path": "/models/test-llama.gguf",
                    "n_ctx": 4096,
                    "total_slots": 1,
                }
            )
            return
        self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/api/show":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        self._write_json(
            {
                "capabilities": ["completion", "tools"],
                "details": {"family": "test", "parameter_size": "1B"},
                "model_info": {"test.context_length": 4096},
            }
        )

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        del format, args

    def _write_json(self, payload: object) -> None:
        content = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)


def _get_json(url: str) -> dict[str, Any]:
    with urlopen(url, timeout=2) as response:
        assert response.status == 200
        payload = json.load(response)
    assert isinstance(payload, dict)
    return cast(dict[str, Any], payload)


def _available_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as stream:
        stream.bind(("127.0.0.1", 0))
        return int(stream.getsockname()[1])


def _endpoint_ready(endpoint: str) -> bool:
    try:
        with urlopen(f"{endpoint}/healthz", timeout=0.2) as response:
            return response.status == 200
    except OSError:
        return False


def _require_docker() -> None:
    if shutil.which("docker") is None:
        pytest.skip("Docker is unavailable")


def _repository() -> Path:
    return Path(__file__).parents[3]
