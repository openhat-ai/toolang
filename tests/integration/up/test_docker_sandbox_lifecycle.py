"""Opt-in Docker checks for AgentServer and run-execution workflows.

Run with ``uv run pytest -m live_docker --live-docker``.
"""

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
from urllib.request import Request, urlopen

import pytest

from toolang.common.layout import AgentLayout
from toolang.up.sandbox import SandboxState


pytestmark = pytest.mark.live_docker

_MODEL = "ollama/test-ollama:latest"
_MODEL_REPLY = "reply from the host model"
_ECHO_PROGRAM = """\
agic echo(_: Text) -> Text:
  recall = none
  context: none
  instruct: Return the input exactly.
  user: {{_}}
"""
_AGENT_SOURCE = f"agent alice\n\n{_ECHO_PROGRAM}"


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
def local_model_port() -> Iterator[int]:
    provider = ThreadingHTTPServer(("0.0.0.0", 0), _LocalModelHandler)
    provider_thread = threading.Thread(target=provider.serve_forever, daemon=True)
    provider_thread.start()
    try:
        yield int(provider.server_address[1])
    finally:
        provider.shutdown()
        provider.server_close()
        provider_thread.join(timeout=2)


@pytest.fixture(scope="module")
def running_docker_agent(
    tmp_path_factory: pytest.TempPathFactory,
    docker_wheel_dir: Path,
    local_model_port: int,
) -> Iterator[_RunningDockerAgent]:
    workspace = tmp_path_factory.mktemp("docker-sandbox-lifecycle")
    root = workspace / "toolang"
    layout = AgentLayout.resident(root, "alice")
    layout.home.mkdir(parents=True)
    layout.program.write_text(_AGENT_SOURCE, encoding="utf-8")
    _write_local_model_env(layout.root_env, local_model_port)
    skill = layout.home / "skills" / "reviewer"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\ndescription: Review text\n---\nReview the supplied text.\n",
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


@pytest.fixture(scope="module")
def completed_docker_run(running_docker_agent: _RunningDockerAgent) -> str:
    return _create_authored_run(running_docker_agent)


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


def test_docker_agent_server_prepares_authored_caps(
    running_docker_agent: _RunningDockerAgent,
) -> None:
    skills = _get_json_list(f"{running_docker_agent.endpoint}/api/v1/skills")

    assert [(item["name"], item["description"]) for item in skills] == [
        ("reviewer", "Review text")
    ]


def test_docker_agent_server_reruns_on_the_existing_container(
    running_docker_agent: _RunningDockerAgent,
    completed_docker_run: str,
) -> None:
    result = _run_agent_command(
        running_docker_agent,
        "rerun",
        completed_docker_run,
        "--default",
        f"model={_MODEL}",
    )

    assert result.returncode == 0, result.stderr
    assert f"reran {completed_docker_run} as run_" in result.stdout
    assert result.stdout.rstrip().endswith(": succeeded")
    _assert_running_container_unchanged(running_docker_agent)


def test_docker_agent_server_retries_on_the_existing_container(
    running_docker_agent: _RunningDockerAgent,
    completed_docker_run: str,
) -> None:
    result = _run_agent_command(
        running_docker_agent,
        "retry",
        completed_docker_run,
        "--default",
        f"model={_MODEL}",
    )

    assert result.returncode == 0, result.stderr
    assert "retried run_" in result.stdout
    assert result.stdout.rstrip().endswith(": succeeded")
    _assert_running_container_unchanged(running_docker_agent)


def test_docker_script_runs_with_a_temporary_agent_server(
    docker_wheel_dir: Path,
    local_model_port: int,
    tmp_path: Path,
) -> None:
    source = tmp_path / "echo.too"
    source.write_text(_ECHO_PROGRAM, encoding="utf-8")
    layout = AgentLayout.roaming(source)
    _write_local_model_env(layout.root_env, local_model_port)

    result = subprocess.run(
        (
            sys.executable,
            "-m",
            "toolang.cli.toolang",
            str(source),
            "echo",
            "--sandbox",
            "docker",
            "--dev",
            str(docker_wheel_dir),
            "--default",
            f"model={_MODEL}",
            "--quiet",
            "--save",
            "-",
            "hello from script",
        ),
        cwd=_repository(),
        check=False,
        capture_output=True,
        text=True,
        timeout=180,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == _MODEL_REPLY
    assert SandboxState.load(layout.sandbox_state) is None


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
        length = int(self.headers.get("Content-Length", "0"))
        raw_payload = self.rfile.read(length)
        payload = json.loads(raw_payload) if raw_payload else {}
        if self.path == "/api/show":
            self._write_json(
                {
                    "capabilities": ["completion", "tools"],
                    "details": {"family": "test", "parameter_size": "1B"},
                    "model_info": {"test.context_length": 4096},
                }
            )
            return
        if self.path == "/v1/chat/completions":
            if isinstance(payload, dict) and payload.get("stream") is True:
                self._write_chat_stream()
            else:
                self._write_json(_chat_completion())
            return
        self.send_error(404)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        del format, args

    def _write_json(self, payload: object) -> None:
        content = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _write_chat_stream(self) -> None:
        chunks = (
            {
                "id": "chatcmpl-toolang-live-docker",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": "test-ollama:latest",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": _MODEL_REPLY},
                        "finish_reason": None,
                    }
                ],
            },
            {
                "id": "chatcmpl-toolang-live-docker",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": "test-ollama:latest",
                "choices": [
                    {
                        "index": 0,
                        "delta": {},
                        "finish_reason": "stop",
                    }
                ],
            },
        )
        content = (
            b"".join(f"data: {json.dumps(chunk)}\n\n".encode() for chunk in chunks)
            + b"data: [DONE]\n\n"
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)


