"""Authored run streaming and RunClient-compatible HTTP controls."""

from __future__ import annotations

import asyncio
from decimal import Decimal
import json
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest
from pydantic import TypeAdapter

from toolang.api.app import create_app
from toolang.api.routers.runs import cancel_run, steer_run
from toolang.api.schemas import (
    InputMessagePayload,
    RunCancelRequest,
    RunSteerRequest,
)
from toolang.base.types.message import Message
from toolang.base.types.model import ModelRequest
from toolang.base.types.policy import RunPolicy
from toolang.base.types.run import ModelCallResult, ModelUsage
from toolang.catalog import CapsManager, JobsManager
from toolang.execution.calls import resolve_run_request
from toolang.execution.records import (
    RetryControlPayload,
    RunControlPayload,
)
from toolang.execution.schemas import RunDetail, RunRequest, RunnableRequest
from toolang.execution.types import ThreadPrefix
from toolang.lang.input import NamedInputSource, RunnableInputRaw
from toolang.up import AgentCore
from tests.support.execution_harness import ExecutionHarness, TEST_MODEL_REF


class _Snapshot:
    def __init__(self, value: object) -> None:
        self.value = value
        self.reads = 0

    def current(self) -> Any:
        self.reads += 1
        return self.value

    def load(self, revision: str) -> Any:
        if getattr(self.value, "revision", None) != revision:
            raise ValueError(f"snapshot revision not found: {revision}")
        return self.value

    async def refresh(self) -> object:
        raise AssertionError("run request boundaries must not refresh publications")


def _authored_request(
    thread_id: str,
    request_id: str,
    *,
    source: str = "hello",
    runnable: str = "agic:chat",
    named: list[dict[str, str]] | None = None,
    allow: list[dict[str, object]] | None = None,
    limits: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "thread_id": thread_id,
        "request_id": request_id,
        "runnable": {
            "ref": runnable,
            "input": {"_": source, "named": named or []},
        },
        "model": {"ref": TEST_MODEL_REF, "parameters": {}},
        "policy": {"allow": allow or [], "limits": limits or {}},
    }


def _core_request(thread_id: str, request_id: str) -> RunRequest:
    return RunRequest(
        thread_id=thread_id,
        request_id=request_id,
        runnable=RunnableRequest("agic:chat", RunnableInputRaw(_="hello")),
        model=ModelRequest(TEST_MODEL_REF),
        policy=RunPolicy(),
    )


