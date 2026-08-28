from __future__ import annotations

import asyncio
from dataclasses import replace
import json
import os
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
from toolang.base.types.progress import ProgressEvent
from toolang.base.types.sandbox import SandboxMount, SandboxRequest
from toolang.common.layout import AgentLayout
from toolang.plugin.sandboxes import _docker_cli as docker_cli
from toolang.plugin.sandboxes import _docker_guest as docker_guest
from toolang.plugin.sandboxes import docker as docker_sandbox
from toolang.plugin.sandboxes import host as host_sandbox
from toolang.plugin.sandboxes.loading import create_sandbox


_CONTAINER_ID = "176191c1528b8e2861cc16422dee13ade59d4977c2148a9ebf5d36a06f090abb"


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
    startup_events_path = Path(cast(str, plan.meta["startup_events_path"]))
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
    assert startup_events_path.is_file()
    assert stat.S_IMODE(startup_events_path.stat().st_mode) == 0o600
    assert "bootstrap.py" in (stage_dir / "start.sh").read_text(encoding="utf-8")
    agent_script = (stage_dir / "agent.sh").read_text(encoding="utf-8")
    assert (
        "TOOLANG_SANDBOX_INSTANCE_PATH="
        "/root/.toolang/agents/alice/.runtime/sandbox/instance" in agent_script
    )
    assert "IFS= read -r TOOLANG_SANDBOX_INSTANCE" in agent_script
    assert (
        "uv tool install --quiet --no-progress --force "
        "/root/.toolang/agents/alice/.runtime/sandbox/"
        "toolang-1.2.3-py3-none-any.whl"
    ) in agent_script
    assert "exec too serve alice --port 8123" in agent_script
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
    assert sandbox_instance_path.read_text(encoding="utf-8") == "container-123\n"
    assert ref.runtime_kind == "container"
    assert ref.runtime_name == container_name
    assert ref.meta["startup_events_path"] == str(startup_events_path)
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


def test_docker_background_sandbox_keeps_errors_and_quiets_success_output(
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
    assert "TOOLANG_UV_ENSUREPIP_DIAGNOSTIC=" in agent_script
    assert "ensurepip --upgrade 2>&1" in agent_script
    assert "--root-user-action=ignore --quiet" in agent_script
    assert "uv tool install --quiet --no-progress" in agent_script
    assert docker_guest.DOCKER_TOOLANG_COMPATIBILITY_ERROR in agent_script


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


def test_docker_agent_script_quotes_a_dev_wheel_path(tmp_path: Path) -> None:
    script = tmp_path / "agent.sh"

    docker_guest.write_agent_script(
        script,
        command=("too", "serve", "alice"),
        hosted_dev_artifact=Path("/runtime/dev wheels/toolang.whl"),
        sandbox_instance_path=Path("/runtime/sandbox instance"),
        startup_events_path=Path("/runtime/startup events"),
        validation_error_to_stderr=False,
    )

    source = script.read_text(encoding="utf-8")
    assert (
        "uv tool install --quiet --no-progress --force "
        "'/runtime/dev wheels/toolang.whl'" in source
    )
    assert ">>'/runtime/startup events'" in source
    assert "exec too serve alice" in source


def test_docker_agent_script_reports_quiet_install_and_server_stages(
    tmp_path: Path,
) -> None:
    script = tmp_path / "agent.sh"
    events = tmp_path / "startup.events"
    events.touch()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_executable(bin_dir / "uv", "#!/bin/sh\nexit 0\n")
    _write_executable(
        bin_dir / "too",
        "#!/bin/sh\n"
        'if [ "$2" = "--help" ]; then exit 0; fi\n'
        'printf "server command\\n"\n',
    )
    docker_guest.write_agent_script(
        script,
        command=("too", "serve", "alice"),
        hosted_dev_artifact=None,
        sandbox_instance_path=_sandbox_instance(tmp_path),
        startup_events_path=events,
        validation_error_to_stderr=False,
    )

    completed = subprocess.run(
        ("/bin/sh", str(script)),
        check=True,
        capture_output=True,
        text=True,
        env={"HOME": str(tmp_path / "home"), "PATH": str(bin_dir)},
    )

    assert completed.stdout == "server command\n"
    assert completed.stderr == ""
    assert events.read_text(encoding="utf-8").splitlines() == [
        "install.running",
        "install.ok",
        "validate.running",
        "validate.ok",
        "server.running",
    ]


def test_docker_agent_script_waits_for_complete_sandbox_instance(
    tmp_path: Path,
) -> None:
    script = tmp_path / "agent.sh"
    instance = tmp_path / "sandbox.instance"
    instance.touch()
    events = tmp_path / "startup.events"
    events.touch()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_executable(bin_dir / "sleep", '#!/bin/sh\n/bin/sleep "$1"\n')
    _write_executable(bin_dir / "uv", "#!/bin/sh\nexit 0\n")
    _write_executable(
        bin_dir / "too",
        "#!/bin/sh\n"
        'if [ "$2" = "--help" ]; then exit 0; fi\n'
        'printf "%s\\n" "$TOOLANG_SANDBOX_INSTANCE"\n',
    )
    docker_guest.write_agent_script(
        script,
        command=("too", "serve", "alice"),
        hosted_dev_artifact=None,
        sandbox_instance_path=instance,
        startup_events_path=events,
        validation_error_to_stderr=False,
    )

    process = subprocess.Popen(
        ("/bin/sh", str(script)),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={"HOME": str(tmp_path / "home"), "PATH": str(bin_dir)},
    )
    try:
        threading.Event().wait(0.1)
        assert process.poll() is None
        instance.write_text(_CONTAINER_ID + "\n", encoding="utf-8")
        stdout, stderr = process.communicate(timeout=2)
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=2)

    assert process.returncode == 0
    assert stdout == _CONTAINER_ID + "\n"
    assert stderr == ""


