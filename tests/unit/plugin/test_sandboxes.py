from __future__ import annotations

import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
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


_CONTAINER_ID = "176191c1528b8e2861cc16422dee13ade59d4977c2148a9ebf5d36a06f090abb"
_TRY_DOCKER_GUEST = (
    Path(__file__).resolve().parents[3] / "scripts" / "try_docker_guest.sh"
)


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
            "TOOLANG_SANDBOX_DESCRIPTION": "wrong-host-description",
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
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        host_sandbox,
        "host_sandbox_description",
        lambda: "macOS 27.0 arm64",
    )
    sandbox = create_sandbox("host", config={})

    assert isinstance(sandbox, Sandbox)
    plan = sandbox.prepare(None, _request(tmp_path))

    assert plan.sandbox == "host"
    assert plan.command[-4:] == ("serve", "alice", "--port", "8123")
    assert plan.working_directory == tmp_path / "agents" / "alice"
    assert plan.envs[host_sandbox.HOST_SANDBOX_DESCRIPTION_ENV] == ("macOS 27.0 arm64")
    with pytest.raises(ValueError, match="does not accept"):
        sandbox.prepare("", _request(tmp_path))


def test_host_sandbox_description_formats_macos(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def system() -> str:
        nonlocal calls
        calls += 1
        return "Darwin"

    host_sandbox.host_sandbox_description.cache_clear()
    monkeypatch.setattr(host_sandbox.platform, "system", system)
    monkeypatch.setattr(host_sandbox.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(
        host_sandbox.platform,
        "mac_ver",
        lambda: ("27.0", ("", "", ""), "arm64"),
    )
    assert host_sandbox.host_sandbox_description() == "macOS 27.0 arm64"
    assert host_sandbox.host_sandbox_description() == "macOS 27.0 arm64"
    assert calls == 1
    host_sandbox.host_sandbox_description.cache_clear()


def test_host_sandbox_description_formats_linux_distribution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host_sandbox.host_sandbox_description.cache_clear()
    monkeypatch.setattr(host_sandbox.platform, "system", lambda: "Linux")
    monkeypatch.setattr(host_sandbox.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(
        host_sandbox.platform,
        "freedesktop_os_release",
        lambda: {"NAME": "Ubuntu", "VERSION_ID": "24.04"},
    )

    assert host_sandbox.host_sandbox_description() == "Ubuntu 24.04 x86_64"
    host_sandbox.host_sandbox_description.cache_clear()


def test_host_sandbox_description_formats_windows_without_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host_sandbox.host_sandbox_description.cache_clear()
    monkeypatch.setattr(host_sandbox.platform, "system", lambda: "Windows")
    monkeypatch.setattr(host_sandbox.platform, "machine", lambda: "AMD64")
    monkeypatch.setattr(
        host_sandbox.platform,
        "win32_ver",
        lambda: ("11", "10.0.26100", "SP0", "Multiprocessor Free"),
    )

    assert host_sandbox.host_sandbox_description() == "Windows 11 AMD64"
    host_sandbox.host_sandbox_description.cache_clear()


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
    sandbox_instance_path = Path(cast(str, plan.meta["sandbox_instance_path"]))
    diagnostic_path = Path(cast(str, plan.meta["diagnostic_path"]))
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
    assert sandbox_instance_path.read_text(encoding="utf-8") == ""
    assert stat.S_IMODE(sandbox_instance_path.stat().st_mode) == 0o600
    assert diagnostic_path.is_file()
    assert stat.S_IMODE(diagnostic_path.stat().st_mode) == 0o600
    guest_script = stage_dir / "docker_guest.sh"
    assert guest_script.is_file()
    assert stat.S_IMODE(guest_script.stat().st_mode) == 0o755
    assert plan.command == (
        "/bin/sh",
        "/root/.toolang/agents/alice/.runtime/sandbox/docker_guest.sh",
        "/root/.toolang/agents/alice/.env",
        f"/root/.toolang/agents/alice/.runtime/sandbox-bootstrap-{stage_dir.name}.log",
        str(diagnostic_path),
        "/root/.toolang/agents/alice/.runtime/sandbox/instance",
        "/root/.toolang/agents/alice/.runtime/sandbox/toolang-1.2.3-py3-none-any.whl",
        "too",
        "serve",
        "alice",
        "--port",
        "8123",
    )
    assert plan.ready_timeout_sec == docker_sandbox.DOCKER_READY_TIMEOUT_SEC
    assert not (stage_dir / "start.sh").exists()
    assert not (stage_dir / "bootstrap.py").exists()
    assert not (stage_dir / "agent.sh").exists()
    assert not (stage_dir / "environment.json").exists()
    assert not any(stage_dir.glob("*.events"))

    ref = asyncio.run(sandbox.launch(plan))

    assert ref.runtime_id == "container-123"
    assert sandbox_instance_path.read_text(encoding="utf-8") == "container-123\n"
    assert ref.runtime_kind == "container"
    assert ref.runtime_name == container_name
    assert ref.meta["diagnostic_path"] == str(diagnostic_path)
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


def test_docker_background_sandbox_uses_container_output_and_private_diagnostics(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    sandbox = create_sandbox("docker", config={})

    plan = sandbox.prepare(None, request)

    log_path = cast(Path, request.log_path)
    assert log_path.is_file()
    assert stat.S_IMODE(log_path.stat().st_mode) == 0o600
    stage_dir = Path(cast(str, plan.meta["stage_dir"]))
    guest_script = (stage_dir / "docker_guest.sh").read_text(encoding="utf-8")
    diagnostic_path = Path(cast(str, plan.meta["diagnostic_path"]))
    assert str(log_path) not in guest_script
    assert str(diagnostic_path) not in guest_script
    assert "https://astral.sh/uv/$UV_BOOTSTRAP_VERSION/install.sh" in guest_script
    assert "UV_PYTHON_INSTALL_DIR" in guest_script
    assert "UV_TOOL_BIN_DIR" in guest_script
    assert '"tool",\n            "install"' in guest_script
    assert "cannot start the required AgentServer" in guest_script


def test_docker_sandbox_mounts_roaming_source_links_as_guest_files(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    source = tmp_path / "demo.too"
    source.write_text("agent demo\n", encoding="utf-8")
    config = tmp_path / "toolang.toml"
    config.write_text("[sandbox]\n", encoding="utf-8")
    (request.local_home / "agent.too").symlink_to(source)
    (request.local_home / "config.toml").symlink_to(config)

    plan = create_sandbox("docker", config={}).prepare(None, request)

    source_mount = next(
        item
        for item in plan.mounts
        if item.hosted_path == request.hosted_home / "agent.too"
    )
    config_mount = next(
        item
        for item in plan.mounts
        if item.hosted_path == request.hosted_home / "config.toml"
    )
    assert source_mount == SandboxMount(
        source,
        request.hosted_home / "agent.too",
        read_only=True,
    )
    assert config_mount == SandboxMount(
        config,
        request.hosted_home / "config.toml",
        read_only=True,
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


def test_docker_guest_core_is_fixed_posix_shell_without_legacy_aliases(
    tmp_path: Path,
) -> None:
    script = tmp_path / "docker_guest.sh"
    docker_guest.write_guest_script(script)

    source = script.read_text(encoding="utf-8")
    assert "UV_BOOTSTRAP_VERSION=0.12.7" in source
    assert "https://astral.sh/uv/$UV_BOOTSTRAP_VERSION/install.sh" in source
    assert "\nsource " not in source
    assert "agent.sh" not in source
    assert "start.sh" not in source
    assert stat.S_IMODE(script.stat().st_mode) == 0o755
    subprocess.run(("/bin/sh", "-n", str(script)), check=True)


def test_try_docker_guest_forwards_paths_command_and_exit_code(
    tmp_path: Path,
) -> None:
    wheel_dir = tmp_path / "wheel files"
    wheel_dir.mkdir()
    wheel = wheel_dir / "toolang-1.2.3-py3-none-any.whl"
    wheel.write_bytes(b"wheel")

    completed, calls, facts = _run_try_docker_guest(
        tmp_path,
        "python:3.13-slim",
        "--dev",
        str(wheel_dir),
        "--",
        "too",
        "serve",
        "agent with spaces",
        exit_code=17,
    )

    assert completed.returncode == 17
    assert completed.stdout == "guest output\n"
    assert [call[0] for call in calls] == ["create", "start", "rm"]
    create = calls[0]
    stage = Path(cast(str, facts["stage"]))
    assert not stage.exists()
    assert facts["instance"] == _CONTAINER_ID + "\n"
    assert facts["guest_core"] is True
    assert f"{stage}:/toolang-bootstrap:ro" in create
    assert f"{stage}/diagnostic.log:/toolang-bootstrap/diagnostic.log" in create
    assert create[-11:] == [
        "python:3.13-slim",
        "/bin/sh",
        "/toolang-bootstrap/docker_guest.sh",
        "/toolang-bootstrap/guest.env",
        "/toolang-bootstrap/diagnostic.log",
        f"{stage}/diagnostic.log",
        "/toolang-bootstrap/instance",
        "/toolang-bootstrap/toolang-1.2.3-py3-none-any.whl",
        "too",
        "serve",
    ] + ["agent with spaces"]
    assert completed.stderr.splitlines() == [
        "Creating container...",
        f"Created container · {_CONTAINER_ID[:12]}",
        "Starting container...",
        "Container exited · status 17",
        "Removing container...",
        "Removed container",
    ]


def test_try_docker_guest_keeps_resources_and_uses_default_command(
    tmp_path: Path,
) -> None:
    completed, calls, facts = _run_try_docker_guest(
        tmp_path,
        "debian:bookworm-slim",
        "--keep",
    )

    stage = Path(cast(str, facts["stage"]))
    try:
        assert completed.returncode == 0
        assert [call[0] for call in calls] == ["create", "start"]
        assert calls[0][-2:] == ["too", "--version"]
        assert stage.is_dir()
        assert f"Container retained · {_CONTAINER_ID}" in completed.stderr
        assert f"Staging retained · {stage}" in completed.stderr
    finally:
        shutil.rmtree(stage, ignore_errors=True)


@pytest.mark.skipif(os.name == "nt", reason="the wrapper runs on Windows via WSL2")
def test_try_docker_guest_cleans_up_after_interrupt(tmp_path: Path) -> None:
    completed, calls, facts = _run_try_docker_guest(
        tmp_path,
        "python:3.13-slim",
        interrupt=True,
    )

    assert completed.returncode == 130
    assert [call[0] for call in calls] == ["create", "start", "rm"]
    assert not Path(cast(str, facts["stage"])).exists()


def test_docker_bootstrap_reuses_uv_and_python_and_round_trips_dotenv(
    tmp_path: Path,
) -> None:
    guest_root = tmp_path / "guest files"
    guest_root.mkdir()
    completed, diagnostic = _run_docker_guest(
        guest_root,
        install_uv=False,
        install_python=False,
    )

    assert completed.returncode == 0
    assert completed.stderr.splitlines() == [
        "Using uv · 9.9.9",
        f"Using Python · {sys.version_info.major}.{sys.version_info.minor}."
        f"{sys.version_info.micro}",
        "Installing Toolang...",
        "Installed Toolang · 1.2.3",
        "Starting agent...",
    ]
    assert json.loads(completed.stdout) == {
        "complex": 'line one\nline "two" `three`\r\\tail${HOME}',
        "instance": _CONTAINER_ID,
        "literal": "${HOME}/literal",
    }
    assert "installer detail" in diagnostic.read_text(encoding="utf-8")


def test_docker_bootstrap_installs_uv_and_managed_python_without_system_python(
    tmp_path: Path,
) -> None:
    completed, diagnostic = _run_docker_guest(
        tmp_path,
        install_uv=True,
        install_python=True,
    )

    assert completed.returncode == 0
    assert completed.stderr.splitlines() == [
        "Installing uv...",
        "Installed uv · 9.9.9",
        "Installing Python...",
        f"Installed Python · {sys.version_info.major}.{sys.version_info.minor}."
        f"{sys.version_info.micro}",
        "Installing Toolang...",
        "Installed Toolang · 1.2.3",
        "Starting agent...",
    ]
    details = diagnostic.read_text(encoding="utf-8")
    assert "uv/0.12.7/install.sh" in details
    assert "managed Python detail" in details


def test_docker_bootstrap_can_install_uv_with_existing_python_and_pip(
    tmp_path: Path,
) -> None:
    completed, diagnostic = _run_docker_guest(
        tmp_path,
        install_uv=True,
        install_python=False,
        install_uv_with_pip=True,
    )

    assert completed.returncode == 0
    assert completed.stderr.splitlines()[0] == "Installing uv..."
    assert "pip install detail" in diagnostic.read_text(encoding="utf-8")


def test_docker_bootstrap_installs_a_staged_wheel_and_executes_its_tool(
    tmp_path: Path,
) -> None:
    wheel = tmp_path / "toolang-1.2.3-py3-none-any.whl"
    wheel.write_bytes(b"wheel")

    completed, diagnostic = _run_docker_guest(
        tmp_path,
        install_uv=False,
        install_python=False,
        hosted_dev_artifact=wheel,
    )

    assert completed.returncode == 0
    assert json.loads(completed.stdout)["instance"] == _CONTAINER_ID
    assert str(wheel) in diagnostic.read_text(encoding="utf-8")


def test_docker_bootstrap_reports_package_install_failure_with_a_fix(
    tmp_path: Path,
) -> None:
    completed, diagnostic = _run_docker_guest(
        tmp_path,
        install_uv=False,
        install_python=False,
        install_tool=False,
    )

    assert completed.returncode == 1
    assert completed.stdout == ""
    assert completed.stderr.splitlines()[-3:] == [
        "Could not install Toolang from package index.",
        "Fix: Check package index access, or build a wheel and pass it with `--dev dist`.",
        f"Log: {diagnostic}",
    ]


def test_docker_bootstrap_reports_incompatible_tool_with_a_fix(
    tmp_path: Path,
) -> None:
    completed, diagnostic = _run_docker_guest(
        tmp_path,
        install_uv=False,
        install_python=False,
        compatible_tool=False,
    )

    assert completed.returncode == 1
    assert completed.stdout == ""
    assert completed.stderr.splitlines()[-3:] == [
        "The installed Toolang package cannot start the required AgentServer.",
        "Fix: Build a wheel with `uv build --wheel` and pass it with `--dev dist`.",
        f"Log: {diagnostic}",
    ]


def test_docker_bootstrap_reports_missing_uv_downloaders_without_chatter(
    tmp_path: Path,
) -> None:
    stage = tmp_path / "stage"
    stage.mkdir()
    diagnostic = tmp_path / "bootstrap.log"
    diagnostic.touch()
    env_path = tmp_path / "guest.env"
    docker_guest.write_guest_env(env_path, dotenv_envs={}, process_envs={})
    instance = _sandbox_instance(tmp_path)
    script = stage / "docker_guest.sh"
    docker_guest.write_guest_script(script)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _link_commands(bin_dir, "cat", "chmod", "mkdir", "rm")
    _write_executable(bin_dir / "uname", "#!/bin/sh\necho Linux\n")

    completed = subprocess.run(
        (
            "/bin/sh",
            str(script),
            str(env_path),
            str(diagnostic),
            str(diagnostic),
            str(instance),
            "toolang",
            "too",
            "serve",
            "alice",
        ),
        check=False,
        capture_output=True,
        text=True,
        env={
            "PATH": str(bin_dir),
            "TOOLANG_GUEST_RUNTIME": str(tmp_path / "runtime"),
        },
    )

    assert completed.returncode == 127
    assert completed.stdout == ""
    assert completed.stderr.splitlines() == [
        "Docker guest image must provide uv, Python 3.8+ with pip, or curl/wget "
        "with the uv installer prerequisites.",
        f"Log: {diagnostic}",
    ]


def test_docker_guest_core_rejects_non_linux_guests(tmp_path: Path) -> None:
    completed, diagnostic = _run_docker_guest(
        tmp_path,
        install_uv=False,
        install_python=False,
        guest_system="Darwin",
    )

    assert completed.returncode == 64
    assert completed.stdout == ""
    assert completed.stderr.splitlines() == [
        "Docker guest bootstrap supports Linux containers only.",
        f"Log: {diagnostic}",
    ]


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
            "UV_TOOL_DIR": "/host/tools",
        },
        dotenv_envs={
            "DOTENV_TOKEN": "dotenv",
            "UV_TOOL_DIR": "/dotenv/tools",
        },
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
    diagnostic_path = Path(cast(str, plan.meta["diagnostic_path"]))

    with pytest.raises(ToolangError, match="Could not start docker sandbox"):
        asyncio.run(sandbox.launch(plan))

    assert not stage_dir.exists()
    assert diagnostic_path.is_file()


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
    diagnostic_path = Path(cast(str, plan.meta["diagnostic_path"]))

    with pytest.raises(SandboxLaunchError, match="could not remove") as captured:
        asyncio.run(sandbox.launch(plan))

    assert captured.value.ref.runtime_id == plan.meta["container_name"]
    assert captured.value.ref.meta["stage_dir"] == str(stage_dir)
    assert stage_dir.is_dir()
    assert diagnostic_path.is_file()


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
    diagnostic_path = Path(cast(str, plan.meta["diagnostic_path"]))

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
    assert diagnostic_path.is_file()
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
    diagnostic_path = Path(cast(str, plan.meta["diagnostic_path"]))
    ref = asyncio.run(sandbox.launch(plan))

    asyncio.run(sandbox.release(ref))

    assert calls == [("logs", ref.runtime_id), ("remove", ref.runtime_id)]
    assert cast(Path, plan.log_path).is_file()
    assert diagnostic_path.is_file()


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
    diagnostic_path = Path(cast(str, plan.meta["diagnostic_path"]))

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
    assert not diagnostic_path.exists()


def test_docker_foreground_interrupt_removes_successful_bootstrap_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    waiting = asyncio.Event()

    class Follower:
        returncode = None

        async def wait(self) -> int:
            waiting.set()
            await asyncio.Event().wait()
            raise AssertionError("cancelled follower returned")

    monkeypatch.setattr(
        docker_sandbox,
        "docker_run_detached",
        _async_value("container-123"),
    )
    monkeypatch.setattr(
        docker_sandbox,
        "docker_follow_container_logs",
        lambda _name: _async_result(Follower()),
    )
    sandbox = create_sandbox("docker", config={})
    plan = sandbox.prepare(None, _request(tmp_path, foreground=True))
    diagnostic_path = Path(cast(str, plan.meta["diagnostic_path"]))
    ref = asyncio.run(sandbox.launch(plan))

    async def interrupt_wait() -> BaseException | None:
        await sandbox.attach(plan, ref)
        task = asyncio.create_task(sandbox.wait(ref))
        await waiting.wait()
        task.cancel()
        try:
            await task
        except BaseException as exc:
            return exc
        return None

    error = asyncio.run(interrupt_wait())

    assert isinstance(error, asyncio.CancelledError)
    assert not diagnostic_path.exists()


def test_docker_background_sandbox_detaches_logs_after_readiness(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, str]] = []
    follower = cast(asyncio.subprocess.Process, object())
    monkeypatch.setattr(
        docker_sandbox,
        "docker_run_detached",
        _async_value("container-123"),
    )

    async def follow(name: str) -> asyncio.subprocess.Process:
        calls.append(("attach", name))
        return follower

    async def terminate(process: asyncio.subprocess.Process) -> None:
        assert process is follower
        calls.append(("detach", "container-123"))

    monkeypatch.setattr(docker_sandbox, "docker_follow_container_logs", follow)
    monkeypatch.setattr(docker_sandbox, "terminate_process", terminate)
    sandbox = create_sandbox("docker", config={})
    plan = sandbox.prepare(None, _request(tmp_path))
    diagnostic_path = Path(cast(str, plan.meta["diagnostic_path"]))
    ref = asyncio.run(sandbox.launch(plan))

    asyncio.run(sandbox.attach(plan, ref))
    asyncio.run(sandbox.detach(plan, ref))

    assert calls == [
        ("attach", ref.runtime_id),
        ("detach", ref.runtime_id),
    ]
    assert not diagnostic_path.exists()


def test_docker_sandbox_rejects_a_log_follower_that_exits_before_readiness(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    follower = cast(
        asyncio.subprocess.Process,
        SimpleNamespace(returncode=1),
    )
    monkeypatch.setattr(
        docker_sandbox,
        "docker_run_detached",
        _async_value("container-123"),
    )
    monkeypatch.setattr(
        docker_sandbox,
        "docker_follow_container_logs",
        lambda _name: _async_result(follower),
    )
    sandbox = create_sandbox("docker", config={})
    plan = sandbox.prepare(None, _request(tmp_path))
    ref = asyncio.run(sandbox.launch(plan))

    asyncio.run(sandbox.attach(plan, ref))

    with pytest.raises(RuntimeError, match="logs stopped"):
        asyncio.run(sandbox.running(ref))


def test_docker_release_cleans_up_an_attached_log_follower(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, str]] = []
    follower = cast(asyncio.subprocess.Process, object())
    monkeypatch.setattr(
        docker_sandbox,
        "docker_run_detached",
        _async_value("container-123"),
    )

    async def follow(name: str) -> asyncio.subprocess.Process:
        calls.append(("attach", name))
        return follower

    async def finish(process: asyncio.subprocess.Process) -> None:
        assert process is follower
        calls.append(("finish", "container-123"))

    monkeypatch.setattr(docker_sandbox, "docker_follow_container_logs", follow)
    monkeypatch.setattr(docker_sandbox, "finish_process", finish)
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

    asyncio.run(sandbox.attach(plan, ref))
    asyncio.run(sandbox.release(ref))

    assert calls == [
        ("attach", ref.runtime_id),
        ("finish", ref.runtime_id),
        ("logs", ref.runtime_id),
        ("remove", ref.runtime_id),
    ]


def _run_docker_guest(
    root: Path,
    *,
    install_uv: bool,
    install_python: bool,
    install_tool: bool = True,
    compatible_tool: bool = True,
    hosted_dev_artifact: Path | None = None,
    install_uv_with_pip: bool = False,
    guest_system: str = "Linux",
) -> tuple[subprocess.CompletedProcess[str], Path]:
    stage = root / "stage"
    stage.mkdir()
    diagnostic = root / "bootstrap.log"
    diagnostic.touch(mode=0o600)
    env_path = root / "guest.env"
    docker_guest.write_guest_env(
        env_path,
        dotenv_envs={
            "COMPLEX": 'line one\nline "two" `three`\r\\tail${HOME}',
            "LITERAL": "${HOME}/literal",
        },
        process_envs={},
    )
    instance = _sandbox_instance(root)
    script = stage / "docker_guest.sh"
    docker_guest.write_guest_script(script)

    bin_dir = root / "bin"
    bin_dir.mkdir()
    _link_commands(bin_dir, "cat", "chmod", "mkdir", "rm")
    _write_executable(bin_dir / "uname", f"#!/bin/sh\necho {guest_system}\n")
    tool_source = root / "too-source"
    _write_executable(
        tool_source,
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "if sys.argv[1:] == ['--version']:\n"
        "    print('toolang 1.2.3')\n"
        "elif sys.argv[1:] == ['serve', '--help']:\n"
        + (
            "    print('serve help')\n"
            if compatible_tool
            else "    raise SystemExit(2)\n"
        )
        + "elif sys.argv[1:] == "
        "['--root', '/root with spaces', 'serve', 'alice']:\n"
        "    print(json.dumps({\n"
        "        'complex': os.environ['COMPLEX'],\n"
        "        'instance': os.environ['TOOLANG_SANDBOX_INSTANCE'],\n"
        "        'literal': os.environ['LITERAL'],\n"
        "    }))\n"
        "else:\n"
        "    raise SystemExit(2)\n",
    )
    uv_source = root / "uv-source"
    _write_executable(
        uv_source,
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then echo "uv 9.9.9"; exit 0; fi\n'
        'if [ "$1" = "python" ] && [ "$2" = "find" ] && '
        '[ "$3" = "--help" ]; then exit 0; fi\n'
        'if [ "$1" = "python" ] && [ "$2" = "install" ] && '
        '[ "$3" = "--help" ]; then exit 0; fi\n'
        'if [ "$1" = "tool" ] && [ "$2" = "install" ] && '
        '[ "$3" = "--help" ]; then exit 0; fi\n'
        'if [ "$1" = "python" ] && [ "$2" = "find" ]; then\n'
        '  if [ "${TOOLANG_TEST_INSTALL_PYTHON:-0}" = "1" ] && '
        '[ "$5" = ">=3.11" ] && '
        '[ ! -f "$TOOLANG_TEST_PYTHON_MARKER" ]; then exit 1; fi\n'
        '  if [ "${TOOLANG_TEST_INSTALL_PYTHON:-0}" = "1" ] && '
        '[ -f "$TOOLANG_TEST_PYTHON_MARKER" ] && '
        '[ "$3" != "--managed-python" ]; then exit 2; fi\n'
        '  printf "%s\\n" "$TOOLANG_TEST_PYTHON"\n'
        "  exit 0\n"
        "fi\n"
        'if [ "$1" = "python" ] && [ "$2" = "install" ]; then\n'
        '  echo "managed Python detail"\n'
        '  : >"$TOOLANG_TEST_PYTHON_MARKER"\n'
        "  exit 0\n"
        "fi\n"
        'if [ "$1" = "tool" ] && [ "$2" = "install" ]; then\n'
        '  echo "installer detail: $*"\n'
        '  if [ "${TOOLANG_TEST_INSTALL_TOOL:-1}" != "1" ]; then exit 1; fi\n'
        '  /bin/mkdir -p "$UV_TOOL_BIN_DIR"\n'
        '  /bin/cp "$TOOLANG_TEST_TOOL_SOURCE" "$UV_TOOL_BIN_DIR/too"\n'
        '  /bin/chmod 755 "$UV_TOOL_BIN_DIR/too"\n'
        "  exit 0\n"
        "fi\n"
        "exit 2\n",
    )
    installer = root / "uv-installer"
    _write_executable(
        installer,
        "#!/bin/sh\n"
        "command -v curl >/dev/null 2>&1 || exit 9\n"
        'curl -sSfL https://example.test/archive -o "$UV_INSTALL_DIR/download"\n'
        '[ -s "$UV_INSTALL_DIR/download" ] || exit 10\n'
        'echo "uv installer detail"\n'
        '/bin/cp "$TOOLANG_TEST_UV_SOURCE" "$UV_UNMANAGED_INSTALL/uv"\n'
        '/bin/chmod 755 "$UV_UNMANAGED_INSTALL/uv"\n',
    )
    if install_uv:
        if install_uv_with_pip:
            _write_executable(
                bin_dir / "python3",
                "#!/bin/sh\n"
                'if [ "$1" = "-c" ]; then exit 0; fi\n'
                'if [ "$1" = "-m" ] && [ "$2" = "pip" ] && '
                '[ "$3" = "--version" ]; then exit 0; fi\n'
                'if [ "$1" = "-m" ] && [ "$2" = "pip" ] && '
                '[ "$3" = "install" ]; then\n'
                '  echo "pip install detail"\n'
                "  exit 0\n"
                "fi\n"
                'if [ "$1" = "-m" ] && [ "$2" = "uv" ]; then\n'
                "  shift 2\n"
                '  exec "$TOOLANG_TEST_UV_SOURCE" "$@"\n'
                "fi\n"
                "exit 2\n",
            )
        else:
            _write_executable(
                bin_dir / "curl",
                "#!/bin/sh\n"
                'echo "$*"\n'
                'while [ "$#" -gt 0 ]; do\n'
                '  if [ "$1" = "-o" ]; then\n'
                '    /bin/cp "$TOOLANG_TEST_INSTALLER" "$2"\n'
                "    exit 0\n"
                "  fi\n"
                "  shift\n"
                "done\n"
                "exit 2\n",
            )
    else:
        shutil.copy2(uv_source, bin_dir / "uv")

    completed = subprocess.run(
        (
            "/bin/sh",
            str(script),
            str(env_path),
            str(diagnostic),
            str(diagnostic),
            str(instance),
            str(hosted_dev_artifact or "toolang"),
            "too",
            "--root",
            "/root with spaces",
            "serve",
            "alice",
        ),
        check=False,
        capture_output=True,
        text=True,
        env={
            "PATH": str(bin_dir),
            "TOOLANG_GUEST_RUNTIME": str(root / "runtime"),
            "TOOLANG_TEST_INSTALLER": str(installer),
            "TOOLANG_TEST_INSTALL_PYTHON": "1" if install_python else "0",
            "TOOLANG_TEST_INSTALL_TOOL": "1" if install_tool else "0",
            "TOOLANG_TEST_PYTHON": sys.executable,
            "TOOLANG_TEST_PYTHON_MARKER": str(root / "python-installed"),
            "TOOLANG_TEST_TOOL_SOURCE": str(tool_source),
            "TOOLANG_TEST_UV_SOURCE": str(uv_source),
        },
    )
    return completed, diagnostic


def _run_try_docker_guest(
    root: Path,
    *arguments: str,
    exit_code: int = 0,
    interrupt: bool = False,
) -> tuple[subprocess.CompletedProcess[str], list[list[str]], dict[str, object]]:
    fake_bin = root / "fake docker bin"
    fake_bin.mkdir()
    log_path = root / "docker-calls.jsonl"
    facts_path = root / "docker-facts.json"
    stage_path = root / "docker-stage.txt"
    _write_executable(
        fake_bin / "docker",
        f"#!{sys.executable}\n"
        "import json, os\n"
        "from pathlib import Path\n"
        "import signal, sys, time\n"
        "args = sys.argv[1:]\n"
        "log = Path(os.environ['TOOLANG_TEST_DOCKER_LOG'])\n"
        "with log.open('a', encoding='utf-8') as stream:\n"
        "    stream.write(json.dumps(args) + '\\n')\n"
        "stage_record = Path(os.environ['TOOLANG_TEST_DOCKER_STAGE'])\n"
        "if args[0] == 'create':\n"
        "    mount = next(\n"
        "        args[index + 1] for index, value in enumerate(args)\n"
        "        if value == '--volume'\n"
        "        and args[index + 1].endswith(':/toolang-bootstrap:ro')\n"
        "    )\n"
        "    stage_record.write_text(\n"
        "        mount.removesuffix(':/toolang-bootstrap:ro'),\n"
        "        encoding='utf-8',\n"
        "    )\n"
        f"    print({_CONTAINER_ID!r})\n"
        "elif args[0] == 'start':\n"
        "    stage = Path(stage_record.read_text(encoding='utf-8'))\n"
        "    facts = {\n"
        "        'stage': str(stage),\n"
        "        'instance': (stage / 'instance').read_text(encoding='utf-8'),\n"
        "        'guest_core': (stage / 'docker_guest.sh').is_file(),\n"
        "    }\n"
        "    Path(os.environ['TOOLANG_TEST_DOCKER_FACTS']).write_text(\n"
        "        json.dumps(facts), encoding='utf-8'\n"
        "    )\n"
        "    print('guest output')\n"
        "    if os.environ.get('TOOLANG_TEST_DOCKER_INTERRUPT') == '1':\n"
        "        os.kill(os.getppid(), signal.SIGINT)\n"
        "        time.sleep(0.2)\n"
        "        raise SystemExit(130)\n"
        "    raise SystemExit(int(os.environ['TOOLANG_TEST_DOCKER_EXIT']))\n",
    )
    temporary = root / "temporary files"
    temporary.mkdir()
    environ = {
        **os.environ,
        "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}",
        "TMPDIR": str(temporary),
        "TOOLANG_TEST_DOCKER_EXIT": str(exit_code),
        "TOOLANG_TEST_DOCKER_FACTS": str(facts_path),
        "TOOLANG_TEST_DOCKER_INTERRUPT": "1" if interrupt else "0",
        "TOOLANG_TEST_DOCKER_LOG": str(log_path),
        "TOOLANG_TEST_DOCKER_STAGE": str(stage_path),
    }
    completed = subprocess.run(
        ("/bin/sh", str(_TRY_DOCKER_GUEST), *arguments),
        check=False,
        capture_output=True,
        text=True,
        env=environ,
        timeout=10,
    )
    calls = [
        cast(list[str], json.loads(line))
        for line in log_path.read_text(encoding="utf-8").splitlines()
    ]
    facts = cast(
        dict[str, object],
        json.loads(facts_path.read_text(encoding="utf-8")),
    )
    return completed, calls, facts


def _link_commands(path: Path, *names: str) -> None:
    for name in names:
        source = shutil.which(name)
        assert source is not None
        (path / name).symlink_to(source)


def _async_value(value: str) -> Any:
    async def resolve(**_kwargs: object) -> str:
        return value

    return resolve


async def _async_result(value: Any) -> Any:
    return value


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _sandbox_instance(root: Path) -> Path:
    path = root / "sandbox.instance"
    path.write_text(_CONTAINER_ID + "\n")
    return path
