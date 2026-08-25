from __future__ import annotations

import asyncio
from dataclasses import replace
import json
from pathlib import Path
import stat
import subprocess
import sys
from typing import Any, cast

from dotenv import dotenv_values
import pytest

from toolang.base.errors import ToolangError
from toolang.base.protocols.sandbox import Sandbox
from toolang.base.types.sandbox import SandboxMount, SandboxRequest
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


def test_docker_sandbox_prepares_and_launches(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: dict[str, object] = {}

    def fake_run_detached(**kwargs: object) -> str:
        calls["run"] = kwargs
        return "container-123"

    monkeypatch.setattr(
        "toolang.plugin.sandboxes.docker.docker_run_detached",
        fake_run_detached,
    )
    monkeypatch.setattr(
        "toolang.plugin.sandboxes.docker.docker_container_running",
        lambda name: name.startswith("toolang-alice-"),
    )
    dev = tmp_path / "dist" / "toolang-1.2.3-py3-none-any.whl"
    dev.parent.mkdir(parents=True)
    dev.write_bytes(b"wheel")
    (tmp_path / "shared").mkdir()
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
    stage_dir = tmp_path / ".sandbox" / "alice"
    stage_mount = next(
        item
        for item in plan.mounts
        if item.hosted_path == Path("/root/.toolang/agents/alice/.runtime/sandbox")
    )
    assert stage_mount.read_only is True
    assert "bootstrap.py" in (stage_dir / "start.sh").read_text(encoding="utf-8")
    agent_script = (stage_dir / "agent.sh").read_text(encoding="utf-8")
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

    assert ref.runtime_id == container_name
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


def test_docker_sandbox_requires_a_concrete_dev_wheel(tmp_path: Path) -> None:
    dev = tmp_path / "dist"
    dev.mkdir()
    sandbox = create_sandbox("docker", config={})

    with pytest.raises(ValueError, match="must be a wheel file"):
        sandbox.prepare(None, _request(tmp_path, dev=dev))


def test_docker_agent_script_quotes_a_dev_wheel_path(tmp_path: Path) -> None:
    script = tmp_path / "agent.sh"

    docker_sandbox._write_agent_script(
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
    def fail_run(**_kwargs: object) -> str:
        raise RuntimeError("docker failed")

    monkeypatch.setattr(docker_sandbox, "docker_run_detached", fail_run)
    sandbox = create_sandbox("docker", config={})
    plan = sandbox.prepare(None, _request(tmp_path))
    stage_dir = Path(cast(str, plan.meta["stage_dir"]))

    with pytest.raises(ToolangError, match="Could not start docker sandbox"):
        asyncio.run(sandbox.launch(plan))

    assert not stage_dir.exists()


def test_docker_run_adds_the_canonical_host_gateway(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: list[str] = []

    def run(args: list[str], **_kwargs: object) -> object:
        captured.extend(args)
        return docker_sandbox.subprocess.CompletedProcess(args, 0, "container-id\n", "")

    monkeypatch.setattr(docker_sandbox.subprocess, "run", run)

    container_id = docker_sandbox.docker_run_detached(
        image="python:3.13-slim",
        container_name="toolang-alice-test",
        workdir="/root/.toolang/agents/alice",
        command=["/bin/true"],
        mounts=(),
        bind_host="127.0.0.1",
        published_port=8123,
        hosted_port=8123,
        env_values={"TOOLANG_ROOT": "/root/.toolang"},
    )

    assert container_id == "container-id"
    assert captured[captured.index("--add-host") + 1] == (
        "host.docker.internal:host-gateway"
    )
    assert "TOOLANG_ROOT=/root/.toolang" in captured


def test_docker_foreground_sandbox_follows_container_logs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "toolang.plugin.sandboxes.docker.docker_run_detached",
        lambda **_kwargs: "container-123",
    )
    monkeypatch.setattr(
        "toolang.plugin.sandboxes.docker.docker_follow_container_logs",
        lambda name: calls.append(("logs", name)),
    )
    monkeypatch.setattr(
        "toolang.plugin.sandboxes.docker.docker_wait_container",
        lambda name: calls.append(("wait", name)) or 0,
    )
    sandbox = create_sandbox("docker", config={})
    plan = sandbox.prepare(None, _request(tmp_path, foreground=True))

    ref = asyncio.run(sandbox.launch(plan))
    result = asyncio.run(sandbox.wait(ref))

    assert result == 0
    assert calls == [("logs", ref.runtime_id), ("wait", ref.runtime_id)]
