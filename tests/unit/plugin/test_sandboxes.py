from __future__ import annotations


import asyncio
from contextlib import suppress
from dataclasses import replace
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from typing import Any, cast

from dotenv import dotenv_values
import psutil
import pytest

from toolang.base.errors import SandboxLaunchError, ToolangError
from toolang.base.protocols.sandbox import Sandbox
from toolang.base.types.progress import ProgressEvent
from toolang.base.types.sandbox import (
    SandboxMount,
    SandboxPlan,
    SandboxRef,
    SandboxRequest,
)
from toolang.common.layout import AgentLayout
from toolang.common.place_codes import (
    PLACE_CODE_EPOCH_MS,
    root_code,
    visiting_place_code,
    roaming_place_code,
)
from toolang.plugin.catalogs.models_dev.path import MODEL_CATALOG_ENV
from toolang.plugin.sandboxes.docker import _cli as docker_cli
from toolang.plugin.sandboxes.docker import _guest as docker_guest
from toolang.plugin.sandboxes.docker import sandbox as docker_sandbox
from toolang.plugin.sandboxes import host as host_sandbox
from toolang.plugin.loading import create_sandbox
from toolang.up.mounts import prepare_place_mounts


_CONTAINER_ID = "176191c1528b8e2861cc16422dee13ade59d4977c2148a9ebf5d36a06f090abb"


@pytest.mark.parametrize("terminal_phase", ["running", "exited"])
def test_foreground_launch_waits_until_creation_has_finished(
    monkeypatch, terminal_phase
):
    process = SimpleNamespace(stdin=BytesIO(), returncode=None)
    process.poll = lambda: process.returncode
    observations = []

    def inspect(identities):
        phase = "created" if not observations else terminal_phase
        observations.append(phase)
        if phase == "exited":
            process.returncode = 0
        return {
            identities[0]: docker_cli._Container(_CONTAINER_ID, phase == "running", {})
        }

    controllers = {}
    monkeypatch.setattr(docker_cli, "_FOREGROUND_CONTROLLERS", controllers)
    monkeypatch.setattr(docker_cli.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(docker_cli, "_inspect_containers", inspect)
    try:
        runtime_id = asyncio.run(
            docker_cli._run_foreground(["docker"], "toolang-alice", {}, None)
        )
        assert runtime_id == _CONTAINER_ID
        assert observations[-1] == terminal_phase
        assert controllers == {_CONTAINER_ID: process}
    finally:
        process.stdin.close()


def _mock_docker_container(
    monkeypatch: pytest.MonkeyPatch, plan: SandboxPlan, *, labels=None, running=True
) -> None:
    identity = plan.meta["docker_identity"]
    labels = (
        labels
        if labels is not None
        else {
            "io.toolang.root": identity["root"],
            "io.toolang.address": identity["address"],
            "io.toolang.launch-id": identity["launch_id"],
        }
    )
    monkeypatch.setattr(docker_cli, "docker_launch_pending", lambda _: False)
    monkeypatch.setattr(
        docker_cli,
        "_docker",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args,
            0,
            stdout=json.dumps(
                [_CONTAINER_ID, "/" + plan.meta["container_name"], running, labels]
            ),
            stderr="",
        ),
    )


@pytest.mark.parametrize(
    "prefix",
    [
        "Error: No such object:",
        "error: no such object:",
        "Error: No such container:",
        "error: no such container:",
        "Error response from daemon: No such container:",
    ],
)
def test_docker_missing_container_diagnostics_are_case_insensitive(monkeypatch, prefix):
    monkeypatch.setattr(
        docker_cli,
        "_docker",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args, 1, stdout="", stderr=f"{prefix} {_CONTAINER_ID}\n"
        ),
    )
    assert docker_cli.docker_container_running(_CONTAINER_ID) is False
    assert docker_cli.docker_container_states((_CONTAINER_ID,)) == {
        _CONTAINER_ID: False
    }


def test_docker_absence_keeps_container_name_case_significant(monkeypatch):
    monkeypatch.setattr(docker_cli, "docker_launch_pending", lambda _identity: False)
    monkeypatch.setattr(
        docker_cli,
        "_docker",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args, 1, stdout="", stderr="error: no such object: toolang-Alice\n"
        ),
    )
    assert docker_cli.docker_container_states(("toolang-Alice", "toolang-alice")) == {
        "toolang-Alice": False
    }
    with pytest.raises(RuntimeError, match="no such object"):
        docker_cli.docker_container_running("toolang-alice")


@pytest.mark.parametrize("removed", [True, False])
def test_docker_remove_waits_for_native_automatic_removal(monkeypatch, removed):
    replies = iter(
        [
            subprocess.CompletedProcess(
                [],
                1,
                stderr=f"Error: removal of container {_CONTAINER_ID} is already in progress",
            ),
            subprocess.CompletedProcess(
                [],
                1 if removed else 0,
                stderr=f"Error: No such container: {_CONTAINER_ID}" if removed else "",
            ),
        ]
    )
    calls = []

    def docker(*args, **kwargs):
        calls.append(args)
        return next(replies)

    monkeypatch.setattr(docker_cli, "_docker", docker)
    docker_cli.docker_remove_container(_CONTAINER_ID)
    assert calls == [("rm", "--force", _CONTAINER_ID)] * 2


@pytest.mark.parametrize(
    "detail",
    ["permission denied", "removal of container is already in progress"],
)
def test_docker_remove_keeps_failures_and_bounds_automatic_removal_wait(
    monkeypatch, detail
):
    calls = []

    def docker(*args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 1, stderr=detail)

    monkeypatch.setattr(docker_cli, "_docker", docker)
    monkeypatch.setattr(docker_cli, "DOCKER_REMOVE_TIMEOUT_SEC", 0)
    with pytest.raises(RuntimeError, match=detail):
        docker_cli.docker_remove_container(_CONTAINER_ID)
    assert len(calls) == 1


@pytest.mark.parametrize("prefix", ["Error: No such object:", "error: no such object:"])
def test_absent_container_with_pending_native_creation_is_unknown(monkeypatch, prefix):
    name = "toolang-alice-launch"
    monkeypatch.setattr(
        docker_cli,
        "_docker",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args, 1, stdout="", stderr=f"{prefix} {name}\n"
        ),
    )
    monkeypatch.setattr(docker_cli, "docker_launch_pending", lambda identity: True)
    with pytest.raises(RuntimeError, match="creation is still pending"):
        docker_cli.docker_container_running(name)
    assert name not in docker_cli.docker_container_states((name,))


@pytest.mark.parametrize("action", ["stop", "remove"])
def test_pending_creation_cannot_be_stopped_or_released(monkeypatch, action):
    name = "toolang-alice-launch"
    monkeypatch.setattr(docker_cli, "docker_launch_pending", lambda identity: True)
    monkeypatch.setattr(
        docker_cli,
        "_docker",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, stdout=name),
    )
    with pytest.raises(RuntimeError, match="creation is still pending"):
        if action == "stop":
            docker_cli.docker_stop_container(name, force=True)
        else:
            docker_cli.docker_remove_container(name)


@pytest.mark.parametrize("match", [True, False])
def test_pending_creation_matches_the_exact_unique_container_name(monkeypatch, match):
    helper = SimpleNamespace(
        info={
            "name": "docker",
            "cmdline": ["docker", "run", "--detach", "--name", "toolang-alice-launch"],
            "uids": SimpleNamespace(real=os.getuid()),
        },
        status=lambda: "running",
    )
    monkeypatch.setattr(docker_cli.psutil, "process_iter", lambda *args: [helper])
    name = "toolang-alice-launch" if match else "toolang-alice-other"
    assert docker_cli.docker_launch_pending(name) == match


@pytest.mark.parametrize("status", ["zombie", "dead", "running"])
def test_pending_creation_does_not_treat_exited_helpers_as_unknown(monkeypatch, status):
    helper = SimpleNamespace(
        info={
            "name": "docker",
            "cmdline": None,
            "uids": SimpleNamespace(real=os.getuid()),
        },
        status=lambda: status,
    )
    monkeypatch.setattr(docker_cli.psutil, "process_iter", lambda *args: [helper])
    if status == "running":
        with pytest.raises(RuntimeError, match="cannot verify pending"):
            docker_cli.docker_launch_pending("toolang-alice-launch")
    else:
        assert not docker_cli.docker_launch_pending("toolang-alice-launch")


@pytest.mark.parametrize("missing", [False, True])
def test_immutable_container_death_does_not_probe_unrelated_creation_helpers(
    tmp_path, monkeypatch, missing
):
    sandbox = docker_sandbox.DockerSandbox({})
    plan = sandbox.prepare(None, _request(tmp_path))
    ref = replace(sandbox.prepared_reference(plan), runtime_id=_CONTAINER_ID)
    monkeypatch.setattr(
        docker_cli,
        "_inspect_containers",
        lambda _: {
            _CONTAINER_ID: None
            if missing
            else docker_cli._Container(
                _CONTAINER_ID,
                False,
                docker_cli.docker_identity_labels(ref.meta["docker_identity"]),
            )
        },
    )

    def unrelated_helpers(*args):
        pytest.fail("immutable container death must not scan creation helpers")

    monkeypatch.setattr(docker_cli.psutil, "process_iter", unrelated_helpers)
    assert not docker_cli.docker_reference_running(ref)
    assert docker_cli.docker_reference_states((ref,)) == (False,)


