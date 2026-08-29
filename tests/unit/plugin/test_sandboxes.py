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
from toolang.base.types.progress import ProgressEvent
from toolang.base.types.sandbox import SandboxMount, SandboxRef, SandboxRequest
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
    diagnostic_path = Path(cast(str, plan.meta["diagnostic_path"]))
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
    assert diagnostic_path.is_file()
    assert stat.S_IMODE(diagnostic_path.stat().st_mode) == 0o600
    assert startup_events_path.is_file()
    assert stat.S_IMODE(startup_events_path.stat().st_mode) == 0o600
    assert {item.name for item in stage_dir.iterdir()} == {
        "docker_guest.sh",
        "docker_guest.py",
        "guest.env",
        "start.json",
        "toolang-1.2.3-py3-none-any.whl",
    }
    assert (stage_dir / "docker_guest.sh").read_bytes() == (
        Path(docker_guest.__file__).with_name("docker_guest.sh").read_bytes()
    )
    assert (stage_dir / "docker_guest.py").read_bytes() == (
        Path(docker_guest.__file__).with_name("docker_guest.py").read_bytes()
    )
    assert plan.command == (
        "/bin/sh",
        "/root/.toolang/agents/alice/.runtime/sandbox/docker_guest.sh",
        "/root/.toolang/agents/alice/.runtime/sandbox/docker_guest.py",
        "/root/.toolang/agents/alice/.env",
        f"/root/.toolang/agents/alice/.runtime/{diagnostic_path.name}",
        str(diagnostic_path),
        f"/root/.toolang/agents/alice/.runtime/{startup_events_path.name}",
        "/root/.toolang/agents/alice/.runtime/sandbox/toolang-1.2.3-py3-none-any.whl",
        "/root/.toolang/agents/alice/.runtime/agent.log",
        "--",
        "too",
        "serve",
        "alice",
        "--port",
        "8123",
    )

    ref = asyncio.run(sandbox.launch(plan))

    assert ref.runtime_id == "container-123"
    assert ref.runtime_kind == "container"
    assert ref.runtime_name == container_name
    assert ref.meta["diagnostic_path"] == str(diagnostic_path)
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
    assert run_call["log_path"] is None


