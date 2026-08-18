from __future__ import annotations

import asyncio
from dataclasses import replace
from decimal import Decimal
import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace
from typing import Any, cast

from fastapi import Request
from fastapi.testclient import TestClient
from pydantic import TypeAdapter

from toolang.api.app import create_app
from toolang.api.common import LiveEventRelay, sse_stream
from toolang.base.types.message import DocumentPart, Message, TextPart
from toolang.base.types.policy import RunBindings, RunLimits
from toolang.base.types.run import ModelCallResult, ModelUsage
from toolang.catalog import CapsManager, JobsManager
from toolang.execution.events import (
    PartBegin,
    RunBegin,
    RunEnd,
    StepBegin,
    StepEnd,
    ThreadCreated,
    run_event_from_data,
    run_event_to_data,
)
from toolang.execution.records import (
    RerunControlPayload,
    RetryControlPayload,
    ThreadControlRef,
    ThreadPeer,
)
from toolang.execution.schemas import RunDetail, ThreadDetail
from toolang.execution.types import ControlRef, Local, StepPath, Pointer
from toolang.up import AgentCore
from tests.support.execution_fixtures import project_run_start, project_step
from tests.support.execution_harness import ExecutionHarness


class _Snapshot:
    def __init__(self, value: object) -> None:
        self.value = value

    def current(self) -> Any:
        return self.value


def test_run_stream_emits_complete_canonical_event_sequence(tmp_path: Path) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic answer(_: Part[]) -> Part[]:
  recall = none
  context: none
  instruct: none
  user: {{_}}
""",
        responses=[ModelCallResult(message=Message.assistant("hello back"))],
    )
    setup = replace(
        harness.setup,
        bindings=RunBindings(
            model="test/scripted",
            runnable="agic:answer",
        ),
    )
    harness.store.close()
    core = AgentCore(setup.layout)
    core.setup = _Snapshot(setup)
    core.state = _Snapshot(harness.state)
    app = create_app(
        core,
        CapsManager(core.layout),
        JobsManager(core.layout),
        cors_allowed_origins=(),
    )

    try:
        with TestClient(app) as client:
            models = client.get("/api/v1/models")
            agics = client.get("/api/v1/agics")
            flows = client.get("/api/v1/flows")
            created = client.post(
                "/api/v1/threads",
                json={"client": "script"},
            )
            thread_id = created.json()["thread"]["id"]
            response = client.post(
                "/api/v1/runs/stream",
                json={
                    "thread": thread_id,
                    "runnable": "answer",
                    "input": [
                        {"type": "text", "text": "hello"},
                        {
                            "type": "document",
                            "data": "data:application/pdf;base64,ZmFrZQ==",
                            "filename": "brief.pdf",
                        },
                    ],
                },
            )
        events = _sse_events(response.text)
        decoded = [run_event_from_data(data) for _event, data in events]

        assert created.status_code == 201
        assert models.json()["default"] == "test/scripted"
        assert agics.json()["default"] == "answer"
        assert flows.json()["default"] is None
        assert thread_id.startswith("script_")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["cache-control"] == "no-cache"
        assert response.headers["x-accel-buffering"] == "no"
        assert [event for event, _data in events] == [
            "run_begin",
            "step_begin",
            "part_begin",
            "part_end",
            "step_end",
            "run_end",
        ]
        assert [event.type for event in decoded] == [event for event, _data in events]
        assert [run_event_to_data(event) for event in decoded] == [
            data for _event, data in events
        ]
        assert all(data["type"] == event for event, data in events)
        assert isinstance(decoded[2], PartBegin)
        assert decoded[2].part_type == "text"
        assert events[2][1]["part_type"] == "text"
        assert "type_" not in events[2][1]
        assert events[-1][1]["status"] == "succeeded"
        assert harness.adapter.invocations[0].call.messages == [
            Message(
                role="user",
                parts=(
                    TextPart("hello"),
                    DocumentPart(
                        data="data:application/pdf;base64,ZmFrZQ==",
                        filename="brief.pdf",
                        media_type="application/pdf",
                    ),
                ),
            )
        ]
    finally:
        asyncio.run(core.close())


def test_run_detail_exposes_one_structured_step_error(tmp_path: Path) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic answer(_: Part[]) -> Part[]:
  recall = none
  context: none
  instruct: none
  user: {{_}}
""",
        responses=[RuntimeError("provider unavailable")],
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
            created = client.post("/api/v1/threads", json={"client": "script"})
            response = client.post(
                "/api/v1/runs/stream",
                json={
                    "thread": created.json()["thread"]["id"],
                    "runnable": "answer",
                    "input": [{"type": "text", "text": "hello"}],
                },
            )
            events = _sse_events(response.text)
            run_id = str(events[0][1]["run"])
            detail = client.get(f"/api/v1/runs/{run_id}").json()

        error = f"{run_id}.0"
        assert events[-1][1]["error"] == {"$ptr": error}
        assert detail["error"] == {"$ptr": error}
        assert detail["steps"][0]["error"] == "provider unavailable"
        assert "failure" not in detail
    finally:
        asyncio.run(core.close())