def _request(
    root: Path,
    *,
    dev: Path | None = None,
    foreground: bool = False,
) -> SandboxRequest:
    home = root / "agents" / "alice"
    home.mkdir(parents=True, exist_ok=True)
    return SandboxRequest(
        local_place=root,
        local_home=home,
        hosted_place=Path("/root/.toolang"),
        hosted_home=Path("/root/.toolang/agents/alice"),
        agent_name="alice",
        agent_address="alice",
        toolang_root=root.resolve(),
        bind_host="127.0.0.1",
        endpoint_host="localhost",
        port=8123,
        endpoint="http://localhost:8123",
        command=("too", "_serve", "alice", "--port", "8123"),
        working_directory=home,
        output="inherit" if foreground else "file",
        log_path=None if foreground else home / ".runtime" / "agent.log",
        envs={
            "COMPLEX": "line one\nline \"two\" 'three'\r\\tail${HOME}",
            "EXAMPLE": "value",
            "HTTP_PROXY": "http://proxy.test:8080",
            "LITERAL": "${HOME}/literal",
            "OPENAI_API_KEY": "provider-secret",
            "AUTHORIZED_CUSTOM_KEY": "admitted-by-caller",
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
    assert plan.command[-4:] == ("_serve", "alice", "--port", "8123")
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


def test_host_foreground_sandbox_buffers_console_streams(
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

    workload = host_sandbox._launch(plan)

    assert workload.process is process
    assert captured["stderr"] is subprocess.STDOUT
    assert captured["stdout"] is not None
    assert workload.output_path is not None
    assert workload.output_path.is_file()
    host_sandbox._remove_output(workload.output_path)


def test_host_foreground_output_starts_after_ready_line(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    sandbox = create_sandbox("host", config={})
    plan = replace(
        sandbox.prepare(None, _request(tmp_path, foreground=True)),
        command=(
            sys.executable,
            "-c",
            "import sys; print('server log', file=sys.stderr)",
        ),
    )

    ref = asyncio.run(sandbox.launch(plan))
    print("Agent alice running", file=sys.stderr)
    exit_code = asyncio.run(sandbox.wait(ref))

    assert exit_code == 0
    assert capsys.readouterr().err.splitlines() == [
        "Agent alice running",
        "server log",
    ]
    asyncio.run(sandbox.release(ref))


def test_host_launch_cancellation_waits_for_creation_and_stops_the_process(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    started = threading.Event()
    finish = threading.Event()
    stopped: list[int] = []
    process = SimpleNamespace(pid=12345)
    workload = host_sandbox._HostWorkload(cast(Any, process))

    def launch(_plan: object) -> object:
        started.set()
        finish.wait(timeout=5)
        return workload

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


def test_host_launch_identity_failure_stops_created_process(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        start_new_session=True,
    )
    output = tmp_path / "output.log"
    output.touch()
    workload = host_sandbox._HostWorkload(process, output)
    monkeypatch.setattr(host_sandbox, "_launch", lambda _plan: workload)

    def fail(*args: object, **kwargs: object) -> SandboxRef:
        raise ValueError("cannot inspect host process")

    monkeypatch.setattr(host_sandbox, "_process_ref", fail)
    sandbox = create_sandbox("host", config={})
    plan = sandbox.prepare(None, _request(tmp_path, foreground=True))
    try:
        with pytest.raises(ValueError, match="cannot inspect"):
            asyncio.run(sandbox.launch(plan))
        assert process.poll() is not None
        assert not output.exists()
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)


@pytest.mark.parametrize("owned", [False, True])
@pytest.mark.parametrize("force", [False, True])
def test_host_stop_allows_final_drain_without_delaying_force(monkeypatch, owned, force):
    elapsed = 0.0
    signals = []

    def poll():
        return 0 if elapsed >= 5 or signal.SIGKILL in signals else None

    def advance(seconds):
        nonlocal elapsed
        elapsed += seconds

    def wait(*, timeout):
        nonlocal elapsed
        if poll() is not None:
            return 0
        if elapsed + timeout < 5:
            advance(timeout)
            raise subprocess.TimeoutExpired("agent", timeout)
        elapsed = 5
        return 0

    monkeypatch.setattr(host_sandbox.os, "killpg", lambda pid, sig: signals.append(sig))
    monkeypatch.setattr(host_sandbox.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(
        host_sandbox,
        "time",
        SimpleNamespace(monotonic=lambda: elapsed, sleep=advance),
    )
    monkeypatch.setattr(
        host_sandbox, "_ref_process_running", lambda ref, pid: poll() is None
    )
    if owned:
        process = SimpleNamespace(pid=123, poll=poll, wait=wait)
        stopped = host_sandbox._stop_process(cast(Any, process), force=force)
    else:
        ref = SandboxRef("123", "http://localhost:1", runtime_kind="process")
        stopped = host_sandbox._stop_ref(ref, force=force)
    assert stopped
    assert signals == [signal.SIGTERM, *([signal.SIGKILL] if force else [])]
    assert elapsed < 3 if force else 5 <= elapsed <= 10


@pytest.mark.parametrize("operation", ["running", "stop", "release"])
def test_host_cached_workload_rejects_stale_reference(
    tmp_path: Path, operation: str
) -> None:
    sandbox = host_sandbox.HostSandbox({})
    plan = replace(
        sandbox.prepare(None, _request(tmp_path, foreground=True)),
        command=(sys.executable, "-c", "import time; time.sleep(30)"),
    )
    ref = asyncio.run(sandbox.launch(plan))
    stale = replace(ref, meta={**ref.meta, "created": 0.0})
    workload = sandbox._processes[int(ref.runtime_id)]
    try:
        if operation == "running":
            assert not asyncio.run(sandbox.running(stale))
        elif operation == "stop":
            asyncio.run(sandbox.stop(stale, force=True))
            assert asyncio.run(sandbox.running(ref))
        else:
            asyncio.run(sandbox.release(stale))
            assert workload.output_path is not None and workload.output_path.exists()
    finally:
        asyncio.run(sandbox.stop(ref, force=True))
        asyncio.run(sandbox.release(ref))


def test_docker_sandbox_prepares_and_launches(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: dict[str, object] = {}

    async def fake_run_detached(**kwargs: object) -> str:
        calls["run"] = kwargs
        return _CONTAINER_ID

    monkeypatch.setattr(
        "toolang.plugin.sandboxes.docker.sandbox.docker_run_detached",
        fake_run_detached,
    )
    monkeypatch.setattr(
        "toolang.plugin.sandboxes.docker.sandbox.docker_container_running",
        lambda name: name == _CONTAINER_ID,
    )
    dev = tmp_path / "dist" / "toolang-1.2.3-py3-none-any.whl"
    dev.parent.mkdir(parents=True)
    dev.write_bytes(b"wheel")
    (tmp_path / "shared").mkdir()
    control_lock = AgentLayout.resident(tmp_path, "alice").runtime_status.with_suffix(
        ".lock"
    )
    control_lock.parent.mkdir(parents=True)
    control_lock.write_text("host control\n", encoding="utf-8")
    sandbox = create_sandbox("docker", config={})

    plan = sandbox.prepare("python:3.13-slim", _request(tmp_path, dev=dev))
    _mock_docker_container(monkeypatch, plan)

    assert plan.sandbox == "docker:python:3.13-slim"
    assert plan.working_directory == Path("/root/.toolang/agents/alice")
    container_name = cast(str, plan.meta["container_name"])
    assert container_name == f"toolang-{root_code(str(tmp_path.resolve()))}-alice"
    mounted = {(item.local_path, item.hosted_path) for item in plan.mounts}
    assert (tmp_path / "shared", Path("/root/.toolang/shared")) in mounted
    assert (
        tmp_path / "agents" / "alice",
        Path("/root/.toolang/agents/alice"),
    ) in mounted
    guest_env_mount = next(
        item
        for item in plan.mounts
        if item.hosted_path == Path("/root/.toolang/.alice.env")
    )
    assert guest_env_mount.read_only is True
    assert dotenv_values(guest_env_mount.local_path, interpolate=False) == {
        "COMPLEX": "line one\nline \"two\" 'three'\r\\tail${HOME}",
        "EXAMPLE": "value",
        "HTTP_PROXY": "http://proxy.test:8080",
        "LITERAL": "${HOME}/literal",
        "OPENAI_API_KEY": "provider-secret",
        "AUTHORIZED_CUSTOM_KEY": "admitted-by-caller",
    }
    guest_env_source = guest_env_mount.local_path.read_text(encoding="utf-8")
    assert guest_env_source.startswith("# Explicit sandbox bindings\n")
    assert "\n# Resolved workload values\n" in guest_env_source
    assert guest_env_source.count("OPENAI_API_KEY=") == 1
    # Direct sandbox callers supply an already selected environment.
    assert "AUTHORIZED_CUSTOM_KEY" in guest_env_source
    assert stat.S_IMODE(guest_env_mount.local_path.stat().st_mode) == 0o600
    stage_dir = Path(cast(str, plan.meta["stage_dir"]))
    diagnostic_path = Path(cast(str, plan.meta["diagnostic_path"]))
    startup_events_path = Path(cast(str, plan.meta["startup_events_path"]))
    stage_mount = next(
        item
        for item in plan.mounts
        if item.hosted_path == Path("/root/.toolang/.runtime/active/alice")
    )
    assert stage_mount.read_only is True
    control_state = AgentLayout.resident(tmp_path, "alice").runtime_status.resolve()
    assert any(
        control_state.is_relative_to(mount.local_path.resolve()) and not mount.read_only
        for mount in plan.mounts
    )
    assert control_lock.read_text(encoding="utf-8") == "host control\n"
    assert stage_dir.parent == tmp_path / ".runtime" / "active" / "alice" / "docker"
    assert (
        stage_dir.name
        == sha256(plan.meta["docker_identity"]["launch_id"].encode()).hexdigest()
    )
    assert diagnostic_path.is_file()
    assert stat.S_IMODE(diagnostic_path.stat().st_mode) == 0o600
    assert startup_events_path.is_file()
    assert stat.S_IMODE(startup_events_path.stat().st_mode) == 0o600
    assert {item.name for item in stage_dir.iterdir()} == {"active"}
    assert {item.name for item in (stage_dir / "active").iterdir()} == {
        "guest.sh",
        "info.json",
        "guest.env",
        "toolang-1.2.3-py3-none-any.whl",
    }
    assert (stage_dir / "active/guest.sh").read_bytes() == (
        Path(docker_guest.__file__).with_name("guest.sh").read_bytes()
    )
    assert not (stage_dir / "active/guest.py").exists()
    assert plan.command == (
        "/bin/sh",
        "/root/.toolang/.runtime/active/alice/guest.sh",
        f"/root/.toolang/agents/alice/.runtime/{diagnostic_path.name}",
        str(diagnostic_path),
        f"/root/.toolang/agents/alice/.runtime/{startup_events_path.name}",
        "/root/.toolang/.runtime/active/alice/toolang-1.2.3-py3-none-any.whl",
        "/root/.toolang/agents/alice/.runtime/agent.log",
        "--",
        "too",
        "_serve",
        "alice",
        "--port",
        "8123",
        "--environment-file",
        "/root/.toolang/.runtime/active/alice/guest.env",
    )

    ref = asyncio.run(sandbox.launch(plan))

    assert ref.runtime_id == _CONTAINER_ID
    assert ref.runtime_kind == "container"
    assert ref.runtime_name == container_name
    assert ref.meta["diagnostic_path"] == str(diagnostic_path)
    assert ref.meta["startup_events_path"] == str(startup_events_path)
    assert ref.endpoint == "http://localhost:8123"
    assert asyncio.run(sandbox.running(ref)) is True
    run_call = cast(dict[str, Any], calls["run"])
    assert run_call["image"] == "python:3.13-slim"
    assert run_call["labels"] == {
        "io.toolang.root": str(tmp_path.resolve()),
        "io.toolang.address": "alice",
        "io.toolang.launch-id": plan.meta["docker_identity"]["launch_id"],
    }
    assert run_call["published_port"] == 8123
    assert run_call["hosted_port"] == 8123
    assert run_call["env_values"] == {
        "TOOLANG_HOST_GATEWAY": "host.docker.internal",
        "TOOLANG_ROOT": "/root/.toolang",
        "TOOLANG_SANDBOX": "docker:python:3.13-slim",
        "TOOLANG_WORKSPACE_MOUNTS": "{}",
        "TOOLANG_WORKSPACE_LOCATION": "guest",
    }
    assert run_call["log_path"] is None


def test_docker_sandbox_mounts_external_model_catalog_at_guest_path(
    tmp_path: Path,
) -> None:
    external = tmp_path.parent / f"{tmp_path.name}-models.json"
    external.write_text("{}", encoding="utf-8")
    request = _request(tmp_path)
    request = replace(
        request,
        envs={**request.envs, MODEL_CATALOG_ENV: str(external)},
    )

    plan = create_sandbox("docker", config={}).prepare(None, request)
    source_mount = next(
        mount for mount in plan.mounts if mount.local_path == external.resolve()
    )
    guest_env_mount = next(
        mount
        for mount in plan.mounts
        if mount.hosted_path == Path("/root/.toolang/.alice.env")
    )
    guest_envs = dotenv_values(guest_env_mount.local_path, interpolate=False)

    assert source_mount.read_only is True
    assert source_mount.hosted_path.is_relative_to(
        request.hosted_place / ".inputs" / "models"
    )
    assert guest_envs[MODEL_CATALOG_ENV] == str(source_mount.hosted_path)
    assert str(external) not in guest_env_mount.local_path.read_text(encoding="utf-8")


def test_explicit_catalog_does_not_require_a_valid_default_root_catalog(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    (tmp_path / "catalog.json").symlink_to(tmp_path / "missing.json")
    selected = tmp_path / "selected.json"
    selected.write_text("{}")
    request = replace(
        request,
        envs={MODEL_CATALOG_ENV: str(selected)},
        mounts=prepare_place_mounts(request.local_place, request.hosted_place),
    )

    plan = create_sandbox("docker", config={}).prepare(None, request)

    assert (
        SandboxMount(
            selected.resolve(), request.hosted_place / "selected.json", read_only=True
        )
        in plan.mounts
    )


@pytest.mark.parametrize("filename", ["models.json", "catalog.json"])
def test_docker_sandbox_rewrites_root_model_catalog_and_mounts_it_read_only(
    tmp_path: Path,
    filename: str,
) -> None:
    source = tmp_path / filename
    source.write_text("{}", encoding="utf-8")
    request = _request(tmp_path)
    request = replace(
        request,
        dotenv_envs={**request.dotenv_envs, MODEL_CATALOG_ENV: str(source)},
        mounts=prepare_place_mounts(request.local_place, request.hosted_place),
    )

    plan = create_sandbox("docker", config={}).prepare(None, request)
    expected = request.hosted_place / filename
    source_mount = next(
        mount for mount in plan.mounts if mount.local_path == source.resolve()
    )
    guest_env_mount = next(
        mount
        for mount in plan.mounts
        if mount.hosted_path == Path("/root/.toolang/.alice.env")
    )

    assert source_mount == SandboxMount(source.resolve(), expected, read_only=True)
    assert sum(mount.hosted_path == expected for mount in plan.mounts) == 1
    assert dotenv_values(guest_env_mount.local_path, interpolate=False)[
        MODEL_CATALOG_ENV
    ] == str(expected)


@pytest.mark.parametrize(
    "version", ["0.4.0", "0.4.0a1", "0.4.0b1", "0.4.0rc1", "0.4.0.dev1"]
)
def test_docker_background_sandbox_uses_docker_output_and_durable_diagnostics(
    tmp_path: Path,
    monkeypatch,
    version,
) -> None:
    monkeypatch.setattr(docker_sandbox, "base_toolang_version", lambda: version)
    request = _request(tmp_path)
    sandbox = create_sandbox("docker", config={})

    plan = sandbox.prepare(None, request)

    assert plan.command[5] == f"toolang=={version}"
    log_path = cast(Path, request.log_path)
    assert log_path.is_file()
    assert stat.S_IMODE(log_path.stat().st_mode) == 0o600
    stage_dir = Path(cast(str, plan.meta["stage_dir"]))
    assert (stage_dir / "active/guest.sh").is_file()
    assert not (stage_dir / "active/guest.py").exists()


def test_docker_sandbox_orders_nested_mounts_after_the_agent_home(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    nested = SandboxMount(
        tmp_path / "external-agent.too",
        request.hosted_home / "agent.too",
        read_only=True,
    )
    request = replace(request, mounts=(*request.mounts, nested))

    plan = create_sandbox("docker", config={}).prepare(None, request)

    home = SandboxMount(request.local_home, request.hosted_home)
    assert plan.mounts.index(home) < plan.mounts.index(nested)


def test_docker_background_log_must_be_inside_the_agent_home(
    tmp_path: Path,
) -> None:
    request = replace(_request(tmp_path), log_path=tmp_path / "outside.log")
    sandbox = create_sandbox("docker", config={})

    with pytest.raises(ValueError, match="inside the agent home"):
        sandbox.prepare(None, request)

    assert not (tmp_path / ".sandbox" / "alice").exists()
    assert not (tmp_path / "outside.log").exists()


def test_docker_sandbox_reuses_stage_only_after_release(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        docker_sandbox,
        "docker_run_detached",
        _async_value(_CONTAINER_ID),
    )
    monkeypatch.setattr(
        docker_sandbox,
        "docker_append_container_logs",
        lambda *_: None,
    )
    monkeypatch.setattr(docker_sandbox, "docker_remove_container", lambda _name: None)
    sandbox = create_sandbox("docker", config={})

    first = sandbox.prepare(None, _request(tmp_path))
    _mock_docker_container(monkeypatch, first)
    ref = asyncio.run(sandbox.launch(first))
    asyncio.run(sandbox.release(ref))
    second = sandbox.prepare(None, _request(tmp_path))

    first_stage = Path(cast(str, first.meta["stage_dir"]))
    second_stage = Path(cast(str, second.meta["stage_dir"]))
    assert first_stage != second_stage
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
    ref = SandboxRef(_CONTAINER_ID, plan.endpoint)

    asyncio.run(sandbox.attach(plan, ref, progress=None, progress_id="runtime:test"))

    assert followed == []


@pytest.mark.parametrize("native_uv", [False, True])
def test_docker_guest_preserves_image_environment_and_scopes_uv_overrides(
    tmp_path: Path, native_uv: bool
) -> None:
    tools = tmp_path / "tools"
    tools.mkdir()
    _link_guest_utilities(tools)
    _write_fake_guest_tools(tools, python=sys.executable)
    diagnostic = tmp_path / "diagnostic.log"
    runtime = tmp_path / "runtime"
    uv_names = (
        "UV_CACHE_DIR",
        "UV_PYTHON_INSTALL_DIR",
        "UV_PYTHON_BIN_DIR",
        "UV_TOOL_DIR",
        "UV_TOOL_BIN_DIR",
        "UV_NO_PROGRESS",
    )
    uv_values = {name: f"image-{name}" for name in uv_names} if native_uv else {}
    environ = {
        "HOSTNAME": _CONTAINER_ID[:12],
        "PATH": f"{tools}{os.pathsep}{os.environ['PATH']}",
        "TOOLANG_TEST_PYTHON": sys.executable,
        "TOOLANG_TEST_INSTALL_ENV_LOG": str(tmp_path / "install-env"),
        "TOOLANG_TEST_TOO": str(tools / "too-template"),
        "TOOL_BIN_DIR": "image-bin",
        "TOO_BIN": "image-too",
        "UV_MODE": "image-mode",
        "UV_PYTHON": "image-python",
        "PYTHON_BIN": "image-python",
        "DIAGNOSTIC": "image-diagnostic",
        "PACKAGE_SOURCE": "image-package",
        "PROGRESS_EVENTS": "image-events",
        "PIP_TARGET": "/image-dependencies",
        "IMAGE_CUSTOM": "image-value",
        "PYTHON_VERSION": "image-python-version",
        "UV_VERSION": "image-uv-version",
        "RUNTIME_ROOT": "image-runtime",
        "CACHE_DIR": "image-cache",
        "CARGO_HOME": "/usr/local/cargo",
        "RUSTUP_HOME": "/usr/local/rustup",
        "HOME": str(tmp_path / "container-home"),
        "LANG": "C",
        "HTTPS_PROXY": "http://image-proxy/$literal",
        "TOOLANG_GUEST_RUNTIME": str(runtime),
        "PYTHONPATH": str(tmp_path / "image-python"),
        **uv_values,
    }
    completed = subprocess.run(
        (
            "/bin/sh",
            str(Path(docker_guest.__file__).with_name("guest.sh")),
            str(diagnostic),
            str(diagnostic),
            "-",
            "/packages with spaces/toolang-test.whl",
            "-",
            "--",
            sys.executable,
            "-c",
            "import json, os; print(json.dumps(dict(os.environ)))",
        ),
        check=False,
        capture_output=True,
        text=True,
        env=environ,
    )
    assert completed.returncode == 0, completed.stderr
    observed = json.loads(completed.stdout)
    for name, value in environ.items():
        assert observed[name] == (
            str(runtime / "bin") + os.pathsep + value if name == "PATH" else value
        )
    assert observed["TOOLANG_SANDBOX_INSTANCE"] == _CONTAINER_ID[:12]
    assert {name: observed[name] for name in uv_names if name in observed} == uv_values
    assert (runtime / "bin/too").is_file()
    assert completed.stderr.splitlines() == [
        "Using uv · test",
        f"Using Python · {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "Installing Toolang · toolang-test.whl...",
        "Installed Toolang · toolang test",
        "Starting command...",
    ]
    assert (tmp_path / "install-env").read_text() == "http://image-proxy/$literal\n"
    assert not diagnostic.exists()


@pytest.mark.parametrize("command", ["too", "toolang", "custom"])
def test_docker_guest_executes_the_real_command_with_original_pid_and_arguments(
    tmp_path: Path, command: str
) -> None:
    tools = tmp_path / "tools"
    tools.mkdir()
    _link_guest_utilities(tools)
    _write_fake_guest_tools(tools, python=sys.executable)
    probe = (
        "import json, os, sys\n"
        "if sys.argv[1:] == ['_serve', '--help']: sys.exit(0)\n"
        "try: os.fstat(3)\n"
        "except OSError: pass\n"
        "else: raise AssertionError('bootstrap console leaked into the workload')\n"
        "print(json.dumps({'pid': os.getpid(), 'args': sys.argv[1:]}))\n"
        "sys.exit(37)\n"
    )
    _write_executable(tools / "too-template", f"#!{sys.executable}\n{probe}")
    # A preexisting image CLI must never shadow the newly installed entry point.
    _write_executable(tools / "too", "#!/bin/sh\nexit 99\n")
    diagnostic = tmp_path / "diagnostic.log"
    events = tmp_path / "startup.events"
    log = tmp_path / "workload log"
    log.touch(mode=0o644)
    log.chmod(0o644)
    arguments = ["argument with spaces", "$literal", "$(literal)", "", "line\nbreak"]
    workload = (
        [sys.executable, "-c", probe, *arguments]
        if command == "custom"
        else [command, "_serve", *arguments]
    )
    with subprocess.Popen(
        [
            "/bin/sh",
            str(Path(docker_guest.__file__).with_name("guest.sh")),
            str(diagnostic),
            str(diagnostic),
            str(events),
            "/packages with spaces/toolang.whl",
            str(log),
            "--",
            *workload,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={
            "HOSTNAME": _CONTAINER_ID[:12],
            "PATH": str(tools),
            "TOOLANG_GUEST_RUNTIME": str(tmp_path / "runtime with spaces"),
            "TOOLANG_TEST_PYTHON": sys.executable,
            "TOOLANG_TEST_TOO": str(tools / "too-template"),
        },
    ) as process:
        stdout, stderr = process.communicate(timeout=15)
    assert process.returncode == 37, stderr
    assert stdout == stderr == ""
    assert json.loads(log.read_text()) == {
        "pid": process.pid,
        "args": arguments if command == "custom" else ["_serve", *arguments],
    }
    assert stat.S_IMODE(log.stat().st_mode) == 0o600
    assert not diagnostic.exists()
    assert events.read_text().splitlines() == [
        "uv.use",
        "python.use",
        "toolang.install.running",
        "toolang.install.ok",
        "toolang.check.running",
        "toolang.check.ok",
        "server.running",
    ]


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
    docker_guest.write_guest_env(
        guest_env,
        dotenv_envs={},
        process_envs={
            "PATH": str(tools),
            "TOOLANG_TEST_TOO": str(tools / "too-template"),
            "TOOLANG_TEST_UV": str(driver),
        },
    )
    diagnostic = tmp_path / "diagnostic.log"
    diagnostic.touch()
    workload_log = tmp_path / "workload.log"

    completed = subprocess.run(
        (
            "/bin/sh",
            str(Path(docker_guest.__file__).with_name("guest.sh")),
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


@pytest.mark.parametrize("invalid_path", ["directory", "missing-parent"])
def test_docker_guest_reports_unusable_workload_log(
    tmp_path: Path, invalid_path: str
) -> None:
    tools = tmp_path / "tools"
    tools.mkdir()
    _link_guest_utilities(tools)
    _write_fake_guest_tools(tools, python=sys.executable)
    log = tmp_path / "workload.log"
    if invalid_path == "directory":
        log.mkdir()
    else:
        log = tmp_path / "missing" / "workload.log"
    diagnostic = tmp_path / "diagnostic.log"
    events = tmp_path / "events"
    completed = subprocess.run(
        (
            "/bin/sh",
            str(Path(docker_guest.__file__).with_name("guest.sh")),
            str(diagnostic),
            str(diagnostic),
            str(events),
            "toolang",
            str(log),
            "--",
            "too",
            "--version",
        ),
        capture_output=True,
        text=True,
        timeout=15,
        env={
            "HOSTNAME": _CONTAINER_ID[:12],
            "PATH": str(tools),
            "TOOLANG_GUEST_RUNTIME": str(tmp_path / "runtime"),
            "TOOLANG_TEST_PYTHON": sys.executable,
            "TOOLANG_TEST_TOO": str(tools / "too-template"),
        },
    )
    assert completed.returncode == 73
    assert completed.stdout == ""
    assert completed.stderr.splitlines() == [
        "Could not open workload log",
        f"See {diagnostic}",
    ]
    assert diagnostic.is_file()
    assert stat.S_IMODE(diagnostic.stat().st_mode) == 0o600
    assert events.read_text().splitlines()[-1] == "toolang.check.ok"


def test_docker_guest_keeps_the_open_workload_log_during_rotation(
    tmp_path: Path,
) -> None:
    tools = tmp_path / "tools"
    tools.mkdir()
    _link_guest_utilities(tools)
    _write_fake_guest_tools(tools, python=sys.executable)
    log = tmp_path / "workload.log"
    diagnostic = tmp_path / "diagnostic.log"
    events = tmp_path / "events"
    (tools / "rm").unlink()
    # Synchronize rotation with diagnostic retirement, immediately before exec.
    _write_executable(
        tools / "rm",
        f"#!{sys.executable}\n"
        "import os, sys\n"
        "from pathlib import Path\n"
        "log = Path(os.environ['TOOLANG_TEST_LOG'])\n"
        "log.rename(log.with_suffix('.rotated'))\n"
        "log.mkdir()\n"
        "Path(sys.argv[-1]).unlink()\n",
    )
    completed = subprocess.run(
        (
            "/bin/sh",
            str(Path(docker_guest.__file__).with_name("guest.sh")),
            str(diagnostic),
            str(diagnostic),
            str(events),
            "toolang",
            str(log),
            "--",
            sys.executable,
            "-c",
            "print('workload')",
        ),
        capture_output=True,
        text=True,
        timeout=15,
        env={
            "HOSTNAME": _CONTAINER_ID[:12],
            "PATH": str(tools),
            "TOOLANG_GUEST_RUNTIME": str(tmp_path / "runtime"),
            "TOOLANG_TEST_PYTHON": sys.executable,
            "TOOLANG_TEST_TOO": str(tools / "too-template"),
            "TOOLANG_TEST_LOG": str(log),
        },
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == completed.stderr == ""
    assert log.is_dir()
    assert log.with_suffix(".rotated").read_text() == "workload\n"
    assert stat.S_IMODE(log.with_suffix(".rotated").stat().st_mode) == 0o600
    assert not diagnostic.exists()
    assert events.read_text().splitlines()[-1] == "server.running"


def test_docker_guest_reports_an_unusable_diagnostic(tmp_path: Path) -> None:
    tools = tmp_path / "tools"
    tools.mkdir()
    _link_guest_utilities(tools)
    diagnostic = tmp_path / "diagnostic"
    diagnostic.mkdir()
    completed = subprocess.run(
        (
            "/bin/sh",
            str(Path(docker_guest.__file__).with_name("guest.sh")),
            str(diagnostic),
            str(diagnostic),
            "-",
            "toolang",
            "-",
            "--",
            "too",
            "--version",
        ),
        capture_output=True,
        text=True,
        timeout=15,
        env={
            "HOSTNAME": _CONTAINER_ID[:12],
            "PATH": str(tools),
            "TOOLANG_GUEST_RUNTIME": str(tmp_path / "runtime"),
        },
    )
    assert completed.returncode == 73
    assert completed.stdout == ""
    assert f"docker guest diagnostic is not writable: {diagnostic}" in completed.stderr


@pytest.mark.parametrize("failure", ["missing", "permission"])
def test_docker_guest_preserves_command_exec_failure(
    tmp_path: Path, failure: str
) -> None:
    tools = tmp_path / "tools"
    tools.mkdir()
    _link_guest_utilities(tools)
    _write_fake_guest_tools(tools, python=sys.executable)
    command = tmp_path / "command"
    if failure == "permission":
        command.write_text("#!/bin/sh\nexit 0\n")
        command.chmod(0o644)
    diagnostic = tmp_path / "diagnostic.log"
    log = tmp_path / "workload.log"
    completed = subprocess.run(
        (
            "/bin/sh",
            str(Path(docker_guest.__file__).with_name("guest.sh")),
            str(diagnostic),
            str(diagnostic),
            "-",
            "toolang",
            str(log),
            "--",
            str(command),
        ),
        capture_output=True,
        text=True,
        timeout=15,
        env={
            "HOSTNAME": _CONTAINER_ID[:12],
            "PATH": str(tools),
            "TOOLANG_GUEST_RUNTIME": str(tmp_path / "runtime"),
            "TOOLANG_TEST_PYTHON": sys.executable,
            "TOOLANG_TEST_TOO": str(tools / "too-template"),
        },
    )
    # macOS's older /bin/sh and Linux shells report exec failures differently.
    reference = subprocess.run(
        ("/bin/sh", "-eu", "-c", 'exec "$@"', "sh", str(command)),
        capture_output=True,
        timeout=15,
    )
    assert completed.returncode == reference.returncode
    assert completed.stdout == ""
    assert "Could not open workload log" not in completed.stderr
    assert str(command) in log.read_text()
    assert stat.S_IMODE(log.stat().st_mode) == 0o600
    assert not diagnostic.exists()


@pytest.mark.parametrize(
    "acquisition", ["native", "pip", "curl", "wget", "pip-failure", "broken-uv"]
)
@pytest.mark.parametrize("managed_python", [False, True])
def test_docker_guest_preserves_uv_acquisition_and_isolated_installation(
    tmp_path: Path, acquisition: str, managed_python: bool
) -> None:
    tools = tmp_path / "tools"
    tools.mkdir()
    _link_guest_utilities(tools)
    driver = tools / ("uv" if acquisition == "native" else "uv-driver")
    _write_fake_guest_tools(tools, python=sys.executable, uv_path=driver)
    driver.write_text(
        driver.read_text().replace(
            '  printf "%s\\n" "$TOOLANG_TEST_PYTHON"',
            '  if [ "$3" = ">=3.11,<4" ] && '
            '[ "$TOOLANG_TEST_MANAGED_PYTHON" = 1 ]; then exit 1; fi\n'
            '  printf "%s\\n" "$TOOLANG_TEST_PYTHON"',
        )
    )
    pip_log = tmp_path / "pip-arguments.json"
    if acquisition in {"pip", "pip-failure"}:
        _write_executable(
            tools / "python3",
            f"#!{sys.executable}\n"
            "import json, os, sys\n"
            "from pathlib import Path\n"
            "if sys.argv[1] == '-c':\n"
            "    sys.version_info = (3, 10, 0)\n"
            "    exec(sys.argv[2])\n"
            "    sys.exit(0)\n"
            "if sys.argv[1:4] == ['-m', 'pip', '--version']:\n"
            "    print('pip probe output must stay in diagnostics')\n"
            "    sys.exit(0)\n"
            "if sys.argv[1:4] == ['-m', 'pip', 'install']:\n"
            "    Path(os.environ['TOOLANG_TEST_PIP_LOG']).write_text(json.dumps(sys.argv[1:]))\n"
            "    sys.exit(int(os.environ['TOOLANG_TEST_PIP_FAIL']))\n"
            "if sys.argv[1:3] == ['-m', 'uv']:\n"
            "    uv = os.environ['TOOLANG_TEST_UV']\n"
            "    os.execv(uv, [uv, *sys.argv[3:]])\n"
            "sys.exit(1)\n",
        )
    if acquisition not in {"native", "pip"}:
        installer = tools / "installer"
        _write_executable(
            installer,
            '#!/bin/sh\nmkdir -p "$UV_UNMANAGED_INSTALL"\n'
            'cp "$TOOLANG_TEST_UV" "$UV_UNMANAGED_INSTALL/uv"\n'
            'chmod 755 "$UV_UNMANAGED_INSTALL/uv"\n',
        )
        downloader = "wget" if acquisition == "wget" else "curl"
        output = "$2" if downloader == "wget" else "$4"
        _write_executable(
            tools / downloader,
            f'#!/bin/sh\ncp "$TOOLANG_TEST_INSTALLER" "{output}"\n',
        )
    if acquisition == "broken-uv":
        _write_executable(tools / "uv", "#!/bin/sh\nexit 1\n")
    runtime = tmp_path / "private runtime"
    diagnostic = tmp_path / "diagnostic.log"
    completed = subprocess.run(
        (
            "/bin/sh",
            str(Path(docker_guest.__file__).with_name("guest.sh")),
            str(diagnostic),
            str(diagnostic),
            "-",
            "/packages with spaces/toolang.whl",
            "-",
            "--",
            "too",
            "--version",
        ),
        capture_output=True,
        text=True,
        timeout=15,
        env={
            "HOSTNAME": _CONTAINER_ID[:12],
            "PATH": str(tools),
            "TOOLANG_GUEST_RUNTIME": str(runtime),
            "TOOLANG_TEST_PYTHON": sys.executable,
            "TOOLANG_TEST_TOO": str(tools / "too-template"),
            "TOOLANG_TEST_UV": str(driver),
            "TOOLANG_TEST_PIP_LOG": str(pip_log),
            "TOOLANG_TEST_PIP_FAIL": "1" if acquisition == "pip-failure" else "0",
            "TOOLANG_TEST_MANAGED_PYTHON": "1" if managed_python else "0",
            "TOOLANG_TEST_INSTALLER": str(tools / "installer"),
            "PIP_TARGET": "/image-dependencies",
        },
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == "toolang test\n"
    assert "pip probe output" not in completed.stderr
    assert ("Using uv" in completed.stderr) == (acquisition == "native")
    assert ("Installed Python" in completed.stderr) == managed_python
    assert (runtime / "bin/install-arguments").read_text().splitlines() == [
        "tool",
        "install",
        "--quiet",
        "--no-progress",
        "--force",
        "--python",
        sys.executable,
        "/packages with spaces/toolang.whl",
    ]
    if acquisition in {"pip", "pip-failure"}:
        arguments = json.loads(pip_log.read_text())
        assert arguments[-3:] == ["--target", str(runtime / "uv-module"), "uv==0.12.6"]
    assert not diagnostic.exists()


@pytest.mark.parametrize("failure", ["install", "missing-cli", "invalid-cli"])
def test_docker_guest_retains_private_failure_diagnostics(
    tmp_path: Path, failure: str
) -> None:
    tools = tmp_path / "tools"
    tools.mkdir()
    _link_guest_utilities(tools)
    _write_fake_guest_tools(tools, python=sys.executable)
    if failure == "missing-cli":
        uv = tools / "uv"
        uv.write_text(
            uv.read_text().replace(
                '  cp "$TOOLANG_TEST_TOO" "$UV_TOOL_BIN_DIR/too"', "  exit 0"
            )
        )
    elif failure == "invalid-cli":
        _write_executable(
            tools / "too-template",
            "#!/bin/sh\necho private validation failure >&2\nexit 1\n",
        )
    diagnostic = tmp_path / "diagnostic.log"
    events = tmp_path / "events"
    completed = subprocess.run(
        (
            "/bin/sh",
            str(Path(docker_guest.__file__).with_name("guest.sh")),
            str(diagnostic),
            str(diagnostic),
            str(events),
            "toolang",
            "-",
            "--",
            "too",
            "--version",
        ),
        capture_output=True,
        text=True,
        timeout=15,
        env={
            "HOSTNAME": _CONTAINER_ID[:12],
            "PATH": str(tools),
            "TOOLANG_GUEST_RUNTIME": str(tmp_path / "runtime"),
            "TOOLANG_TEST_PYTHON": sys.executable,
            "TOOLANG_TEST_TOO": str(tools / "too-template"),
            "TOOLANG_TEST_INSTALL_FAIL": "1" if failure == "install" else "0",
        },
    )
    assert completed.returncode == (1 if failure == "install" else 69)
    assert completed.stdout == ""
    assert "private installer failure" not in completed.stderr
    assert "private validation failure" not in completed.stderr
    assert f"See {diagnostic}" in completed.stderr
    assert stat.S_IMODE(diagnostic.stat().st_mode) == 0o600
    expected = (
        "toolang.install.failed" if failure == "install" else "toolang.check.failed"
    )
    assert events.read_text().splitlines()[-1] == expected
    assert "server.running" not in events.read_text()
    if failure != "missing-cli":
        assert "private" in diagnostic.read_text()


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
            str(Path(docker_guest.__file__).with_name("guest.sh")),
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
            str(Path(docker_guest.__file__).with_name("guest.sh")),
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


def test_docker_sandbox_maps_public_root_to_its_guest_place(tmp_path: Path) -> None:
    sandbox = create_sandbox("docker", config={"root": "/workspace/toolang"})

    assert sandbox.runtime_place(tmp_path) == Path("/workspace/toolang")


@pytest.mark.parametrize("name", ["place", "unknown"])
def test_docker_sandbox_rejects_unknown_path_settings(name):
    with pytest.raises(ValueError, match="unknown docker settings"):
        create_sandbox("docker", config={name: "/workspace/toolang"})


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
        if item.hosted_path == Path("/root/.toolang/.alice.env")
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
    _mock_docker_container(monkeypatch, plan)
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
    _mock_docker_container(monkeypatch, plan)
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
    _mock_docker_container(monkeypatch, plan)
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
    assert removed == [_CONTAINER_ID]
    assert not stage_dir.exists()
    assert diagnostic_path.is_file()
    assert cast(Path, plan.log_path).is_file()


def test_docker_release_preserves_container_diagnostics_before_removal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, str]] = []

    async def run(**_kwargs: object) -> str:
        return _CONTAINER_ID

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
    _mock_docker_container(monkeypatch, plan)
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
        _async_value(_CONTAINER_ID),
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
    _mock_docker_container(monkeypatch, plan)
    stage_dir = Path(cast(str, plan.meta["stage_dir"]))
    ref = asyncio.run(sandbox.launch(plan))

    asyncio.run(sandbox.release(ref))

    assert calls == [("logs", ref.runtime_id), ("remove", ref.runtime_id)]
    assert not stage_dir.exists()


@pytest.mark.parametrize("foreground", [False, True])
@pytest.mark.parametrize("remove_on_exit", [False, True])
def test_docker_run_adds_the_canonical_host_gateway(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    remove_on_exit: bool,
    foreground: bool,
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

    async def run_foreground(args, *_args):
        captured.extend(args)
        return "container-id"

    monkeypatch.setattr(docker_cli, "_run_foreground", run_foreground)

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
            labels={
                "io.toolang.root": "/srv/toolang with spaces",
                "io.toolang.address": "alice",
                "io.toolang.launch-id": "a" * 32,
            },
            log_path=None,
            remove_on_exit=remove_on_exit,
            foreground=foreground,
        )
    )

    assert ("--rm" in captured) == remove_on_exit
    assert ("--stop-signal" in captured) == foreground
    if foreground:
        assert captured[captured.index("--stop-signal") + 1] == "SIGTERM"
    assert container_id == "container-id"
    assert captured[captured.index("--add-host") + 1] == (
        "host.docker.internal:host-gateway"
    )
    assert "TOOLANG_ROOT=/root/.toolang" in captured
    assert [captured[i + 1] for i, arg in enumerate(captured) if arg == "--label"] == [
        "io.toolang.root=/srv/toolang with spaces",
        "io.toolang.address=alice",
        "io.toolang.launch-id=" + "a" * 32,
    ]


@pytest.mark.parametrize("fault", ["closed-stderr", "unwritable-marker"])
def test_guest_owner_monitor_enforces_shutdown_despite_output_failure(tmp_path, fault):
    source = (Path(docker_guest.__file__).parent / "guest.sh").read_text()
    monitor = source[
        source.index(
            'if [ -n "${TOOLANG_FOREGROUND_OWNER_FILE:-}" ]; then'
        ) : source.index('\nbootstrap "$@" </dev/null')
    ]
    marker = tmp_path / "owner"
    ready = tmp_path / "ready"
    Path(f"{marker}.closing").write_text(
        "0\n" if fault == "closed-stderr" else f"{int(time.time()) + 60}\n"
    )
    if fault == "unwritable-marker":
        marker.mkdir()
    script = "set -eu\n"
    if fault == "closed-stderr":
        script += "exec 2>&-\n"
    script += monitor + f'\nprintf ready >"{ready}"\n'
    script += (
        "kill -STOP $$\n" if fault == "closed-stderr" else "while :; do sleep 1; done\n"
    )
    worker = subprocess.Popen(
        ["/bin/sh", "-c", script],
        env={**os.environ, "TOOLANG_FOREGROUND_OWNER_FILE": str(marker)},
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        end = time.monotonic() + 3
        while not ready.exists() or (
            fault == "closed-stderr"
            and psutil.Process(worker.pid).status() != psutil.STATUS_STOPPED
        ):
            assert worker.poll() is None
            assert time.monotonic() < end, "monitor fixture did not stop its owner"
            time.sleep(0.01)
        assert worker.stdin is not None
        worker.stdin.close()
        expected = -signal.SIGKILL if fault == "closed-stderr" else 143
        assert worker.wait(timeout=3) == expected
    finally:
        with suppress(ProcessLookupError):
            os.killpg(worker.pid, signal.SIGKILL)
        worker.wait(timeout=3)


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


def test_docker_foreground_sandbox_follows_logs_only_when_waiting(
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
        "toolang.plugin.sandboxes.docker.sandbox.docker_run_detached",
        _async_value(_CONTAINER_ID),
    )

    async def follow(name: str) -> Any:
        calls.append(("logs", name))
        return Follower()

    monkeypatch.setattr(
        "toolang.plugin.sandboxes.docker.sandbox.docker_follow_container_logs",
        follow,
    )
    monkeypatch.setattr(
        "toolang.plugin.sandboxes.docker.sandbox.docker_wait_container",
        lambda name: calls.append(("wait", name)) or 0,
    )
    sandbox = create_sandbox("docker", config={})
    plan = sandbox.prepare(None, _request(tmp_path, foreground=True))
    _mock_docker_container(monkeypatch, plan)
    stage_dir = Path(cast(str, plan.meta["stage_dir"]))
    assert {path.name for path in stage_dir.iterdir()} == {"active"}
    assert {path.name for path in (stage_dir / "active").iterdir()} == {
        "guest.sh",
        "info.json",
        "guest.env",
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


def test_docker_foreground_wait_stops_log_follower_before_cancellation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class Follower:
        def __init__(self) -> None:
            self.returncode: int | None = None
            self.started = asyncio.Event()
            self.finished = asyncio.Event()
            self.terminated = False

        async def wait(self) -> int:
            self.started.set()
            await self.finished.wait()
            return cast(int, self.returncode)

        def terminate(self) -> None:
            self.terminated = True
            self.returncode = -15
            self.finished.set()

    follower = Follower()
    monkeypatch.setattr(
        docker_sandbox,
        "docker_run_detached",
        _async_value(_CONTAINER_ID),
    )
    monkeypatch.setattr(
        docker_sandbox,
        "docker_follow_container_logs",
        lambda _name: asyncio.sleep(0, result=follower),
    )
    sandbox = create_sandbox("docker", config={})
    plan = sandbox.prepare(None, _request(tmp_path, foreground=True))
    _mock_docker_container(monkeypatch, plan)
    ref = asyncio.run(sandbox.launch(plan))
    asyncio.run(sandbox.attach(plan, ref))

    async def cancel_wait() -> None:
        task = asyncio.create_task(sandbox.wait(ref))
        await follower.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel_wait())

    assert follower.terminated is True


@pytest.mark.parametrize("placement", ["resident", "visiting", "roaming"])
@pytest.mark.parametrize("agent", ["alice", "a" + "_" * 63])
def test_docker_names_follow_bound_addresses_and_preserve_full_codes(
    tmp_path, placement, agent, monkeypatch
):
    code = {
        "resident": None,
        "visiting": visiting_place_code("https://example.com/agent.too"),
        "roaming": roaming_place_code("/work/agent.too", PLACE_CODE_EPOCH_MS + 123),
    }[placement]
    address = agent + (f"@{code}" if code else "")
    request = replace(
        _request(tmp_path),
        agent_name=agent,
        agent_address=address,
        envs={"TOOLANG_HOST_LAUNCH": "a" * 32},
    )
    sandbox = docker_sandbox.DockerSandbox({})
    intent = sandbox.intent_reference(request)
    plan = sandbox.prepare(None, request)
    prepared = sandbox.prepared_reference(plan)
    monkeypatch.setattr(
        docker_sandbox, "docker_run_detached", _async_value(_CONTAINER_ID)
    )
    ref = asyncio.run(sandbox.launch(plan))
    expected = f"toolang-{root_code(str(tmp_path.resolve()))}-{agent}" + (
        f"-{code}" if code else ""
    )
    assert intent.runtime_id == prepared.runtime_id == expected
    assert intent.runtime_name == prepared.runtime_name == ref.runtime_name == expected
    assert plan.meta["container_name"] == expected
    assert ref.runtime_id == _CONTAINER_ID
    assert (
        ref.meta["docker_identity"]
        == intent.meta["docker_identity"]
        == {
            "version": 1,
            "root": str(tmp_path.resolve()),
            "address": address,
            "launch_id": "a" * 32,
        }
    )
    assert len(expected) <= {"resident": 81, "visiting": 92, "roaming": 101}[placement]


@pytest.mark.parametrize(
    "field,value",
    [
        ("toolang_root", None),
        ("toolang_root", Path("relative")),
        ("agent_address", None),
        ("agent_address", ""),
        ("agent_address", "bob"),
        ("agent_address", "alice@bad"),
        ("agent_address", "alice@" + "s" * 18),
    ],
)
@pytest.mark.parametrize("method", ["intent_reference", "prepare"])
def test_docker_rejects_missing_or_inconsistent_request_identity_before_staging(
    tmp_path, field, value, method
):
    request = replace(_request(tmp_path), **{field: value})
    sandbox = docker_sandbox.DockerSandbox({})
    with pytest.raises(ValueError):
        if method == "prepare":
            sandbox.prepare(None, request)
        else:
            sandbox.intent_reference(request)
    assert not (tmp_path / ".sandbox").exists()
    assert not (request.local_home / ".runtime").exists()


def test_docker_restart_reuses_name_with_a_fresh_stage_after_release(tmp_path):
    sandbox = docker_sandbox.DockerSandbox({})
    request = _request(tmp_path)
    first = sandbox.prepare(
        None, replace(request, envs={"TOOLANG_HOST_LAUNCH": "a" * 32})
    )
    with pytest.raises(FileExistsError):
        sandbox.prepare(None, replace(request, envs={"TOOLANG_HOST_LAUNCH": "b" * 32}))
    assert (
        json.loads((Path(first.meta["stage_dir"]) / "active/info.json").read_text())[
            "launch"
        ]
        == "a" * 32
    )
    docker_guest.remove_stage_directory(
        Path(first.meta["stage_dir"]), launch_id="a" * 32
    )
    second = sandbox.prepare(
        None, replace(request, envs={"TOOLANG_HOST_LAUNCH": "b" * 32})
    )
    assert first.meta["container_name"] == second.meta["container_name"]
    assert first.meta["stage_dir"] != second.meta["stage_dir"]
    assert first.meta["docker_identity"] != second.meta["docker_identity"]
    other_root = replace(request, toolang_root=tmp_path / "other")
    assert (
        sandbox.intent_reference(
            replace(other_root, envs={"TOOLANG_HOST_LAUNCH": "c" * 32})
        ).runtime_name
        != first.meta["container_name"]
    )


@pytest.mark.parametrize("placement", ["resident", "visiting", "roaming"])
def test_docker_bootstrap_has_a_guest_info_view_under_the_selected_root(
    tmp_path, placement
):
    request = _request(tmp_path)
    root = tmp_path / "selected"
    source = tmp_path / "project/alice.too"
    if placement == "resident":
        layout = AgentLayout.resident(root, "alice")
    elif placement == "visiting":
        layout = AgentLayout.visiting(
            "https://example.com/alice.too", "alice", root=root
        )
    else:
        layout = AgentLayout.roaming(
            source,
            root=root,
            place_code=roaming_place_code(str(source), PLACE_CODE_EPOCH_MS + 1),
        )
    guest_root = Path("/guest/toolang")
    guest_place = guest_root
    guest_home = guest_place / "agents/alice"
    runtime = root / ".runtime/active" / layout.address
    request = replace(
        request,
        toolang_root=root,
        agent_address=layout.address,
        local_place=layout.place,
        local_home=layout.home,
        log_path=layout.home / ".runtime/agent.log",
        hosted_root=guest_root,
        hosted_place=guest_place,
        hosted_home=guest_home,
        local_runtime=runtime,
        hosted_runtime=guest_root / ".runtime/active" / layout.name,
        hosted_source=Path("/workspace/project/alice.too")
        if placement == "roaming"
        else None,
        source_ref=layout.source_ref,
        envs={"TOOLANG_HOST_LAUNCH": "a" * 32},
    )
    plan = docker_sandbox.DockerSandbox({}).prepare(None, request)
    stage = runtime / "docker" / sha256(("a" * 32).encode()).hexdigest()
    assert Path(plan.meta["stage_dir"]) == stage
    assert request.hosted_runtime is not None
    assert (
        SandboxMount(stage / "active", request.hosted_runtime, read_only=True)
        in plan.mounts
    )
    info = json.loads((stage / "active/info.json").read_text())
    assert info["address"] == layout.address
    assert info["home"] == str(guest_home)
    assert info["launch"] == "a" * 32
    assert info["location"] == "guest"
    assert info["sandbox"] == "docker:python:3.13-slim"
    assert "status" not in info and "launch_id" not in info
    if placement == "roaming":
        assert info["source"] == "/workspace/project/alice.too"
    if placement == "visiting":
        assert info["source_ref"] == layout.source_ref
    assert all(
        "/.runtime/registration" not in str(item.hosted_path) for item in plan.mounts
    )


def test_delayed_docker_release_preserves_replacement_bootstrap(tmp_path, monkeypatch):
    sandbox = docker_sandbox.DockerSandbox({})
    request = _request(tmp_path)
    old = sandbox.prepare(
        None, replace(request, envs={"TOOLANG_HOST_LAUNCH": "a" * 32})
    )
    old_ref = sandbox.prepared_reference(old)
    stage = Path(old.meta["stage_dir"])
    docker_guest.remove_stage_directory(stage, launch_id="a" * 32)
    new = sandbox.prepare(
        None, replace(request, envs={"TOOLANG_HOST_LAUNCH": "b" * 32})
    )
    replacement_stage = Path(new.meta["stage_dir"])
    before = (replacement_stage / "active/info.json").read_bytes()
    monkeypatch.setattr(docker_sandbox, "docker_reference_id", lambda _ref: None)
    asyncio.run(sandbox.release(old_ref))
    assert (replacement_stage / "active/info.json").read_bytes() == before
    assert (replacement_stage / "active/guest.sh").is_file()


def test_missing_old_stage_cannot_remove_a_replacement_preparation_slot(tmp_path):
    sandbox = docker_sandbox.DockerSandbox({})
    plan = sandbox.prepare(
        None, replace(_request(tmp_path), envs={"TOOLANG_HOST_LAUNCH": "a" * 32})
    )
    stage = Path(plan.meta["stage_dir"])
    docker_guest.remove_stage_directory(stage, launch_id="a" * 32)
    stage.parent.mkdir()
    docker_guest.remove_stage_directory(stage, launch_id="a" * 32)
    assert stage.parent.is_dir()


def test_guest_config_and_active_mounts_have_disjoint_sources(tmp_path):
    sandbox = docker_sandbox.DockerSandbox({})
    request = replace(
        _request(tmp_path), guest_config='[workspaces.alice]\nrepo = "/guest/repo"\n'
    )
    plan = sandbox.prepare(None, request)
    stage = Path(plan.meta["stage_dir"])
    active = next(m for m in plan.mounts if m.local_path == stage / "active")
    config = next(m for m in plan.mounts if m.local_path == stage / "config.toml")
    assert active.read_only and config.read_only
    assert config.hosted_path == Path("/root/.toolang/config.toml")
    assert active.hosted_path == Path("/root/.toolang/.runtime/active/alice")
    assert not config.local_path.is_relative_to(active.local_path)
    assert not (active.local_path / "config.toml").exists()
    assert config.local_path.read_text() == request.guest_config


def test_rejected_duplicate_preparation_preserves_current_diagnostics(tmp_path):
    sandbox = docker_sandbox.DockerSandbox({})
    request = replace(_request(tmp_path), envs={"TOOLANG_HOST_LAUNCH": "a" * 32})
    plan = sandbox.prepare(None, request)
    diagnostic = Path(plan.meta["diagnostic_path"])
    events = Path(plan.meta["startup_events_path"])
    diagnostic.write_text("current diagnostic")
    events.write_text("uv.use\n")
    with pytest.raises(FileExistsError):
        sandbox.prepare(None, request)
    assert diagnostic.read_text() == "current diagnostic"
    assert events.read_text() == "uv.use\n"


def test_docker_distinct_places_with_same_agent_have_distinct_names(tmp_path):
    sandbox = docker_sandbox.DockerSandbox({})
    request = replace(_request(tmp_path), envs={"TOOLANG_HOST_LAUNCH": "a" * 32})
    codes = [
        visiting_place_code("https://example.com/one.too"),
        visiting_place_code("https://example.com/two.too"),
        roaming_place_code("/work/one.too", PLACE_CODE_EPOCH_MS + 123),
        roaming_place_code("/work/one.too", PLACE_CODE_EPOCH_MS + 124),
    ]
    refs = [
        sandbox.intent_reference(replace(request, agent_address=f"alice@{code}"))
        for code in codes
    ]
    assert len({ref.runtime_name for ref in refs}) == len(codes)


@pytest.mark.parametrize("locator", ["name", "id"])
@pytest.mark.parametrize(
    "field",
    ["io.toolang.root", "io.toolang.address", "io.toolang.launch-id", "missing"],
)
@pytest.mark.parametrize("operation", ["running", "stop", "release", "wait", "attach"])
def test_docker_stale_or_foreign_reference_cannot_use_replacement(
    tmp_path, monkeypatch, locator, field, operation
):
    sandbox = docker_sandbox.DockerSandbox({})
    plan = sandbox.prepare(None, _request(tmp_path))
    ref = sandbox.prepared_reference(plan)
    if locator == "id":
        ref = replace(ref, runtime_id=_CONTAINER_ID)
    identity = plan.meta["docker_identity"]
    labels = {
        "io.toolang.root": identity["root"],
        "io.toolang.address": identity["address"],
        "io.toolang.launch-id": identity["launch_id"],
    }
    if field == "missing":
        labels.clear()
    else:
        labels[field] = "another-owner"
    _mock_docker_container(monkeypatch, plan, labels=labels)

    def reject_access(*args, **kwargs):
        pytest.fail("a foreign container must not be accessed")

    for name in (
        "docker_stop_container",
        "docker_remove_container",
        "docker_wait_container",
        "docker_append_container_logs",
        "docker_follow_container_logs",
        "_observe_startup_events_safely",
    ):
        monkeypatch.setattr(docker_sandbox, name, reject_access)
    with pytest.raises(RuntimeError, match="ownership"):
        if operation == "attach":
            asyncio.run(sandbox.attach(plan, ref, progress=lambda _: None))
        else:
            asyncio.run(getattr(sandbox, operation)(ref))
    assert Path(plan.meta["stage_dir"]).is_dir()
    assert docker_cli.docker_reference_states((ref,)) == (None,)


@pytest.mark.parametrize(
    "identity",
    [None, {}, {"version": 2}, {"version": True}, {"version": 1, "root": "/tmp"}],
)
def test_docker_malformed_new_metadata_never_falls_back_to_legacy(
    monkeypatch, identity
):
    ref = SandboxRef(
        _CONTAINER_ID, "http://localhost:1", meta={"docker_identity": identity}
    )

    def reject_inspection(*args, **kwargs):
        pytest.fail("invalid identity must be rejected before inspection")

    monkeypatch.setattr(docker_cli, "_docker", reject_inspection)
    with pytest.raises(ValueError, match="identity"):
        docker_cli.docker_reference_id(ref)


@pytest.mark.parametrize("operation", ["stop", "release", "wait", "attach"])
def test_docker_name_replacement_after_inspection_uses_original_immutable_id(
    tmp_path, monkeypatch, operation
):
    sandbox = docker_sandbox.DockerSandbox({})
    plan = sandbox.prepare(None, _request(tmp_path))
    ref = sandbox.prepared_reference(plan)
    _mock_docker_container(monkeypatch, plan)
    targets = []
    monkeypatch.setattr(
        docker_sandbox,
        "docker_stop_container",
        lambda target, **kwargs: targets.append(target),
    )
    monkeypatch.setattr(docker_sandbox, "docker_remove_container", targets.append)
    monkeypatch.setattr(
        docker_sandbox,
        "docker_append_container_logs",
        lambda target, path: targets.append(target),
    )
    monkeypatch.setattr(
        docker_sandbox,
        "docker_wait_container",
        lambda target: targets.append(target) or 0,
    )

    async def observe(*args, **kwargs):
        targets.append(kwargs["runtime_id"])

    monkeypatch.setattr(docker_sandbox, "_observe_startup_events_safely", observe)
    if operation == "attach":
        asyncio.run(sandbox.attach(plan, ref, progress=lambda _: None))
    else:
        asyncio.run(getattr(sandbox, operation)(ref))
    assert targets and set(targets) == {_CONTAINER_ID}


def test_docker_root_code_collision_preserves_foreign_container_and_recovery(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(docker_sandbox, "root_code", lambda _: "00000000")
    sandbox = docker_sandbox.DockerSandbox({})
    request = _request(tmp_path)
    first = sandbox.prepare(None, request)
    second = sandbox.prepare(None, replace(request, toolang_root=tmp_path / "other"))
    assert first.meta["container_name"] == second.meta["container_name"]
    _mock_docker_container(monkeypatch, first)

    async def conflict(**kwargs):
        assert kwargs["labels"]["io.toolang.root"] == str(tmp_path / "other")
        raise RuntimeError("container name conflict")

    monkeypatch.setattr(docker_sandbox, "docker_run_detached", conflict)
    removed = []
    monkeypatch.setattr(docker_sandbox, "docker_remove_container", removed.append)
    with pytest.raises(SandboxLaunchError, match="ownership mismatch") as failure:
        asyncio.run(sandbox.launch(second))
    assert not removed
    assert failure.value.ref.meta["docker_identity"] == second.meta["docker_identity"]
    assert Path(second.meta["stage_dir"]).is_dir()
    assert asyncio.run(sandbox.running(sandbox.prepared_reference(first)))


@pytest.mark.parametrize("pending", [False, True])
def test_docker_missing_owned_container_releases_only_after_creation_finishes(
    tmp_path, monkeypatch, pending
):
    sandbox = docker_sandbox.DockerSandbox({})
    plan = sandbox.prepare(None, _request(tmp_path))
    ref = sandbox.prepared_reference(plan)
    monkeypatch.setattr(docker_cli, "docker_launch_pending", lambda _: pending)
    monkeypatch.setattr(
        docker_cli,
        "_docker",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args,
            1,
            stdout="",
            stderr=f"Error: No such object: {ref.runtime_id}",
        ),
    )
    removed = []
    monkeypatch.setattr(docker_sandbox, "docker_remove_container", removed.append)
    if pending:
        with pytest.raises(RuntimeError, match="pending"):
            asyncio.run(sandbox.release(ref))
        assert Path(plan.meta["stage_dir"]).is_dir()
        assert docker_cli.docker_reference_states((ref,)) == (None,)
    else:
        assert not asyncio.run(sandbox.running(ref))
        asyncio.run(sandbox.release(ref))
        assert not Path(plan.meta["stage_dir"]).exists()
        assert docker_cli.docker_reference_states((ref,)) == (False,)
    assert not removed


def test_docker_missing_container_is_not_confused_with_a_same_name_volume(
    tmp_path, monkeypatch
):
    sandbox = docker_sandbox.DockerSandbox({})
    plan = sandbox.prepare(None, _request(tmp_path))
    ref = sandbox.prepared_reference(plan)
    monkeypatch.setattr(docker_cli, "docker_launch_pending", lambda _: False)

    def inspect(*args, **kwargs):
        container_only = (
            "--type" in args and args[args.index("--type") + 1] == "container"
        )
        return subprocess.CompletedProcess(
            args,
            1,
            stdout="",
            stderr=(
                f"Error: No such container: {ref.runtime_id}"
                if container_only
                else 'template parsing error: map has no entry for key "Id"'
            ),
        )

    monkeypatch.setattr(docker_cli, "_docker", inspect)
    assert asyncio.run(sandbox.running(ref)) is False
    assert docker_cli.docker_reference_states((ref,)) == (False,)
    asyncio.run(sandbox.release(ref))
    assert not Path(plan.meta["stage_dir"]).exists()


@pytest.mark.parametrize("terminal", [True, False])
def test_docker_attach_reads_local_events_after_automatic_container_removal(
    tmp_path, monkeypatch, terminal
):
    sandbox = docker_sandbox.DockerSandbox({})
    plan = sandbox.prepare(None, replace(_request(tmp_path), remove_on_exit=True))
    ref = replace(sandbox.prepared_reference(plan), runtime_id=_CONTAINER_ID)
    content = "uv.use\npython.use\ntoolang.install.running\ntoolang.install.ok\ntoolang.check.running\n"
    if terminal:
        content += "toolang.check.failed\n"
    Path(plan.meta["startup_events_path"]).write_text(content)
    monkeypatch.setattr(docker_cli, "docker_launch_pending", lambda _: False)
    monkeypatch.setattr(docker_sandbox, "_STARTUP_EVENT_INTERVAL_SEC", 0)
    inspected = []

    def inspect(*args, **kwargs):
        inspected.append(args[-1])
        return subprocess.CompletedProcess(
            args, 1, stdout="", stderr=f"Error: No such container: {_CONTAINER_ID}"
        )

    monkeypatch.setattr(docker_cli, "_docker", inspect)
    events = []

    async def attach():
        await asyncio.wait_for(
            sandbox.attach(plan, ref, progress=events.append), timeout=1
        )

    asyncio.run(attach())
    assert events[-1].status == ("failed" if terminal else "running")
    assert (
        "Checking Toolang" in events[-1].label
        or events[-1].label == "Failed to check Toolang"
    )
    assert inspected == [_CONTAINER_ID]


def test_docker_batch_keeps_generations_distinct_even_with_same_name(
    tmp_path, monkeypatch
):
    sandbox = docker_sandbox.DockerSandbox({})
    plan = sandbox.prepare(None, _request(tmp_path))
    current = sandbox.prepared_reference(plan)
    stale = replace(
        current,
        meta={
            **current.meta,
            "docker_identity": {
                **plan.meta["docker_identity"],
                "launch_id": "old-generation",
            },
        },
    )
    _mock_docker_container(monkeypatch, plan)
    assert docker_cli.docker_reference_states((stale, current, stale)) == (
        None,
        True,
        None,
    )


@pytest.mark.parametrize(
    "failure", ["timeout", "missing-cli", "invalid-output", "permission"]
)
def test_docker_inconclusive_inspection_preserves_owned_resources(
    tmp_path, monkeypatch, failure
):
    sandbox = docker_sandbox.DockerSandbox({})
    plan = sandbox.prepare(None, _request(tmp_path))
    ref = sandbox.prepared_reference(plan)

    def inspect(*args, **kwargs):
        if failure == "timeout":
            raise subprocess.TimeoutExpired("docker", 2)
        if failure == "missing-cli":
            return None
        return subprocess.CompletedProcess(
            args, 1, stdout="invalid", stderr="permission denied"
        )

    monkeypatch.setattr(docker_cli, "_docker", inspect)
    with pytest.raises(RuntimeError):
        asyncio.run(sandbox.release(ref))
    assert Path(plan.meta["stage_dir"]).is_dir()
    assert docker_cli.docker_reference_states((ref,)) == (None,)


def test_docker_legacy_references_keep_original_locators(monkeypatch):
    ref = SandboxRef("toolang-alice-old-launch", "http://localhost:1")
    sandbox = docker_sandbox.DockerSandbox({})
    calls = []
    monkeypatch.setattr(
        docker_cli,
        "docker_container_running",
        lambda target: calls.append(("running", target)) or True,
    )
    monkeypatch.setattr(
        docker_sandbox,
        "docker_stop_container",
        lambda target, **kwargs: calls.append(("stop", target)),
    )
    monkeypatch.setattr(
        docker_sandbox,
        "docker_remove_container",
        lambda target: calls.append(("remove", target)),
    )
    assert asyncio.run(sandbox.running(ref))
    asyncio.run(sandbox.stop(ref))
    asyncio.run(sandbox.release(ref))
    assert calls == [
        (operation, ref.runtime_id) for operation in ("running", "stop", "remove")
    ]


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
        '  if [ "${TOOLANG_TEST_INSTALL_FAIL:-}" = 1 ]; then\n'
        '    echo "private installer failure" >&2; exit 1\n'
        "  fi\n"
        '  if [ -n "${TOOLANG_TEST_INSTALL_ENV_LOG:-}" ]; then\n'
        '    printf "%s\\n" "${HTTPS_PROXY:-}" >"$TOOLANG_TEST_INSTALL_ENV_LOG"\n'
        "  fi\n"
        '  printf "%s\\n" "$@" >"$UV_TOOL_BIN_DIR/install-arguments"\n'
        '  cp "$TOOLANG_TEST_TOO" "$UV_TOOL_BIN_DIR/too"\n'
        '  chmod 755 "$UV_TOOL_BIN_DIR/too"\n'
        "  exit 0\n"
        "fi\n"
        "exit 1\n",
    )
    _write_executable(
        directory / "too-template",
        "#!/bin/sh\n"
        'if [ "$1" = "_serve" ] && [ "$2" = "--help" ]; then exit 0; fi\n'
        'if [ "$1" = "--version" ]; then echo "toolang test"; exit 0; fi\n'
        "exit 1\n",
    )
    assert Path(python).is_file()


def _link_guest_utilities(directory: Path) -> None:
    for name in ("cat", "chmod", "cp", "mkdir", "rm", "sh"):
        source = shutil.which(name)
        assert source is not None
        (directory / name).symlink_to(source)
    _write_executable(directory / "uname", "#!/bin/sh\nprintf 'Linux\\n'\n")


@pytest.mark.parametrize("shadowed", [False, True])
def test_docker_preserves_already_mounted_catalog_input(tmp_path, shadowed):
    request = _request(tmp_path)
    catalog = tmp_path / "selected.json"
    catalog.write_text("{}")
    mounted = SandboxMount(
        catalog, request.hosted_home / ".runtime/model-catalog.json", read_only=True
    )
    request = replace(
        request,
        envs={MODEL_CATALOG_ENV: str(mounted.hosted_path)},
        mounts=(*request.mounts, mounted),
        dotenv_envs={MODEL_CATALOG_ENV: str(tmp_path / "unused-missing.json")}
        if shadowed
        else {},
    )
    plan = create_sandbox("docker", config={}).prepare(None, request)
    assert plan.mounts.count(mounted) == 1
    env_mount = next(
        mount
        for mount in plan.mounts
        if mount.hosted_path == Path("/root/.toolang/.alice.env")
    )
    assert dotenv_values(env_mount.local_path, interpolate=False)[
        MODEL_CATALOG_ENV
    ] == str(mounted.hosted_path)


def test_docker_stages_only_resolved_agent_values(tmp_path):
    from toolang.common.environment import (
        resolve_root_environment,
        resolve_agent_environment,
    )

    (tmp_path / ".env").write_text("ADMITTED=central\n")
    (tmp_path / ".alice.env").write_text("EXCLUDED\nADMITTED\n")
    shell = {"EXCLUDED": "synthetic-excluded", "OPENAI_API_KEY": "synthetic-excluded"}
    environment = resolve_agent_environment(
        resolve_root_environment(tmp_path, shell),
        AgentLayout.resident(tmp_path, "alice"),
    )
    request = replace(_request(tmp_path), envs=dict(environment.values), dotenv_envs={})
    plan = create_sandbox("docker", config={}).prepare(None, request)
    mount = next(
        item
        for item in plan.mounts
        if item.hosted_path == Path("/root/.toolang/.alice.env")
    )
    assert dotenv_values(mount.local_path, interpolate=False) == {"ADMITTED": "central"}
    assert "synthetic-excluded" not in mount.local_path.read_text()
    assert "central" not in repr(plan)
    assert "central" not in json.dumps(plan.meta)


@pytest.mark.parametrize("home_env", [None, "", "PRIVATE=local\n"])
def test_docker_mounts_generated_environment_at_root_without_touching_home(
    tmp_path, home_env
):
    request = _request(tmp_path)
    local_env = request.local_home / ".env"
    if home_env is not None:
        local_env.write_text(home_env)
    plan = create_sandbox("docker", config={}).prepare(None, request)
    mount = next(mount for mount in plan.mounts if mount.local_path.name == "guest.env")
    assert mount.hosted_path == Path("/root/.toolang/.alice.env")
    assert mount.read_only
    assert all(item.hosted_path != request.hosted_home / ".env" for item in plan.mounts)
    assert (
        local_env.read_text() == home_env
        if home_env is not None
        else not local_env.exists()
    )


def test_generated_guest_environment_uses_the_common_parser(tmp_path):
    from toolang.common.environment import apply_environment_file

    path = tmp_path / "guest.env"
    docker_guest.write_guest_env(
        path,
        dotenv_envs={"LITERAL": "overridden", "MULTILINE": "first\nsecond\rthird"},
        process_envs={"LITERAL": '${HOME}/"quoted"/\\literal'},
    )
    assert apply_environment_file(path, {}) == {
        "LITERAL": '${HOME}/"quoted"/\\literal',
        "MULTILINE": "first\nsecond\rthird",
    }


@pytest.mark.parametrize(
    "name", ["WITH-DASH", "WITH.DOT", "1NUMBER", "QUOTED'", "NON_ASCII_é"]
)
def test_docker_rejects_names_the_common_dotenv_parser_cannot_load(tmp_path, name):
    request = replace(_request(tmp_path), envs={name: "synthetic-value"})
    with pytest.raises(ValueError, match="invalid guest environment variable name"):
        create_sandbox("docker", config={}).prepare(None, request)
    assert not list(tmp_path.glob(".sandbox/alice/launches/*"))
