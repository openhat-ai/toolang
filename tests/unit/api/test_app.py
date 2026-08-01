from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

from toolang.api.app import create_app
from toolang.catalog import CapsManager, JobsManager
from toolang.common.layout import AgentLayout
from toolang.up import AgentCore


def test_app_owns_explicit_core_and_catalog_dependencies(tmp_path: Path) -> None:
    layout = AgentLayout.resident(tmp_path, "alice")
    core = AgentCore(layout)
    caps = CapsManager(layout)
    jobs = JobsManager(layout)
    app = create_app(core, caps, jobs, cors_allowed_origins=())

    assert app.state.agent_core is core
    assert app.state.caps_manager is caps
    assert app.state.jobs_manager is jobs

    with TestClient(app) as client:
        assert client.get("/healthz").json() == {"ok": True}
        assert client.get("/api/v1/threads").json() == []

    asyncio.run(core.close())