def test_docker_agent_script_ignores_unwritable_startup_event_target(
    tmp_path: Path,
) -> None:
    script = tmp_path / "agent.sh"
    events = tmp_path / "startup.events"
    events.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_executable(bin_dir / "uv", "#!/bin/sh\nexit 0\n")
    _write_executable(
        bin_dir / "too",
        "#!/bin/sh\n"
        'if [ "$2" = "--help" ]; then exit 0; fi\n'
        'printf "server command\\n"\n',
    )
    docker_guest.write_agent_script(
        script,
        command=("too", "serve", "alice"),
        hosted_dev_artifact=None,
        sandbox_instance_path=_sandbox_instance(tmp_path),
        startup_events_path=events,
        validation_error_to_stderr=False,
    )

    completed = subprocess.run(
        ("/bin/sh", str(script)),
        check=False,
        capture_output=True,
        text=True,
        env={"HOME": str(tmp_path / "home"), "PATH": str(bin_dir)},
    )

    assert completed.returncode == 0
    assert completed.stdout == "server command\n"
    assert completed.stderr == ""


def test_docker_agent_script_discards_successful_uv_fallback_output(
    tmp_path: Path,
) -> None:
    script = tmp_path / "agent.sh"
    events = tmp_path / "startup.events"
    events.touch()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_executable(
        bin_dir / "python",
        "#!/bin/sh\n"
        'if [ "$2" = "ensurepip" ]; then\n'
        '  echo "ensurepip intermediate failure" >&2\n'
        "  exit 1\n"
        "fi\n"
        "printf '#!/bin/sh\\nexit 0\\n' >\"$TOOLANG_TEST_UV_PATH\"\n"
        '/bin/chmod 755 "$TOOLANG_TEST_UV_PATH"\n'
        'echo "pip noisy success" >&2\n',
    )
    _write_executable(
        bin_dir / "too",
        "#!/bin/sh\n"
        'if [ "$2" = "--help" ]; then exit 0; fi\n'
        'printf "server command\\n"\n',
    )
    docker_guest.write_agent_script(
        script,
        command=("too", "serve", "alice"),
        hosted_dev_artifact=None,
        sandbox_instance_path=_sandbox_instance(tmp_path),
        startup_events_path=events,
        validation_error_to_stderr=False,
    )

    completed = subprocess.run(
        ("/bin/sh", str(script)),
        check=False,
        capture_output=True,
        text=True,
        env={
            "HOME": str(tmp_path / "home"),
            "PATH": str(bin_dir),
            "TOOLANG_TEST_UV_PATH": str(bin_dir / "uv"),
        },
    )

    assert completed.returncode == 0
    assert completed.stdout == "server command\n"
    assert completed.stderr == ""


