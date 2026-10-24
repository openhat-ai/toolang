"""Remote Chat support endpoints and runtime identity."""

from __future__ import annotations

from toolang.up import workload as workloads

import asyncio
from dataclasses import replace
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
from toolang.base.types.model import (
    ModelRequest,
    Reasoning,
)
from toolang.base.types.policy import RunDefaults
from toolang.base.types.run import ModelCallResult
from toolang.catalog import CapsManager, ChoresManager
from toolang.common.errors import ToolangError
from toolang.common.layout import AgentLayout
from toolang.execution.schemas import RunDetail
from toolang.execution.records import RunControlPayload
from toolang.execution.store import RunStore
from toolang.execution.types import (
    FieldRef,
    RunRef,
    Output,
    value_for_type,
)
from toolang.execution.values import parts_from_value
from toolang.state.state import CapSource, StateCap
from toolang.up import AgentCore, process as agents
from tests.support.execution_harness import (
    ExecutionHarness,
    RecordingTool,
    TEST_MODEL_REF,
)
from tests.support.execution_fixtures import (
    project_run_end,
    project_run_start,
    project_step,
)


class _Snapshot:
    def __init__(self, value: object) -> None:
        self.value = value

    def current(self) -> Any:
        return self.value

    async def refresh(self) -> object:
        raise AssertionError("ordinary API requests must not refresh publications")


class _BrokenSnapshot:
    def current(self) -> object:
        raise ToolangError("snapshot unavailable")


_CONTAINER_ID = "176191c1528b8e2861cc16422dee13ade59d4977c2148a9ebf5d36a06f090abb"
_HOST_DESCRIPTION = "macOS 27.0 arm64"


@pytest.mark.parametrize(
    "endpoint", ["runs/run_a", "threads/chat_a", "threads/chat_a/result"]
)
def test_history_response_keeps_one_snapshot_during_concurrent_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, endpoint: str
) -> None:
    harness = ExecutionHarness.create(
        tmp_path, source="agic chat:\n  hello\n", responses=()
    )
    harness.store.close()
    core = AgentCore(harness.setup.layout)
    writer = RunStore(core.store.db_path)
    try:
        project_run_start(
            writer,
            run_id="run_a",
            thread_id="chat_a",
            input=Message.user("input"),
        )
        parent = project_step(
            writer,
            run_id="run_a",
            step_index=0,
            kind="value",
            status="succeeded",
            input=(),
            output=(TextPart("original answer"),),
            started_at="2026-09-01T00:00:01Z",
            finished_at="2026-09-01T00:00:02Z",
        )
        project_run_start(
            writer,
            run_id="run_b",
            thread_id="chat_a",
            parent=parent.ref,
            input=Message.user("child input"),
        )
        project_run_end(writer, run_id="run_b", output=Output("child answer", None))
        project_run_end(
            writer,
            run_id="run_a",
            output=Output(
                value_for_type(
                    "Part[]", FieldRef.from_path(parent.ref, "output", "value")
                ),
                None,
            ),
        )
        control = writer.get_run_control(run_id="run_a", index=0)
        assert control is not None and isinstance(control.payload, RunControlPayload)
        get_output = core.history.get_output
        changed = False

        def change_before_output(run: RunRef | str) -> Output | None:
            nonlocal changed
            if not changed:
                writer._conn.executescript(
                    "DELETE FROM steps WHERE run IN ('run_a', 'run_b'); "
                    "DELETE FROM runs WHERE id='run_b'; UPDATE runs SET output=NULL WHERE id='run_a';"
                )
                changed = True
            return get_output(run)

        monkeypatch.setattr(core.history, "get_output", change_before_output)
        app = create_app(
            core,
            CapsManager(core.layout),
            ChoresManager(core.layout),
            cors_allowed_origins=(),
        )
        with TestClient(app) as client:
            response = client.get(f"/api/v0/{endpoint}")
        assert changed
        assert response.status_code == 200
        data = response.json()
        detail = TypeAdapter(RunDetail).validate_python(
            data["runs"][0] if endpoint == "threads/chat_a" else data
        )
        assert detail.status == "succeeded"
        assert detail.output is not None
        assert parts_from_value(detail.output.value) == (TextPart("original answer"),)
        if endpoint == "threads/chat_a":
            child = TypeAdapter(RunDetail).validate_python(data["runs"][1])
            assert child.output == Output("child answer", None)
        assert writer.get_run(run_id="run_b") is None
        assert get_output("run_a") is None
    finally:
        writer.close()
        asyncio.run(core.close())