def test_docker_background_sandbox_uses_docker_output_and_durable_diagnostics(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    sandbox = create_sandbox("docker", config={})

    plan = sandbox.prepare(None, request)

    log_path = cast(Path, request.log_path)
    assert log_path.is_file()
    assert stat.S_IMODE(log_path.stat().st_mode) == 0o600
    stage_dir = Path(cast(str, plan.meta["stage_dir"]))
    shell = (stage_dir / "docker_guest.sh").read_text(encoding="utf-8")
    helper = (stage_dir / "docker_guest.py").read_text(encoding="utf-8")
    assert 'exec "$PYTHON_BIN" "$HELPER"' in shell
    assert 'TOOLANG_SANDBOX_INSTANCE"] = hostname' in helper
    assert "PROGRESS_EVENTS" in shell
    assert "report_progress" in helper
    assert 'source "$GUEST_ENV"' not in shell


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

    runtime_dir = tmp_path / "agents" / "alice" / ".runtime"
    assert not list(runtime_dir.glob("docker-guest-*.log"))


def test_docker_background_attach_is_quiet_without_progress(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    followed: list[str] = []

    async def follow(container_id: str) -> Any:
        followed.append(container_id)
        return cast(Any, object())

    monkeypatch.setattr(docker_sandbox, "docker_follow_container_logs", follow)
    sandbox = create_sandbox("docker", config={})
    plan = sandbox.prepare(None, _request(tmp_path))
    ref = SandboxRef("container-123", plan.endpoint)

    asyncio.run(sandbox.attach(plan, ref, progress=None, progress_id="runtime:test"))

    assert followed == []


def test_docker_guest_reuses_uv_and_preserves_generated_environment(
    tmp_path: Path,
) -> None:
    tools = tmp_path / "tools"
    tools.mkdir()
    _link_guest_utilities(tools)
    _write_fake_guest_tools(tools, python=sys.executable)
    guest_env = tmp_path / "guest.env"
    docker_guest.write_guest_env(
        guest_env,
        dotenv_envs={
            "HTTPS_PROXY": "http://proxy/$literal",
            "LITERAL": "${HOME}/`literal`\\tail",
            "MULTILINE": "first\nsecond\rthird",
        },
        process_envs={"LITERAL": "process ${HOME}"},
    )
    diagnostic = tmp_path / "diagnostic.log"
    diagnostic.touch()

    completed = subprocess.run(
        (
            "/bin/sh",
            str(Path(docker_guest.__file__).with_name("docker_guest.sh")),
            str(Path(docker_guest.__file__).with_name("docker_guest.py")),
            str(guest_env),
            str(diagnostic),
            str(diagnostic),
            "-",
            "/packages with spaces/toolang-test.whl",
            "-",
            "--",
            sys.executable,
            "-c",
            "import json, os; print(json.dumps({"
            "'instance': os.environ['TOOLANG_SANDBOX_INSTANCE'], "
            "'literal': os.environ['LITERAL'], "
            "'multiline': os.environ['MULTILINE']}))",
        ),
        check=False,
        capture_output=True,
        text=True,
        env={
            "HOSTNAME": _CONTAINER_ID[:12],
            "PATH": f"{tools}{os.pathsep}{os.environ['PATH']}",
            "TOOLANG_TEST_PYTHON": sys.executable,
            "TOOLANG_TEST_INSTALL_ENV_LOG": str(tmp_path / "install-env"),
            "TOOLANG_TEST_TOO": str(tools / "too-template"),
            "TOOLANG_GUEST_RUNTIME": str(tmp_path / "runtime"),
        },
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {
        "instance": _CONTAINER_ID[:12],
        "literal": "process ${HOME}",
        "multiline": "first\nsecond\rthird",
    }
    assert completed.stderr.splitlines() == [
        "Using uv · test",
        f"Using Python · {sys.version_info.major}.{sys.version_info.minor}."
        f"{sys.version_info.micro}",
        "Installing Toolang · toolang-test.whl...",
        "Installed Toolang · toolang test",
        "Starting command...",
    ]
    assert (tmp_path / "install-env").read_text(encoding="utf-8") == (
        "http://proxy/$literal\n"
    )
    assert not diagnostic.exists()


def test_docker_guest_uses_python_and_pip_to_install_uv(tmp_path: Path) -> None:
    tools = tmp_path / "tools"
    tools.mkdir()
    _link_guest_utilities(tools)
    driver = tools / "uv-driver"
    _write_fake_guest_tools(tools, python=sys.executable, uv_path=driver)
    _write_executable(
        tools / "python3",
        "#!/bin/sh\n"
        'if [ "$1" = "-c" ]; then exit 0; fi\n'
        'if [ "$1" = "-m" ] && [ "$2" = "pip" ]; then exit 0; fi\n'
        'if [ "$1" = "-m" ] && [ "$2" = "uv" ]; then\n'
        "  shift 2\n"
        '  exec "$TOOLANG_TEST_UV" "$@"\n'
        "fi\n"
        "exit 1\n",
    )
    guest_env = tmp_path / "guest.env"
    docker_guest.write_guest_env(guest_env, dotenv_envs={}, process_envs={})
    diagnostic = tmp_path / "diagnostic.log"
    diagnostic.touch()
    workload_log = tmp_path / "workload.log"

    completed = subprocess.run(
        (
            "/bin/sh",
            str(Path(docker_guest.__file__).with_name("docker_guest.sh")),
            str(Path(docker_guest.__file__).with_name("docker_guest.py")),
            str(guest_env),
            str(diagnostic),
            str(diagnostic),
            "-",
            "toolang",
            str(workload_log),
            "--",
            sys.executable,
            "-c",
            "print('workload')",
        ),
        check=False,
        capture_output=True,
        text=True,
        env={
            "HOSTNAME": _CONTAINER_ID[:12],
            "PATH": str(tools),
            "TOOLANG_TEST_PYTHON": sys.executable,
            "TOOLANG_TEST_TOO": str(tools / "too-template"),
            "TOOLANG_TEST_UV": str(driver),
            "TOOLANG_GUEST_RUNTIME": str(tmp_path / "runtime"),
        },
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == ""
    assert workload_log.read_text(encoding="utf-8") == "workload\n"
    assert stat.S_IMODE(workload_log.stat().st_mode) == 0o600
    assert completed.stderr.splitlines()[0] == "Installing uv..."
    assert completed.stderr.splitlines()[1] == "Installed uv · test"


def test_docker_guest_rejects_an_image_without_bootstrap_capabilities(
    tmp_path: Path,
) -> None:
    tools = tmp_path / "tools"
    tools.mkdir()
    _link_guest_utilities(tools)
    guest_env = tmp_path / "guest.env"
    docker_guest.write_guest_env(guest_env, dotenv_envs={}, process_envs={})
    diagnostic = tmp_path / "diagnostic.log"
    diagnostic.touch()

    completed = subprocess.run(
        (
            "/bin/sh",
            str(Path(docker_guest.__file__).with_name("docker_guest.sh")),
            str(Path(docker_guest.__file__).with_name("docker_guest.py")),
            str(guest_env),
            str(diagnostic),
            str(diagnostic),
            "-",
            "toolang",
            "-",
            "--",
            "too",
            "--version",
        ),
        check=False,
        capture_output=True,
        text=True,
        env={
            "HOSTNAME": _CONTAINER_ID[:12],
            "PATH": str(tools),
            "TOOLANG_GUEST_RUNTIME": str(tmp_path / "runtime"),
        },
    )

    assert completed.returncode == 69
    assert completed.stderr.splitlines() == [
        "Installing uv...",
        "Unsupported image: provide uv, Python 3.8+ with pip, curl, or wget",
        f"See {diagnostic}",
    ]
    assert diagnostic.is_file()


def test_docker_guest_requires_the_default_short_container_hostname(
    tmp_path: Path,
) -> None:
    tools = tmp_path / "tools"
    tools.mkdir()
    _link_guest_utilities(tools)
    guest_env = tmp_path / "guest.env"
    docker_guest.write_guest_env(guest_env, dotenv_envs={}, process_envs={})
    diagnostic = tmp_path / "diagnostic.log"

    completed = subprocess.run(
        (
            "/bin/sh",
            str(Path(docker_guest.__file__).with_name("docker_guest.sh")),
            str(Path(docker_guest.__file__).with_name("docker_guest.py")),
            str(guest_env),
            str(diagnostic),
            str(diagnostic),
            "-",
            "toolang",
            "-",
            "--",
            "too",
            "--version",
        ),
        check=False,
        capture_output=True,
        text=True,
        env={
            "HOSTNAME": "custom-host",
            "PATH": str(tools),
            "TOOLANG_GUEST_RUNTIME": str(tmp_path / "runtime"),
        },
    )

    assert completed.returncode == 64
    assert completed.stderr.splitlines() == [
        "docker guest hostname is not a short container ID",
        f"See {diagnostic}",
    ]


def test_docker_startup_observer_preserves_order_and_curated_failure(
    tmp_path: Path,
) -> None:
    path = tmp_path / "startup.events"
    path.write_text(
        "python.use\n"
        "uv.use\n"
        "uv.use\n"
        "unknown\n"
        "python.use\n"
        "toolang.install.running\n"
        "toolang.install.ok\n"
        "toolang.check.running\n"
        "toolang.check.failed\n",
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
        ("runtime", "create", "running", None),
        ("runtime", "create", "running", None),
        ("runtime", "create", "running", "toolang.whl"),
        ("runtime", "create", "running", None),
        ("runtime", "create", "running", None),
        (
            "runtime",
            "create",
            "failed",
            "The installed Toolang package cannot start the required AgentServer.",
        ),
    ]
    assert [event.label for event in events] == [
        "Using uv",
        "Using Python",
        "Installing Toolang from toolang.whl...",
        "Installed Toolang from toolang.whl",
        "Checking Toolang...",
        "Failed to check Toolang",
    ]


def test_docker_startup_observer_reads_final_token_after_container_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "startup.events"
    path.write_text(
        "uv.use\npython.use\ntoolang.install.running\n"
        "toolang.install.ok\ntoolang.check.running\n",
        encoding="utf-8",
    )
    events: list[ProgressEvent] = []
    monkeypatch.setattr(docker_sandbox, "docker_container_running", lambda _id: False)

    async def append_failure() -> None:
        await asyncio.sleep(0.01)
        with path.open("a", encoding="utf-8") as stream:
            stream.write("toolang.check.failed\n")

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


def test_docker_startup_event_reader_rejects_untrusted_file_shapes(
    tmp_path: Path,
) -> None:
    regular = tmp_path / "regular.events"
    regular.write_text("uv.use\n", encoding="utf-8")
    assert docker_sandbox._read_startup_events(regular) == "uv.use\n"

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
    stage_dir = Path(cast(str, plan.meta["stage_dir"]))
    assert {path.name for path in stage_dir.iterdir()} == {
        "docker_guest.sh",
        "docker_guest.py",
        "guest.env",
        "start.json",
    }

    ref = asyncio.run(sandbox.launch(plan))
    assert calls == []
    asyncio.run(sandbox.attach(plan, ref))
    assert calls == []
    result = asyncio.run(sandbox.wait(ref))

    assert result == 0
    assert calls == [
        ("logs", ref.runtime_id),
        ("log_wait", "container"),
        ("wait", ref.runtime_id),
    ]


def test_docker_log_follower_detaches_without_waiting_for_the_container() -> None:
    class Follower:
        returncode: int | None = None
        terminated = False

        async def wait(self) -> int:
            assert self.returncode is not None
            return self.returncode

        def terminate(self) -> None:
            self.terminated = True
            self.returncode = -15

        def kill(self) -> None:
            raise AssertionError("graceful follower termination should succeed")

    follower = Follower()

    asyncio.run(docker_cli.finish_process(cast(Any, follower)))

    assert follower.terminated is True


def _async_value(value: str) -> Any:
    async def resolve(**_kwargs: object) -> str:
        return value

    return resolve


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _write_fake_guest_tools(
    directory: Path,
    *,
    python: str,
    uv_path: Path | None = None,
) -> None:
    uv = uv_path or directory / "uv"
    _write_executable(
        uv,
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then echo "uv test"; exit 0; fi\n'
        'if [ "$1" = "python" ] && [ "$2" = "find" ]; then\n'
        '  if [ "${3:-}" = "--help" ]; then exit 0; fi\n'
        '  printf "%s\\n" "$TOOLANG_TEST_PYTHON"\n'
        "  exit 0\n"
        "fi\n"
        'if [ "$1" = "python" ] && [ "$2" = "install" ]; then exit 0; fi\n'
        'if [ "$1" = "tool" ] && [ "$2" = "install" ]; then\n'
        '  if [ "${3:-}" = "--help" ]; then exit 0; fi\n'
        '  if [ -n "${TOOLANG_TEST_INSTALL_ENV_LOG:-}" ]; then\n'
        '    printf "%s\\n" "${HTTPS_PROXY:-}" >"$TOOLANG_TEST_INSTALL_ENV_LOG"\n'
        "  fi\n"
        '  cp "$TOOLANG_TEST_TOO" "$UV_TOOL_BIN_DIR/too"\n'
        '  chmod 755 "$UV_TOOL_BIN_DIR/too"\n'
        "  exit 0\n"
        "fi\n"
        "exit 1\n",
    )
    _write_executable(
        directory / "too-template",
        "#!/bin/sh\n"
        'if [ "$1" = "serve" ] && [ "$2" = "--help" ]; then exit 0; fi\n'
        'if [ "$1" = "--version" ]; then echo "toolang test"; exit 0; fi\n'
        "exit 1\n",
    )
    assert Path(python).is_file()


def _link_guest_utilities(directory: Path) -> None:
    for name in ("cat", "chmod", "cp", "mkdir", "rm"):
        source = shutil.which(name)
        assert source is not None
        (directory / name).symlink_to(source)
    _write_executable(directory / "uname", "#!/bin/sh\nprintf 'Linux\\n'\n")