def test_chat_client_can_create_thread_then_use_canonical_run_stream(
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
        responses=[ModelCallResult(message=Message.assistant("chat reply"))],
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
            created = client.post(
                "/api/v1/threads",
                json={"client": "web"},
            )
            thread_id = created.json()["thread"]["id"]
            response = client.post(
                "/api/v1/runs/stream",
                json={
                    "thread": thread_id,
                    "runnable": "chat",
                    "input": [{"type": "text", "text": "hello"}],
                },
            )
            events = _sse_events(response.text)
            run_id = str(events[0][1]["run"])
            run_response = client.get(f"/api/v1/runs/{run_id}")
            thread_response = client.get(f"/api/v1/threads/{thread_id}")

        run_detail = TypeAdapter(RunDetail).validate_python(run_response.json())
        thread_detail = TypeAdapter(ThreadDetail).validate_python(
            thread_response.json()
        )

        assert created.status_code == 201
        assert response.status_code == 200
        assert run_response.status_code == 200
        assert thread_response.status_code == 200
        assert events[0][0] == "run_begin"
        assert events[-1][0] == "run_end"
        assert {event for event, _data in events} <= {
            "run_begin",
            "step_begin",
            "part_begin",
            "part_delta",
            "part_end",
            "step_end",
            "run_end",
        }
        assert run_detail.output == Local.typed(
            "Part[]", Pointer.step(StepPath.parse(f"{run_id}/0")), "_", 0
        )
        assert thread_detail.runs[0].output == run_detail.output
        threads = core.store.list_threads()
        assert len(threads) == 1
        assert threads[0].thread_id == thread_id
        assert threads[0].origin == "chat"
    finally:
        asyncio.run(core.close())


def test_stream_validation_fails_before_sse_headers(tmp_path: Path) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="agic answer:\n  hello\n",
        responses=[],
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
            created = client.post(
                "/api/v1/threads",
                json={"client": "script"},
            )
            thread_id = created.json()["thread"]["id"]
            response = client.post(
                "/api/v1/runs/stream",
                json={"thread": thread_id, "runnable": "missing"},
            )
            missing_thread = client.post(
                "/api/v1/runs/stream",
                json={"runnable": "answer"},
            )

        assert created.status_code == 201
        assert response.status_code == 422
        assert response.json()["detail"] == "Runnable not found: missing"
        assert missing_thread.status_code == 422
        assert core.store.list_threads()[0].thread_id == thread_id
        assert len(core.store.list_threads()) == 1
    finally:
        asyncio.run(core.close())


def test_retry_and_rerun_api_accept_partial_limit_overrides(tmp_path: Path) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic answer(_: Part[]) -> Part[]:
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
            ModelCallResult(
                message=Message.assistant("reran"),
                usage=ModelUsage(input_tokens=1, output_tokens=1),
            ),
        ],
    )
    setup = replace(
        harness.setup,
        limits=RunLimits(tokens=100, cost=Decimal("5")),
    )
    harness.store.close()
    core = AgentCore(setup.layout)
    core.setup = _Snapshot(setup)
    core.state = _Snapshot(harness.state)
    app = create_app(
        core,
        CapsManager(core.layout),
        JobsManager(core.layout),
        cors_allowed_origins=(),
    )

    try:
        with TestClient(app) as client:
            created = client.post("/api/v1/threads", json={"client": "script"})
            thread_id = created.json()["thread"]["id"]
            short_name = client.post(
                "/api/v1/runs/stream",
                json={
                    "thread": thread_id,
                    "request": "short-request",
                    "runnable": "answer",
                    "input": [{"type": "text", "text": "hello"}],
                },
            )
            started = client.post(
                "/api/v1/runs/stream",
                json={
                    "thread": thread_id,
                    "runnable": "answer",
                    "input": [{"type": "text", "text": "hello"}],
                },
            )
            source_id = str(_sse_events(started.text)[0][1]["run"])

            retry = client.post(
                f"/api/v1/runs/{source_id}/retry",
                json={
                    "request_id": "retry-request",
                    "limits": {"tokens": 10, "cost": None},
                },
            )
            _wait_for_terminal(core, source_id)
            rerun = client.post(
                f"/api/v1/runs/{source_id}/rerun",
                json={
                    "request_id": "rerun-request",
                    "limits": {"cost": None, "time": 30},
                },
            )
            rerun_id = str(rerun.json()["run"]["id"])
            _wait_for_terminal(core, rerun_id)

        assert started.status_code == 200
        assert short_name.status_code == 422
        assert retry.status_code == 202
        assert retry.json()["command"]["kind"] == "retry"
        assert retry.json()["command"]["request_id"] == "retry-request"
        assert rerun.status_code == 202
        assert rerun.json()["command"]["kind"] == "rerun"
        assert rerun.json()["command"]["payload"]["rerun_from"] == source_id

        retry_control = core.store.list_run_controls(run_id=source_id)[-1]
        rerun_control = core.store.get_run_control(run_id=rerun_id, index=0)
        assert isinstance(retry_control.payload, RetryControlPayload)
        assert retry_control.payload.limits == RunLimits(tokens=10)
        assert rerun_control is not None
        assert isinstance(rerun_control.payload, RerunControlPayload)
        assert rerun_control.payload.limits == RunLimits(tokens=100, time=30)
    finally:
        asyncio.run(core.close())