def test_resource_discovery_endpoints_filter_effective_collections(
    tmp_path: Path,
) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
prompt review:
  Review {{focus}}.

agic chat:
  hello
""",
        responses=(),
        tools={
            "test__lookup": RecordingTool(
                "test__lookup",
                output={"ok": True},
                description="Look up a test value.",
            )
        },
    )
    harness.store.close()
    prompt = StateCap(
        kind="prompt",
        name="review",
        shape="file",
        ref="inline://prompts/review",
        path="source/agent.too",
        source=CapSource(
            origin="local",
            form="inline",
            path="agent.too",
            updated_at="2026-09-01T00:00:00Z",
            fingerprint="0" * 64,
        ),
        meta={"description": "Review input."},
    )
    core = AgentCore(harness.setup.layout)
    core.setup = _Snapshot(harness.setup)
    core.state = _Snapshot(replace(harness.state, module_caps={"agent": (prompt,)}))
    app = create_app(
        core,
        CapsManager(core.layout),
        ChoresManager(core.layout),
        cors_allowed_origins=(),
    )

    try:
        with TestClient(app) as client:
            models = client.get(
                "/api/v0/models",
                params=[("query", "missing/*"), ("query", "test/*")],
            )
            no_models = client.get(
                "/api/v0/models",
                params={"query": "missing/*"},
            )
            tools = client.get(
                "/api/v0/tools",
                params={"query": "test/*"},
            )
            caps = client.get(
                "/api/v0/caps",
                params={"query": "prompt/review"},
            )
            prompts = client.get(
                "/api/v0/prompts",
                params={"query": "prompt/review"},
            )
            invalid = client.get(
                "/api/v0/models",
                params={"query": "*[unknown=true]"},
            )
            invalid_tools = client.get(
                "/api/v0/tools",
                params={"query": "*[unknown=true]"},
            )

        assert [item["ref"] for item in models.json()["items"]] == [TEST_MODEL_REF]
        assert models.json()["default"] == TEST_MODEL_REF
        model_item = models.json()["items"][0]
        assert model_item["price"] == {"input": None, "output": None}
        assert model_item["parameters"]["reasoning"]["applicable"] is True
        assert no_models.json() == {"default": None, "items": []}
        assert tools.json() == {
            "items": [
                {
                    "ref": "test/test__lookup",
                    "toolset": "test",
                    "plugin": "test",
                    "description": "Look up a test value.",
                }
            ]
        }
        assert caps.json()["counts"] == {
            "psyches": 0,
            "skills": 0,
            "services": 0,
            "prompts": 1,
        }
        assert [item["name"] for item in caps.json()["prompts"]] == ["review"]
        assert caps.json()["prompts"][0]["summary"] == "Review input."
        assert caps.json()["prompts"][0]["form"] == "inline"
        assert [item["name"] for item in prompts.json()] == ["review"]
        assert invalid.status_code == 200
        assert invalid.json()["items"] == []
        assert invalid_tools.status_code == 200
        assert invalid_tools.json()["items"] == []
    finally:
        asyncio.run(core.close())


@pytest.mark.parametrize("endpoint", (agent_router.models, agent_router.tools))
def test_resource_query_does_not_mask_snapshot_failures_as_client_errors(
    endpoint: Any,
    tmp_path: Path,
) -> None:
    layout = AgentLayout.resident(tmp_path, "alice")
    core = AgentCore(layout)
    core.setup = _BrokenSnapshot()

    try:
        with pytest.raises(HTTPException) as caught:
            endpoint(core, query=["test/*"])

        assert caught.value.status_code == 500
        assert caught.value.detail == "snapshot unavailable"
    finally:
        asyncio.run(core.close())


def test_remote_chat_defaults_and_latest_result_endpoints(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_version = "0.2.7-88-gc73484a9"
    monkeypatch.setattr(agent_router, "toolang_version", lambda: source_version)
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic chat(_: Part[]) -> Part[]:
  recall = none
  context = none
  instruct = none
  user: {{_}}
""",
        responses=[ModelCallResult(message=Message.assistant("remote answer"))],
    )
    setup = replace(
        harness.setup,
        defaults=RunDefaults(
            model=ModelRequest(
                TEST_MODEL_REF,
                reasoning=Reasoning(effort="high"),
            ),
        ),
    )
    harness.store.close()
    core = AgentCore(setup.layout)
    core.setup = _Snapshot(setup)
    core.state = _Snapshot(harness.state)
    (core.layout.lab / "subdir").mkdir(parents=True)
    workloads.write_runtime_state(
        core.layout,
        endpoint="http://127.0.0.1:7001",
        started_at="2026-08-25T00:00:00Z",
        pid=123,
        sandbox_description=_HOST_DESCRIPTION,
    )
    app = create_app(
        core,
        CapsManager(core.layout),
        ChoresManager(core.layout),
        cors_allowed_origins=(),
    )

    try:
        with TestClient(app) as client:
            runtime = client.get("/api/v0/profile").json()["runtime"]
            models = client.get("/api/v0/models")
            defaults = client.get("/api/v0/runs/defaults")
            created = client.post("/api/v0/threads", json={"client_id": "test-tui"})
            thread_id = created.json()["thread"]["id"]
            thread_defaults = client.get(
                "/api/v0/runs/defaults", params={"thread_id": thread_id}
            )
            absolute_workdir = client.post(
                "/api/v0/runs/workdir/resolve",
                json={
                    "thread_id": thread_id,
                    "workdir": str(core.layout.lab / "subdir"),
                },
            )
            relative_workdir = client.post(
                "/api/v0/runs/workdir/resolve",
                json={
                    "thread_id": thread_id,
                    "workdir": "subdir",
                    "workdir_base": "lab://",
                },
            )
            empty = client.get(f"/api/v0/threads/{thread_id}/result")
            unknown = client.get("/api/v0/threads/chat_missing/result")
            wrong_namespace = client.get("/api/v0/threads/run_missing/result")
            executed = client.post(
                "/api/v0/runs/authored/stream",
                json={
                    "thread_id": thread_id,
                    "request_id": "chat_remote_chat",
                    "runnable": {"ref": "agic:chat", "input": {"_": "hello"}},
                    "model": {
                        "ref": TEST_MODEL_REF,
                    },
                    "policy": {"allow": [], "limits": {}},
                    "workdir": "subdir",
                    "workdir_base": "lab://",
                },
            )
            run_id = executed.headers["X-Toolang-Run-ID"]
            explicit_response = client.get(f"/api/v0/runs/{run_id}")
            latest_response = client.get(f"/api/v0/threads/{thread_id}/result")
            final_thread_defaults = client.get(
                "/api/v0/runs/defaults", params={"thread_id": thread_id}
            )
            new_session_defaults = client.get("/api/v0/runs/defaults")

        explicit = TypeAdapter(RunDetail).validate_python(explicit_response.json())
        latest = TypeAdapter(RunDetail).validate_python(latest_response.json())

        assert runtime == {
            "drivers": ["chat", "chore", "talk"],
            "version": source_version,
            "sandbox": {
                "driver": "host",
                "selector": "host",
                "instance": None,
                "description": _HOST_DESCRIPTION,
            },
        }
        assert defaults.status_code == 200
        assert defaults.json()["model"] == {
            "ref": TEST_MODEL_REF,
            "reasoning": {"effort": "high", "budget_tokens": None},
            "max_output": None,
        }
        assert defaults.json()["runnable"] == "agic:chat"
        assert defaults.json()["workdir"] == "lab://"
        assert thread_defaults.status_code == 200
        assert thread_defaults.json()["workdir"] == "lab://"
        assert absolute_workdir.status_code == 200
        assert absolute_workdir.json() == {"workdir": "lab://subdir"}
        assert relative_workdir.status_code == 200
        assert relative_workdir.json() == {"workdir": "lab://subdir"}
        assert final_thread_defaults.status_code == 200
        assert final_thread_defaults.json()["workdir"] == "lab://subdir"
        assert new_session_defaults.status_code == 200
        assert new_session_defaults.json()["workdir"] == "lab://subdir"
        assert models.status_code == 200
        assert models.json()["default"] == TEST_MODEL_REF
        assert core.store.list_runs(limit=None) == [core.store.get_run(run_id=run_id)]
        assert empty.status_code == 404
        assert empty.json()["detail"] == f"thread has no result: {thread_id}"
        assert unknown.status_code == 404
        assert unknown.json()["detail"] == "thread not found: chat_missing"
        assert wrong_namespace.status_code == 404
        assert wrong_namespace.json()["detail"] == "thread not found: run_missing"
        assert explicit.id == latest.id == run_id
        assert explicit.output is not None
        assert latest.output is not None
        assert parts_from_value(explicit.output.value) == (TextPart("remote answer"),)
        assert parts_from_value(latest.output.value) == (TextPart("remote answer"),)
    finally:
        asyncio.run(core.close())