def test_authored_run_stream_resolves_fallback_policy_and_server_include(
    tmp_path: Path,
) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
prompt review:
  {{focus}} {{_}}

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
    (harness.setup.layout.home / "note.txt").write_text("included", encoding="utf-8")
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
            prompt_completions = client.get(
                "/api/v1/prompt-completions",
                params={"runnable": "agic:selected"},
            )
            assert prompt_completions.json() == {
                "items": [
                    {
                        "name": "review",
                        "params": [{"name": "focus", "optional": False}],
                    }
                ]
            }
            created = client.post("/api/v1/threads", json={"client": "tui"})
            thread_id = created.json()["thread"]["id"]
            fallback = client.post(
                "/api/v1/runs/authored/stream",
                headers={"origin": "https://ui.test"},
                json=_authored_request(
                    thread_id,
                    "fallback_request",
                    source="$review focus=security -\n@note.txt",
                    named=[{"name": "tone", "source": "brief"}],
                    limits={"cost": "2.50"},
                ),
            )
            fallback_events = _sse_events(fallback.text)
            fallback_id = str(fallback_events[0][1]["run"])
            fallback_detail_response = client.get(f"/api/v1/runs/{fallback_id}")
            selected = client.post(
                "/api/v1/runs/authored/stream",
                json=_authored_request(
                    thread_id,
                    "selected_request",
                    runnable="agic:selected",
                    named=[{"name": "tone", "source": "direct"}],
                ),
            )
            selected_events = _sse_events(selected.text)
            selected_id = str(selected_events[0][1]["run"])
            selected_detail_response = client.get(f"/api/v1/runs/{selected_id}")
            duplicate = client.post(
                "/api/v1/runs/authored/stream",
                json=_authored_request(
                    thread_id,
                    "selected_request",
                    source="duplicate",
                    named=[{"name": "tone", "source": "duplicate"}],
                ),
            )
            invalid_policy = client.post(
                "/api/v1/runs/authored/stream",
                json=_authored_request(
                    thread_id,
                    "invalid_request",
                    source="invalid",
                    allow=[{"models": "all"}],
                ),
            )
            invalid_fallback = client.post(
                "/api/v1/runs/authored/stream",
                json=_authored_request(
                    thread_id,
                    "invalid_fallback_request",
                    source="invalid",
                    runnable="agic:missing",
                ),
            )
            invalid_input = client.post(
                "/api/v1/runs/authored/stream",
                json=_authored_request(
                    thread_id,
                    "invalid_input_request",
                    source="invalid",
                    named=[{"name": "not-valid", "source": "value"}],
                ),
            )
            invalid_include = client.post(
                "/api/v1/runs/authored/stream",
                json=_authored_request(
                    thread_id,
                    "invalid_include_request",
                    source="@missing.txt",
                    named=[{"name": "tone", "source": "brief"}],
                ),
            )
            invalid_home_include = client.post(
                "/api/v1/runs/authored/stream",
                json=_authored_request(
                    thread_id,
                    "invalid_home_include_request",
                    source="@~toolang_user_that_does_not_exist/file.txt",
                    named=[{"name": "tone", "source": "brief"}],
                ),
            )
            missing_thread = client.post(
                "/api/v1/runs/authored/stream",
                json=_authored_request(
                    "term_missing", "missing_request", source="missing"
                ),
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
        assert fallback_detail.input_text == "$review focus=security - @note.txt"
        assert fallback_detail.controls[0].request_id == "fallback_request"
        assert (
            fallback_detail_response.json()["controls"][0]["payload"]["sandbox"]
            == "host"
        )
        assert fallback_control is not None
        assert isinstance(fallback_control.payload, RunControlPayload)
        assert fallback_control.payload.limits.cost == Decimal("2.50")
        assert fallback_control.payload.sandbox == "host"
        assert fallback_control.payload.authored_input == RunnableInputRaw(
            _="$review focus=security -\n@note.txt",
            named=(NamedInputSource("tone", "brief"),),
        )
        assert len(fallback_control.payload.prompt_invocations) == 1
        prompt = fallback_control.payload.prompt_invocations[0]
        assert prompt.name == "review"
        assert prompt.arguments == (("focus", "security"),)
        assert prompt.parent is None
        assert prompt.cap_ref
        assert len(prompt.content_hash) == 64
        assert selected.status_code == 200
        assert selected.headers["X-Toolang-Run-ID"] == selected_id
        assert selected_detail.runnable_name == "selected"
        assert selected_detail.controls[0].request_id == "selected_request"
        assert [
            invocation.call.messages for invocation in harness.adapter.invocations
        ] == [
            [Message.user("brief security @note.txt")],
            [Message.user("direct hello")],
        ]
        assert setup.reads == 7
        assert state.reads == 7
        assert duplicate.status_code == 422
        assert duplicate.json()["detail"] == (
            "run control request already exists: selected_request"
        )
        assert invalid_policy.status_code == 422
        assert invalid_policy.json()["detail"][0]["msg"] == (
            "Input should be a valid tuple"
        )
        assert invalid_fallback.status_code == 422
        assert invalid_fallback.json()["detail"] == "runnable query matched no items"
        assert invalid_input.status_code == 422
        assert (
            "named input must use a canonical name"
            in (invalid_input.json()["detail"][0]["msg"])
        )
        assert invalid_include.status_code == 422
        assert "missing.txt" in invalid_include.json()["detail"]
        assert invalid_home_include.status_code == 422
        assert invalid_home_include.json()["detail"] == (
            "included file not found: ~toolang_user_that_does_not_exist/file.txt"
        )
        assert missing_thread.status_code == 404
        assert missing_thread.json()["detail"] == "thread not found: term_missing"
        assert len(core.store.list_runs(thread_id=thread_id, limit=None)) == 2
    finally:
        asyncio.run(core.close())


def test_authored_retry_and_rerun_streams_subscribe_at_acceptance(
    tmp_path: Path,
) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic chat(_: Part[]) -> Part[]:
  recall = none
  context: none
  instruct: none
  user: {{_}}
""",
        responses=[
            RuntimeError("temporary failure"),
            ModelCallResult(
                message=Message.assistant("recovered"),
                usage=ModelUsage(input_tokens=1, output_tokens=1),
            ),
            ModelCallResult(message=Message.assistant("reran")),
        ],
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
            thread_id = client.post(
                "/api/v1/threads",
                json={"client": "tui"},
            ).json()["thread"]["id"]
            source = client.post(
                "/api/v1/runs/authored/stream",
                json=_authored_request(thread_id, "source_request"),
            )
            source_id = source.headers["X-Toolang-Run-ID"]

            retry = client.post(
                f"/api/v1/runs/{source_id}/retry/stream",
                json={
                    "request_id": "retry_request",
                    "commands": [{"group": "limit", "field": "tokens", "value": 10}],
                },
            )
            duplicate = client.post(
                f"/api/v1/runs/{source_id}/retry/stream",
                json={"request_id": "retry_request"},
            )
            invalid_anchor = client.post(
                f"/api/v1/runs/{source_id}/retry/stream",
                json={
                    "request_id": "invalid_anchor_request",
                    "anchor": "run_other.0",
                },
            )
            rerun = client.post(
                f"/api/v1/runs/{source_id}/rerun/stream",
                json={
                    "request_id": "rerun_request",
                    "commands": [{"group": "limit", "field": "time", "value": 30}],
                },
            )

        source_events = _sse_events(source.text)
        retry_events = _sse_events(retry.text)
        rerun_events = _sse_events(rerun.text)
        rerun_id = rerun.headers["X-Toolang-Run-ID"]
        retry_detail = core.history.get_run(source_id)
        rerun_detail = core.history.get_run(rerun_id)

        assert source_events[0][0] == retry_events[0][0] == "run_begin"
        assert retry_events[-1][0] == rerun_events[-1][0] == "run_end"
        assert retry.headers["X-Toolang-Run-ID"] == source_id
        assert rerun_id != source_id
        assert retry_detail is not None and retry_detail.status == "succeeded"
        assert rerun_detail is not None and rerun_detail.status == "succeeded"
        assert retry_detail.controls[-1].request_id == "retry_request"
        assert isinstance(retry_detail.controls[-1].payload, RetryControlPayload)
        assert retry_detail.controls[-1].payload.limits.tokens == 10
        assert rerun_detail.controls[0].request_id == "rerun_request"
        assert isinstance(rerun_detail.controls[0].payload, RunControlPayload)
        assert rerun_detail.controls[0].payload.limits.time == 30
        assert duplicate.status_code == 409
        assert duplicate.json()["detail"] == (
            "run control request already exists: retry_request"
        )
        assert invalid_anchor.status_code == 422
        assert invalid_anchor.json()["detail"] == (
            "retry request anchor must belong to its source run"
        )
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
        request = _core_request(thread, "pending_request")
        handle = core.executor.run(
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
        cancellation = cancel_run(
            core,
            handle.run_id,
            RunCancelRequest(
                request_id="cancel_request",
                reason="cancel pending",
            ),
        )
        await handle

        assert steer.command.kind == "steer"
        assert steer.command.request_id == "steer_request"
        assert cancellation.command.kind == "cancel"
        assert cancellation.command.request_id == "cancel_request"
        terminal = core.store.get_run(run_id=handle.run_id)
        assert terminal is not None
        assert terminal.status in {"succeeded", "canceled"}

    try:
        asyncio.run(scenario())
    finally:
        asyncio.run(core.close())


def test_http_controls_map_transaction_state_races_to_conflict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="agic chat:\n  hello\n",
        responses=[],
    )
    harness.store.close()
    core = AgentCore(harness.setup.layout)

    async def scenario() -> None:
        thread = core.threads.create(prefix=ThreadPrefix.TERM)
        handle = core.executor.run(
            resolve_run_request(
                _core_request(thread, "pending_race_request"),
                setup=harness.setup,
                state=harness.state,
            ),
            request_id="pending_race_request",
        )

        def reject_control(**_kwargs: object) -> None:
            raise ValueError(f"run is not active: {handle.run_id}")

        monkeypatch.setattr(core.executor, "cancel", reject_control)
        monkeypatch.setattr(core.executor, "steer", reject_control)

        with pytest.raises(HTTPException) as cancel_error:
            cancel_run(core, handle.run_id)
        with pytest.raises(HTTPException) as steer_error:
            steer_run(
                core,
                handle.run_id,
                RunSteerRequest(message=InputMessagePayload(parts=[])),
            )

        assert cancel_error.value.status_code == 409
        assert cancel_error.value.detail == f"run is not active: {handle.run_id}"
        assert steer_error.value.status_code == 409
        assert steer_error.value.detail == f"run is not active: {handle.run_id}"
        await handle

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
