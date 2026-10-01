"""Workspace inspection uses runtime snapshots and runtime filesystem mappings."""

import asyncio
from dataclasses import replace

from fastapi.testclient import TestClient
import pytest

from toolang.api.app import create_app
from toolang.catalog import CapsManager, JobsManager
from toolang.common.layout import AgentLayout
from toolang.setup import AgentSetup
from toolang.setup.types import AgentEnvironment
from toolang.up import AgentCore


@pytest.mark.parametrize("guest", [False, True])
def test_workspace_inspection_uses_published_state_and_runtime_paths(
    tmp_path, monkeypatch, guest
):
    layout = AgentLayout.resident(tmp_path / "root", "alice")
    layout.home.mkdir(parents=True)
    runtime_repo = tmp_path / "runtime-repo"
    (runtime_repo / "src").mkdir(parents=True)
    source = "/host-only/repo" if guest else str(runtime_repo)
    layout.config.write_text(f'[workspaces]\nrepo = "{source}"\n')
    core = AgentCore(layout)
    state = asyncio.run(core.state.refresh())
    environment = AgentEnvironment.capture(layout, sandbox="host")
    if guest:
        guest_lab = tmp_path / "guest-lab"
        guest_lab.mkdir()
        environment = replace(
            environment,
            sandbox="docker",
            workspace_location="guest",
            workspace_mounts={
                "lab": (layout.home / "lab", guest_lab),
                "repo": (type(runtime_repo)(source), runtime_repo),
            },
        )
    setup = AgentSetup(layout=layout, envs={}, environment=environment)
    monkeypatch.setattr(core.setup, "current", lambda: setup)
    layout.config.write_text("invalid = [")
    try:
        with TestClient(
            create_app(core, CapsManager(layout), JobsManager(layout))
        ) as client:
            response = client.get(
                "/api/v1/workspaces", params={"workdir": "repo://src"}
            )
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["revision"] == state.revision
            assert body["workdir"] == "repo://src"
            assert body["items"][-1] == {
                "name": "repo",
                "path": source,
                "available": True,
            }
            assert (
                client.get(
                    "/api/v1/workspaces", params={"workdir": "missing://"}
                ).status_code
                == 400
            )
            assert (
                client.get(
                    "/api/v1/workspaces", params={"workdir": "repo://missing"}
                ).status_code
                == 400
            )
            assert core.state.current() is state
    finally:
        asyncio.run(core.close())


def test_guest_workspace_added_after_launch_is_not_reported_available(
    tmp_path, monkeypatch
):
    layout = AgentLayout.resident(tmp_path, "alice")
    layout.home.mkdir(parents=True)
    core = AgentCore(layout)
    asyncio.run(core.state.refresh())
    added = tmp_path / "added"
    added.mkdir()
    layout.config.write_text(f'[workspaces]\nadded = "{added}"\n')
    asyncio.run(core.state.refresh())
    environment = replace(
        AgentEnvironment.capture(layout, sandbox="host"),
        sandbox="docker",
        workspace_location="guest",
        workspace_mounts={"lab": (layout.home / "lab", layout.home / "lab")},
    )
    monkeypatch.setattr(
        core.setup,
        "current",
        lambda: AgentSetup(layout=layout, envs={}, environment=environment),
    )
    try:
        with TestClient(
            create_app(core, CapsManager(layout), JobsManager(layout))
        ) as client:
            response = client.get("/api/v1/workspaces")
            assert response.status_code == 200
            assert response.json()["items"][-1]["available"] is False
            assert (
                client.get(
                    "/api/v1/workspaces", params={"workdir": "added://"}
                ).status_code
                == 400
            )
    finally:
        asyncio.run(core.close())
