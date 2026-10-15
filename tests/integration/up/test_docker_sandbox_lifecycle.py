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

from toolang.up import source as agent_sources

from toolang.common.layout import AgentLayout
from toolang.up.records import SandboxState


pytestmark = pytest.mark.live_docker

_MODEL = "ollama/test-ollama:latest"
_MODEL_REPLY = "reply from the host model"
_ECHO_PROGRAM = """instruct echo_instruct: Return the input exactly.

agic echo(_: Text) -> Text:
  recall = none
  context = none
  instruct = echo_instruct
  user: {{_}}
"""
_AGENT_SOURCE = f"# Agent alice\n\n{_ECHO_PROGRAM}"


@pytest.mark.parametrize("promote", [False, True])
def test_visiting_chat_reclaims_or_explicitly_stops_an_automatic_container(
    tmp_path: Path, docker_wheel_dir: Path, local_model_port: int, promote: bool
) -> None:
    from toolang.plugin.sandboxes.docker._cli import docker_container_running
    from toolang.up.source import visiting_layout

    class Source(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"flow:\n  pass\n")

        def log_message(self, format: str, *args: object) -> None:
            pass

    root = tmp_path / "root"
    root.mkdir()
    (root / "config.toml").write_text(
        f'[teaming]\nenabled = false\n[default]\nmodel = "{_MODEL}"\n'
        '[allow]\nmodels = ["ollama/*"]\ntools = []\n'
    )
    source = ThreadingHTTPServer(("127.0.0.1", 0), Source)
    source_thread = threading.Thread(target=source.serve_forever, daemon=True)
    source_thread.start()
    selector = f"http://127.0.0.1:{source.server_port}/remote.too"
    layout = visiting_layout(selector, toolang_root=root)
    base = (sys.executable, "-m", "toolang.cli.toolang", "--root", str(root), selector)
    env = {
        **os.environ,
        "TOOLANG_ROOT": str(root),
        "TOOLANG_TMUX": "0",
        "OLLAMA_HOST": f"http://127.0.0.1:{local_model_port}",
        "LLAMA_CPP_HOST": f"http://127.0.0.1:{local_model_port}",
    }
    state = None
    with (tmp_path / "chat.log").open("w+") as log:
        client = subprocess.Popen(
            (*base, "chat", "--sandbox", "docker", "--dev", str(docker_wheel_dir)),
            env=env,
            stdin=subprocess.PIPE,
            stdout=log,
            stderr=log,
            text=True,
        )
        try:
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                log.seek(0)
                output = log.read()
                assert client.poll() is None, output
                if "> " in output:
                    break
                time.sleep(0.1)
            else:
                pytest.fail(output)
            state = SandboxState.load(layout.runtime_status)
            assert state is not None
            stage_dir = state.ref.meta["stage_dir"]
            assert isinstance(stage_dir, str)
            stage = Path(stage_dir)
            inspected = subprocess.run(
                ("docker", "inspect", state.ref.runtime_id),
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            )
            assert json.loads(inspected.stdout)[0]["HostConfig"]["AutoRemove"] is True
            if promote:
                started = subprocess.run(
                    (*base, "start"),
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                assert started.returncode == 0, started.stderr
                assert "drivers=chat lifetime=persistent" in started.stdout
            assert client.stdin is not None
            client.stdin.close()
            assert client.wait(timeout=15) == 0
            if promote:
                stopped = subprocess.run(
                    (*base, "stop"), env=env, capture_output=True, text=True, timeout=30
                )
                assert stopped.returncode == 0, stopped.stderr
                assert not stage.exists()
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                absent = subprocess.run(
                    ("docker", "inspect", state.ref.runtime_id),
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if absent.returncode != 0:
                    assert "no such object:" in absent.stderr.lower(), absent.stderr
                    break
                time.sleep(0.1)
            else:
                pytest.fail("automatic container was not removed")
            assert not docker_container_running(state.ref.runtime_id)
            assert layout.run_store.exists() and layout.runtime_log.exists()
        finally:
            if client.poll() is None:
                client.kill()
            client.wait(timeout=10)
            if state is None:
                state = SandboxState.load(layout.runtime_status)
            if state is not None:
                subprocess.run(
                    (*base, "stop", "--force"),
                    env=env,
                    capture_output=True,
                    timeout=30,
                )
            source.shutdown()
            source.server_close()
            source_thread.join(timeout=2)


@pytest.mark.parametrize("placement", ["roaming", "visiting"])
def test_docker_foreground_nonresident_uses_selected_root_and_cleans_reference(
    tmp_path: Path, docker_wheel_dir: Path, local_model_port: int, placement: str
) -> None:
    from toolang.up.source import visiting_layout
    from toolang.up.records import read_record

    program = "flow complete:\n  let result = Done\n"

    class SourceHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            data = program.encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *args: Any) -> None:
            pass

    root = tmp_path / "selected"
    source_server = ThreadingHTTPServer(("127.0.0.1", 0), SourceHandler)
    source_thread = threading.Thread(target=source_server.serve_forever, daemon=True)
    source_thread.start()
    if placement == "roaming":
        source = tmp_path / "local.too"
        source.write_text(program)
        selector = str(source)
        layout = AgentLayout.roaming(source)
    else:
        selector = f"http://127.0.0.1:{source_server.server_port}/remote.too"
        layout = visiting_layout(selector, toolang_root=root)
    layout.home.mkdir(parents=True, exist_ok=True)
    _write_local_model_env(layout.root_env, local_model_port)
    port = _available_port()
    env = {**os.environ, "TOOLANG_ROOT": str(root)}
    base = (sys.executable, "-m", "toolang.cli.toolang")
    container_id = None
    with (tmp_path / "foreground.log").open("w+") as log:
        worker = subprocess.Popen(
            (
                *base,
                selector,
                "serve",
                "--sandbox",
                "docker",
                "--dev",
                str(docker_wheel_dir),
                "--port",
                str(port),
            ),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
        )
        try:
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                state = SandboxState.load(layout.runtime_status)
                if state is not None:
                    container_id = state.ref.runtime_id
                if state is not None and _endpoint_ready(state.ref.endpoint):
                    break
                if worker.poll() is not None:
                    log.seek(0)
                    pytest.fail(log.read())
                time.sleep(0.1)
            else:
                pytest.fail("nonresident guest did not become ready")
            entry = read_record(layout.runtime_status)
            assert entry is not None and entry["control"]["placement"] == placement
            assert entry["control"]["toolang_root"] == str(root)
            assert entry["control"]["source_id"] == layout.source_id
            references = tuple((root / ".runtime" / "instances").glob("*.json"))
            assert len(references) == 1
            assert (
                references[0].name
                == f"{layout.name}@{placement[0]}{layout.source_id}.json"
            )
            assert json.loads(references[0].read_text())["status"] == str(
                layout.runtime_status
            )
            observed = subprocess.run(
                (*base, "top", "--once"),
                env=env,
                capture_output=True,
                text=True,
                timeout=15,
            )
            assert observed.returncode == 0, observed.stderr
            assert layout.name in observed.stdout
            worker.send_signal(signal.SIGINT)
            assert worker.wait(timeout=30) == 130
            assert SandboxState.load(layout.runtime_status) is None
            assert not tuple((root / ".runtime" / "instances").glob("*.json"))
            assert not (layout.root_runtime / "instances").exists()
            assert not layout.previous_sandbox_state.exists()
            assert layout.runtime_status.exists() and layout.run_store.exists()
        finally:
            if worker.poll() is None:
                worker.send_signal(signal.SIGINT)
                try:
                    worker.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    worker.kill()
                    worker.wait(timeout=10)
            if container_id is not None:
                subprocess.run(
                    ("docker", "rm", "--force", container_id),
                    capture_output=True,
                    timeout=15,
                )
            source_server.shutdown()
            source_server.server_close()
            source_thread.join(timeout=2)


def test_docker_restart_reuses_settings_after_queries_reconcile_killed_guest(
    tmp_path: Path, docker_wheel_dir: Path, local_model_port: int
) -> None:
    from toolang.up.records import read_record

    layout = AgentLayout.resident(tmp_path / "toolang", "restart")
    layout.home.mkdir(parents=True)
    layout.program.write_text("flow complete:\n  let result = Done\n")
    layout.config.write_text("[teaming]\nenabled = false\n")
    _write_local_model_env(layout.root_env, local_model_port)
    env = {**os.environ, "TOOLANG_ROOT": str(layout.root)}
    base = (sys.executable, "-m", "toolang.cli.toolang", "--root", str(layout.root))
    selected_port = _available_port()
    containers: set[str] = set()

    def command(*args: str, expected: int = 0) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            (*base, *args), env=env, capture_output=True, text=True, timeout=180
        )
        assert result.returncode == expected, result.stdout + result.stderr
        return result

    try:
        command(
            "start",
            "restart",
            "--sandbox",
            "docker",
            "--dev",
            str(docker_wheel_dir),
            "--port",
            str(selected_port),
        )
        first = SandboxState.load(layout.runtime_status)
        assert first is not None
        containers.add(first.ref.runtime_id)
        entry = read_record(layout.runtime_status)
        assert entry is not None and first.ref.runtime_kind == "container"
        settings = entry["control"]["last_successful"]
        assert settings["sandbox"] == "docker"
        assert settings["endpoint"] == f"http://localhost:{selected_port}"
        assert not layout.previous_sandbox_state.exists()

        killed = subprocess.run(
            ("docker", "kill", first.ref.runtime_id),
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert killed.returncode == 0, killed.stderr
        command("list")
        entry = read_record(layout.runtime_status)
        assert entry is not None and entry["workload"]["status"] == "stopped"
        assert entry["workload"]["pid"] is None
        unchanged = layout.runtime_status.stat().st_mtime_ns
        command("info", "restart")
        command("top", "--once")
        assert layout.runtime_status.stat().st_mtime_ns == unchanged
        assert SandboxState.load(layout.runtime_status) == first
        assert (
            subprocess.run(
                ("docker", "inspect", first.ref.runtime_id),
                capture_output=True,
                timeout=10,
            ).returncode
            == 0
        ), "queries must retain resources for control-side release"

        command("start", "restart", "--dev", str(docker_wheel_dir))
        second = SandboxState.load(layout.runtime_status)
        assert second is not None
        containers.add(second.ref.runtime_id)
        assert second.ref.runtime_id != first.ref.runtime_id
        assert second.launch_id != first.launch_id
        assert second.sandbox == first.sandbox
        assert second.ref.endpoint == first.ref.endpoint
        assert (
            subprocess.run(
                ("docker", "inspect", first.ref.runtime_id),
                capture_output=True,
                timeout=10,
            ).returncode
            != 0
        ), "restart must release the previous container"
        command("stop", "restart")
        repeated = command("stop", "restart", expected=1)
        assert "not running" in repeated.stderr
        entry = read_record(layout.runtime_status)
        assert entry is not None and entry["workload"]["status"] == "stopped"
        assert entry["control"]["last_successful"] == settings
        assert not entry["control"].get("ref")
        assert not tuple((layout.root_runtime / "instances").glob("*.json"))
        assert layout.run_store.exists()
    finally:
        subprocess.run(
            (*base, "stop", "restart", "--force"),
            env=env,
            capture_output=True,
            timeout=30,
        )
        for container in containers:
            subprocess.run(
                ("docker", "rm", "--force", container),
                capture_output=True,
                timeout=15,
            )


def test_killed_temporary_controller_is_recovered_by_next_control_operation(
    tmp_path, docker_wheel_dir, local_model_port
):
    from dataclasses import replace
    from toolang.up import sandbox
    from toolang.up.records import read_record

    source = tmp_path / "temporary.too"
    source.write_text("flow complete:\n  let result = Done\n")
    layout = agent_sources.materialize_roaming_program(source)
    old = replace(layout, instance="abandoned")
    code = """
from pathlib import Path
from dataclasses import replace
import sys, time
from toolang.up.source import materialize_roaming_program
from toolang.cli.common.agent_server import acquire_agent_server
layout = replace(materialize_roaming_program(Path(sys.argv[1])), instance='abandoned')
with acquire_agent_server(layout, sandbox='docker', dev=Path(sys.argv[2]), temporary=True, show_progress=False):
    print('ready', flush=True)
    time.sleep(60)
"""
    env = {**os.environ, "OLLAMA_HOST": f"http://localhost:{local_model_port}"}
    worker = subprocess.Popen(
        (sys.executable, "-c", code, str(source), str(docker_wheel_dir)),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    state = None
    try:
        assert worker.stdout is not None and worker.stdout.readline().strip() == "ready"
        state = SandboxState.load(layout.runtime_status, instance=old.instance)
        assert state is not None
        worker.kill()
        worker.wait(timeout=5)
        entry = read_record(layout.runtime_status, old.instance)
        assert entry is not None and entry["control"]["owner"]["pid"] == worker.pid
        assert (
            subprocess.run(
                (
                    "docker",
                    "inspect",
                    "--format",
                    "{{.State.Running}}",
                    state.ref.runtime_id,
                ),
                capture_output=True,
                text=True,
                timeout=10,
            ).stdout.strip()
            == "true"
        )
        import asyncio

        asyncio.run(sandbox.release_stopped(replace(layout, instance="next")))
        assert read_record(layout.runtime_status, old.instance) is None
        assert (
            subprocess.run(
                ("docker", "inspect", state.ref.runtime_id),
                capture_output=True,
                timeout=10,
            ).returncode
            != 0
        )
        assert old.run_store.exists()
    finally:
        if worker.poll() is None:
            worker.kill()
            worker.wait(timeout=5)
        if state is not None:
            subprocess.run(
                ("docker", "rm", "--force", state.ref.runtime_id),
                capture_output=True,
                timeout=10,
            )


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
        state = SandboxState.load(layout.runtime_status)
        assert state is not None
        container_id = state.ref.runtime_id
        yield _RunningDockerAgent(
            layout=layout,
            endpoint=endpoint,
            container_id=container_id,
            started=started,
        )
    finally:
        if SandboxState.load(layout.runtime_status) is not None:
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
        assert SandboxState.load(layout.runtime_status) is None
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
    state = SandboxState.load(agent.layout.runtime_status)

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
        (item["provider"], item["ref"].partition("/")[2])
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
        "--model",
        _MODEL,
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
            "--model",
            _MODEL,
            "--quiet",
            "--out",
            "-",
            "hello from script",
        ),
        cwd=_repository(),
        env={
            **os.environ,
            "OLLAMA_HOST": f"http://127.0.0.1:{local_model_port}",
            "LLAMA_CPP_HOST": f"http://localhost:{local_model_port}",
        },
        check=False,
        capture_output=True,
        text=True,
        timeout=180,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == _MODEL_REPLY
    assert SandboxState.load(layout.runtime_status) is None


def test_docker_agent_run_interrupt_stops_and_removes_container(
    docker_wheel_dir: Path,
    tmp_path: Path,
) -> None:
    _require_docker()
    root = tmp_path / "toolang"
    layout = AgentLayout.resident(root, "bob")
    layout.home.mkdir(parents=True)
    layout.program.write_text("# Agent bob\n", encoding="utf-8")
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
            "serve",
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
            state = SandboxState.load(layout.runtime_status)
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
        assert SandboxState.load(layout.runtime_status) is None
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
        if SandboxState.load(layout.runtime_status) is not None:
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
        {"client_id": "test-script"},
    )
    thread = cast(dict[str, Any], created["thread"])
    run_id, stream = _post_stream(
        f"{agent.endpoint}/api/v1/runs/authored/stream",
        {
            "thread_id": thread["id"],
            "request_id": "live_docker_seed",
            "runnable": {"ref": "agic:echo", "input": {"_": "seed run"}},
            "model": {"ref": _MODEL},
            "policy": {"allow": [], "limits": {}},
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
    state = SandboxState.load(agent.layout.runtime_status)
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


def test_guest_status_updates_share_host_lock_and_preserve_control_and_ownership(
    running_docker_agent: _RunningDockerAgent,
) -> None:
    from toolang.common.files import file_lock_path
    from toolang.up.records import read_record, update_record, runtime_lock

    agent = running_docker_agent
    status = agent.layout.runtime_status
    marker = status.parent / "mount-test.marker"
    lock = file_lock_path(status)
    ownership = (status.stat().st_uid, status.stat().st_gid, status.stat().st_mode)
    assert not lock.exists()
    script = """
from pathlib import Path
from toolang.up.records import read_record, patch_workload
status = Path('/root/.toolang/agents/alice/.runtime/status.json')
entry = read_record(status, location='guest')
status.with_name('mount-test.marker').write_text('waiting')
assert patch_workload(status, entry['launch_id'], {'message': 'mount-test'}, location='guest')
"""
    worker = None
    try:
        with runtime_lock(status):
            worker = subprocess.Popen(
                (
                    "docker",
                    "exec",
                    agent.container_id,
                    "/tmp/toolang-runtime/tools/toolang/bin/python",
                    "-c",
                    script,
                ),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            deadline = time.monotonic() + 5
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            assert marker.exists(), "guest writer did not reach the shared record"
            time.sleep(0.1)
            assert worker.poll() is None, "guest writer did not wait for the host"
            entry = read_record(status)
            assert (
                entry is not None and entry["workload"].get("message") != "mount-test"
            )
            update_record(
                status, lambda entry: entry["control"].update(mount_test=True)
            )
        _, stderr = worker.communicate(timeout=10)
        assert worker.returncode == 0, stderr
        entry = read_record(status)
        assert entry is not None and entry["control"]["mount_test"] is True
        assert entry["workload"]["message"] == "mount-test"
        assert (
            status.stat().st_uid,
            status.stat().st_gid,
            status.stat().st_mode,
        ) == ownership
        assert not lock.exists()
        # Alternate differently sized replacements in both kernels. Reading a
        # cached canonical name on Docker Desktop can truncate valid new JSON.
        rounds_script = """
import json, sys
from pathlib import Path
from toolang.up.records import read_record, patch_workload
status = Path('/root/.toolang/agents/alice/.runtime/status.json')
for line in sys.stdin:
    round = int(line)
    entry = read_record(status, location='guest')
    assert entry['control']['mount_round'] == round
    assert patch_workload(status, entry['launch_id'], {'mount_round': round, 'padding': 'g' * (3072 + round % 50 * 61)}, location='guest')
    assert read_record(status, location='guest')['control']['mount_round'] == round
    print(round, flush=True)
"""
        worker = subprocess.Popen(
            (
                "docker",
                "exec",
                "-i",
                agent.container_id,
                "/tmp/toolang-runtime/tools/toolang/bin/python",
                "-u",
                "-c",
                rounds_script,
            ),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert worker.stdin is not None and worker.stdout is not None
        for round in range(200):
            update_record(
                status,
                lambda entry: entry["control"].update(
                    mount_round=round, padding="h" * (4096 + round % 50 * 35)
                ),
            )
            worker.stdin.write(f"{round}\n")
            worker.stdin.flush()
            assert worker.stdout.readline().strip() == str(round)
            entry = read_record(status)
            assert entry is not None
            assert entry["control"]["mount_round"] == round
            assert entry["workload"]["mount_round"] == round
        worker.stdin.close()
        worker.stdin = None
        _, stderr = worker.communicate(timeout=10)
        assert worker.returncode == 0, stderr
    finally:
        if worker is not None and worker.poll() is None:
            worker.kill()
            worker.wait(timeout=5)
        marker.unlink(missing_ok=True)

        def clear(entry):
            for key in ("mount_test", "mount_round", "padding"):
                entry["control"].pop(key, None)
            for key in ("message", "mount_round", "padding"):
                entry["workload"].pop(key, None)

        update_record(status, clear)


def test_killed_guest_writer_claim_is_recovered_by_query(
    running_docker_agent: _RunningDockerAgent,
) -> None:
    from toolang.common.files import file_lock_path
    from toolang.up.records import read_record

    agent = running_docker_agent
    status = agent.layout.runtime_status
    marker = status.parent / "killed-writer.marker"
    script = """
from pathlib import Path
import time
from toolang.up.records import runtime_lock, read_record
status = Path('/root/.toolang/agents/alice/.runtime/status.json')
entry = read_record(status)
with runtime_lock(status, location='guest', launch_id=entry['launch_id']):
    status.with_name('killed-writer.marker').write_text('owned')
    time.sleep(60)
"""
    worker = subprocess.Popen(
        (
            "docker",
            "exec",
            agent.container_id,
            "/tmp/toolang-runtime/tools/toolang/bin/python",
            "-c",
            script,
        ),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert marker.exists()
        killed = subprocess.run(
            ("docker", "kill", agent.container_id), capture_output=True, timeout=10
        )
        assert killed.returncode == 0, killed.stderr
        worker.communicate(timeout=5)
        listed = subprocess.run(
            (
                sys.executable,
                "-m",
                "toolang.cli.toolang",
                "--root",
                str(agent.layout.root),
                "list",
            ),
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert listed.returncode == 0, listed.stderr
        entry = read_record(status)
        assert entry is not None and entry["workload"]["status"] == "stopped"
        assert not file_lock_path(status).exists()
    finally:
        if worker.poll() is None:
            worker.kill()
            worker.wait(timeout=5)
        marker.unlink(missing_ok=True)