def _chat_completion() -> dict[str, object]:
    return {
        "id": "chatcmpl-toolang-live-docker",
        "object": "chat.completion",
        "created": 0,
        "model": "test-ollama:latest",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": _MODEL_REPLY},
                "finish_reason": "stop",
            }
        ],
    }


def _create_authored_run(agent: _RunningDockerAgent) -> str:
    created = _post_json(
        f"{agent.endpoint}/api/v1/threads",
        {"client": "script"},
    )
    thread = cast(dict[str, Any], created["thread"])
    run_id, stream = _post_stream(
        f"{agent.endpoint}/api/v1/runs/authored/stream",
        {
            "thread": thread["id"],
            "request_id": "live_docker_seed",
            "commands": [{"group": "default", "field": "model", "value": _MODEL}],
            "input": {"primary": "seed run"},
            "runnable_fallbacks": ["agic:echo"],
        },
    )
    detail = _get_json(f"{agent.endpoint}/api/v1/runs/{run_id}")

    assert b"run_end" in stream
    assert detail["status"] == "succeeded"
    return run_id


def _run_agent_command(
    agent: _RunningDockerAgent,
    *arguments: str,
) -> subprocess.CompletedProcess[str]:
    environ = dict(os.environ)
    environ.pop("OLLAMA_HOST", None)
    environ.pop("LLAMA_CPP_HOST", None)
    environ["TOOLANG_ROOT"] = str(agent.layout.root)
    return subprocess.run(
        (
            sys.executable,
            "-m",
            "toolang.cli.toolang",
            "--root",
            str(agent.layout.root),
            agent.layout.name,
            *arguments,
        ),
        cwd=_repository(),
        check=False,
        capture_output=True,
        text=True,
        env=environ,
        timeout=60,
    )


def _assert_running_container_unchanged(agent: _RunningDockerAgent) -> None:
    state = SandboxState.load(agent.layout.sandbox_state)
    assert state is not None
    assert state.ref.runtime_id == agent.container_id
    inspected = subprocess.run(
        (
            "docker",
            "inspect",
            "--format",
            "{{.State.Running}}",
            agent.container_id,
        ),
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert inspected.returncode == 0, inspected.stderr
    assert inspected.stdout.strip() == "true"


def _post_json(url: str, payload: object) -> dict[str, Any]:
    request = _json_request(url, payload)
    with urlopen(request, timeout=30) as response:
        assert response.status in {200, 201}
        result = json.load(response)
    assert isinstance(result, dict)
    return cast(dict[str, Any], result)


def _post_stream(url: str, payload: object) -> tuple[str, bytes]:
    request = _json_request(url, payload)
    with urlopen(request, timeout=30) as response:
        assert response.status == 200
        run_id = response.headers.get("X-Toolang-Run-ID")
        content = response.read()
    assert run_id is not None
    return run_id, content


def _json_request(url: str, payload: object) -> Request:
    return Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )


def _get_json(url: str) -> dict[str, Any]:
    with urlopen(url, timeout=2) as response:
        assert response.status == 200
        payload = json.load(response)
    assert isinstance(payload, dict)
    return cast(dict[str, Any], payload)


def _get_json_list(url: str) -> list[dict[str, Any]]:
    with urlopen(url, timeout=2) as response:
        assert response.status == 200
        payload = json.load(response)
    assert isinstance(payload, list)
    assert all(isinstance(item, dict) for item in payload)
    return cast(list[dict[str, Any]], payload)


def _write_local_model_env(path: Path, port: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"OLLAMA_HOST=http://127.0.0.1:{port}\n"
        f"LLAMA_CPP_HOST=http://localhost:{port}\n",
        encoding="utf-8",
    )


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
