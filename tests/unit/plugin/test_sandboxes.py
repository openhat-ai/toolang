from __future__ import annotations

import asyncio
from dataclasses import replace
import json
from pathlib import Path
import stat
import subprocess
import sys
import threading
from types import SimpleNamespace
from typing import Any, cast

from dotenv import dotenv_values
import pytest

from toolang.base.errors import SandboxLaunchError, ToolangError
from toolang.base.protocols.sandbox import Sandbox
from toolang.base.types.sandbox import SandboxMount, SandboxRequest
from toolang.common.layout import AgentLayout
from toolang.plugin.sandboxes import _docker_cli as docker_cli
from toolang.plugin.sandboxes import _docker_guest as docker_guest
from toolang.plugin.sandboxes import docker as docker_sandbox
from toolang.plugin.sandboxes import host as host_sandbox
from toolang.plugin.sandboxes.loading import create_sandbox


def _request(
    root: Path,
    *,
    dev: Path | None = None,
    foreground: bool = False,
) -> SandboxRequest:
    home = root / "agents" / "alice"
    home.mkdir(parents=True, exist_ok=True)
    return SandboxRequest(
        local_root=root,
        local_home=home,
        hosted_root=Path("/root/.toolang"),
        hosted_home=Path("/root/.toolang/agents/alice"),
        agent_name="alice",
        bind_host="127.0.0.1",
        endpoint_host="localhost",
        port=8123,
        endpoint="http://localhost:8123",
        command=("too", "serve", "alice", "--port", "8123"),
        working_directory=home,
        output="inherit" if foreground else "file",
        log_path=None if foreground else home / ".runtime" / "agent.log",
        envs={
            "COMPLEX": "line one\nline \"two\" 'three'\r\\tail${HOME}",
            "EXAMPLE": "value",
            "HTTP_PROXY": "http://proxy.test:8080",
            "LITERAL": "${HOME}/literal",
            "OPENAI_API_KEY": "provider-secret",
            "UNRELATED_HOST_SECRET": "must-not-be-exposed",
            "TOOLANG_HOST_GATEWAY": "wrong-gateway",
            "TOOLANG_ROOT": str(root),
            "TOOLANG_SANDBOX": "host",
        },
        dotenv_envs={
            "COMPLEX": "line one\nline \"two\" 'three'\r\\tail${HOME}",
            "EXAMPLE": "value",
            "LITERAL": "${HOME}/literal",
            "OPENAI_API_KEY": "dotenv-secret",
        },
        mounts=(
            SandboxMount(
                local_path=root / "shared",
                hosted_path=Path("/root/.toolang/shared"),
            ),
        ),
        local_dev_artifact=dev,
    )


def test_host_sandbox_parses_own_spec_and_prepares_local_process(
    tmp_path: Path,
) -> None:
    sandbox = create_sandbox("host", config={})

    assert isinstance(sandbox, Sandbox)
    plan = sandbox.prepare(None, _request(tmp_path))

    assert plan.sandbox == "host"
    assert plan.command[-4:] == ("serve", "alice", "--port", "8123")
    assert plan.working_directory == tmp_path / "agents" / "alice"
    with pytest.raises(ValueError, match="does not accept"):
        sandbox.prepare("", _request(tmp_path))


def test_none_sandbox_selector_is_not_supported() -> None:
    with pytest.raises(ValueError, match="unknown toolang.sandbox plugin: none"):
        create_sandbox("none", config={})


def test_host_foreground_sandbox_inherits_console_streams(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}
    process = cast(Any, object())

    def popen(command: tuple[str, ...], **kwargs: object) -> object:
        captured.update(command=command, **kwargs)
        return process

    monkeypatch.setattr(host_sandbox.subprocess, "Popen", popen)
    sandbox = create_sandbox("host", config={})
    plan = sandbox.prepare(None, _request(tmp_path, foreground=True))

    assert host_sandbox._launch(plan) is process
    assert "stdout" not in captured
    assert "stderr" not in captured