def test_docker_agent_script_reports_all_uv_fallback_failures(
    tmp_path: Path,
) -> None:
    script = tmp_path / "agent.sh"
    events = tmp_path / "startup.events"
    events.touch()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_executable(
        bin_dir / "python",
        "#!/bin/sh\n"
        'if [ "$2" = "ensurepip" ]; then\n'
        '  echo "ensurepip failed" >&2\n'
        "else\n"
        '  echo "pip failed" >&2\n'
        "fi\n"
        "exit 1\n",
    )
    docker_guest.write_agent_script(
        script,
        command=("too", "serve", "alice"),
        hosted_dev_artifact=None,
        sandbox_instance_path=_sandbox_instance(tmp_path),
        startup_events_path=events,
        validation_error_to_stderr=False,
    )

    completed = subprocess.run(
        ("/bin/sh", str(script)),
        check=False,
        capture_output=True,
        text=True,
        env={"HOME": str(tmp_path / "home"), "PATH": str(bin_dir)},
    )

    assert completed.returncode == 127
    assert completed.stdout == ""
    assert completed.stderr.splitlines() == [
        "ensurepip failed",
        "pip failed",
        "uv not available",
    ]
    assert events.read_text(encoding="utf-8").splitlines() == [
        "install.running",
        "install.failed",
    ]


def test_docker_agent_script_reports_curated_compatibility_failure(
    tmp_path: Path,
) -> None:
    script = tmp_path / "agent.sh"
    events = tmp_path / "startup.events"
    events.touch()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_executable(bin_dir / "uv", "#!/bin/sh\nexit 0\n")
    _write_executable(bin_dir / "too", "#!/bin/sh\nexit 2\n")
    docker_guest.write_agent_script(
        script,
        command=("too", "serve", "alice"),
        hosted_dev_artifact=None,
        sandbox_instance_path=_sandbox_instance(tmp_path),
        startup_events_path=events,
        validation_error_to_stderr=True,
    )

    completed = subprocess.run(
        ("/bin/sh", str(script)),
        check=False,
        capture_output=True,
        text=True,
        env={"HOME": str(tmp_path / "home"), "PATH": str(bin_dir)},
    )

    assert completed.returncode == 64
    assert completed.stdout == ""
    assert completed.stderr.strip() == (docker_guest.DOCKER_TOOLANG_COMPATIBILITY_ERROR)
    assert events.read_text(encoding="utf-8").splitlines() == [
        "install.running",
        "install.ok",
        "validate.running",
        "validate.failed",
    ]


def test_docker_agent_script_reports_uv_bootstrap_as_install_failure(
    tmp_path: Path,
) -> None:
    script = tmp_path / "agent.sh"
    events = tmp_path / "startup.events"
    events.touch()
    empty_bin = tmp_path / "bin"
    empty_bin.mkdir()
    docker_guest.write_agent_script(
        script,
        command=("too", "serve", "alice"),
        hosted_dev_artifact=None,
        sandbox_instance_path=_sandbox_instance(tmp_path),
        startup_events_path=events,
        validation_error_to_stderr=False,
    )

    completed = subprocess.run(
        ("/bin/sh", str(script)),
        check=False,
        capture_output=True,
        text=True,
        env={"HOME": str(tmp_path / "home"), "PATH": str(empty_bin)},
    )

    assert completed.returncode == 127
    assert completed.stdout == ""
    assert completed.stderr.strip() == "python not available"
    assert events.read_text(encoding="utf-8").splitlines() == [
        "install.running",
        "install.failed",
    ]


def test_docker_startup_observer_preserves_order_and_curated_failure(
    tmp_path: Path,
) -> None:
    path = tmp_path / "startup.events"
    path.write_text(
        "validate.running\n"
        "install.running\n"
        "install.running\n"
        "unknown\n"
        "install.ok\n"
        "validate.running\n"
        "validate.failed\n",
        encoding="utf-8",
    )
    events: list[ProgressEvent] = []

    asyncio.run(
        docker_sandbox._observe_startup_events(
            path,
            progress=events.append,
            progress_id="runtime:container",
            package_source="toolang.whl",
        )
    )

    assert [
        (event.kind, event.stage, event.status, event.detail) for event in events
    ] == [
        ("runtime", "create", "running", "toolang.whl"),
        ("runtime", "create", "running", None),
        (
            "runtime",
            "create",
            "failed",
            docker_guest.DOCKER_TOOLANG_COMPATIBILITY_ERROR,
        ),
    ]
    assert [event.label for event in events] == [
        "Installing Toolang",
        "Checking Toolang compatibility",
        "Checking Toolang compatibility",
    ]


def test_docker_startup_observer_reads_final_token_after_container_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "startup.events"
    path.write_text(
        "install.running\ninstall.ok\nvalidate.running\n",
        encoding="utf-8",
    )
    events: list[ProgressEvent] = []
    monkeypatch.setattr(docker_sandbox, "docker_container_running", lambda _id: False)

    async def append_failure() -> None:
        await asyncio.sleep(0.01)
        with path.open("a", encoding="utf-8") as stream:
            stream.write("validate.failed\n")

    async def observe() -> None:
        await asyncio.gather(
            docker_sandbox._observe_startup_events(
                path,
                progress=events.append,
                progress_id="runtime:container",
                package_source="toolang.whl",
                runtime_id="container",
            ),
            append_failure(),
        )

    asyncio.run(observe())

    assert [(event.stage, event.status) for event in events][-1] == (
        "create",
        "failed",
    )


