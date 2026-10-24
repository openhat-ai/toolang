"""Opt-in Docker checks for AgentServer and run-execution workflows.

Run with ``uv run pytest -m live_docker --live-docker``.
"""

from __future__ import annotations

from collections.abc import Generator, Iterator
from dataclasses import dataclass, field as dataclass_field
from contextlib import closing, contextmanager
import asyncio
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
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from toolang.up import source as agent_sources

from toolang.common.layout import AgentLayout
from toolang.up.instance import SandboxState


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
        f'[teaming]\nenabled = false\n[model]\nref = "{_MODEL}"\n'
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
                assert "Agent remote running:" in started.stdout
                from toolang.up.instance import read_instance

                promoted = read_instance(layout.runtime_status)
                assert promoted is not None
                assert promoted["workload"]["drivers"] == ["chat"]
                assert promoted["workload"]["lifetime"] == "persistent"
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
def test_nonresident_launch_uses_selected_root_environment_and_cleans_reference(
    tmp_path: Path, docker_wheel_dir: Path, local_model_port: int, placement: str
) -> None:
    from toolang.up.source import visiting_layout
    from toolang.up.instance import read_instance
    from toolang.common.environment import agent_environment_paths
    from toolang.up.allocation import allocate_roaming
    from toolang.up.identity import InstanceIdentity

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
        source = tmp_path / ("a" * 120) / ("b" * 120) / "local.too"
        source.parent.mkdir(parents=True)
        source.write_text(program)
        selector = str(source)
        layout = allocate_roaming(source, toolang_root=root, register=False)
        agent_sources.materialize_roaming_program(source, layout=layout)
    else:
        selector = (
            f"http://127.0.0.1:{source_server.server_port}/remote.too?version="
            + "a" * 300
        )
        layout = agent_sources.prepare_visiting_layout(
            visiting_layout(selector, toolang_root=root), toolang_root=root
        )
    layout.home.mkdir(parents=True, exist_ok=True)
    _write_local_model_env(layout.place_env, local_model_port)
    _write_local_model_env(agent_environment_paths(root, layout)[0], local_model_port)
    env = {**os.environ, "TOOLANG_ROOT": str(root)}
    base = (sys.executable, "-m", "toolang.cli.toolang")
    container_id = None
    identity = tmp_path / "identity.json"
    identity.write_text(json.dumps(InstanceIdentity.from_layout(layout).to_data()))
    controller = """
import json, signal, sys
from pathlib import Path
from toolang.cli.common.agent_server import acquire_agent_server
from toolang.up.identity import layout_from_control
layout = layout_from_control(json.loads(Path(sys.argv[1]).read_text()))
try:
    with acquire_agent_server(layout, sandbox='docker', dev=Path(sys.argv[2]), temporary=True, bound=True):
        signal.pause()
except KeyboardInterrupt:
    raise SystemExit(130)
"""
    with (tmp_path / "foreground.log").open("w+") as log:
        worker = subprocess.Popen(
            (sys.executable, "-c", controller, str(identity), str(docker_wheel_dir)),
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
            entry = read_instance(layout.runtime_status)
            assert entry is not None and entry["control"]["placement"] == placement
            assert entry["control"]["root"] == str(root)
            assert entry["control"]["source_id"] == layout.source_id
            references = tuple((root / ".runtime" / "active").glob("*/*.json"))
            assert len(references) == 1
            assert references[0].name == "info.json"
            assert references[0].parent.name == layout.address
            assert json.loads(references[0].read_text())["home"] == str(layout.home)
            observed = subprocess.run(
                (*base, "hub"),
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
            assert not tuple((root / ".runtime" / "active").glob("*/*.json"))
            assert not (layout.place_runtime / "instances").exists()
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
    from toolang.up.instance import read_instance

    layout = AgentLayout.resident(tmp_path / "toolang", "restart")
    layout.home.mkdir(parents=True)
    layout.program.write_text("flow complete:\n  let result = Done\n")
    layout.config.write_text("[teaming]\nenabled = false\n")
    _write_local_model_env(layout.place_env, local_model_port)
    env = {**os.environ, "TOOLANG_ROOT": str(layout.place)}
    base = (sys.executable, "-m", "toolang.cli.toolang", "--root", str(layout.place))
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
            "restart",
            "start",
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
        entry = read_instance(layout.runtime_status)
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
        entry = read_instance(layout.runtime_status)
        assert entry is not None and entry["workload"]["status"] == "stopped"
        assert entry["workload"]["pid"] is None
        unchanged = layout.runtime_status.stat().st_mtime_ns
        command("restart", "info")
        command("list")
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

        command("restart", "start", "--dev", str(docker_wheel_dir))
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
        command("restart", "stop")
        repeated = command("restart", "stop", expected=1)
        assert "not running" in repeated.stderr
        entry = read_instance(layout.runtime_status)
        assert entry is not None and entry["workload"]["status"] == "stopped"
        assert entry["control"]["last_successful"] == settings
        assert not entry["control"].get("ref")
        assert not tuple((layout.place_runtime / "instances").glob("*.json"))
        assert layout.run_store.exists()
    finally:
        subprocess.run(
            (*base, "restart", "stop", "--force"),
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
    tmp_path, docker_wheel_dir
):
    from toolang.up import sandbox
    from toolang.up.allocation import retained_roaming
    from toolang.execution.store import RunStore

    source = tmp_path / "temporary.too"
    source.write_text(
        "agic main() -> Text:\n  context = none\n  recall = none\n  user: WAIT.\n"
    )
    root = tmp_path / "root"
    scenario = _ScriptScenario(source, "model")
    home = None
    with _script_model(scenario):
        worker = _script_process(source, root, docker_wheel_dir)
        try:
            home, state = _wait_script_home(source, root, worker)
            assert scenario.entered.wait(60)
            worker.kill()
            worker.communicate(timeout=5)
            ended = subprocess.run(
                ("docker", "wait", state.ref.runtime_id),
                capture_output=True,
                text=True,
                timeout=15,
            )
            assert ended.returncode == 0, ended.stderr
            asyncio.run(sandbox.release_stopped(home))
            assert SandboxState.load(home.runtime_status) is None
            assert home.run_store.exists()
            scenario.resume.set()
            for _ in range(2):
                worker = _script_process(
                    source,
                    root,
                    docker_wheel_dir,
                    operand=f"{source}@{home.place_code}",
                )
                stdout, stderr = worker.communicate(timeout=180)
                assert worker.returncode == 0, (stdout, stderr)
                assert stdout == "done"
            with closing(RunStore(home.run_store, read_only=True)) as store:
                assert len(store.list_runs()) == 3
                assert {run.status for run in store.list_runs()} == {
                    "canceled",
                    "succeeded",
                }
            assert len(retained_roaming(source, toolang_root=root)) == 1
        finally:
            homes = retained_roaming(source, toolang_root=root)
            _clean_script(worker, home or (homes[0] if homes else None))


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


@pytest.mark.parametrize("kind", ["store", "state"])
def test_root_guest_runtime_data_retains_mounted_parent_ownership(
    docker_wheel_dir, kind
):
    """Exercise Linux ownership even on Docker Desktop's translated mounts."""
    from uuid import uuid4

    resource = f"toolang-test-store-owner-{uuid4().hex}"
    package = next(docker_wheel_dir.glob("*.whl"))
    bootstrap = _repository() / "src/toolang/plugin/sandboxes/docker"
    code = """
import os, subprocess, sys, tempfile
from pathlib import Path
from toolang.execution.store import RunStore
home = Path(tempfile.mkdtemp())
os.chown(home, 65534, 65534)
path = home / '.runtime' / 'runs.db'
store = RunStore(path)
try:
    for item in (path.parent, path, path.with_name('runs.db-wal'), path.with_name('runs.db-shm')):
        assert item.stat().st_uid == 65534, (item, item.stat())
    checked = subprocess.run(
        [sys.executable, '-c',
         'from pathlib import Path; import os, sys; from toolang.execution.store import RunStore; '
         'os.setgid(65534); os.setuid(65534); '
         'p=Path(sys.argv[1]); r=RunStore(p, read_only=True); r.list_runs(); r.close(); '
         'w=RunStore(p); w.list_runs(); w.close()', str(path)],
        capture_output=True, text=True, timeout=15,
    )
    assert checked.returncode == 0, checked.stdout + checked.stderr
finally:
    store.close()
"""
    if kind == "state":
        code = """
import os, subprocess, sys, tempfile
from pathlib import Path
from toolang.common.layout import AgentLayout, ensure_scratch_workspace
from toolang.state.prepare import prepare_agent_state
home = Path(tempfile.mkdtemp())
layout = AgentLayout.resident(home, 'alice')
layout.home.mkdir(parents=True)
layout.program.write_text('flow main():\\n  pass\\n')
ensure_scratch_workspace(layout.home)
for path in [home, *home.rglob('*')]:
    os.chown(path, 65534, 65534)
state = prepare_agent_state(layout)
for anchor in (layout.place_state, layout.home_state, layout.agent_state):
    for path in [anchor, *anchor.rglob('*')]:
        assert path.stat().st_uid == 65534, (path, path.stat())
checked = subprocess.run(
    [sys.executable, '-c',
     'import os, sys; from pathlib import Path; '
     'from toolang.common.layout import AgentLayout; '
     'from toolang.state.prepare import prepare_agent_state; '
     'layout=AgentLayout.resident(Path(sys.argv[1]), "alice"); '
     'prepare_agent_state(layout); os.setgid(65534); os.setuid(65534); '
     'prepare_agent_state(layout)', str(home)],
    capture_output=True, text=True, timeout=15,
)
assert checked.returncode == 0, checked.stdout + checked.stderr
"""
    try:
        result = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--name",
                resource,
                "--volume",
                f"{bootstrap}:/bootstrap:ro",
                "--volume",
                f"{docker_wheel_dir}:/packages:ro",
                "python:3.13-slim",
                "/bin/sh",
                "/bootstrap/guest.sh",
                "/tmp/diagnostic.log",
                "/tmp/diagnostic.log",
                "-",
                f"/packages/{package.name}",
                "-",
                "--",
                "/tmp/toolang-runtime/tools/toolang/bin/python",
                "-c",
                code,
            ],
            capture_output=True,
            text=True,
            timeout=180,
        )
        assert result.returncode == 0, result.stdout + result.stderr
    finally:
        subprocess.run(
            ["docker", "rm", "--force", resource],
            capture_output=True,
            timeout=15,
        )


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
    yield from _docker_agent(tmp_path_factory, docker_wheel_dir, local_model_port)