def test_host_launch_cancellation_waits_for_creation_and_stops_the_process(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    started = threading.Event()
    finish = threading.Event()
    stopped: list[int] = []
    process = SimpleNamespace(pid=12345)

    def launch(_plan: object) -> object:
        started.set()
        finish.wait(timeout=5)
        return process

    monkeypatch.setattr(host_sandbox, "_launch", launch)
    monkeypatch.setattr(host_sandbox, "_process_identity", lambda _pid: "identity")
    monkeypatch.setattr(
        host_sandbox,
        "_stop_process",
        lambda item, *, force: stopped.append(item.pid) or force,
    )
    sandbox = create_sandbox("host", config={})
    plan = sandbox.prepare(None, _request(tmp_path, foreground=True))

    async def cancel_launch() -> BaseException | None:
        task = asyncio.create_task(sandbox.launch(plan))
        await asyncio.to_thread(started.wait, 5)
        task.cancel()
        finish.set()
        try:
            await task
        except BaseException as exc:
            return exc
        return None

    error = asyncio.run(cancel_launch())

    assert isinstance(error, asyncio.CancelledError)
    assert stopped == [process.pid]


def test_docker_sandbox_prepares_and_launches(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: dict[str, object] = {}

    async def fake_run_detached(**kwargs: object) -> str:
        calls["run"] = kwargs
        return "container-123"

    monkeypatch.setattr(
        "toolang.plugin.sandboxes.docker.docker_run_detached",
        fake_run_detached,
    )
    monkeypatch.setattr(
        "toolang.plugin.sandboxes.docker.docker_container_running",
        lambda name: name == "container-123",
    )
    dev = tmp_path / "dist" / "toolang-1.2.3-py3-none-any.whl"
    dev.parent.mkdir(parents=True)
    dev.write_bytes(b"wheel")
    (tmp_path / "shared").mkdir()
    control_lock = AgentLayout.resident(tmp_path, "alice").sandbox_state.with_suffix(
        ".lock"
    )
    control_lock.parent.mkdir(parents=True)
    control_lock.write_text("host control\n", encoding="utf-8")
    sandbox = create_sandbox("docker", config={})

    plan = sandbox.prepare("python:3.13-slim", _request(tmp_path, dev=dev))

    assert plan.sandbox == "docker:python:3.13-slim"
    assert plan.working_directory == Path("/root/.toolang/agents/alice")
    container_name = cast(str, plan.meta["container_name"])
    assert container_name.startswith("toolang-alice-")
    mounted = {(item.local_path, item.hosted_path) for item in plan.mounts}
    assert (tmp_path / "shared", Path("/root/.toolang/shared")) in mounted
    assert (
        tmp_path / "agents" / "alice",
        Path("/root/.toolang/agents/alice"),
    ) in mounted
    guest_env_mount = next(
        item
        for item in plan.mounts
        if item.hosted_path == Path("/root/.toolang/agents/alice/.env")
    )
    assert guest_env_mount.read_only is True
    assert dotenv_values(guest_env_mount.local_path, interpolate=False) == {
        "COMPLEX": "line one\nline \"two\" 'three'\r\\tail${HOME}",
        "EXAMPLE": "value",
        "HTTP_PROXY": "http://proxy.test:8080",
        "LITERAL": "${HOME}/literal",
        "OPENAI_API_KEY": "provider-secret",
    }
    guest_env_source = guest_env_mount.local_path.read_text(encoding="utf-8")
    assert guest_env_source.startswith("# Root and agent dotenv values\n")
    assert "\n# Filtered host process values\n" in guest_env_source
    assert guest_env_source.count("OPENAI_API_KEY=") == 2
    assert "UNRELATED_HOST_SECRET" not in guest_env_source
    assert stat.S_IMODE(guest_env_mount.local_path.stat().st_mode) == 0o600
    stage_dir = Path(cast(str, plan.meta["stage_dir"]))
    stage_mount = next(
        item
        for item in plan.mounts
        if item.hosted_path == Path("/root/.toolang/agents/alice/.runtime/sandbox")
    )
    assert stage_mount.read_only is True
    control_state = AgentLayout.resident(tmp_path, "alice").sandbox_state.resolve()
    assert not any(
        control_state.is_relative_to(mount.local_path.resolve())
        for mount in plan.mounts
    )
    assert control_lock.read_text(encoding="utf-8") == "host control\n"
    assert stage_dir.is_relative_to(control_lock.parent / "launches")
    assert "bootstrap.py" in (stage_dir / "start.sh").read_text(encoding="utf-8")
    agent_script = (stage_dir / "agent.sh").read_text(encoding="utf-8")
    assert 'export TOOLANG_SANDBOX_INSTANCE="${HOSTNAME:?' in agent_script
    assert (
        "exec uv tool run --from "
        "/root/.toolang/agents/alice/.runtime/sandbox/"
        "toolang-1.2.3-py3-none-any.whl too serve alice --port 8123"
    ) in agent_script
    assert not (stage_dir / "environment.json").exists()
    bootstrap = subprocess.run(
        (
            sys.executable,
            str(stage_dir / "bootstrap.py"),
            str(guest_env_mount.local_path),
            sys.executable,
            "-c",
            "import json, os; print(json.dumps({"
            "'COMPLEX': os.environ['COMPLEX'], "
            "'LITERAL': os.environ['LITERAL'], "
            "'OPENAI_API_KEY': os.environ['OPENAI_API_KEY']}))",
        ),
        check=True,
        capture_output=True,
        text=True,
        env={"HOME": "/outside"},
    )
    assert json.loads(bootstrap.stdout) == {
        "COMPLEX": "line one\nline \"two\" 'three'\r\\tail${HOME}",
        "LITERAL": "${HOME}/literal",
        "OPENAI_API_KEY": "provider-secret",
    }

    ref = asyncio.run(sandbox.launch(plan))

    assert ref.runtime_id == "container-123"
    assert ref.meta["container_name"] == container_name
    assert ref.endpoint == "http://localhost:8123"
    assert asyncio.run(sandbox.running(ref)) is True
    run_call = cast(dict[str, Any], calls["run"])
    assert run_call["image"] == "python:3.13-slim"
    assert run_call["published_port"] == 8123
    assert run_call["hosted_port"] == 8123
    assert run_call["env_values"] == {
        "TOOLANG_HOST_GATEWAY": "host.docker.internal",
        "TOOLANG_ROOT": "/root/.toolang",
        "TOOLANG_SANDBOX": "docker:python:3.13-slim",
    }
    assert run_call["log_path"] == (
        tmp_path / "agents" / "alice" / ".runtime" / "agent.log"
    )


def test_docker_background_sandbox_persists_bootstrap_output(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    sandbox = create_sandbox("docker", config={})

    plan = sandbox.prepare(None, request)

    log_path = cast(Path, request.log_path)
    assert log_path.is_file()
    assert stat.S_IMODE(log_path.stat().st_mode) == 0o600
    stage_dir = Path(cast(str, plan.meta["stage_dir"]))
    start_script = (stage_dir / "start.sh").read_text(encoding="utf-8")
    assert "/root/.toolang/agents/alice/.runtime/agent.log" in start_script
    assert "2>&1" in start_script
    agent_script = (stage_dir / "agent.sh").read_text(encoding="utf-8")
    assert "ensurepip --upgrade >/dev/null" not in agent_script
    assert "pip install --disable-pip-version-check --user -U uv >/dev/null" not in (
        agent_script
    )


def test_docker_background_log_must_be_inside_the_agent_home(
    tmp_path: Path,
) -> None:
    request = replace(_request(tmp_path), log_path=tmp_path / "outside.log")
    sandbox = create_sandbox("docker", config={})

    with pytest.raises(ValueError, match="inside the agent home"):
        sandbox.prepare(None, request)

    assert not (tmp_path / ".sandbox" / "alice").exists()
    assert not (tmp_path / "outside.log").exists()


def test_docker_sandbox_does_not_reuse_a_released_stage_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        docker_sandbox,
        "docker_run_detached",
        _async_value("container-123"),
    )
    monkeypatch.setattr(
        docker_sandbox,
        "docker_append_container_logs",
        lambda *_: None,
    )
    monkeypatch.setattr(docker_sandbox, "docker_remove_container", lambda _name: None)
    sandbox = create_sandbox("docker", config={})

    first = sandbox.prepare(None, _request(tmp_path))
    ref = asyncio.run(sandbox.launch(first))
    asyncio.run(sandbox.release(ref))
    second = sandbox.prepare(None, _request(tmp_path))

    first_stage = Path(cast(str, first.meta["stage_dir"]))
    second_stage = Path(cast(str, second.meta["stage_dir"]))
    assert first_stage != second_stage
    assert not first_stage.exists()
    assert second_stage.is_dir()


def test_docker_sandbox_requires_a_concrete_dev_wheel(tmp_path: Path) -> None:
    dev = tmp_path / "dist"
    dev.mkdir()
    sandbox = create_sandbox("docker", config={})

    with pytest.raises(ValueError, match="must be a wheel file"):
        sandbox.prepare(None, _request(tmp_path, dev=dev))


def test_docker_agent_script_quotes_a_dev_wheel_path(tmp_path: Path) -> None:
    script = tmp_path / "agent.sh"

    docker_guest.write_agent_script(
        script,
        command=("too", "serve", "alice"),
        hosted_dev_artifact=Path("/runtime/dev wheels/toolang.whl"),
    )

    assert (
        "exec uv tool run --from '/runtime/dev wheels/toolang.whl' too serve alice"
        in script.read_text(encoding="utf-8")
    )


def test_docker_sandbox_uses_configured_default_image(tmp_path: Path) -> None:
    sandbox = create_sandbox("docker", config={"image": "python:3.14"})

    plan = sandbox.prepare(None, _request(tmp_path))

    assert plan.sandbox == "docker:python:3.14"


def test_docker_sandbox_owns_its_guest_root_configuration(tmp_path: Path) -> None:
    sandbox = create_sandbox("docker", config={"root": "/workspace/toolang"})

    assert sandbox.runtime_root(tmp_path) == Path("/workspace/toolang")


def test_docker_sandbox_uses_a_configured_environment_allow_pattern(
    tmp_path: Path,
) -> None:
    sandbox = create_sandbox(
        "docker",
        config={"environment_allow_pattern": r"^CUSTOM_[A-Z]+$"},
    )
    request = replace(
        _request(tmp_path),
        envs={
            "CUSTOM_TOKEN": "custom",
            "DOTENV_TOKEN": "dotenv",
            "OPENAI_API_KEY": "not-allowed-by-override",
        },
        dotenv_envs={"DOTENV_TOKEN": "dotenv"},
    )

    plan = sandbox.prepare(None, request)

    guest_env_mount = next(
        item
        for item in plan.mounts
        if item.hosted_path == Path("/root/.toolang/agents/alice/.env")
    )
    assert dotenv_values(guest_env_mount.local_path, interpolate=False) == {
        "CUSTOM_TOKEN": "custom",
        "DOTENV_TOKEN": "dotenv",
    }


def test_docker_sandbox_rejects_an_invalid_environment_allow_pattern() -> None:
    with pytest.raises(ValueError, match="environment_allow_pattern"):
        create_sandbox("docker", config={"environment_allow_pattern": "["})


def test_docker_launch_failure_removes_the_staged_guest_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def fail_run(**_kwargs: object) -> str:
        raise RuntimeError("docker failed")

    monkeypatch.setattr(docker_sandbox, "docker_run_detached", fail_run)
    monkeypatch.setattr(docker_sandbox, "docker_remove_container", lambda _name: None)
    sandbox = create_sandbox("docker", config={})
    plan = sandbox.prepare(None, _request(tmp_path))
    stage_dir = Path(cast(str, plan.meta["stage_dir"]))

    with pytest.raises(ToolangError, match="Could not start docker sandbox"):
        asyncio.run(sandbox.launch(plan))

    assert not stage_dir.exists()


def test_docker_launch_cleanup_failure_returns_a_recovery_reference(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def fail_run(**_kwargs: object) -> str:
        raise RuntimeError("docker failed")

    def fail_remove(_name: str) -> None:
        raise RuntimeError("daemon unavailable")

    monkeypatch.setattr(docker_sandbox, "docker_run_detached", fail_run)
    monkeypatch.setattr(docker_sandbox, "docker_remove_container", fail_remove)
    sandbox = create_sandbox("docker", config={})
    plan = sandbox.prepare(None, _request(tmp_path))
    stage_dir = Path(cast(str, plan.meta["stage_dir"]))

    with pytest.raises(SandboxLaunchError, match="could not remove") as captured:
        asyncio.run(sandbox.launch(plan))

    assert captured.value.ref.runtime_id == plan.meta["container_name"]
    assert captured.value.ref.meta["stage_dir"] == str(stage_dir)
    assert stage_dir.is_dir()


def test_docker_launch_cancellation_terminates_cli_and_removes_container(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class BlockingProcess:
        def __init__(self) -> None:
            self.returncode: int | None = None
            self.started = asyncio.Event()
            self.finished = asyncio.Event()
            self.terminated = False

        async def communicate(self) -> tuple[bytes, None]:
            self.started.set()
            await self.finished.wait()
            return b"", None

        async def wait(self) -> int:
            await self.finished.wait()
            return cast(int, self.returncode)

        def terminate(self) -> None:
            self.terminated = True
            self.returncode = -15
            self.finished.set()

        def kill(self) -> None:
            self.returncode = -9
            self.finished.set()

    process = BlockingProcess()
    removed: list[str] = []

    async def create_subprocess_exec(*_args: str, **_kwargs: object) -> object:
        return process

    monkeypatch.setattr(
        docker_cli.asyncio,
        "create_subprocess_exec",
        create_subprocess_exec,
    )
    monkeypatch.setattr(
        docker_sandbox,
        "docker_remove_container",
        removed.append,
    )
    sandbox = create_sandbox("docker", config={})
    plan = sandbox.prepare(None, _request(tmp_path))
    container_name = cast(str, plan.meta["container_name"])
    stage_dir = Path(cast(str, plan.meta["stage_dir"]))

    async def cancel_launch() -> None:
        task = asyncio.create_task(sandbox.launch(plan))
        await process.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel_launch())

    assert process.terminated is True
    assert removed == [container_name]
    assert not stage_dir.exists()
    assert cast(Path, plan.log_path).is_file()


def test_docker_release_preserves_container_diagnostics_before_removal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, str]] = []

    async def run(**_kwargs: object) -> str:
        return "container-123"

    monkeypatch.setattr(docker_sandbox, "docker_run_detached", run)
    monkeypatch.setattr(
        docker_sandbox,
        "docker_append_container_logs",
        lambda name, _path: calls.append(("logs", name)),
    )
    monkeypatch.setattr(
        docker_sandbox,
        "docker_remove_container",
        lambda name: calls.append(("remove", name)),
    )
    sandbox = create_sandbox("docker", config={})
    plan = sandbox.prepare(None, _request(tmp_path))
    ref = asyncio.run(sandbox.launch(plan))

    asyncio.run(sandbox.release(ref))

    assert calls == [("logs", ref.runtime_id), ("remove", ref.runtime_id)]
    assert cast(Path, plan.log_path).is_file()


def test_docker_release_removes_the_container_when_diagnostics_fail(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        docker_sandbox,
        "docker_run_detached",
        _async_value("container-123"),
    )

    def fail_diagnostics(name: str, _path: Path) -> None:
        calls.append(("logs", name))
        raise OSError("disk full")

    monkeypatch.setattr(
        docker_sandbox,
        "docker_append_container_logs",
        fail_diagnostics,
    )
    monkeypatch.setattr(
        docker_sandbox,
        "docker_remove_container",
        lambda name: calls.append(("remove", name)),
    )
    sandbox = create_sandbox("docker", config={})
    plan = sandbox.prepare(None, _request(tmp_path))
    stage_dir = Path(cast(str, plan.meta["stage_dir"]))
    ref = asyncio.run(sandbox.launch(plan))

    asyncio.run(sandbox.release(ref))

    assert calls == [("logs", ref.runtime_id), ("remove", ref.runtime_id)]
    assert not stage_dir.exists()


def test_docker_run_adds_the_canonical_host_gateway(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: list[str] = []

    class Process:
        returncode = 0

        async def communicate(self) -> tuple[bytes, None]:
            return b"container-id\n", None

    async def create_subprocess_exec(*args: str, **_kwargs: object) -> Process:
        captured.extend(args)
        return Process()

    monkeypatch.setattr(
        docker_cli.asyncio,
        "create_subprocess_exec",
        create_subprocess_exec,
    )

    container_id = asyncio.run(
        docker_cli.docker_run_detached(
            image="python:3.13-slim",
            container_name="toolang-alice-test",
            workdir="/root/.toolang/agents/alice",
            command=["/bin/true"],
            mounts=(),
            bind_host="127.0.0.1",
            published_port=8123,
            hosted_port=8123,
            env_values={"TOOLANG_ROOT": "/root/.toolang"},
            log_path=None,
        )
    )

    assert container_id == "container-id"
    assert captured[captured.index("--add-host") + 1] == (
        "host.docker.internal:host-gateway"
    )
    assert "TOOLANG_ROOT=/root/.toolang" in captured


def test_docker_diagnostics_are_bounded_and_streamed_to_a_private_log(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}

    def run(args: tuple[str, ...], **kwargs: object) -> object:
        captured.update(args=args, **kwargs)
        stream = kwargs.get("stdout")
        if stream is not None:
            cast(Any, stream).write(b"container diagnostics\n")
        return subprocess.CompletedProcess(
            args,
            0,
            "container diagnostics\n",
            "",
        )

    monkeypatch.setattr(docker_cli.subprocess, "run", run)
    log_path = tmp_path / "agent.log"

    docker_cli.docker_append_container_logs("toolang-alice-test", log_path)

    assert captured["args"] == (
        "docker",
        "logs",
        "--tail",
        "2000",
        "toolang-alice-test",
    )
    assert captured["stderr"] is subprocess.STDOUT
    assert "capture_output" not in captured
    assert "text" not in captured
    assert log_path.read_bytes() == b"container diagnostics\n"
    assert stat.S_IMODE(log_path.stat().st_mode) == 0o600


def test_docker_foreground_sandbox_follows_container_logs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, str]] = []

    class Follower:
        returncode = 0

        async def wait(self) -> int:
            calls.append(("log_wait", "container"))
            return 0

    monkeypatch.setattr(
        "toolang.plugin.sandboxes.docker.docker_run_detached",
        _async_value("container-123"),
    )

    async def follow(name: str) -> Any:
        calls.append(("logs", name))
        return Follower()

    monkeypatch.setattr(
        "toolang.plugin.sandboxes.docker.docker_follow_container_logs",
        follow,
    )
    monkeypatch.setattr(
        "toolang.plugin.sandboxes.docker.docker_wait_container",
        lambda name: calls.append(("wait", name)) or 0,
    )
    sandbox = create_sandbox("docker", config={})
    plan = sandbox.prepare(None, _request(tmp_path, foreground=True))

    ref = asyncio.run(sandbox.launch(plan))
    assert calls == []
    asyncio.run(sandbox.attach(plan, ref))
    assert calls == [("logs", ref.runtime_id)]
    result = asyncio.run(sandbox.wait(ref))

    assert result == 0
    assert calls == [
        ("logs", ref.runtime_id),
        ("log_wait", "container"),
        ("wait", ref.runtime_id),
    ]


def _async_value(value: str) -> Any:
    async def resolve(**_kwargs: object) -> str:
        return value

    return resolve
