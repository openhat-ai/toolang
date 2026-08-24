from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast

import pytest

from toolang.base.protocols.hosting import Hosting
from toolang.base.types.hosting import HostingMount, HostingRequest
from toolang.plugin.config import parse_sandbox_binding
from toolang.plugin.sandboxes import none as none_sandbox
from toolang.plugin.sandboxes.loading import load_hosting


def _request(
    root: Path,
    *,
    dev: Path | None = None,
    foreground: bool = False,
) -> HostingRequest:
    home = root / "agents" / "alice"
    home.mkdir(parents=True, exist_ok=True)
    return HostingRequest(
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
        envs={"EXAMPLE": "value"},
        workspaces={
            "project": Path("/root/.toolang/agents/alice/.runtime/workspaces/project")
        },
        workspace_sources={"project": root / "project"},
        mounts=(
            HostingMount(
                local_path=root / "shared",
                hosted_path=Path("/root/.toolang/shared"),
            ),
        ),
        local_dev_artifact=dev,
    )


def test_none_hosting_parses_own_spec_and_prepares_local_process(
    tmp_path: Path,
) -> None:
    hosting = load_hosting("none", config={})

    assert isinstance(hosting, Hosting)
    plan = hosting.prepare(None, _request(tmp_path))

    assert plan.sandbox == "none"
    assert plan.command[-4:] == ("serve", "alice", "--port", "8123")
    assert plan.working_directory == tmp_path / "agents" / "alice"
    with pytest.raises(ValueError, match="does not accept"):
        hosting.prepare("", _request(tmp_path))


def test_none_foreground_hosting_inherits_console_streams(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}
    process = cast(Any, object())

    def popen(command: tuple[str, ...], **kwargs: object) -> object:
        captured.update(command=command, **kwargs)
        return process

    monkeypatch.setattr(none_sandbox.subprocess, "Popen", popen)
    hosting = load_hosting("none", config={})
    plan = hosting.prepare(None, _request(tmp_path, foreground=True))

    assert none_sandbox._launch(plan) is process
    assert "stdout" not in captured
    assert "stderr" not in captured


def test_docker_hosting_prepares_and_launches(
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
    dev = tmp_path / "dist" / "toolang.whl"
    dev.parent.mkdir(parents=True)
    dev.write_bytes(b"wheel")
    (tmp_path / "shared").mkdir()
    hosting = load_hosting("docker", config={})

    plan = hosting.prepare("python:3.13-slim", _request(tmp_path, dev=dev))

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
    script = tmp_path / ".sandbox" / "alice" / "start.sh"
    assert script.is_file()
    assert " too serve alice --port 8123" in script.read_text(encoding="utf-8")

    ref = asyncio.run(hosting.launch(plan))

    assert ref.runtime_id == container_name
    assert ref.endpoint == "http://localhost:8123"
    assert asyncio.run(hosting.running(ref)) is True
    run_call = cast(dict[str, Any], calls["run"])
    assert run_call["image"] == "python:3.13-slim"
    assert run_call["published_port"] == 8123
    assert run_call["hosted_port"] == 8123
    assert ref.meta["workspaces"] == {
        "project": {
            "configured_path": str(tmp_path / "project"),
            "active_path": "/root/.toolang/agents/alice/.runtime/workspaces/project",
        }
    }


def test_docker_hosting_uses_configured_default_image(tmp_path: Path) -> None:
    hosting = load_hosting("docker", config={"image": "python:3.14"})

    plan = hosting.prepare(None, _request(tmp_path))

    assert plan.sandbox == "docker:python:3.14"


def test_docker_foreground_hosting_follows_container_logs(
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
    hosting = load_hosting("docker", config={})
    plan = hosting.prepare(None, _request(tmp_path, foreground=True))

    ref = asyncio.run(hosting.launch(plan))
    result = asyncio.run(hosting.wait(ref))

    assert result == 0
    assert calls == [("logs", ref.runtime_id), ("wait", ref.runtime_id)]


def test_parse_sandbox_binding_keeps_plugin_owned_spec() -> None:
    binding = parse_sandbox_binding(
        {
            "driver": "docker",
            "target": "python:3.13",
            "config": {
                "image": "python:3.13-slim",
                "token": "secret",
            },
        }
    )

    assert binding is not None
    assert binding.name == "docker"
    assert binding.spec == "python:3.13"
    assert binding.config == {
        "image": "python:3.13-slim",
        "token": "secret",
    }