@pytest.fixture
def isolated_docker_agent(
    tmp_path_factory: pytest.TempPathFactory,
    docker_wheel_dir: Path,
    local_model_port: int,
) -> Iterator[_RunningDockerAgent]:
    yield from _docker_agent(tmp_path_factory, docker_wheel_dir, local_model_port)


def _docker_agent(
    tmp_path_factory: pytest.TempPathFactory,
    docker_wheel_dir: Path,
    local_model_port: int,
    *,
    image: str = "python:3.13-slim",
) -> Generator[_RunningDockerAgent, None, None]:
    workspace = tmp_path_factory.mktemp("docker-sandbox-lifecycle")
    root = workspace / "toolang"
    layout = AgentLayout.resident(root, "alice")
    layout.home.mkdir(parents=True)
    layout.program.write_text(_AGENT_SOURCE, encoding="utf-8")
    _write_local_model_env(layout.place_env, local_model_port)
    # Agent policy explicitly grants the root's local-provider endpoints.
    (root / ".alice.env").write_text("OLLAMA_HOST\nLLAMA_CPP_HOST\n")
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
                f"docker:{image}",
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


def test_rust_guest_shell_uses_image_toolchain_without_bootstrap_uv_leaks(
    tmp_path_factory, docker_wheel_dir, local_model_port
):
    agents = _docker_agent(
        tmp_path_factory, docker_wheel_dir, local_model_port, image="rust:1.75"
    )
    try:
        agent = next(agents)
        assert not agent.layout.env.exists()
        code = """
import asyncio, json, os
from pathlib import Path
from toolang.base.types.tool import ToolContext
from toolang.plugin.toolsets.shell import ShellToolset
environ = dict(item.split('=', 1) for item in Path('/proc/1/environ').read_bytes().decode().split('\\0') if item)
os.environ.clear()
os.environ.update(environ)
home = Path(environ['TOOLANG_ROOT']) / 'agents/alice'
assert (home.parent.parent / '.alice.env').is_file()
context = ToolContext(home=home, room=home, workspaces={'repo': home}, cwd='repo://')
tool = ShellToolset({}).tools()['execute']
result = asyncio.run(tool.invoke({'command': 'cargo --version && rustc --version'}, context))
assert result.error is None, result.error
assert result.output['exit_code'] == 0, result.output
print(json.dumps({'output': result.output['stdout'], 'cargo_home': environ.get('CARGO_HOME'), 'rustup_home': environ.get('RUSTUP_HOME'), 'uv': {name: value for name, value in environ.items() if name in ('UV_CACHE_DIR', 'UV_PYTHON_INSTALL_DIR', 'UV_PYTHON_BIN_DIR', 'UV_TOOL_DIR', 'UV_TOOL_BIN_DIR', 'UV_NO_PROGRESS')}}))
"""
        checked = subprocess.run(
            (
                "docker",
                "exec",
                agent.container_id,
                "/tmp/toolang-runtime/tools/toolang/bin/python",
                "-c",
                code,
            ),
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert checked.returncode == 0, checked.stderr
        result = json.loads(checked.stdout)
        assert result["output"].splitlines()[0].startswith("cargo 1.75.0 ")
        assert result["output"].splitlines()[1].startswith("rustc 1.75.0 ")
        assert result["cargo_home"] == "/usr/local/cargo"
        assert result["rustup_home"] == "/usr/local/rustup"
        assert result["uv"] == {}
        assert not agent.layout.env.exists()
    finally:
        agents.close()


def test_docker_agent_server_start_exposes_canonical_runtime_identity(
    running_docker_agent: _RunningDockerAgent,
) -> None:
    agent = running_docker_agent

    with urlopen(f"{agent.endpoint}/healthz", timeout=2) as response:
        assert response.status == 200
    profile = _get_json(f"{agent.endpoint}/api/v0/profile")
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
    payload = _get_json(f"{running_docker_agent.endpoint}/api/v0/models")
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
    skills = _get_json_list(f"{running_docker_agent.endpoint}/api/v0/skills")

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


def test_docker_agent_server_rejects_removed_interfaces_without_mutation(
    running_docker_agent: _RunningDockerAgent,
    completed_docker_run: str,
) -> None:
    url = f"{running_docker_agent.endpoint}/api/v0/runs/{completed_docker_run}"
    before = _get_json(url)
    with pytest.raises(HTTPError) as retry:
        _post_json(f"{url}/retry", {})
    assert retry.value.code == 404
    with pytest.raises(HTTPError) as activity:
        _get_json(f"{running_docker_agent.endpoint}/api/v0/activity")
    assert activity.value.code == 404
    result = _run_agent_command(
        running_docker_agent,
        "retry",
        completed_docker_run,
    )

    assert result.returncode != 0
    assert _get_json(url) == before
    _assert_running_container_unchanged(running_docker_agent)


def test_docker_script_runs_with_a_temporary_agent_server(
    docker_wheel_dir: Path,
    local_model_port: int,
    tmp_path: Path,
) -> None:
    from toolang.up.allocation import select_roaming

    source = tmp_path / "echo.too"
    source.write_text(_ECHO_PROGRAM, encoding="utf-8")
    _write_local_model_env(source.parent / ".toolang.env", local_model_port)

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
    layout = select_roaming(source, toolang_root=Path(os.environ["TOOLANG_ROOT"]))
    assert layout.run_store.is_file()
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
    layout.place_env.write_text(
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
        f"{agent.endpoint}/api/v0/threads",
        {"client_id": "test-script"},
    )
    thread = cast(dict[str, Any], created["thread"])
    run_id, stream = _post_stream(
        f"{agent.endpoint}/api/v0/runs/authored/stream",
        {
            "thread_id": thread["id"],
            "request_id": "live_docker_seed",
            "runnable": {"ref": "agic:echo", "input": {"_": "seed run"}},
            "model": {"ref": _MODEL},
            "policy": {"allow": [], "limits": {}},
        },
    )
    detail = _get_json(f"{agent.endpoint}/api/v0/runs/{run_id}")

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
    environ["TOOLANG_ROOT"] = str(agent.layout.place)
    return subprocess.run(
        (
            sys.executable,
            "-m",
            "toolang.cli.toolang",
            "--root",
            str(agent.layout.place),
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
    from toolang.up.instance import read_instance, update_instance, runtime_lock

    agent = running_docker_agent
    status = agent.layout.runtime_status
    marker = status.parent / "mount-test.marker"
    lock = file_lock_path(status)
    ownership = (status.stat().st_uid, status.stat().st_gid, status.stat().st_mode)
    assert not lock.exists()
    script = """
from pathlib import Path
from toolang.up.instance import read_instance, patch_workload
status = Path('/root/.toolang/agents/alice/.runtime/status.json')
entry = read_instance(status, location='guest')
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
            entry = read_instance(status)
            assert (
                entry is not None and entry["workload"].get("message") != "mount-test"
            )
            update_instance(
                status, lambda entry: entry["control"].update(mount_test=True)
            )
        _, stderr = worker.communicate(timeout=10)
        assert worker.returncode == 0, stderr
        entry = read_instance(status)
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
from toolang.up.instance import read_instance, patch_workload
status = Path('/root/.toolang/agents/alice/.runtime/status.json')
for line in sys.stdin:
    round = int(line)
    entry = read_instance(status, location='guest')
    assert entry['control']['mount_round'] == round
    assert patch_workload(status, entry['launch_id'], {'mount_round': round, 'padding': 'g' * (3072 + round % 50 * 61)}, location='guest')
    assert read_instance(status, location='guest')['control']['mount_round'] == round
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
            update_instance(
                status,
                lambda entry: entry["control"].update(
                    mount_round=round, padding="h" * (4096 + round % 50 * 35)
                ),
            )
            worker.stdin.write(f"{round}\n")
            worker.stdin.flush()
            assert worker.stdout.readline().strip() == str(round)
            entry = read_instance(status)
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

        update_instance(status, clear)


@pytest.mark.parametrize("terminal", [False, True])
def test_killed_guest_writer_claim_is_recovered_by_query(
    isolated_docker_agent: _RunningDockerAgent,
    terminal: bool,
) -> None:
    from toolang.common.files import file_lock_path
    from toolang.up.instance import read_instance

    agent = isolated_docker_agent
    status = agent.layout.runtime_status
    marker = status.parent / "killed-writer.marker"
    script = """
from pathlib import Path
import sys, time
from toolang.up.instance import runtime_lock, read_instance, update_instance
status = Path('/root/.toolang/agents/alice/.runtime/status.json')
entry = read_instance(status, location='guest')
with runtime_lock(status, location='guest', launch_id=entry['launch_id']):
    if sys.argv[1] == 'terminal':
        update_instance(status, lambda value: value['workload'].update(status='stopped', pid=None),
                        launch_id=entry['launch_id'], location='guest')
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
            "terminal" if terminal else "active",
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
                str(agent.layout.place),
                "list",
            ),
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert listed.returncode == 0, listed.stderr
        entry = read_instance(status)
        assert entry is not None and entry["workload"]["status"] == "stopped"
        assert not file_lock_path(status).exists()
    finally:
        if worker.poll() is None:
            worker.kill()
            worker.wait(timeout=5)
        marker.unlink(missing_ok=True)


def test_guest_reads_its_info_without_a_writable_host_registry(
    running_docker_agent: _RunningDockerAgent,
) -> None:
    agent = running_docker_agent
    directory = agent.layout.place / ".runtime" / "active" / agent.layout.address
    probe = directory / "host_only.json"
    probe.write_text('{"probe": true}')
    script = """
import errno, json
from pathlib import Path
runtime = Path('/root/.toolang/.runtime/active/alice')
info = runtime / 'info.json'
assert json.loads(info.read_text())['home'] == '/root/.toolang/agents/alice'
assert (runtime / 'guest.sh').is_file() and not (runtime / 'guest.py').exists()
assert not (runtime / 'docker').exists()
assert not (runtime / 'config.toml').exists()
assert Path('/root/.toolang/config.toml').is_file()
assert not (runtime / 'host_only.json').exists()
assert not Path('/root/.toolang/agents/alice/.runtime/registration').exists()
lab = Path('/root/.toolang/workspaces/alice/lab')
assert lab.is_dir()
assert not Path('/root/.toolang/agents/alice/.lab').exists()
(lab / 'mount-probe.txt').write_text('shared lab')
try:
    info.write_text('{}')
except OSError as error:
    assert error.errno == errno.EROFS, error
else:
    raise AssertionError('bootstrap info was writable')
"""
    try:
        result = subprocess.run(
            (
                "docker",
                "exec",
                agent.container_id,
                "/tmp/toolang-runtime/tools/toolang/bin/python",
                "-c",
                script,
            ),
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 0, result.stderr
        assert (agent.layout.lab / "mount-probe.txt").read_text() == "shared lab"
        host = json.loads((directory / "info.json").read_text())
        state = SandboxState.load(agent.layout.runtime_status)
        assert state is not None
        stage_dir = state.ref.meta["stage_dir"]
        assert isinstance(stage_dir, str)
        guest = json.loads((Path(stage_dir) / "active/info.json").read_text())
        assert host["home"] == str(agent.layout.home)
        assert host["launch"] == guest["launch"]
        assert host["location"] == guest["location"] == "guest"
        assert probe.exists()
    finally:
        probe.unlink(missing_ok=True)


def test_guest_exit_preserves_inputs_until_host_resource_release(
    running_docker_agent: _RunningDockerAgent,
) -> None:
    from toolang.up.instance import read_instance

    agent = running_docker_agent
    entry = read_instance(agent.layout.runtime_status)
    assert entry is not None
    pid = entry["workload"]["pid"]
    assert isinstance(pid, int) and pid > 0
    reference = agent.layout.place / ".runtime" / "active" / "alice" / "info.json"
    assert reference.exists()
    signaled = subprocess.run(
        ("docker", "exec", agent.container_id, "sh", "-c", f"kill -TERM {pid}"),
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert signaled.returncode == 0, signaled.stderr
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        entry = read_instance(agent.layout.runtime_status)
        if entry is not None and entry["workload"]["status"] == "stopped":
            break
        time.sleep(0.05)
    assert entry is not None and entry["workload"]["status"] == "stopped"
    assert reference.exists(), "inputs must retain their ownership record until release"
    assert SandboxState.load(agent.layout.runtime_status) is not None
    import asyncio
    from toolang.up.sandbox import release_stopped
    from toolang.up.watcher import runtime_inventory

    assert all(
        item.state != "running"
        for item in runtime_inventory(agent.layout.place).snapshot.instances
    )
    asyncio.run(release_stopped(agent.layout))
    assert not reference.parent.exists()


@dataclass
class _ScriptScenario:
    source: Path
    mode: str
    phase: int = 0
    entered: threading.Event = dataclass_field(default_factory=threading.Event)
    resume: threading.Event = dataclass_field(default_factory=threading.Event)


def _script_handler(scenario: _ScriptScenario):
    class Handler(_LocalModelHandler):
        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/v1/chat/completions":
                return super().do_POST()
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            tool_name = None
            arguments = {}
            reply = "done"
            if scenario.mode == "model":
                scenario.entered.set()
                scenario.resume.wait(60)
            elif scenario.mode == "shell":
                tool_name = "shell__execute"
                arguments = {
                    "command": "printf ready > owner-ready; sleep 60",
                    "timeout_sec": 120,
                }
            elif scenario.mode == "write":
                scenario.phase += 1
                if scenario.phase == 1:
                    tool_name = "shell__execute"
                    arguments = {
                        "command": "printf owned > owner-ready; chmod 644 owner-ready; cat input.txt"
                    }
            else:
                raise AssertionError(f"unknown fixture mode: {scenario.mode}")
            message: dict[str, Any] = {
                "role": "assistant",
                "content": None if tool_name else reply,
            }
            if tool_name:
                message["tool_calls"] = [
                    {
                        "id": f"call_{scenario.phase}",
                        "type": "function",
                        "function": {
                            "name": tool_name,
                            "arguments": json.dumps(arguments),
                        },
                    }
                ]
            body = {
                "id": "fixture-script",
                "object": "chat.completion",
                "created": 0,
                "model": "test-ollama:latest",
                "choices": [
                    {
                        "index": 0,
                        "message": message,
                        "finish_reason": "tool_calls" if tool_name else "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 3,
                    "completion_tokens": 1,
                    "total_tokens": 4,
                },
            }
            try:
                if payload.get("stream"):
                    delta = dict(message)
                    if tool_name:
                        delta["tool_calls"] = [
                            dict(item, index=index)
                            for index, item in enumerate(message["tool_calls"])
                        ]
                    chunk = dict(
                        body,
                        object="chat.completion.chunk",
                        choices=[
                            {
                                "index": 0,
                                "delta": delta,
                                "finish_reason": "tool_calls" if tool_name else "stop",
                            }
                        ],
                    )
                    data = f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n".encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                else:
                    self._write_json(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    return Handler


@contextmanager
def _script_model(scenario: _ScriptScenario):
    server = ThreadingHTTPServer(("0.0.0.0", 0), _script_handler(scenario))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _write_local_model_env(
        scenario.source.parent / ".toolang.env", int(server.server_port)
    )
    try:
        yield
    finally:
        scenario.resume.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _script_process(
    source: Path,
    root: Path,
    wheel: Path,
    *,
    sandbox="docker",
    operand=None,
    grant=True,
    workspace: Path | None = None,
):
    args = [
        sys.executable,
        "-m",
        "toolang.cli.toolang",
        operand or str(source),
        "main",
        "--quiet",
        "--out",
        "-",
        "--model",
        _MODEL,
        "--sandbox",
        sandbox,
    ]
    if sandbox.partition(":")[0] == "docker":
        args.extend(["--dev", str(wheel)])
    workspace = workspace or (
        source.parent if grant else source.parent / "other workspace"
    )
    workspace.mkdir(exist_ok=True)
    args.extend(["-w", f"repo={workspace}", "--workdir", "repo://"])
    return subprocess.Popen(
        args,
        cwd=_repository(),
        env={**os.environ, "TOOLANG_ROOT": str(root)},
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )


def _wait_script_home(source: Path, root: Path, process):
    from toolang.up.allocation import retained_roaming

    end = time.monotonic() + 180
    while time.monotonic() < end:
        homes = retained_roaming(source, toolang_root=root)
        if homes:
            state = SandboxState.load(homes[0].runtime_status)
            if state is not None:
                return homes[0], state
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            pytest.fail(f"script exited before container creation: {stdout}\n{stderr}")
        time.sleep(0.05)
    pytest.fail("script did not create its container")


def _clean_script(process, home):
    if process.poll() is None:
        process.kill()
    process.communicate(timeout=15)
    state = SandboxState.load(home.runtime_status) if home is not None else None
    if state is not None:
        from toolang.up.sandbox import load_state_sandbox

        implementation = load_state_sandbox(home, state)
        asyncio.run(implementation.stop(state.ref, force=True))
        asyncio.run(implementation.release(state.ref))


@pytest.mark.parametrize(
    "interruption", ["sigint", "owner-kill", "client-kill", "bootstrap-kill"]
)
@pytest.mark.parametrize("wait_kind", ["model", "shell"])
def test_docker_script_owner_loss_finalizes_records(
    tmp_path, docker_wheel_dir, interruption, wait_kind
):
    from toolang.execution.store import RunStore
    from toolang.plugin.sandboxes.docker._cli import (
        docker_container_running,
        docker_reference_running,
    )
    from toolang.up.instance import read_instance
    import psutil

    source = tmp_path / "wait.too"
    source.write_text(
        "agic main() -> Text:\n  tools = shell/*\n  context = none\n  recall = none\n  user: WAIT.\n"
    )
    root = tmp_path / "root"
    scenario = _ScriptScenario(source, wait_kind)
    home = None
    with _script_model(scenario):
        worker = _script_process(source, root, docker_wheel_dir)
        try:
            home, state = _wait_script_home(source, root, worker)
            if interruption == "bootstrap-kill":
                end = time.monotonic() + 60
                while time.monotonic() < end:
                    current = SandboxState.load(home.runtime_status)
                    if (
                        current is not None
                        and len(current.ref.runtime_id) == 64
                        and docker_container_running(current.ref.runtime_id)
                    ):
                        entry = read_instance(home.runtime_status)
                        assert (
                            entry is not None and entry["workload"].get("pid") is None
                        )
                        state = current
                        break
                    assert worker.poll() is None, worker.communicate()
                    time.sleep(0.05)
                else:
                    pytest.fail("guest bootstrap never entered its running container")
            else:
                end = time.monotonic() + 180
                while time.monotonic() < end:
                    if (
                        (source.parent / "owner-ready").exists()
                        if wait_kind == "shell"
                        else scenario.entered.is_set()
                    ):
                        break
                    if worker.poll() is not None:
                        pytest.fail(str(worker.communicate()))
                    time.sleep(0.05)
                else:
                    pytest.fail("script did not reach its controlled model/tool wait")
            before = time.monotonic()
            if interruption == "sigint":
                worker.send_signal(signal.SIGINT)
            elif interruption == "client-kill":
                clients = [
                    child
                    for child in psutil.Process(worker.pid).children()
                    if "docker" in child.name() and "run" in child.cmdline()
                ]
                assert len(clients) == 1
                clients[0].kill()
            else:
                worker.kill()
            end = before + 13
            while time.monotonic() < end and docker_reference_running(state.ref):
                time.sleep(0.05)
            assert not docker_reference_running(state.ref), (
                "guest outlived the owner deadline"
            )
            assert time.monotonic() - before < 13
            stdout, stderr = worker.communicate(timeout=15)
            if interruption == "sigint":
                assert worker.returncode == 130, (stdout, stderr)
            if interruption != "bootstrap-kill":
                with closing(RunStore(home.run_store, read_only=True)) as store:
                    roots = [run for run in store.list_runs() if run.parent is None]
                    assert roots and all(run.status == "canceled" for run in roots)
                    reason = (
                        "script interrupted"
                        if interruption == "sigint"
                        else "foreground owner channel closed"
                    )
                    assert any(
                        reason in str(store.resolve_error(run.error))
                        for run in roots
                        if run.error
                    )
                    for run in roots:
                        assert all(
                            step.status not in {"pending", "running"}
                            for step in store.list_steps(run_id=run.id)
                        )
                        assert any(
                            control.kind == "cancel"
                            for control in store.list_run_controls(run_id=run.id)
                        )
            entry = read_instance(home.runtime_status)
            assert entry is not None
            assert home.home.exists()
        finally:
            if home is None:
                from toolang.up.allocation import retained_roaming

                homes = retained_roaming(source, toolang_root=root)
                home = homes[0] if homes else None
            _clean_script(worker, home)


@pytest.mark.parametrize(
    "locations", [("docker", "host"), ("host", "docker"), ("docker", "docker")]
)
def test_script_home_reuse_resolves_current_workspace_grants(
    tmp_path, docker_wheel_dir, locations
):
    from toolang.up.allocation import select_roaming, retained_roaming
    from toolang.up.instance import read_instance
    from toolang.execution.store import RunStore

    source = tmp_path / "write.too"
    source.write_text(
        "agic main() -> Text:\n  tools = shell/*\n  context = none\n  recall = none\n  user: WRITE.\n"
    )
    root = tmp_path / "root"
    home = None
    worker = None
    launches = []
    scenario = _ScriptScenario(source, "write")
    with _script_model(scenario):
        try:
            for index, location in enumerate(locations):
                workspace = source.parent / f"workspace {index}"
                workspace.mkdir()
                (workspace / "input.txt").write_text(f"workspace-{index}")
                scenario.phase = 0
                worker = _script_process(
                    source,
                    root,
                    docker_wheel_dir,
                    sandbox=location,
                    operand=f"{source}@{home.place_code}" if home else None,
                    workspace=workspace,
                )
                stdout, stderr = worker.communicate(timeout=180)
                assert worker.returncode == 0, (stdout, stderr)
                assert stdout == "done"
                home = select_roaming(source, toolang_root=root)
                entry = read_instance(home.runtime_status)
                assert entry is not None
                launches.append(entry["launch_id"])
                assert (workspace / "owner-ready").read_text() == "owned"
                if index == 0:
                    (home.lab / "kept.txt").write_text("keep")
                else:
                    assert (home.lab / "kept.txt").read_text() == "keep"
            assert len(set(launches)) == 2
            assert len(retained_roaming(source, toolang_root=root)) == 1
            assert home is not None
            with closing(RunStore(home.run_store, read_only=True)) as store:
                assert len(store.list_runs()) == 2
                assert all(run.status == "succeeded" for run in store.list_runs())
            assert not (source.parent / "owner-ready").exists()
            assert not list((root / ".runtime" / "active").glob("*/info.json"))
        finally:
            if worker is not None:
                homes = retained_roaming(source, toolang_root=root)
                _clean_script(worker, home or (homes[0] if homes else None))


@pytest.mark.parametrize("wait_kind", ["model", "shell"])
def test_host_script_ctrl_c_finalizes_records(tmp_path, wait_kind):
    from toolang.up.allocation import retained_roaming
    from toolang.execution.store import RunStore

    source = tmp_path / "wait.too"
    source.write_text(
        "agic main() -> Text:\n  tools = shell/*\n  context = none\n  recall = none\n  user: WAIT.\n"
    )
    root = tmp_path / "root"
    scenario = _ScriptScenario(source, wait_kind)
    with _script_model(scenario):
        worker = _script_process(source, root, tmp_path, sandbox="host")
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if (
                    (source.parent / "owner-ready").exists()
                    if wait_kind == "shell"
                    else scenario.entered.is_set()
                ):
                    break
                assert worker.poll() is None, worker.communicate()
                time.sleep(0.05)
            else:
                pytest.fail("host script did not reach controlled work")
            worker.send_signal(signal.SIGINT)
            stdout, stderr = worker.communicate(timeout=13)
            assert worker.returncode == 130, (stdout, stderr)
            (home,) = retained_roaming(source, toolang_root=root)
            with closing(RunStore(home.run_store, read_only=True)) as store:
                roots = [run for run in store.list_runs() if run.parent is None]
                assert roots and all(run.status == "canceled" for run in roots)
                assert all(
                    "script interrupted" in str(store.resolve_error(run.error))
                    for run in roots
                    if run.error
                )
                assert all(
                    step.status not in {"pending", "running"}
                    for run in roots
                    for step in store.list_steps(run_id=run.id)
                )
            assert not list((root / ".runtime" / "active").glob("*/info.json"))
        finally:
            if worker.poll() is None:
                worker.kill()
            worker.communicate(timeout=5)


def test_distinct_homes_run_on_host_and_guest_with_independent_workspaces(
    tmp_path, docker_wheel_dir
):
    from toolang.up.allocation import retained_roaming
    from toolang.up.instance import read_instance

    source = tmp_path / "parallel.too"
    source.write_text(
        "agic main() -> Text:\n  tools = shell/*\n  context = none\n  recall = none\n  user: WAIT.\n"
    )
    root = tmp_path / "root"
    workers = []
    scenario = _ScriptScenario(source, "shell")
    with _script_model(scenario):
        try:
            for location in ("host", "docker"):
                workspace = tmp_path / f"{location} workspace"
                workspace.mkdir()
                workers.append(
                    _script_process(
                        source,
                        root,
                        docker_wheel_dir,
                        sandbox=location,
                        workspace=workspace,
                    )
                )
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                if all(
                    (tmp_path / f"{location} workspace" / "owner-ready").exists()
                    for location in ("host", "docker")
                ):
                    break
                assert all(worker.poll() is None for worker in workers)
                time.sleep(0.05)
            else:
                pytest.fail("both isolated HOMEs did not enter tool execution")
            homes = retained_roaming(source, toolang_root=root)
            assert len(homes) == 2 and homes[0].place_code != homes[1].place_code
            assert all(
                home.source_path == source.resolve() and home.root == root.resolve()
                for home in homes
            )
            assert {
                (read_instance(home.runtime_status) or {})["control"][
                    "sandbox"
                ].partition(":")[0]
                for home in homes
            } == {"host", "docker"}
            for worker in workers:
                worker.send_signal(signal.SIGINT)
            for worker in workers:
                stdout, stderr = worker.communicate(timeout=13)
                assert worker.returncode == 130, (stdout, stderr)
            assert not list((root / ".runtime" / "active").glob("*/info.json"))
        finally:
            homes = retained_roaming(source, toolang_root=root)
            for worker in workers:
                _clean_script(worker, None)
            for home in homes:
                _clean_script(workers[0], home)


def test_foreground_launch_preserves_docker_error_after_stage_cleanup(
    tmp_path, docker_wheel_dir
):
    from toolang.up.allocation import retained_roaming

    source = tmp_path / "invalid_image.too"
    source.write_text("flow main():\n  pass\n")
    root = tmp_path / "root"
    worker = subprocess.run(
        [
            sys.executable,
            "-m",
            "toolang.cli.toolang",
            str(source),
            "main",
            "--quiet",
            "--sandbox",
            "docker:invalid image",
            "--dev",
            str(docker_wheel_dir),
        ],
        env={**os.environ, "TOOLANG_ROOT": str(root)},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert worker.returncode == 1
    assert "invalid reference format" in worker.stderr.lower(), worker.stderr
    (home,) = retained_roaming(source, toolang_root=root)
    assert SandboxState.load(home.runtime_status) is None
    assert not home.run_store.exists()
    assert not list((root / ".runtime" / "active").glob("*/info.json"))


def test_foreground_launch_preserves_finalization_with_an_image_stop_signal(
    tmp_path, docker_wheel_dir
):
    from uuid import uuid4
    from toolang.execution.store import RunStore
    from toolang.plugin.sandboxes.docker._cli import docker_stop_container

    image = f"toolang-test-stop-signal:{uuid4().hex}"
    home = None
    worker = None
    try:
        built = subprocess.run(
            ["docker", "build", "--tag", image, "-"],
            input="FROM python:3.13-slim\nSTOPSIGNAL SIGQUIT\n",
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert built.returncode == 0, built.stderr
        source = tmp_path / "custom-stop.too"
        source.write_text(
            "agic main() -> Text:\n  context = none\n  recall = none\n  user: WAIT.\n"
        )
        root = tmp_path / "root"
        scenario = _ScriptScenario(source, "model")
        with _script_model(scenario):
            worker = _script_process(
                source, root, docker_wheel_dir, sandbox=f"docker:{image}"
            )
            home, _state = _wait_script_home(source, root, worker)
            assert scenario.entered.wait(60)
            state = SandboxState.load(home.runtime_status)
            assert state is not None
            docker_stop_container(state.ref.runtime_id, force=False)
            stdout, stderr = worker.communicate(timeout=15)
            assert worker.returncode != 0, (stdout, stderr)
            with closing(RunStore(home.run_store, read_only=True)) as store:
                roots = [run for run in store.list_runs() if run.parent is None]
                assert roots and all(run.status == "canceled" for run in roots)
                for run in roots:
                    assert all(
                        step.status not in {"pending", "running"}
                        for step in store.list_steps(run_id=run.id)
                    )
                    assert any(
                        control.kind == "cancel"
                        for control in store.list_run_controls(run_id=run.id)
                    )
            assert not list((root / ".runtime" / "active").glob("*/info.json"))
    finally:
        if worker is not None:
            _clean_script(worker, home)
        subprocess.run(
            ["docker", "image", "rm", image],
            capture_output=True,
            timeout=15,
        )


def test_owner_eof_stops_a_guest_whose_primary_process_cannot_handle_signals(
    tmp_path, docker_wheel_dir
):
    from toolang.up.allocation import retained_roaming
    from toolang.plugin.sandboxes.docker._cli import docker_reference_running
    from toolang.up.instance import read_instance

    source = tmp_path / "frozen.too"
    source.write_text(
        "agic main() -> Text:\n  context = none\n  recall = none\n  user: WAIT.\n"
    )
    root = tmp_path / "root"
    scenario = _ScriptScenario(source, "model")
    home = None
    with _script_model(scenario):
        worker = _script_process(source, root, docker_wheel_dir)
        try:
            home, _state = _wait_script_home(source, root, worker)
            assert scenario.entered.wait(60)
            state = SandboxState.load(home.runtime_status)
            entry = read_instance(home.runtime_status)
            assert state is not None and len(state.ref.runtime_id) == 64
            assert entry is not None
            pid = entry["workload"]["pid"]
            # Fault injection only: freeze the actual workload, not Docker's init.
            command = (
                ["docker", "kill", "--signal", "STOP", state.ref.runtime_id]
                if pid == 1
                else [
                    "docker",
                    "exec",
                    state.ref.runtime_id,
                    "python3",
                    "-c",
                    "import os,signal,sys; os.kill(int(sys.argv[1]),signal.SIGSTOP)",
                    str(pid),
                ]
            )
            stopped = subprocess.run(
                command, capture_output=True, text=True, timeout=10
            )
            assert stopped.returncode == 0, stopped.stderr
            started = time.monotonic()
            worker.kill()
            worker.communicate(timeout=5)
            deadline = started + 13
            while time.monotonic() < deadline and docker_reference_running(state.ref):
                time.sleep(0.05)
            assert not docker_reference_running(state.ref), (
                "unresponsive guest outlived its owner deadline"
            )
            assert time.monotonic() - started >= 8, (
                "fault never reached forced finalization"
            )
            ended = subprocess.run(
                ["docker", "wait", state.ref.runtime_id],
                capture_output=True,
                text=True,
                timeout=5,
            )
            assert ended.returncode == 0 and ended.stdout.strip() == "137", ended
            logs = subprocess.run(
                ["docker", "logs", "--tail", "20", state.ref.runtime_id],
                capture_output=True,
                text=True,
                timeout=5,
            )
            assert "records may be incomplete" in logs.stdout + logs.stderr
        finally:
            homes = retained_roaming(source, toolang_root=root)
            _clean_script(worker, home or (homes[0] if homes else None))
