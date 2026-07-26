from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast

import pytest

from toolang.base.protocols.hosting import Hosting
from toolang.base.types.hosting import HostingMount, HostingRequest
from toolang.plugin.config import parse_sandbox_binding
from toolang.plugin.sandboxes.loading import load_hosting


def _request(root: Path, *, dev: Path | None = None) -> HostingRequest:
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
        log_path=home / ".runtime" / "agent.log",
        envs={"EXAMPLE": "value"},
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


def test_docker_hosting_uses_configured_default_image(tmp_path: Path) -> None:
    hosting = load_hosting("docker", config={"image": "python:3.14"})

    plan = hosting.prepare(None, _request(tmp_path))

    assert plan.sandbox == "docker:python:3.14"


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
