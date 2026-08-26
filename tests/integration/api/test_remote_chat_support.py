"""Remote Chat support endpoints and runtime identity."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import TypeAdapter
import pytest

from toolang.api.app import create_app
from toolang.api.routers import agent as agent_router
from toolang.api.routers.agent import profile
from toolang.base.types.message import Message, TextPart
from toolang.base.types.run import ModelCallResult
from toolang.catalog import CapsManager, JobsManager
from toolang.common.layout import AgentLayout
from toolang.execution.schemas import RunDetail
from toolang.execution.values import parts_from_local
from toolang.up import AgentCore, process as agents
from tests.support.execution_harness import ExecutionHarness, TEST_MODEL_REF


class _Snapshot:
    def __init__(self, value: object) -> None:
        self.value = value

    def current(self) -> Any:
        return self.value


_CONTAINER_ID = "176191c1528b8e2861cc16422dee13ade59d4977c2148a9ebf5d36a06f090abb"
_HOST_DESCRIPTION = "macOS 27.0(26A5416b) arm64"


def test_remote_chat_validation_and_latest_result_endpoints(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_version = "v0.2.7-88-gc73484a9"
    monkeypatch.setattr(agent_router, "toolang_version", lambda: source_version)
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
    agents.write_runtime_state(
        core.layout,
        endpoint="http://127.0.0.1:7001",
        started_at="2026-08-25T00:00:00Z",
        pid=123,
        sandbox_description=_HOST_DESCRIPTION,
    )
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
            "version": source_version,
            "sandbox": {
                "driver": "host",
                "selector": "host",
                "instance": None,
                "description": _HOST_DESCRIPTION,
            },
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


def test_profile_preserves_source_version_and_complete_docker_instance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = AgentLayout.resident(tmp_path, "alice")
    layout.home.mkdir(parents=True)
    agents.write_runtime_state(
        layout,
        endpoint="http://127.0.0.1:7001",
        started_at="2026-08-25T00:00:00Z",
        pid=123,
        sandbox="docker:python:3.13-slim",
        sandbox_instance=_CONTAINER_ID,
    )
    core = AgentCore(layout)
    source_version = "v0.2.7-88-gc73484a9"
    monkeypatch.setattr(
        agent_router,
        "toolang_version",
        lambda: source_version,
        raising=False,
    )

    try:
        payload = profile(core)

        assert payload["runtime"] == {
            "version": source_version,
            "sandbox": {
                "driver": "docker",
                "selector": "docker:python:3.13-slim",
                "instance": _CONTAINER_ID,
                "description": None,
            },
        }
    finally:
        asyncio.run(core.close())


def test_profile_rejects_short_docker_instance(tmp_path: Path) -> None:
    layout = AgentLayout.resident(tmp_path, "alice")
    layout.home.mkdir(parents=True)
    agents.write_runtime_state(
        layout,
        endpoint="http://127.0.0.1:7001",
        started_at="2026-08-25T00:00:00Z",
        pid=123,
        sandbox="docker:python:3.13-slim",
        sandbox_instance="a1b2c3",
    )
    core = AgentCore(layout)

    try:
        with pytest.raises(
            HTTPException,
            match="runtime sandbox instance is unavailable",
        ):
            profile(core)
    finally:
        asyncio.run(core.close())


def test_profile_rejects_missing_host_description(tmp_path: Path) -> None:
    layout = AgentLayout.resident(tmp_path, "alice")
    layout.home.mkdir(parents=True)
    agents.write_runtime_state(
        layout,
        endpoint="http://127.0.0.1:7001",
        started_at="2026-08-25T00:00:00Z",
        pid=123,
    )
    core = AgentCore(layout)

    try:
        with pytest.raises(
            HTTPException,
            match="runtime sandbox description is unavailable",
        ):
            profile(core)
    finally:
        asyncio.run(core.close())
