"""Authored run streaming and RunClient-compatible HTTP controls."""

from __future__ import annotations

import asyncio
from decimal import Decimal
import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from pydantic import TypeAdapter

from toolang.api.app import create_app
from toolang.api.routers.runs import cancel_run, steer_run
from toolang.api.schemas import (
    InputMessagePayload,
    RunCancelRequest,
    RunSteerRequest,
)
from toolang.base.types.message import Message
from toolang.base.types.run import ModelCallResult
from toolang.catalog import CapsManager, JobsManager
from toolang.execution.calls import resolve_run_request
from toolang.execution.records import StartControlPayload
from toolang.execution.schemas import RunDetail, RunRequest
from toolang.execution.types import ThreadPrefix
from toolang.lang.input import RunnableInputRaw
from toolang.up import AgentCore
from tests.support.execution_harness import ExecutionHarness


class _Snapshot:
    def __init__(self, value: object) -> None:
        self.value = value
        self.reads = 0

    def current(self) -> Any:
        self.reads += 1
        return self.value


def test_authored_run_stream_resolves_fallback_policy_and_server_include(
    tmp_path: Path,
) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic chat(_: Part[], tone: Text) -> Part[]:
  recall = none
  context: none
  instruct: none
  user: {{tone}} {{_}}

agic selected(_: Part[], tone: Text) -> Part[]:
  recall = none
  context: none
  instruct: none
  user: {{tone}} {{_}}
