"""Remote Chat support endpoints and runtime identity."""

from __future__ import annotations

import asyncio
from importlib.metadata import version as package_version
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from pydantic import TypeAdapter

from toolang.api.app import create_app
from toolang.api.routers.agent import profile
from toolang.base.types.message import Message, TextPart
from toolang.base.types.run import ModelCallResult
from toolang.base.types.sandbox import SandboxRef
from toolang.catalog import CapsManager, JobsManager
from toolang.common.layout import AgentLayout
from toolang.execution.schemas import RunDetail
from toolang.execution.values import parts_from_local
from toolang.up import AgentCore, process as agents
from toolang.up.sandbox import SandboxState
from tests.support.execution_harness import ExecutionHarness, TEST_MODEL_REF


class _Snapshot:
    def __init__(self, value: object) -> None:
        self.value = value

    def current(self) -> Any:
        return self.value


def test_remote_chat_validation_and_latest_result_endpoints(tmp_path: Path) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic chat(_: Part[]) -> Part[]:
  recall = none
  context: none
  instruct: none
  user: {{_}}
""",
        responses=[ModelCallResult(message=Message.assistant("remote answer"))],
    )
    harness.store.close()
    core = AgentCore(harness.setup.layout)
    core.setup = _Snapshot(harness.setup)
    core.state = _Snapshot(harness.state)
    app = create_app(
        core,
        CapsManager(core.layout),
        JobsManager(core.layout),
        cors_allowed_origins=(),
    )

    try:
        with TestClient(app) as client:
            runtime = client.get("/api/v1/profile").json()["runtime"]
            valid = client.post(
                "/api/v1/runs/authored/validate",
                json={
                    "session_commands": [
                        {
                            "group": "allow",
                            "field": "models",
                            "value": [TEST_MODEL_REF],
                        },
                        {"group": "default", "field": "model", "value": None},
                        {"group": "limit", "field": "cost", "value": "2.50"},
                    ],
                    "runnable_fallbacks": ["agic:missing", "agic:chat", "default"],
                },
            )
            invalid = client.post(
                "/api/v1/runs/authored/validate",
                json={
                    "session_commands": [],
                    "runnable_fallbacks": ["agic:missing", "flow:missing"],
                },
            )
            created = client.post("/api/v1/threads", json={"client": "tui"})
            thread_id = created.json()["thread"]["id"]
            empty = client.get(f"/api/v1/threads/{thread_id}/result")
            unknown = client.get("/api/v1/threads/term_missing/result")
            executed = client.post(
                "/api/v1/runs/authored/stream",
                json={
                    "thread": thread_id,
                    "request_id": "term_remote_chat",
                    "input": {"primary": "hello"},
                    "runnable_fallbacks": ["agic:chat", "default"],
                },
            )
            run_id = executed.headers["X-Toolang-Run-ID"]
            explicit_response = client.get(f"/api/v1/runs/{run_id}")
            latest_response = client.get(f"/api/v1/threads/{thread_id}/result")

        explicit = TypeAdapter(RunDetail).validate_python(explicit_response.json())
        latest = TypeAdapter(RunDetail).validate_python(latest_response.json())

        assert runtime == {
            "version": package_version("toolang"),
            "sandbox": {"driver": "host", "instance": None},
        }
        assert valid.status_code == 204
        assert invalid.status_code == 422
        assert invalid.json()["detail"] == (
            "no runnable fallback is available: agic:missing, flow:missing"
        )
        assert core.store.list_runs(limit=None) == [core.store.get_run(run_id=run_id)]
        assert empty.status_code == 404
        assert empty.json()["detail"] == f"thread has no result: {thread_id}"
        assert unknown.status_code == 404
        assert unknown.json()["detail"] == "thread not found: term_missing"
        assert explicit.id == latest.id == run_id
        assert explicit.output is not None
        assert latest.output is not None
        assert parts_from_local(explicit.output) == (TextPart("remote answer"),)
        assert parts_from_local(latest.output) == (TextPart("remote answer"),)
    finally:
        asyncio.run(core.close())


def test_profile_reports_six_character_docker_instance(tmp_path: Path) -> None:
    layout = AgentLayout.resident(tmp_path, "alice")
    layout.home.mkdir(parents=True)
    agents.write_runtime_state(
        layout,
        endpoint="http://127.0.0.1:7001",
        started_at="2026-08-25T00:00:00Z",
        pid=123,
        sandbox="docker:python:3.13-slim",
    )
    SandboxState(
        sandbox="docker:python:3.13-slim",
        ref=SandboxRef(
            runtime_id="toolang-alice-runtime",
            endpoint="http://127.0.0.1:7001",
            meta={"container_id": "a1b2c3d4e5f67890"},
        ),
    ).save(layout.sandbox_state)
    core = AgentCore(layout)

    try:
        payload = profile(core)

        assert payload["runtime"] == {
            "version": package_version("toolang"),
            "sandbox": {"driver": "docker", "instance": "a1b2c3"},
        }
    finally:
        asyncio.run(core.close())