def test_live_relay_preserves_complete_root_run_tree_order() -> None:
    async def scenario() -> None:
        relay = LiveEventRelay()
        run = relay.subscribe_run("run_test")
        thread = relay.subscribe_thread("term_test")
        tracer = relay.trace(thread_id="term_test")
        events = (
            RunBegin(
                run="run_test",
                control=ControlRef("run_test", 0),
                started_at="2026-01-01T00:00:00Z",
            ),
            StepBegin(
                step=StepPath.parse("run_test/0"),
                kind="run",
                started_at="2026-01-01T00:00:01Z",
            ),
            RunBegin(
                run="run_child",
                control=ControlRef("run_child", 0),
                started_at="2026-01-01T00:00:02Z",
            ),
            StepBegin(
                step=StepPath.parse("run_child/0"),
                kind="system",
                started_at="2026-01-01T00:00:03Z",
            ),
            StepEnd(
                step=StepPath.parse("run_child/0"),
                kind="system",
                status="succeeded",
                finished_at="2026-01-01T00:00:04Z",
            ),
            RunEnd(
                run="run_child",
                status="succeeded",
                finished_at="2026-01-01T00:00:05Z",
            ),
            StepEnd(
                step=StepPath.parse("run_test/0"),
                kind="run",
                status="succeeded",
                finished_at="2026-01-01T00:00:06Z",
            ),
            RunEnd(
                run="run_test",
                status="succeeded",
                finished_at="2026-01-01T00:00:07Z",
            ),
        )
        for event in events:
            await tracer.on_event(event)

        run_events = [await run.receive(timeout=1) for _event in events]
        thread_events = [await thread.receive(timeout=1) for _event in events]
        run.close()
        thread.close()

        assert run_events == list(events)
        assert thread_events == list(events)
        assert run.empty
        assert thread.empty

    asyncio.run(scenario())


def test_live_relay_accepts_thread_events_from_worker_threads() -> None:
    async def scenario() -> None:
        relay = LiveEventRelay()
        subscription = relay.subscribe_thread("term_test")
        event = ThreadCreated(
            thread="term_test",
            control=ThreadControlRef("term_test", 0),
            origin="chat",
            peer=ThreadPeer(),
            created_at="2026-01-01T00:00:00Z",
        )

        worker = threading.Thread(target=relay.on_event, args=(event,))
        worker.start()
        worker.join()
        observed = await subscription.receive(timeout=1)
        subscription.close()

        assert observed == event

    asyncio.run(scenario())