def test_docker_launch_observer_forwards_only_closed_guest_setup_tokens(
    tmp_path: Path,
) -> None:
    path = tmp_path / "startup.events"
    path.write_text(
        "install.running\n"
        "install.ok\n"
        "validate.running\n"
        "validate.ok\n"
        "server.running\n"
        "setup.load.running\n"
        "setup.load.ok\n"
        "setup.discover.running\n"
        "setup.discover.ok\n",
        encoding="utf-8",
    )
    events: list[ProgressEvent] = []

    asyncio.run(
        docker_sandbox._observe_startup_events(
            path,
            progress=events.append,
            progress_id="runtime:container",
            setup_progress_id="setup:alice",
            package_source="toolang.whl",
        )
    )

    assert [(event.id, event.kind, event.stage, event.status) for event in events] == [
        ("runtime:container", "runtime", "create", "running"),
        ("runtime:container", "runtime", "create", "running"),
        ("setup:alice", "setup", "load", "running"),
        ("setup:alice", "setup", "load", "ok"),
        ("setup:alice", "setup", "discover", "running"),
        ("setup:alice", "setup", "discover", "ok"),
    ]


def test_docker_startup_event_reader_rejects_untrusted_file_shapes(
    tmp_path: Path,
) -> None:
    regular = tmp_path / "regular.events"
    regular.write_text("install.running\n", encoding="utf-8")
    assert docker_sandbox._read_startup_events(regular) == "install.running\n"

    oversized = tmp_path / "oversized.events"
    oversized.write_bytes(b"x" * (docker_sandbox._STARTUP_EVENT_MAX_BYTES + 1))
    assert docker_sandbox._read_startup_events(oversized) == ""

    invalid = tmp_path / "invalid.events"
    invalid.write_bytes(b"\xff")
    assert docker_sandbox._read_startup_events(invalid) == ""

    symlink = tmp_path / "symlink.events"
    symlink.symlink_to(regular)
    assert docker_sandbox._read_startup_events(symlink) == ""

    directory = tmp_path / "directory.events"
    directory.mkdir()
    assert docker_sandbox._read_startup_events(directory) == ""

    mkfifo = getattr(os, "mkfifo", None)
    if mkfifo is not None:
        fifo = tmp_path / "fifo.events"
        mkfifo(fifo)
        assert docker_sandbox._read_startup_events(fifo) == ""


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
    startup_events_path = Path(cast(str, plan.meta["startup_events_path"]))

    with pytest.raises(ToolangError, match="Could not start docker sandbox"):
        asyncio.run(sandbox.launch(plan))

    assert not stage_dir.exists()
    assert not startup_events_path.exists()


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
    startup_events_path = Path(cast(str, plan.meta["startup_events_path"]))

    with pytest.raises(SandboxLaunchError, match="could not remove") as captured:
        asyncio.run(sandbox.launch(plan))

    assert captured.value.ref.runtime_id == plan.meta["container_name"]
    assert captured.value.ref.meta["stage_dir"] == str(stage_dir)
    assert stage_dir.is_dir()
    assert startup_events_path.is_file()


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
    startup_events_path = Path(cast(str, plan.meta["startup_events_path"]))

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
    assert not startup_events_path.exists()
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
    startup_events_path = Path(cast(str, plan.meta["startup_events_path"]))
    ref = asyncio.run(sandbox.launch(plan))

    asyncio.run(sandbox.release(ref))

    assert calls == [("logs", ref.runtime_id), ("remove", ref.runtime_id)]
    assert cast(Path, plan.log_path).is_file()
    assert not startup_events_path.exists()


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
    stage_dir = Path(cast(str, plan.meta["stage_dir"]))
    assert docker_guest.DOCKER_TOOLANG_COMPATIBILITY_ERROR not in (
        stage_dir / "agent.sh"
    ).read_text(encoding="utf-8")

    ref = asyncio.run(sandbox.launch(plan))
    assert calls == []
    asyncio.run(sandbox.attach(plan, ref, progress_id="runtime:container"))
    assert calls == []
    asyncio.run(sandbox.follow(plan, ref))
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


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _sandbox_instance(root: Path) -> Path:
    path = root / "sandbox.instance"
    path.write_text(_CONTAINER_ID + "\n")
    return path