def test_profile_does_not_scan_execution_history(tmp_path: Path, monkeypatch) -> None:
    core = AgentCore(AgentLayout.resident(tmp_path, "alice"))
    try:

        def unavailable(*args, **kwargs):
            raise AssertionError("profile must not read execution history")

        monkeypatch.setattr(core.history, "list_threads", unavailable)
        monkeypatch.setattr(core.store, "list_runs", unavailable)
        monkeypatch.setattr(
            agents.AgentProcess,
            "state",
            lambda self: {"sandbox": "host", "sandbox_description": "local"},
        )
        assert "metrics" not in profile(core)
    finally:
        asyncio.run(core.close())


def test_profile_preserves_source_version_and_short_docker_instance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = AgentLayout.resident(tmp_path, "alice")
    layout.home.mkdir(parents=True)
    workloads.write_runtime_state(
        layout,
        endpoint="http://127.0.0.1:7001",
        started_at="2026-08-25T00:00:00Z",
        pid=123,
        sandbox="docker:python:3.13-slim",
        sandbox_instance=_CONTAINER_ID[:12],
    )
    core = AgentCore(layout)
    source_version = "0.2.7-88-gc73484a9"
    monkeypatch.setattr(
        agent_router,
        "toolang_version",
        lambda: source_version,
        raising=False,
    )

    try:
        payload = profile(core)

        assert payload["runtime"] == {
            "drivers": ("chat", "chore", "talk"),
            "version": source_version,
            "sandbox": {
                "driver": "docker",
                "selector": "docker:python:3.13-slim",
                "instance": _CONTAINER_ID[:12],
                "description": None,
            },
        }
    finally:
        asyncio.run(core.close())


def test_profile_rejects_invalid_docker_instance(tmp_path: Path) -> None:
    layout = AgentLayout.resident(tmp_path, "alice")
    layout.home.mkdir(parents=True)
    workloads.write_runtime_state(
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
    workloads.write_runtime_state(
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