def test_existing_run_stream_attaches_to_live_events(tmp_path: Path) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="agic answer:\n  hello\n",
        responses=[],
    )
    harness.store.close()
    core = AgentCore(harness.setup.layout)
    project_run_start(
        core.store,
        run_id="run_live",
        thread_id="script_live",
        origin="script",
        input=Message.user("hello"),
    )
    app = create_app(
        core,
        CapsManager(core.layout),
        JobsManager(core.layout),
        cors_allowed_origins=(),
    )
    relay = cast(LiveEventRelay, app.state.live_events)
    errors: list[str] = []

    def publish() -> None:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            with relay._lock:
                if relay._runs.get("run_live"):
                    break
            time.sleep(0.01)
        else:
            errors.append("run stream did not subscribe")
            return
        tracer = relay.trace(thread_id="script_live")

        async def emit() -> None:
            await tracer.on_event(
                RunBegin(
                    run="run_live",
                    control=ControlRef("run_live", 0),
                    started_at="2026-01-01T00:00:00Z",
                )
            )
            await tracer.on_event(
                RunEnd(
                    run="run_live",
                    status="succeeded",
                    finished_at="2026-01-01T00:00:01Z",
                )
            )

        asyncio.run(emit())

    worker = threading.Thread(target=publish)
    worker.start()
    try:
        with TestClient(app) as client:
            response = client.get("/api/v1/runs/run_live/stream")
        worker.join(timeout=2)
        events = _sse_events(response.text)

        assert errors == []
        assert response.status_code == 200
        assert [event for event, _data in events] == ["run_begin", "run_end"]
    finally:
        asyncio.run(core.close())


def test_child_run_stream_redirects_client_to_root_run(tmp_path: Path) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="agic answer:\n  hello\n",
        responses=[],
    )
    harness.store.close()
    core = AgentCore(harness.setup.layout)
    project_run_start(
        core.store,
        run_id="run_root",
        thread_id="script_tree",
        origin="script",
        input=Message.user("root"),
    )
    project_step(
        core.store,
        run_id="run_root",
        step_index=0,
        kind="run",
        status="running",
        input=(Pointer.control("run_root", 0, "_"),),
        output=(),
        started_at="2026-01-01T00:00:01Z",
        finished_at=None,
    )
    project_run_start(
        core.store,
        run_id="run_child",
        thread_id="script_tree",
        origin="script",
        input=Message.user("child"),
        root_run_id="run_root",
        parent=StepPath.parse("run_root/0"),
    )
    app = create_app(
        core,
        CapsManager(core.layout),
        JobsManager(core.layout),
        cors_allowed_origins=(),
    )

    try:
        with TestClient(app) as client:
            response = client.get("/api/v1/runs/run_child/stream")

        assert response.status_code == 409
        assert response.json()["detail"] == (
            "run stream requires a root run: run_child; subscribe to run_root"
        )
    finally:
        asyncio.run(core.close())


def test_event_collections_are_removed_and_missing_streams_are_404(
    tmp_path: Path,
) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="agic answer:\n  hello\n",
        responses=[],
    )
    harness.store.close()
    layout = harness.setup.layout
    core = AgentCore(layout)
    app = create_app(
        core,
        CapsManager(layout),
        JobsManager(layout),
        cors_allowed_origins=(),
    )

    try:
        with TestClient(app) as client:
            assert client.get("/api/v1/runs/missing/stream").status_code == 404
            assert client.get("/api/v1/threads/missing/stream").status_code == 404
            assert client.get("/api/v1/runs/missing/events").status_code == 404
            assert client.get("/api/v1/threads/missing/events").status_code == 404
            paths = client.get("/openapi.json").json()["paths"]
            tags = client.get("/openapi.json").json()["tags"]
            assert "/api/v1/runs/{run_id}/events" not in paths
            assert "/api/v1/threads/{thread_id}/events" not in paths
            assert all(not path.startswith("/api/v1/chat") for path in paths)
            assert all(tag["name"] != "chat" for tag in tags)
    finally:
        asyncio.run(core.close())


def test_sse_generator_close_removes_subscription() -> None:
    class ConnectedRequest:
        app = SimpleNamespace(state=SimpleNamespace())

        async def is_disconnected(self) -> bool:
            return False

    async def scenario() -> None:
        relay = LiveEventRelay()
        subscription = relay.subscribe_run("run_test")
        tracer = relay.trace(thread_id="term_test")
        stream = sse_stream(
            cast(Request, ConnectedRequest()),
            subscription,
            terminal_run_id="run_test",
        )
        await tracer.on_event(
            RunBegin(
                run="run_test",
                control=ControlRef("run_test", 0),
                started_at="2026-01-01T00:00:00Z",
            )
        )

        first = await anext(stream)
        await stream.aclose()

        assert first.event == "run_begin"
        assert relay._runs == {}

    asyncio.run(scenario())


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


def _wait_for_terminal(core: AgentCore, run_id: str) -> None:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        run = core.store.get_run(run_id=run_id)
        if run is not None and run.status not in {"pending", "running"}:
            return
        time.sleep(0.01)
    raise AssertionError(f"run did not become terminal: {run_id}")