""",
        responses=[
            ModelCallResult(message=Message.assistant("fallback reply")),
            ModelCallResult(message=Message.assistant("selected reply")),
        ],
    )
    harness.setup.layout.home.mkdir(parents=True, exist_ok=True)
    (harness.setup.layout.home / "note.md").write_text("included", encoding="utf-8")
    harness.store.close()
    core = AgentCore(harness.setup.layout)
    setup = _Snapshot(harness.setup)
    state = _Snapshot(harness.state)
    core.setup = setup
    core.state = state
    app = create_app(
        core,
        CapsManager(core.layout),
        JobsManager(core.layout),
        cors_allowed_origins=("https://ui.test",),
    )

    try:
        with TestClient(app) as client:
            created = client.post("/api/v1/threads", json={"client": "tui"})
            thread_id = created.json()["thread"]["id"]
            fallback = client.post(
                "/api/v1/runs/authored/stream",
                headers={"origin": "https://ui.test"},
                json={
                    "thread": thread_id,
                    "request_id": "fallback_request",
                    "input": {
                        "primary": "@note.md",
                        "named": [{"name": "tone", "source": "brief"}],
                    },
                    "session_commands": [
                        {"group": "limit", "field": "cost", "value": "2.50"}
                    ],
                    "runnable_fallbacks": ["agic:missing", "agic:chat", "default"],
                },
            )
            fallback_events = _sse_events(fallback.text)
            fallback_id = str(fallback_events[0][1]["run"])
            fallback_detail_response = client.get(f"/api/v1/runs/{fallback_id}")
            selected = client.post(
                "/api/v1/runs/authored/stream",
                json={
                    "thread": thread_id,
                    "request_id": "selected_request",
                    "commands": [
                        {
                            "group": "default",
                            "field": "runnable",
                            "value": "agic:selected",
                        }
                    ],
                    "input": {
                        "primary": "hello",
                        "named": [{"name": "tone", "source": "direct"}],
                    },
                    "session_commands": [
                        {
                            "group": "default",
                            "field": "runnable",
                            "value": "agic:chat",
                        }
                    ],
                    "runnable_fallbacks": ["agic:chat", "default"],
                },
            )
            selected_events = _sse_events(selected.text)
            selected_id = str(selected_events[0][1]["run"])
            selected_detail_response = client.get(f"/api/v1/runs/{selected_id}")
            duplicate = client.post(
                "/api/v1/runs/authored/stream",
                json={
                    "thread": thread_id,
                    "request_id": "selected_request",
                    "input": {
                        "primary": "duplicate",
                        "named": [{"name": "tone", "source": "duplicate"}],
                    },
                    "runnable_fallbacks": ["agic:chat", "default"],
                },
            )
            invalid_policy = client.post(
                "/api/v1/runs/authored/stream",
                json={
                    "thread": thread_id,
                    "request_id": "invalid_request",
                    "commands": [{"group": "allow", "field": "models", "value": "all"}],
                    "input": {"primary": "invalid"},
                    "runnable_fallbacks": ["agic:chat", "default"],
                },
            )
            invalid_fallback = client.post(
                "/api/v1/runs/authored/stream",
                json={
                    "thread": thread_id,
                    "request_id": "invalid_fallback_request",
                    "input": {"primary": "invalid"},
                    "runnable_fallbacks": ["agic:missing", "flow:missing"],
                },
            )
            invalid_input = client.post(
                "/api/v1/runs/authored/stream",
                json={
                    "thread": thread_id,
                    "request_id": "invalid_input_request",
                    "input": {
                        "primary": "invalid",
                        "named": [{"name": "not-valid", "source": "value"}],
                    },
                    "runnable_fallbacks": ["agic:chat", "default"],
                },
            )
            invalid_include = client.post(
                "/api/v1/runs/authored/stream",
                json={
                    "thread": thread_id,
                    "request_id": "invalid_include_request",
                    "input": {
                        "primary": "@missing.md",
                        "named": [{"name": "tone", "source": "brief"}],
                    },
                    "runnable_fallbacks": ["agic:chat", "default"],
                },
            )
            missing_thread = client.post(
                "/api/v1/runs/authored/stream",
                json={
                    "thread": "term_missing",
                    "request_id": "missing_request",
                    "input": {"primary": "missing"},
                    "runnable_fallbacks": ["agic:chat", "default"],
                },
            )

        fallback_detail = TypeAdapter(RunDetail).validate_python(
            fallback_detail_response.json()
        )
        selected_detail = TypeAdapter(RunDetail).validate_python(
            selected_detail_response.json()
        )
        fallback_control = core.store.get_run_control(run_id=fallback_id, index=0)

        assert created.status_code == 201
        assert fallback.status_code == 200
        assert fallback.headers["X-Toolang-Run-ID"] == fallback_id
        assert fallback.headers["access-control-expose-headers"] == ("X-Toolang-Run-ID")
        assert fallback_events[0][0] == "run_begin"
        assert fallback_events[-1][0] == "run_end"
        assert fallback_detail.runnable_name == "chat"
        assert fallback_detail.input_text == "included"
        assert fallback_detail.controls[0].request_id == "fallback_request"
        assert fallback_control is not None
        assert isinstance(fallback_control.payload, StartControlPayload)
        assert fallback_control.payload.limits.cost == Decimal("2.50")
        assert selected.status_code == 200
        assert selected.headers["X-Toolang-Run-ID"] == selected_id
        assert selected_detail.runnable_name == "selected"
        assert selected_detail.controls[0].request_id == "selected_request"
        assert [
            invocation.call.messages for invocation in harness.adapter.invocations
        ] == [
            [Message.user("brief included")],
            [Message.user("direct hello")],
        ]
        assert setup.reads == 5
        assert state.reads == 5
        assert duplicate.status_code == 422
        assert duplicate.json()["detail"] == (
            "run control request already exists: selected_request"
        )
        assert invalid_policy.status_code == 422
        assert invalid_policy.json()["detail"] == (
            "allow policy value must be selectors, all, or none"
        )
        assert invalid_fallback.status_code == 422
        assert invalid_fallback.json()["detail"] == (
            "no runnable fallback is available: agic:missing, flow:missing"
        )
        assert invalid_input.status_code == 422
        assert invalid_input.json()["detail"] == (
            "named input must use a canonical name"
        )
        assert invalid_include.status_code == 422
        assert "missing.md" in invalid_include.json()["detail"]
        assert missing_thread.status_code == 404
        assert missing_thread.json()["detail"] == "thread not found: term_missing"
        assert len(core.store.list_runs(thread_id=thread_id, limit=None)) == 2
    finally:
        asyncio.run(core.close())


def test_http_controls_accept_pending_run_and_empty_steer(tmp_path: Path) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic chat(_: Part[]) -> Part[]:
  recall = none
  context: none
  instruct: none
  user: {{_}}
""",
        responses=[ModelCallResult(message=Message.assistant("finished"))],
    )
    harness.store.close()
    core = AgentCore(harness.setup.layout)
    core.setup = _Snapshot(harness.setup)
    core.state = _Snapshot(harness.state)

    async def scenario() -> None:
        thread = core.threads.create(prefix=ThreadPrefix.TERM)
        request = RunRequest(
            thread=thread,
            commands=(),
            input=RunnableInputRaw(primary="hello"),
            session_commands=(),
            runnable_fallbacks=("agic:chat", "default"),
            request_id="pending_request",
        )
        handle = core.executor.start(
            resolve_run_request(
                request,
                setup=harness.setup,
                state=harness.state,
            ),
            request_id=request.request_id,
        )

        stored = core.store.get_run(run_id=handle.run_id)
        assert stored is not None and stored.status == "pending"
        steer = steer_run(
            core,
            handle.run_id,
            RunSteerRequest(
                request_id="steer_request",
                message=InputMessagePayload(parts=[]),
            ),
        )
        stop = cancel_run(
            core,
            handle.run_id,
            RunCancelRequest(
                request_id="stop_request",
                reason="stop pending",
            ),
        )
        await handle

        assert steer.command.kind == "steer"
        assert steer.command.request_id == "steer_request"
        assert stop.command.kind == "stop"
        assert stop.command.request_id == "stop_request"
        terminal = core.store.get_run(run_id=handle.run_id)
        assert terminal is not None
        assert terminal.status in {"succeeded", "canceled"}

    try:
        asyncio.run(scenario())
    finally:
        asyncio.run(core.close())


def _sse_events(source: str) -> list[tuple[str, dict[str, Any]]]:
    events: list[tuple[str, dict[str, Any]]] = []
    event_name: str | None = None
    data_lines: list[str] = []
    for line in (*source.splitlines(), ""):
        if line.startswith("event: "):
            event_name = line.removeprefix("event: ")
        elif line.startswith("data: "):
            data_lines.append(line.removeprefix("data: "))
        elif not line and event_name is not None:
            events.append((event_name, json.loads("\n".join(data_lines))))
            event_name = None
            data_lines = []
    return events
