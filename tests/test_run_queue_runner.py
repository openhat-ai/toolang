from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, contextmanager, suppress
import json
import logging
import os
import socket
import threading
import time
from pathlib import Path
from typing import Literal, cast
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from toolang import agents
from toolang import work
from toolang.base.protocols.channel import ChannelPlugin
from toolang.base.protocols.sandbox import SandboxPlugin
from toolang.base.types.channel import (
    ChannelState,
    DeliveryResult,
    InboundDelivery,
    PluginHealth,
    PollResult,
    ReplyTarget,
)
from toolang.base.types.run import RunResult
from toolang.base.types.message import (
    Message,
    TextDelta,
    TextPart,
    ToolCallDelta,
    ToolCallPart,
    ToolResultPart,
)
from toolang.execution.events import (
    PartDelta,
    PartEnd,
    PartStart,
    StepEnd,
    StepStart,
)
from toolang.execution.records import (
    ModelCallStepPayload,
    RunInputRef,
    RuntimeStepPayload,
    StepOutputRef,
    ToolCallStepPayload,
)
from toolang.base.types.sandbox import (
    SandboxPlan,
    SandboxSelector,
    SandboxStartRequest,
    SandboxStartResult,
    SandboxState,
)
from toolang.caps import (
    add_remote_entry,
    build_scope_lock,
    list_entries,
    list_local_entries,
    put_local_entry,
    remove_entry,
    remove_local_entry,
)
from toolang.config.plugins import ChannelBinding
from toolang.execution import execute as run_execute_module
from toolang.execution.input import assemble_run_input, bind_run_request
from toolang.execution.snapshot import SnapshotTask, SnapshotTaskServices
from toolang.execution.runner import QueueRunner, RunRequest, RunSubmission
from toolang.execution.db import ExecutionStore, execution_db_path
from toolang.loops import chat as chat_loop, inspect, poll, prepare, pulse, reload
from toolang.state.durable import scan_durable_state
from toolang.state.live import load_live_state
from toolang.state.prepared import load_prepared_state, write_prepared_lock
from toolang import up as up_module
from toolang.up import (
    RUN_LOOPS,
    UptimeConfig,
    UptimeContext,
    create_app,
    load_default_models,
    load_model_plugins,
    load_model_profiles,
    load_tool_plugins,
    up as run_experiments_up,
)


def test_runner_queue_is_fifo() -> None:
    async def run_test() -> None:
        runner = QueueRunner(delay_sec=0.0)
        first = RunRequest(group="chat", origin="chat", thunk="summarize inbox")
        second = RunRequest(group="pulse", origin="pulse", thunk="review open tasks")

        assert runner.enqueue(first) == 1
        assert runner.enqueue(second) == 2
        assert runner.peek() == first

        runner.close()

        assert await runner.dequeue() == first
        assert await runner.dequeue() == second
        assert await runner.dequeue() is None

    asyncio.run(run_test())


def test_queue_runner_drains_requests_in_order(tmp_path: Path, caplog) -> None:
    async def run_test() -> None:
        toolang_root = tmp_path / "toolang"
        _write_text(toolang_root / "agents" / "alice" / "alice.too", "agent alice\n")
        context = _build_context(
            toolang_root=toolang_root,
            agent_name="alice",
            enabled_loops=("chat",),
            runner=QueueRunner(
                group_limits={"chat": 1, "hook": 1},
                delay_sec=0.01,
                sleep=asyncio.sleep,
            ),
        )
        context.runner.enqueue(
            RunRequest(
                group="chat",
                origin="chat",
                thread_id="thread-1",
                thunk="say hello",
                delay_sec=0.02,
            )
        )
        context.runner.enqueue(
            RunRequest(
                group="chat",
                origin="chat",
                thread_id="thread-1",
                thunk="draft follow-up",
                delay_sec=0.0,
            )
        )
        context.runner.enqueue(
            RunRequest(
                group="hook",
                origin="hook",
                thunk="refresh status",
                delay_sec=0.0,
            )
        )
        context.runner.close()

        with (
            caplog.at_level(logging.INFO, logger="toolang.runner"),
            _patched_runner_execution(),
        ):
            results = await context.runner.drain(context)

        printed = [
            record.message
            for record in caplog.records
            if record.name == "toolang.runner"
        ]
        assert [result.input_text for result in results] == [
            "say hello",
            "draft follow-up",
            "refresh status",
        ]
        assert _index_where(
            printed,
            lambda item: (
                item.startswith("finished run ")
                and "group=chat " in item
                and "thread_id=thread-1 " in item
                and "status=finished" in item
            ),
        ) < printed.index(
            "starting run group=chat origin=chat thread_id=thread-1 input='draft follow-up'"
        )
        assert len(context.runner) == 0
        assert context.runner.snapshot()["concurrency_groups"] == [
            {"available": 1, "group": "chat", "in_flight": 0, "limit": 1},
            {"available": 1, "group": "hook", "in_flight": 0, "limit": 1},
        ]

    asyncio.run(run_test())


def test_create_app_mounts_only_enabled_routes(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(toolang_root / "agents" / "alice" / "alice.too", "agent alice\n")
    context = _build_context(
        toolang_root=toolang_root,
        agent_name="alice",
        enabled_loops=("chat", "inspect"),
    )
    app = _create_test_app(context)

    with _patched_runner_execution():
        with TestClient(app) as client:
            response = client.post(
                "/api/v1/chat",
                json={"thread": "thread-1", "message": "say hello"},
            )
            assert response.status_code == 200
            body = response.json()
            assert body["thread_id"] == "thread-1"
            assert body["message"]["parts"][0]["text"] == "say hello"
            assert body["assistant"]["parts"][0]["text"] == "assistant:say hello"
            assert client.put("/api/v1/skills/reviewer", json={"scope": "agent", "ref": "acme/reviewer"}).status_code == 405

            runs = client.get("/api/v1/runs").json()["items"]
            profile = client.get("/api/v1/profile").json()
            caps_response = client.get("/api/v1/caps").json()
            threads = client.get("/api/v1/threads").json()["items"]
            snapshot = inspect.snapshot_context(context, enabled_loops=("chat", "inspect"))
            durable = cast(dict[str, object], snapshot["durable"])
            prepared = cast(dict[str, object], snapshot["prepared"])
            live = cast(dict[str, object], snapshot["live"])
            definitions = cast(dict[str, object], durable["definitions"])
            operational_facts = cast(dict[str, object], durable["operational_facts"])
            prepared_fingerprint = cast(str, prepared["fingerprint"])

            assert profile["environment"] == {
                "sandbox": "none",
                "home": str(context.home),
                "endpoint": "http://127.0.0.1:8765",
            }
            assert profile["metrics"] == {
                "threads": {"total": 1, "chat": 1, "chore": 0, "task": 0},
                "steps": {"total": 1, "model_call": 1, "tool_call": 0, "runtime": 0},
                "tokens": {"input": 0, "output": 0, "total": 0},
            }
            assert caps_response["agent"] == "alice"
            assert [item["input_text"] for item in runs] == ["say hello"]
            assert [item["id"] for item in threads] == ["thread-1"]
            assert threads[0] == {
                "id": "thread-1",
                "title": "say hello",
                "origin": "chat",
                "updated_at": runs[0]["updated_at"],
            }
            thread_detail = client.get("/api/v1/threads/thread-1").json()
            run_detail = client.get(f"/api/v1/runs/{body['run_id']}").json()
            assert thread_detail["info"] == threads[0]
            assert [item["info"]["id"] for item in thread_detail["runs"]] == [body["run_id"]]
            assert run_detail["info"] == thread_detail["runs"][0]["info"]
            assert run_detail["input"]["role"] == "user"
            assert run_detail["input"]["parts"][0]["text"] == "say hello"
            assert run_detail["output"]["status"] == "finished"
            assert [item["record"]["kind"] for item in run_detail["output"]["steps"]] == ["model_call"]
            assert run_detail["output"]["steps"][0]["message"]["role"] == "assistant"
            assert (
                run_detail["output"]["steps"][0]["message"]["parts"][0]["text"]
                == "assistant:say hello"
            )
            instructions_hash = run_detail["output"]["steps"][0]["record"]["payload"]["instructions_hash"]
            assert isinstance(instructions_hash, str) and instructions_hash
            instructions = client.get(f"/api/v1/instructions/{instructions_hash}").json()
            assert instructions == {
                "hash": instructions_hash,
                "body": "You are a helpful assistant.",
            }
            assert definitions["program_source"] == "agents/alice/alice.too"
            assert definitions["agent_entries"] == []
            assert prepared_fingerprint == live["fingerprint"]
            assert operational_facts["completed_runs"] == 1
            assert operational_facts["prepared_fingerprint"] == prepared_fingerprint
            assert live["completed_runs"] == 1
            assert live["queue_pending"] == 0


def test_profile_reports_activity_metrics(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(toolang_root / "agents" / "alice" / "alice.too", "agent alice\n")
    context = _build_context(
        toolang_root=toolang_root,
        agent_name="alice",
        enabled_loops=("inspect",),
    )
    app = _create_test_app(context)
    store = context.store
    agents.write_runtime_state(
        toolang_root,
        "alice",
        endpoint="http://127.0.0.1:9999",
        started_at="2026-01-01T00:00:00Z",
        pid=123,
        sandbox=SandboxState(
            selector=SandboxSelector.parse("docker:python:3.13-slim"),
            runtime_id="sandbox-alice",
        ).to_data(),
    )

    chat_run = store.start_run(
        run_id="run-chat",
        thread_id="thread-chat",
        origin="chat",
        input=Message.user("list tools"),
    )
    store.append_step(
        run_id=chat_run.run_id,
        step_index=1,
        kind="model_call",
        status="finished",
        input=(RunInputRef(),),
        output=(
            ToolCallPart(
                tool_call_id="call-1",
                tool_name="shell",
                tool_family="shell",
                input={"command": "pwd"},
            ),
        ),
        payload=ModelCallStepPayload(
            model_ref="gpt-5",
            input_tokens=11,
            output_tokens=7,
        ),
        started_at="2026-01-01T00:00:01Z",
        finished_at="2026-01-01T00:00:02Z",
    )
    store.append_step(
        run_id=chat_run.run_id,
        step_index=2,
        kind="tool_call",
        status="finished",
        input=(StepOutputRef(step_index=1, part_index=0),),
        output=(
            ToolResultPart(
                tool_call_id="call-1",
                tool_name="shell",
                tool_family="shell",
                output={"cwd": "/tmp"},
            ),
        ),
        payload=ToolCallStepPayload(),
        started_at="2026-01-01T00:00:03Z",
        finished_at="2026-01-01T00:00:04Z",
    )
    store.append_step(
        run_id=chat_run.run_id,
        step_index=3,
        kind="model_call",
        status="finished",
        input=(RunInputRef(), StepOutputRef(step_index=2)),
        output=(TextPart(text="done"),),
        payload=ModelCallStepPayload(
            model_ref="gpt-5",
            input_tokens=3,
            output_tokens=5,
        ),
        started_at="2026-01-01T00:00:05Z",
        finished_at="2026-01-01T00:00:06Z",
    )
    store.finish_run(run_id=chat_run.run_id)

    task_run = store.start_run(
        run_id="run-task",
        thread_id="task:local:task-1",
        origin="task",
        input=Message.user("do the task"),
    )
    store.finish_run(run_id=task_run.run_id)

    chore_run = store.start_run(
        run_id="run-chore",
        thread_id="chore:daily-sync",
        origin="chore",
        input=Message.user("run the chore"),
    )
    store.finish_run(run_id=chore_run.run_id)

    with TestClient(app) as client:
        profile = client.get("/api/v1/profile")
        assert profile.status_code == 200
        assert profile.json()["environment"] == {
            "sandbox": "docker:python:3.13-slim",
            "home": str(context.home),
            "endpoint": "http://127.0.0.1:9999",
        }
        assert profile.json()["metrics"] == {
            "threads": {"total": 3, "chat": 1, "chore": 1, "task": 1},
            "steps": {"total": 3, "model_call": 2, "tool_call": 1, "runtime": 0},
            "tokens": {"input": 14, "output": 12, "total": 26},
        }


def test_create_app_allows_webui_cors_origin(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(toolang_root / "agents" / "alice" / "alice.too", "agent alice\n")
    context = _build_context(
        toolang_root=toolang_root,
        agent_name="alice",
        enabled_loops=("inspect",),
    )
    app = _create_test_app(context)

    with TestClient(app) as client:
        response = client.get(
            "/api/v1/profile",
            headers={"Origin": "https://too.run"},
        )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "https://too.run"


def test_chat_returns_failed_run_as_assistant_message(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(toolang_root / "agents" / "alice" / "alice.too", "agent alice\n")
    context = _build_context(
        toolang_root=toolang_root,
        agent_name="alice",
        enabled_loops=("chat", "inspect"),
    )
    app = _create_test_app(context)

    with _patched_runner_failure("model boom"):
        with TestClient(app) as client:
            response = client.post(
                "/api/v1/chat",
                json={"thread": "thread-1", "message": "say hello"},
            )
            runs = client.get("/api/v1/runs").json()["items"]

    assert response.status_code == 200
    body = response.json()
    assert body["message"]["parts"][0]["text"] == "say hello"
    assert body["assistant"]["parts"][0]["text"] == "model boom"
    assert runs[0]["status"] == "failed"


def test_chat_projects_tool_parts_from_tool_call_steps(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(toolang_root / "agents" / "alice" / "alice.too", "agent alice\n")
    context = _build_context(
        toolang_root=toolang_root,
        agent_name="alice",
        enabled_loops=("chat", "inspect"),
    )
    app = _create_test_app(context)

    with _patched_runner_execution_with_tools(output_text="assistant:tool me"):
        with TestClient(app) as client:
            response = client.post(
                "/api/v1/chat",
                json={"thread": "thread-1", "message": "tool me"},
            )

    assert response.status_code == 200
    body = response.json()
    assistant_parts = body["assistant"]["parts"]
    assert assistant_parts == [{"type": "text", "text": "assistant:tool me"}]


def test_chat_stream_emits_tool_and_text_chunks(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(toolang_root / "agents" / "alice" / "alice.too", "agent alice\n")
    context = _build_context(
        toolang_root=toolang_root,
        agent_name="alice",
        enabled_loops=("chat", "inspect"),
    )
    app = _create_test_app(context)

    with _patched_runner_execution_with_tools(output_text="assistant:tool me"):
        with TestClient(app) as client:
            with client.stream(
                "POST",
                "/api/v1/chat/stream",
                json={"thread": "thread-1", "message": "tool me"},
            ) as response:
                assert response.status_code == 200
                stream_text = "".join(chunk.decode("utf-8") for chunk in response.iter_raw())

    assert '"type":"start"' in stream_text
    assert '"type":"message-metadata"' in stream_text
    assert '"type":"start-step"' in stream_text
    assert '"type":"tool-input-start"' in stream_text
    assert '"type":"tool-input-delta"' in stream_text
    assert '"type":"tool-input-available"' in stream_text
    assert '"type":"tool-output-available"' in stream_text
    assert '"toolCallId":"call_1"' in stream_text
    assert '"toolName":"math_add"' in stream_text
    assert '"inputTextDelta":"{\\"a\\":7,\\"b\\":8}"' in stream_text
    assert '"providerMetadata":{"toolang":{"toolFamily":"math_add","toolName":"math_add"}}' in stream_text
    assert '"type":"text-delta"' in stream_text
    assert '"delta":"assistant:tool me"' in stream_text
    assert '"type":"finish-step"' in stream_text
    assert '"type":"finish"' in stream_text
    assert "data: [DONE]" in stream_text


def test_chat_stream_emits_before_run_completion(tmp_path: Path) -> None:
    async def run_test() -> None:
        toolang_root = tmp_path / "toolang"
        _write_text(toolang_root / "agents" / "alice" / "alice.too", "agent alice\n")
        context = _build_context(
            toolang_root=toolang_root,
            agent_name="alice",
            enabled_loops=("chat", "inspect"),
            runner=QueueRunner(delay_sec=0.0),
        )

        release = threading.Event()
        timer = threading.Timer(1.0, release.set)
        timer.start()
        try:
            with _patched_runner_streaming_text(release):
                async with _running_context(context, enabled_loops=("chat", "inspect")):
                    stream = chat_loop._stream_chat_run(
                        context,
                        chat_loop.ChatRequest(thread="thread-1", message="stream me"),
                    )
                    started_at = time.monotonic()
                    stream_text = ""
                    async for chunk in stream:
                        stream_text += chunk
                        if '"type":"text-delta"' in stream_text:
                            break
                    elapsed = time.monotonic() - started_at
                    release.set()
                    async for chunk in stream:
                        stream_text += chunk
        finally:
            timer.cancel()

        assert elapsed < 0.8
        assert '"type":"text-delta"' in stream_text
        assert '"delta":"streaming hello"' in stream_text
        assert "data: [DONE]" in stream_text

    asyncio.run(run_test())


def test_chat_stream_allows_tool_only_turns(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(toolang_root / "agents" / "alice" / "alice.too", "agent alice\n")
    context = _build_context(
        toolang_root=toolang_root,
        agent_name="alice",
        enabled_loops=("chat", "inspect"),
    )
    app = _create_test_app(context)

    with _patched_runner_execution_with_tools(output_text=""):
        with TestClient(app) as client:
            with client.stream(
                "POST",
                "/api/v1/chat/stream",
                json={"thread": "thread-1", "message": "tool only"},
            ) as response:
                assert response.status_code == 200
                stream_text = "".join(chunk.decode("utf-8") for chunk in response.iter_raw())

    assert '"type":"tool-input-start"' in stream_text
    assert '"type":"tool-input-available"' in stream_text
    assert '"type":"tool-output-available"' in stream_text
    assert '"type":"text-delta"' not in stream_text
    assert '"type":"finish"' in stream_text
    assert "data: [DONE]" in stream_text


def test_create_app_is_pure_route_assembly(tmp_path: Path) -> None:
    context = _build_context(
        toolang_root=tmp_path / "toolang",
        agent_name="alice",
        enabled_loops=("chat", "inspect"),
    )

    app = create_app(context)

    assert app.router.lifespan_context is not None
    assert app.state.runtime is context
    context.store.close()


def test_hook_routes_enqueue_runs(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(toolang_root / "agents" / "alice" / "alice.too", "agent alice\n")
    context = _build_context(
        toolang_root=toolang_root,
        agent_name="alice",
        enabled_loops=("hook", "inspect"),
    )
    app = _create_test_app(context)

    with _patched_runner_execution():
        with TestClient(app) as client:
            response = client.post("/hook/runs", json={"thunk": "decode webhook"})
            assert response.status_code == 202
            snapshot = inspect.snapshot_context(context, enabled_loops=("hook", "inspect"))
            for _ in range(50):
                if snapshot["completed_runs"]:
                    break
                time.sleep(0.01)
                snapshot = inspect.snapshot_context(context, enabled_loops=("hook", "inspect"))
            completed_runs = cast(list[dict[str, object]], snapshot["completed_runs"])
            assert [item["group"] for item in completed_runs] == ["hook"]
            assert [item["input_text"] for item in completed_runs] == ["decode webhook"]


def test_poll_loop_queues_channel_deliveries_and_delivers_reply(tmp_path: Path) -> None:
    class FakeTelegramPlugin:
        def __init__(self) -> None:
            self._emitted = False
            self.deliveries: list[tuple[ReplyTarget, str]] = []

        def poll(self, state: ChannelState, context) -> PollResult:
            del context
            if self._emitted:
                return PollResult(next_state=state)
            self._emitted = True
            return PollResult(
                deliveries=[
                    InboundDelivery(
                        origin="chat",
                        channel="telegram",
                        sender="owner",
                        thread_id="telegram:123",
                        text="hello from poll",
                        reply_target=ReplyTarget(channel="telegram", address="chat:123"),
                    )
                ],
                next_state=ChannelState(cursor="43"),
            )

        def decode_hook(self, request, context) -> InboundDelivery | None:
            del request, context
            return None

        def deliver(self, target: ReplyTarget, message, context) -> DeliveryResult:
            del context
            self.deliveries.append((target, message.text))
            return DeliveryResult(ok=True, remote_id="99")

        def health(self, context) -> PluginHealth:
            del context
            return PluginHealth(ok=True)

    toolang_root = tmp_path / "toolang"
    _write_text(toolang_root / "agents" / "alice" / "alice.too", "agent alice\n")
    plugin = FakeTelegramPlugin()
    context = _build_context(
        toolang_root=toolang_root,
        agent_name="alice",
        enabled_loops=("poll", "inspect"),
        channel_bindings={
            "telegram": ChannelBinding(
                name="telegram",
                plugin="telegram",
                config={"token": "secret"},
            )
        },
        channel_plugins={"telegram": plugin},
    )

    with _patched_runner_execution():
        async def run_test() -> None:
            async with _running_context(
                context,
                enabled_loops=("poll", "inspect"),
                loop_intervals_ms={"poll": 10.0},
            ):
                await _wait_for_completed_count(context, 1)
                run = context.store.list_runs(limit=1)[0]
                assert run.thread_id == "telegram:123"
                assert run.origin == "chat"

        asyncio.run(run_test())

    assert plugin.deliveries == [
        (ReplyTarget(channel="telegram", address="chat:123"), ""),
        (ReplyTarget(channel="telegram", address="chat:123"), "assistant:hello from poll"),
    ]
    state_path = agents.channel_room(toolang_root, "alice", "telegram") / "state.json"
    assert state_path.is_file()
    assert ChannelState.from_data(json.loads(state_path.read_text(encoding="utf-8"))).cursor == "43"


def test_channel_reply_uses_streaming_delivery_for_telegram(tmp_path: Path) -> None:
    class FakeTelegramPlugin:
        def __init__(self) -> None:
            self.deliveries: list[tuple[ReplyTarget, str, dict[str, object]]] = []
            self._next_remote_id = 99

        def poll(self, state: ChannelState, context) -> PollResult:
            del state, context
            return PollResult()

        def decode_hook(self, request, context) -> InboundDelivery | None:
            del request, context
            return None

        def deliver(self, target: ReplyTarget, message, context) -> DeliveryResult:
            del context
            self.deliveries.append((target, message.text, dict(message.meta)))
            if message.meta.get("action") == "typing":
                return DeliveryResult(ok=True)
            remote_id = str(self._next_remote_id)
            self._next_remote_id += 1
            return DeliveryResult(ok=True, remote_id=remote_id)

        def health(self, context) -> PluginHealth:
            del context
            return PluginHealth(ok=True)

    toolang_root = tmp_path / "toolang"
    _write_text(toolang_root / "agents" / "alice" / "alice.too", "agent alice\n")
    plugin = FakeTelegramPlugin()
    context = _build_context(
        toolang_root=toolang_root,
        agent_name="alice",
        enabled_loops=("poll",),
        channel_bindings={
            "telegram": ChannelBinding(
                name="telegram",
                plugin="telegram",
                config={"token": "secret"},
            )
        },
        channel_plugins={"telegram": plugin},
        runner=QueueRunner(delay_sec=0.0),
    )
    context.enqueue_delivery(
        "poll",
        "telegram",
        InboundDelivery(
            origin="chat",
            channel="telegram",
            sender="owner",
            thread_id="telegram:123",
            text="hello from poll",
            reply_target=ReplyTarget(channel="telegram", address="chat:123"),
        ),
    )
    context.runner.close()

    def fake_assemble(_context: UptimeContext, bound):
        return _fake_run_input(bound)

    def fake_execute_stream(_bound, _model, *, on_event) -> RunResult:
        on_event(
            _started(
                1,
                run_id="run-1",
                thread_id="telegram:123",
                kind="model_call",
            )
        )
        on_event(
            PartStart(
                run_id="run-1",
                thread_id="telegram:123",
                step_index=1,
                part_index=0,
                kind="tool_call",
            )
        )
        on_event(
            PartStart(
                run_id="run-1",
                thread_id="telegram:123",
                step_index=1,
                part_index=1,
                kind="text",
            )
        )
        on_event(
            PartDelta(
                run_id="run-1",
                thread_id="telegram:123",
                step_index=1,
                part_index=1,
                delta=TextDelta(text="hel"),
            )
        )
        time.sleep(0.02)
        on_event(
            PartDelta(
                run_id="run-1",
                thread_id="telegram:123",
                step_index=1,
                part_index=1,
                delta=TextDelta(text="lo"),
            )
        )
        time.sleep(0.02)
        on_event(
            PartDelta(
                run_id="run-1",
                thread_id="telegram:123",
                step_index=1,
                part_index=1,
                delta=TextDelta(text=" world"),
            )
        )
        time.sleep(0.02)
        on_event(
            PartDelta(
                run_id="run-1",
                thread_id="telegram:123",
                step_index=1,
                part_index=1,
                delta=TextDelta(text=" and more"),
            )
        )
        time.sleep(0.02)
        on_event(
            PartDelta(
                run_id="run-1",
                thread_id="telegram:123",
                step_index=1,
                part_index=1,
                delta=TextDelta(text=" from telegram"),
            )
        )
        on_event(
            _completed(
                1,
                run_id="run-1",
                thread_id="telegram:123",
                kind="model_call",
                output=(
                    ToolCallPart(
                        tool_call_id="call_1",
                        tool_name="math_add",
                        tool_family="math_add",
                        input={"a": 7, "b": 8},
                    ),
                    TextPart(text="hello world and more from telegram"),
                ),
            )
        )
        return RunResult(output_text="hello world and more from telegram")

    with (
        patch.object(run_execute_module, "assemble_run_input", side_effect=fake_assemble),
        patch.object(
            run_execute_module,
            "load_run_strategy",
            return_value=_FakeStrategy(
                run=lambda context: fake_execute_stream(None, None, on_event=context.on_event),
            ),
        ),
    ):
        asyncio.run(context.runner.drain(context))

    assert plugin.deliveries[0] == (
        ReplyTarget(channel="telegram", address="chat:123"),
        "",
        {"action": "typing"},
    )
    non_typing = [item for item in plugin.deliveries if item[2].get("action") != "typing"]
    assert non_typing[0][0] == ReplyTarget(channel="telegram", address="chat:123")
    assert non_typing[0][1].startswith("hel")
    assert non_typing[0][2] == {}
    assert non_typing[-1][0] == ReplyTarget(channel="telegram", address="chat:123")
    assert non_typing[-1][1] == "hello world and more from telegram"
    assert "replace_remote_id" in non_typing[-1][2]


def test_channel_reply_sends_typing_before_plain_text_stream(tmp_path: Path) -> None:
    class FakeTelegramPlugin:
        def __init__(self) -> None:
            self.deliveries: list[tuple[ReplyTarget, str, dict[str, object]]] = []

        def poll(self, state: ChannelState, context) -> PollResult:
            del state, context
            return PollResult()

        def decode_hook(self, request, context) -> InboundDelivery | None:
            del request, context
            return None

        def deliver(self, target: ReplyTarget, message, context) -> DeliveryResult:
            del context
            self.deliveries.append((target, message.text, dict(message.meta)))
            if message.meta.get("action") == "typing":
                return DeliveryResult(ok=True)
            return DeliveryResult(ok=True, remote_id="101")

        def health(self, context) -> PluginHealth:
            del context
            return PluginHealth(ok=True)

    toolang_root = tmp_path / "toolang"
    _write_text(toolang_root / "agents" / "alice" / "alice.too", "agent alice\n")
    plugin = FakeTelegramPlugin()
    context = _build_context(
        toolang_root=toolang_root,
        agent_name="alice",
        enabled_loops=("poll",),
        channel_bindings={
            "telegram": ChannelBinding(
                name="telegram",
                plugin="telegram",
                config={"token": "secret"},
            )
        },
        channel_plugins={"telegram": plugin},
        runner=QueueRunner(delay_sec=0.0),
    )
    context.enqueue_delivery(
        "poll",
        "telegram",
        InboundDelivery(
            origin="chat",
            channel="telegram",
            sender="owner",
            thread_id="telegram:123",
            text="hello from poll",
            reply_target=ReplyTarget(channel="telegram", address="chat:123"),
        ),
    )
    context.runner.close()

    def fake_assemble(_context: UptimeContext, bound):
        return _fake_run_input(bound)

    def fake_execute_stream(_bound, _model, *, on_event) -> RunResult:
        on_event(
            _started(
                1,
                run_id="run-1",
                thread_id="telegram:123",
                kind="model_call",
            )
        )
        on_event(
            PartStart(
                run_id="run-1",
                thread_id="telegram:123",
                step_index=1,
                part_index=0,
                kind="text",
            )
        )
        on_event(
            PartDelta(
                run_id="run-1",
                thread_id="telegram:123",
                step_index=1,
                part_index=0,
                delta=TextDelta(text="hello"),
            )
        )
        time.sleep(0.02)
        on_event(
            PartDelta(
                run_id="run-1",
                thread_id="telegram:123",
                step_index=1,
                part_index=0,
                delta=TextDelta(text=" world"),
            )
        )
        on_event(
            _completed(
                1,
                run_id="run-1",
                thread_id="telegram:123",
                kind="model_call",
                output=(TextPart(text="hello world"),),
            )
        )
        return RunResult(output_text="hello world")

    with (
        patch.object(run_execute_module, "assemble_run_input", side_effect=fake_assemble),
        patch.object(
            run_execute_module,
            "load_run_strategy",
            return_value=_FakeStrategy(
                run=lambda context: fake_execute_stream(None, None, on_event=context.on_event),
            ),
        ),
    ):
        asyncio.run(context.runner.drain(context))

    assert plugin.deliveries[0] == (
        ReplyTarget(channel="telegram", address="chat:123"),
        "",
        {"action": "typing"},
    )
    assert plugin.deliveries[1] == (
        ReplyTarget(channel="telegram", address="chat:123"),
        "hello",
        {},
    )
    assert plugin.deliveries[-1] == (
        ReplyTarget(channel="telegram", address="chat:123"),
        "hello world",
        {"replace_remote_id": "101"},
    )


def test_control_routes_update_durable_only_without_prepare_reload(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(toolang_root / "agents" / "alice" / "alice.too", "agent alice\n")
    context = _build_context(
        toolang_root=toolang_root,
        agent_name="alice",
        enabled_loops=("control", "inspect"),
    )
    initial_live_fingerprint = context.live.fingerprint
    initial_prepared_fingerprint = load_prepared_state(toolang_root, "alice").fingerprint
    app = _create_test_app(context)

    with TestClient(app) as client:
        add_response = client.put(
            "/api/v1/skills/reviewer",
            json={"scope": "agent", "ref": "acme/reviewer"},
        )
        assert add_response.status_code == 200
        assert add_response.json()["item"]["name"] == "reviewer"
        assert add_response.json()["item"]["source"] == "remote"

        snapshot = inspect.snapshot_context(context, enabled_loops=("control", "inspect"))
        durable = cast(dict[str, object], snapshot["durable"])
        prepared = cast(dict[str, object], snapshot["prepared"])
        live = cast(dict[str, object], snapshot["live"])
        definitions = cast(dict[str, object], durable["definitions"])
        agent_entries = cast(list[dict[str, object]], definitions["agent_entries"])
        assert [item["name"] for item in agent_entries] == ["reviewer"]
        assert prepared["fingerprint"] == initial_prepared_fingerprint
        assert live["fingerprint"] == initial_live_fingerprint
        assert live["caps"] == []
        assert client.get("/api/v1/skills").json()["items"] == []

        remove_response = client.delete("/api/v1/skills/reviewer?scope=agent")
        assert remove_response.status_code == 200
        assert remove_response.json() == {"ok": True}

        snapshot = inspect.snapshot_context(context, enabled_loops=("control", "inspect"))
        durable = cast(dict[str, object], snapshot["durable"])
        definitions = cast(dict[str, object], durable["definitions"])
        assert definitions["agent_entries"] == []


def test_background_loops_enqueue_runs(tmp_path: Path) -> None:
    async def run_test() -> None:
        toolang_root = tmp_path / "toolang"
        _write_text(toolang_root / "agents" / "alice" / "alice.too", "agent alice\n")
        _write_text(
            toolang_root / "agents" / "alice" / "tasks" / "review.md",
            "---\nrequester: owner\nstatus: todo\n---\nReview the current plan.\n",
        )
        context = _build_context(
            toolang_root=toolang_root,
            agent_name="alice",
            enabled_loops=("pulse",),
        )

        with _patched_runner_execution():
            async with _running_context(
                context, enabled_loops=("pulse",), loop_intervals_ms={"pulse": 10.0}
            ):
                for _ in range(50):
                    if inspect.snapshot_context(context, enabled_loops=("pulse",))[
                        "completed_runs"
                    ]:
                        break
                    await asyncio.sleep(0.01)
                completed = cast(
                    list[dict[str, object]],
                    inspect.snapshot_context(context, enabled_loops=("pulse",))[
                        "completed_runs"
                    ],
                )
                assert completed
                assert completed[0]["group"] == "pulse"
                assert completed[0]["origin"] == "task"
                assert completed[0]["input_text"] == "Review the current plan."
                assert str(completed[0]["thread_id"]).startswith("task:local:")

    asyncio.run(run_test())


def test_up_picks_free_port_when_unspecified(tmp_path: Path, monkeypatch) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(toolang_root / "agents" / "alice" / "alice.too", "agent alice\n")
    captured: dict[str, object] = {}

    monkeypatch.setattr("toolang.up._pick_free_port", lambda host: 43210)

    def fake_uvicorn_run(app, *, host: str, port: int, log_config) -> None:
        captured["app"] = app
        captured["host"] = host
        captured["port"] = port
        captured["log_config"] = log_config

    monkeypatch.setattr("toolang.up.uvicorn.run", fake_uvicorn_run)

    result = run_experiments_up(
        toolang_root=toolang_root,
        agent_name="alice",
        host="0.0.0.0",
        loop_names=("inspect",),
        environ={},
    )

    assert result == 0
    assert captured["host"] == "0.0.0.0"
    assert captured["port"] == 43210


def test_up_reuses_previous_agent_port_when_unspecified(tmp_path: Path, monkeypatch) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(toolang_root / "agents" / "alice" / "alice.too", "agent alice\n")
    agents.write_runtime_state(
        toolang_root,
        "alice",
        endpoint="http://127.0.0.1:53322",
        started_at="2026-04-07T11:00:00Z",
        pid=12345,
    )
    captured: dict[str, object] = {}

    monkeypatch.setattr("toolang.up._pick_free_port", lambda host: 43210)

    def fake_uvicorn_run(app, *, host: str, port: int, log_config) -> None:
        captured["app"] = app
        captured["host"] = host
        captured["port"] = port
        captured["log_config"] = log_config

    monkeypatch.setattr("toolang.up.uvicorn.run", fake_uvicorn_run)

    result = run_experiments_up(
        toolang_root=toolang_root,
        agent_name="alice",
        host="127.0.0.1",
        loop_names=("inspect",),
        environ={},
    )

    assert result == 0
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 53322


def test_up_falls_back_when_previous_agent_port_is_unavailable(
    tmp_path: Path, monkeypatch
) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(toolang_root / "agents" / "alice" / "alice.too", "agent alice\n")
    captured: dict[str, object] = {}

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as blocker:
        blocker.bind(("127.0.0.1", 0))
        blocked_port = int(blocker.getsockname()[1])
        agents.write_runtime_state(
            toolang_root,
            "alice",
            endpoint=f"http://127.0.0.1:{blocked_port}",
            started_at="2026-04-07T11:00:00Z",
            pid=12345,
        )

        monkeypatch.setattr("toolang.up._pick_free_port", lambda host: 43210)

        def fake_uvicorn_run(app, *, host: str, port: int, log_config) -> None:
            captured["app"] = app
            captured["host"] = host
            captured["port"] = port
            captured["log_config"] = log_config

        monkeypatch.setattr("toolang.up.uvicorn.run", fake_uvicorn_run)

        result = run_experiments_up(
            toolang_root=toolang_root,
            agent_name="alice",
            host="127.0.0.1",
            loop_names=("inspect",),
            environ={},
        )

    assert result == 0
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 43210


def test_up_waits_for_stopped_agent_port_to_become_available(tmp_path: Path, monkeypatch) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(toolang_root / "agents" / "alice" / "alice.too", "agent alice\n")
    agents.write_runtime_state(
        toolang_root,
        "alice",
        endpoint="http://127.0.0.1:53322",
        started_at="2026-04-07T11:00:00Z",
        pid=12345,
    )
    agents.stop_runtime_state(toolang_root, "alice")
    captured: dict[str, object] = {}
    attempts = {"count": 0}

    def fake_port_is_available(host: str, port: int) -> bool:
        assert host == "127.0.0.1"
        assert port == 53322
        attempts["count"] += 1
        return attempts["count"] >= 3

    monkeypatch.setattr("toolang.up._port_is_available", fake_port_is_available)
    monkeypatch.setattr("toolang.up.time.sleep", lambda _: None)
    monkeypatch.setattr("toolang.up._pick_free_port", lambda host: 43210)

    def fake_uvicorn_run(app, *, host: str, port: int, log_config) -> None:
        captured["host"] = host
        captured["port"] = port

    monkeypatch.setattr("toolang.up.uvicorn.run", fake_uvicorn_run)

    result = run_experiments_up(
        toolang_root=toolang_root,
        agent_name="alice",
        host="127.0.0.1",
        loop_names=("inspect",),
        environ={},
    )

    assert result == 0
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 53322
    assert attempts["count"] >= 3


def test_up_waits_longer_for_stopped_agent_port_to_become_available(
    tmp_path: Path, monkeypatch
) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(toolang_root / "agents" / "alice" / "alice.too", "agent alice\n")
    agents.write_runtime_state(
        toolang_root,
        "alice",
        endpoint="http://127.0.0.1:53322",
        started_at="2026-04-07T11:00:00Z",
        pid=12345,
    )
    agents.stop_runtime_state(toolang_root, "alice")
    observed: dict[str, object] = {}

    monkeypatch.setattr("toolang.up._port_is_available", lambda host, port: False)

    def fake_wait_for_port_available(host: str, port: int, *, timeout_sec: float) -> bool:
        observed["host"] = host
        observed["port"] = port
        observed["timeout_sec"] = timeout_sec
        return False

    monkeypatch.setattr("toolang.up._wait_for_port_available", fake_wait_for_port_available)
    monkeypatch.setattr("toolang.up._pick_free_port", lambda host: 43210)

    resolved = up_module.resolve_runtime_port(
        host="127.0.0.1",
        explicit_port=None,
        toolang_root=toolang_root,
        agent_name="alice",
    )

    assert resolved == 43210
    assert observed == {
        "host": "127.0.0.1",
        "port": 53322,
        "timeout_sec": 5.0,
    }


def test_up_uses_cors_origins_from_root_config(tmp_path: Path, monkeypatch) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(toolang_root / "agents" / "alice" / "alice.too", "agent alice\n")
    _write_text(
        toolang_root / "config.toml",
        '[web]\n'
        'cors_allowed_origins = ["http://localhost:3000", "https://too.run"]\n',
    )
    captured: dict[str, object] = {}

    def fake_uvicorn_run(app, *, host: str, port: int, log_config) -> None:
        captured["app"] = app
        captured["host"] = host
        captured["port"] = port
        captured["log_config"] = log_config

    monkeypatch.setattr("toolang.up.uvicorn.run", fake_uvicorn_run)

    result = run_experiments_up(
        toolang_root=toolang_root,
        agent_name="alice",
        host="127.0.0.1",
        port=8765,
        loop_names=("inspect",),
        environ={},
    )

    assert result == 0
    app = cast(FastAPI, captured["app"])
    with TestClient(app) as client:
        response = client.get(
            "/api/v1/profile",
            headers={"Origin": "http://localhost:3000"},
        )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:3000"


def test_up_starts_managed_sandbox_without_local_uvicorn(tmp_path: Path, monkeypatch) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(toolang_root / "agents" / "alice" / "alice.too", "agent alice\n")
    captured: dict[str, object] = {}

    class FakeSandbox:
        name = "docker"

        def resolve_selector(self, raw_selector, *, configured_selector=None):
            del configured_selector
            return SandboxSelector.parse(raw_selector or "docker")

        def prepare(self, request):
            captured["request"] = request
            return SandboxPlan(
                selector=request.selector,
                start_mode="managed",
                sandbox_root=request.sandbox_root,
                sandbox_home=request.sandbox_home,
                sandbox_working_directory=request.sandbox_home,
                run_command=("python", "-m", "toolang.cli.main"),
                state=SandboxState(
                    selector=request.selector,
                    runtime_id="sandbox-alice",
                ),
            )

        def start(self, plan: SandboxPlan) -> SandboxStartResult:
            captured["plan"] = plan
            return SandboxStartResult(
                state=cast(SandboxState, plan.state),
                endpoint="http://127.0.0.1:8765",
            )

        def alive(self, state: SandboxState) -> bool:
            del state
            return True

        def stop(self, state: SandboxState, *, force: bool = False) -> None:
            del state, force

    def fail_uvicorn_run(*args, **kwargs) -> None:
        raise AssertionError("uvicorn.run should not be called for managed sandboxes")

    monkeypatch.setattr("toolang.up.create_sandbox_plugin", lambda name, config=None: FakeSandbox())
    monkeypatch.setattr("toolang.up._wait_for_sandbox_ready", lambda **kwargs: None)
    monkeypatch.setattr("toolang.up.uvicorn.run", fail_uvicorn_run)

    result = run_experiments_up(
        toolang_root=toolang_root,
        agent_name="alice",
        host="127.0.0.1",
        port=8765,
        sandbox="docker:python:3.13-slim",
        loop_names=("inspect",),
        environ={"OPENAI_API_KEY": "secret"},
    )

    assert result == 0
    request = cast("SandboxStartRequest", captured["request"])
    assert request.selector == SandboxSelector(driver="docker", target="python:3.13-slim")
    assert request.sandbox_root == Path("/root/.toolang")
    assert request.sandbox_home == Path("/root/.toolang/agents/alice")
    assert request.env_vars["OPENAI_API_KEY"] == "secret"
    assert request.run_command[:6] == (
        "--root",
        "/root/.toolang",
        "run",
        "alice",
        "--host",
        "0.0.0.0",
    )
    runtime_state = json.loads(
        agents.agent_runtime_state_path(toolang_root, "alice").read_text(encoding="utf-8")
    )
    assert runtime_state["status"] == "running"
    assert runtime_state["sandbox"]["selector"]["driver"] == "docker"
    assert runtime_state["sandbox"]["runtime_id"] == "sandbox-alice"


def test_up_defaults_docker_target_when_selector_omits_one(tmp_path: Path, monkeypatch) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(toolang_root / "agents" / "alice" / "alice.too", "agent alice\n")
    captured: dict[str, object] = {}

    class FakeSandbox:
        name = "docker"

        def resolve_selector(self, raw_selector, *, configured_selector=None):
            del configured_selector
            if raw_selector is None:
                return SandboxSelector(driver="docker", target="python:3.13-slim")
            parsed = SandboxSelector.parse(raw_selector)
            return SandboxSelector(
                driver=parsed.driver,
                target=parsed.target or "python:3.13-slim",
            )

        def prepare(self, request):
            captured["request"] = request
            return SandboxPlan(
                selector=request.selector,
                start_mode="managed",
                sandbox_root=request.sandbox_root,
                sandbox_home=request.sandbox_home,
                sandbox_working_directory=request.sandbox_home,
                state=SandboxState(
                    selector=request.selector,
                    runtime_id="sandbox-alice",
                ),
            )

        def start(self, plan: SandboxPlan) -> SandboxStartResult:
            return SandboxStartResult(
                state=cast(SandboxState, plan.state),
                endpoint="http://127.0.0.1:8765",
            )

        def alive(self, state: SandboxState) -> bool:
            del state
            return True

        def stop(self, state: SandboxState, *, force: bool = False) -> None:
            del state, force

    monkeypatch.setattr("toolang.up.create_sandbox_plugin", lambda name, config=None: FakeSandbox())
    monkeypatch.setattr("toolang.up._wait_for_sandbox_ready", lambda **kwargs: None)

    result = run_experiments_up(
        toolang_root=toolang_root,
        agent_name="alice",
        host="127.0.0.1",
        port=8765,
        sandbox="docker",
        loop_names=("inspect",),
        environ={},
    )

    assert result == 0
    request = cast("SandboxStartRequest", captured["request"])
    assert request.selector == SandboxSelector(driver="docker", target="python:3.13-slim")


def test_up_marks_managed_sandbox_failed_when_ready_check_fails(tmp_path: Path, monkeypatch) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(toolang_root / "agents" / "alice" / "alice.too", "agent alice\n")

    class FakeSandbox:
        name = "docker"

        def resolve_selector(self, raw_selector, *, configured_selector=None):
            del configured_selector
            return SandboxSelector.parse(raw_selector or "docker")

        def prepare(self, request):
            return SandboxPlan(
                selector=request.selector,
                start_mode="managed",
                sandbox_root=request.sandbox_root,
                sandbox_home=request.sandbox_home,
                sandbox_working_directory=request.sandbox_home,
                run_command=("too", "run"),
                state=SandboxState(
                    selector=request.selector,
                    runtime_id="sandbox-alice",
                ),
            )

        def start(self, plan: SandboxPlan) -> SandboxStartResult:
            return SandboxStartResult(
                state=cast(SandboxState, plan.state),
                endpoint="http://127.0.0.1:8765",
            )

        def alive(self, state: SandboxState) -> bool:
            del state
            return False

        def stop(self, state: SandboxState, *, force: bool = False) -> None:
            del state, force

    monkeypatch.setattr("toolang.up.create_sandbox_plugin", lambda name, config=None: FakeSandbox())
    monkeypatch.setattr(
        "toolang.up._wait_for_sandbox_ready",
        lambda **kwargs: (_ for _ in ()).throw(ValueError("sandbox failed")),
    )

    try:
        run_experiments_up(
            toolang_root=toolang_root,
            agent_name="alice",
            host="127.0.0.1",
            port=8765,
            sandbox="docker:python:3.13-slim",
            loop_names=("inspect",),
            environ={},
        )
    except ValueError as exc:
        assert str(exc) == "sandbox failed"
    else:
        raise AssertionError("expected managed sandbox startup failure")

    runtime_state = json.loads(
        agents.agent_runtime_state_path(toolang_root, "alice").read_text(encoding="utf-8")
    )
    assert runtime_state["status"] == "failed"
    assert runtime_state["message"] == "sandbox failed"


def test_up_marks_managed_sandbox_failed_when_prepare_fails(tmp_path: Path, monkeypatch) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(toolang_root / "agents" / "alice" / "alice.too", "agent alice\n")

    class FakeSandbox:
        name = "docker"

        def resolve_selector(self, raw_selector, *, configured_selector=None):
            del configured_selector
            return SandboxSelector(driver="docker", target="python:3.13-slim")

        def prepare(self, request):
            del request
            raise ValueError("prepare failed")

        def start(self, plan: SandboxPlan) -> SandboxStartResult:
            del plan
            raise AssertionError("start should not be called")

        def alive(self, state: SandboxState) -> bool:
            del state
            return False

        def stop(self, state: SandboxState, *, force: bool = False) -> None:
            del state, force

    monkeypatch.setattr("toolang.up.create_sandbox_plugin", lambda name, config=None: FakeSandbox())

    try:
        run_experiments_up(
            toolang_root=toolang_root,
            agent_name="alice",
            host="127.0.0.1",
            port=8765,
            sandbox="docker",
            loop_names=("inspect",),
            environ={},
        )
    except ValueError as exc:
        assert str(exc) == "prepare failed"
    else:
        raise AssertionError("expected managed sandbox prepare failure")

    runtime_state = json.loads(
        agents.agent_runtime_state_path(toolang_root, "alice").read_text(encoding="utf-8")
    )
    assert runtime_state["status"] == "failed"
    assert runtime_state["message"] == "prepare failed"


def test_list_agent_statuses_surfaces_preparing_and_failed_states(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    agents.create_agent(toolang_root, "alice")
    agents.create_agent(toolang_root, "bob")
    agents.write_runtime_state(
        toolang_root,
        "alice",
        endpoint="http://127.0.0.1:8765",
        started_at="2026-04-08T10:00:00Z",
        pid=os.getpid(),
        sandbox={
            "selector": {
                "driver": "docker",
                "target": "python:3.13-slim",
                "value": "docker:python:3.13-slim",
            },
            "runtime_id": None,
            "meta": {},
        },
        status="preparing",
    )
    agents.write_runtime_state(
        toolang_root,
        "bob",
        endpoint="http://127.0.0.1:9000",
        started_at="2026-04-08T10:00:00Z",
        pid=None,
        sandbox={
            "selector": {
                "driver": "docker",
                "target": "python:3.13-slim",
                "value": "docker:python:3.13-slim",
            },
            "runtime_id": None,
            "meta": {},
        },
        status="failed",
        message="sandbox failed",
    )

    statuses = {item.name: item for item in agents.list_agent_statuses(toolang_root, ui_base_url="http://localhost:3000")}

    assert statuses["alice"].status == "preparing"
    assert statuses["alice"].endpoint == "http://127.0.0.1:8765"
    assert statuses["alice"].api_url == "http://127.0.0.1:8765/docs"
    assert statuses["alice"].webui_url is None
    assert statuses["bob"].status == "failed"
    assert statuses["bob"].endpoint is None
    assert statuses["bob"].api_url is None
    assert statuses["bob"].webui_url is None


def test_resolve_dev_artifact_picks_newest_wheel_recursively(tmp_path: Path) -> None:
    from toolang import up as up_module

    dist = tmp_path / "dist"
    dist.mkdir(parents=True, exist_ok=True)
    older = dist / "toolang-0.1.0-py3-none-any.whl"
    nested = dist / "nested"
    nested.mkdir(parents=True, exist_ok=True)
    newer = nested / "toolang-0.2.0-py3-none-any.whl"
    older.write_bytes(b"old")
    newer.write_bytes(b"new")
    older.touch()
    time.sleep(0.01)
    newer.touch()

    assert up_module._resolve_dev_artifact(dist) == newer


def test_stop_agent_terminates_local_pid_and_marks_state_stopped(tmp_path: Path, monkeypatch) -> None:
    toolang_root = tmp_path / "toolang"
    agents.create_agent(toolang_root, "alice")
    pid = 43210
    alive = {"running": True}

    def fake_kill(target_pid: int, signal_value: int) -> None:
        assert target_pid == pid
        if signal_value == 0:
            if alive["running"]:
                return
            raise OSError("dead")
        alive["running"] = False

    monkeypatch.setattr("toolang.agents.os.kill", fake_kill)
    agents.write_runtime_state(
        toolang_root,
        "alice",
        endpoint="http://127.0.0.1:8765",
        started_at="2026-04-08T10:00:00Z",
        pid=pid,
    )

    stopped = agents.stop_agent(toolang_root, "alice")

    assert stopped is True
    runtime_state = cast(dict[str, object], agents.load_runtime_state(toolang_root, "alice"))
    assert runtime_state["status"] == "stopped"
    assert runtime_state["pid"] is None


def test_stop_agent_waits_for_endpoint_release_before_marking_stopped(
    tmp_path: Path, monkeypatch
) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(toolang_root / "agents" / "alice" / "alice.too", "agent alice\n")
    agents.write_runtime_state(
        toolang_root,
        "alice",
        endpoint="http://127.0.0.1:53322",
        started_at="2026-04-08T10:00:00Z",
        pid=43210,
    )
    observed: dict[str, object] = {}

    monkeypatch.setattr("toolang.agents._pid_alive", lambda pid: pid == 43210)
    monkeypatch.setattr(
        "toolang.agents._stop_pid",
        lambda pid, *, force: observed.setdefault("stopped_pid", pid),
    )

    def fake_wait_for_endpoint_release(endpoint: object, *, timeout_sec: float) -> bool:
        observed["endpoint"] = endpoint
        observed["timeout_sec"] = timeout_sec
        return True

    monkeypatch.setattr(
        "toolang.agents._wait_for_endpoint_release",
        fake_wait_for_endpoint_release,
    )

    stopped = agents.stop_agent(toolang_root, "alice")

    assert stopped is True
    assert observed["stopped_pid"] == 43210
    assert observed["endpoint"] == "http://127.0.0.1:53322"
    assert observed["timeout_sec"] == 5.0
    runtime_state = agents.load_runtime_state(toolang_root, "alice")
    assert runtime_state is not None
    assert runtime_state["status"] == "stopped"


def test_stop_agent_stops_managed_sandbox_and_marks_state_stopped(tmp_path: Path) -> None:
    class FakeSandbox:
        def __init__(self) -> None:
            self.runtime_id: str | None = None
            self.force = False

        def stop(self, state: SandboxState, *, force: bool = False) -> None:
            self.runtime_id = state.runtime_id
            self.force = force

    toolang_root = tmp_path / "toolang"
    agents.create_agent(toolang_root, "alice")
    agents.write_runtime_state(
        toolang_root,
        "alice",
        endpoint="http://127.0.0.1:8765",
        started_at="2026-04-08T10:00:00Z",
        pid=None,
        sandbox=SandboxState(
            selector=SandboxSelector(driver="docker", target="python:3.13-slim"),
            runtime_id="sandbox-alice",
        ).to_data(),
    )
    plugin = FakeSandbox()

    stopped = agents.stop_agent(
        toolang_root,
        "alice",
        sandbox_plugin=cast("SandboxPlugin", plugin),
        force=True,
    )

    assert stopped is True
    assert plugin.runtime_id == "sandbox-alice"
    assert plugin.force is True
    runtime_state = cast(dict[str, object], agents.load_runtime_state(toolang_root, "alice"))
    assert runtime_state["status"] == "stopped"
    assert runtime_state["pid"] is None


def test_up_reads_web_config_without_validating_experiments_caps(tmp_path: Path, monkeypatch) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(toolang_root / "agents" / "alice" / "alice.too", "agent alice\n")
    (toolang_root / "config.toml").write_text(
        '[web]\n'
        'cors_allowed_origins = ["http://localhost:3000", "https://too.run"]\n'
        '\n'
        '[skills]\n'
        'pdf-processing = { ref = "github://by3gus/agent-skills/skills/pdf-processing" }\n',
        encoding="utf-8",
    )
    captured: dict[str, object] = {}

    def fake_uvicorn_run(app, *, host: str, port: int, log_config) -> None:
        captured["app"] = app

    monkeypatch.setattr("toolang.up.uvicorn.run", fake_uvicorn_run)

    result = run_experiments_up(
        toolang_root=toolang_root,
        agent_name="alice",
        host="127.0.0.1",
        port=8765,
        loop_names=("inspect",),
        environ={},
    )

    assert result == 0
    app = cast(FastAPI, captured["app"])
    with TestClient(app) as client:
        response = client.get(
            "/api/v1/profile",
            headers={"Origin": "https://too.run"},
        )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "https://too.run"


def test_prepare_reload_refreshes_prepared_and_live(tmp_path: Path) -> None:
    async def run_test() -> None:
        toolang_root = tmp_path / "toolang"
        prompt_path = toolang_root / "agents" / "alice" / "prompts" / "rewrite.md"
        _write_text(prompt_path, "---\ndescription: v1\n---\nPrompt v1\n")
        context = _build_context(
            toolang_root=toolang_root,
            agent_name="alice",
            enabled_loops=("prepare", "reload"),
        )

        initial_fingerprint = context.live.fingerprint
        async with _running_context(
            context,
            enabled_loops=("prepare", "reload"),
            loop_intervals_ms={"prepare": 10.0},
        ):
            _write_text(prompt_path, "---\ndescription: v2\n---\nPrompt v2\n")
            refreshed = await _wait_for_fingerprint_change(context, initial_fingerprint)
            assert refreshed
            prepared = load_prepared_state(context.root, context.name)
            assert prepared.global_lock.lock_path.is_file()
            assert prepared.agent_lock.lock_path.is_file()
            assert context.live.fingerprint == prepared.fingerprint
            assert any(
                entry.path == "agents/alice/prompts/rewrite.md"
                for entry in prepared.agent_lock.entries
            )

    asyncio.run(run_test())


def test_durable_caps_collapse_skill_directories(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(toolang_root / "skills" / "reviewer" / "SKILL.md", "# Reviewer\n")
    _write_text(toolang_root / "skills" / "reviewer" / "notes.txt", "asset\n")
    _write_text(toolang_root / "prompts" / "rewrite.md", "# Rewrite\n")

    entries = list_local_entries(toolang_root, "alice", scope="global")

    assert [(entry.kind, entry.path) for entry in entries] == [
        ("prompt", "prompts/rewrite.md"),
        ("skill", "skills/reviewer/SKILL.md"),
    ]


def test_caps_put_list_remove_local_entries(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"

    prompt_path = put_local_entry(
        toolang_root,
        "alice",
        scope="global",
        kind="prompt",
        name="rewrite",
        meta={"description": "Rewrite text"},
        body="Rewrite this text.",
    )
    skill_path = put_local_entry(
        toolang_root,
        "alice",
        scope="agent",
        kind="skill",
        name="reviewer",
        meta={"description": "Review code"},
        body="Review code carefully.",
    )

    assert prompt_path == toolang_root / "prompts" / "rewrite.md"
    assert skill_path == toolang_root / "agents" / "alice" / "skills" / "reviewer" / "SKILL.md"

    global_entries = list_local_entries(toolang_root, "alice", scope="global")
    agent_entries = list_local_entries(toolang_root, "alice", scope="agent")

    assert [(entry.kind, entry.meta["description"]) for entry in global_entries] == [
        ("prompt", "Rewrite text")
    ]
    assert [(entry.kind, entry.path) for entry in agent_entries] == [
        ("skill", "agents/alice/skills/reviewer/SKILL.md")
    ]

    assert remove_local_entry(toolang_root, "alice", scope="global", kind="prompt", name="rewrite") is True
    assert remove_local_entry(toolang_root, "alice", scope="agent", kind="skill", name="reviewer") is True
    assert list_local_entries(toolang_root, "alice", scope="global") == ()
    assert list_local_entries(toolang_root, "alice", scope="agent") == ()


def test_prepare_materializes_remote_entries_from_config(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"

    config_path = add_remote_entry(
        toolang_root,
        "alice",
        scope="global",
        kind="prompt",
        ref="acme/rewrite",
    )
    assert config_path == toolang_root / "config.toml"

    durable = scan_durable_state(toolang_root, "alice")
    prepared = prepare.build_prepared_state(durable)
    live = load_live_state(prepared, enabled_loops=("reload",))

    assert (toolang_root / ".prepared" / "remote" / "prompts" / "rewrite.md").is_file()
    assert [entry.source.form for entry in prepared.global_lock.entries] == ["remote"]
    assert prepared.global_lock.entries[0].path == ".prepared/remote/prompts/rewrite.md"
    assert prepared.global_lock.entries[0].ref == "github://acme/agent-prompts/prompts/rewrite.md"
    assert live.caps == (".prepared/remote/prompts/rewrite.md",)


def test_prepare_builds_program_into_agent_lock(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(toolang_root / "agents" / "alice" / "alice.too", "agent alice\n")

    durable = scan_durable_state(toolang_root, "alice")
    prepared = prepare.build_prepared_state(durable)

    assert prepared.program.agent_name == "alice"
    assert prepared.program.source_path == "agents/alice/alice.too"
    assert len(prepared.program.thunks) == 1
    assert prepared.program.thunks[0].name == "main"
    program_snapshot = cast(dict[str, object], prepared.agent_lock.to_snapshot()["program"])
    assert program_snapshot["agent_name"] == "alice"


def test_prepare_rewrites_legacy_agent_lock_missing_program(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(toolang_root / "agents" / "alice" / "alice.too", "agent alice\n")

    durable = scan_durable_state(toolang_root, "alice")
    global_lock, global_files = build_scope_lock(durable, scope="global")
    write_prepared_lock(toolang_root, global_lock, files=global_files)
    legacy_agent_lock, legacy_agent_files = build_scope_lock(durable, scope="agent")
    write_prepared_lock(toolang_root, legacy_agent_lock, files=legacy_agent_files)

    prepared = prepare.build_prepared_state(durable)

    assert prepared.agent_lock.program is not None
    assert prepared.agent_lock.program.agent_name == "alice"


def test_caps_list_and_remove_include_remote_entries(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"

    add_remote_entry(
        toolang_root,
        "alice",
        scope="agent",
        kind="skill",
        ref="acme/reviewer",
    )

    entries = list_entries(toolang_root, "alice", scope="agent", kinds={"skill"})

    assert [(entry.source.form, entry.path) for entry in entries] == [
        ("remote", "agents/alice/.prepared/remote/skills/reviewer/SKILL.md")
    ]
    assert remove_entry(toolang_root, "alice", scope="agent", kind="skill", name="reviewer") is True
    assert list_entries(toolang_root, "alice", scope="agent", kinds={"skill"}) == ()


def test_runs_bind_latest_live_snapshot(tmp_path: Path) -> None:
    async def run_test() -> None:
        toolang_root = tmp_path / "toolang"
        prompt_path = toolang_root / "agents" / "alice" / "prompts" / "rewrite.md"
        _write_text(prompt_path, "---\ndescription: v1\n---\nPrompt v1\n")
        context = _build_context(
            toolang_root=toolang_root,
            agent_name="alice",
            enabled_loops=("chat", "prepare", "reload"),
            runner=QueueRunner(delay_sec=0.03),
        )

        with _patched_runner_execution():
            async with _running_context(
                context,
                enabled_loops=("chat", "prepare", "reload"),
                loop_intervals_ms={"prepare": 10.0},
            ):
                first_fingerprint = context.live.fingerprint
                context.runner.enqueue(
                    RunRequest(
                        group="chat",
                        origin="chat",
                        thread_id="thread-1",
                        thunk="first",
                    )
                )
                await _wait_for_active_run(context)
                _write_text(prompt_path, "---\ndescription: v2\n---\nPrompt v2\n")
                changed = await _wait_for_fingerprint_change(context, first_fingerprint)
                assert changed
                second_fingerprint = context.live.fingerprint
                context.runner.enqueue(
                    RunRequest(
                        group="chat",
                        origin="chat",
                        thread_id="thread-1",
                        thunk="second",
                    )
                )
                await _wait_for_completed_count(context, 2)
                completed_runs = cast(
                    list[dict[str, object]],
                    inspect.snapshot_context(
                        context,
                        enabled_loops=("chat", "prepare", "reload"),
                    )["completed_runs"],
                )
                fingerprints = [item["live_fingerprint"] for item in completed_runs]
                assert fingerprints == [first_fingerprint, second_fingerprint]

    asyncio.run(run_test())


def test_new_task_reloads_into_live_state_and_tasks_endpoint(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(toolang_root / "agents" / "alice" / "alice.too", "agent alice\n")
    context = _build_context(
        toolang_root=toolang_root,
        agent_name="alice",
        enabled_loops=("inspect", "prepare", "reload"),
        runner=QueueRunner(delay_sec=0.0),
    )
    app = _create_test_app(context)

    with TestClient(app) as client:
        first_fingerprint = context.live.fingerprint
        _write_text(
            toolang_root / "agents" / "alice" / "tasks" / "review.md",
            "---\nrequester: owner\nstatus: todo\n---\nReview the current plan.\n",
        )
        for _ in range(200):
            snapshot = inspect.snapshot_context(
                context,
                enabled_loops=("inspect", "prepare", "reload"),
            )
            live = cast(dict[str, object], snapshot["live"])
            tasks = client.get("/api/v1/tasks").json()["items"]
            if context.live.fingerprint != first_fingerprint and live["jobs"] and tasks:
                break
            time.sleep(0.01)

        snapshot = inspect.snapshot_context(
            context,
            enabled_loops=("inspect", "prepare", "reload"),
        )
        live = cast(dict[str, object], snapshot["live"])
        tasks = client.get("/api/v1/tasks").json()["items"]

    assert context.live.fingerprint != first_fingerprint
    assert live["jobs"] == ["agents/alice/tasks/review.md"]
    assert len(tasks) == 1
    assert tasks[0]["name"] == "review"
    assert tasks[0]["body"] == "Review the current plan."
    assert tasks[0]["status"] == "todo"
    assert tasks[0]["requester"] == "owner"
    assert tasks[0]["paused"] is False
    assert tasks[0]["thread_id"] == f"task:local:{tasks[0]['id']}"
    assert tasks[0]["path"] == str(toolang_root / "agents" / "alice" / "tasks" / "review.md")


def test_new_task_reloads_and_pulse_runs_it(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(toolang_root / "agents" / "alice" / "alice.too", "agent alice\n")
    context = _build_context(
        toolang_root=toolang_root,
        agent_name="alice",
        enabled_loops=("inspect", "prepare", "reload", "pulse"),
        runner=QueueRunner(delay_sec=0.0),
    )
    context.config.set("loops.prepare.interval_ms", 10.0)
    context.config.set("loops.reload.debounce_ms", 10.0)
    context.config.set("loops.pulse.interval_ms", 10.0)
    app = _create_test_app(context)
    completed: list[dict[str, object]] = []

    with _patched_runner_execution():
        with TestClient(app):
            _write_text(
                toolang_root / "agents" / "alice" / "tasks" / "review.md",
                "---\nrequester: owner\nstatus: todo\n---\nReview the current plan.\n",
            )
            for _ in range(200):
                completed = cast(
                    list[dict[str, object]],
                    inspect.snapshot_context(
                        context,
                        enabled_loops=("inspect", "prepare", "reload", "pulse"),
                    )["completed_runs"],
                )
                if completed and completed[0]["origin"] == "task":
                    break
                time.sleep(0.01)
    assert completed
    assert completed[0]["group"] == "pulse"
    assert completed[0]["origin"] == "task"
    assert completed[0]["input_text"] == "Review the current plan."
    assert str(completed[0]["thread_id"]).startswith("task:local:")


def test_task_run_includes_local_task_protocol_in_prompt_bundle(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(toolang_root / "agents" / "alice" / "alice.too", "agent alice\n")
    _write_text(
        toolang_root / "agents" / "alice" / "tasks" / "review.md",
        "---\nrequester: owner\nstatus: todo\n---\nReview the current plan.\n",
    )
    context = _build_context(
        toolang_root=toolang_root,
        agent_name="alice",
        enabled_loops=("pulse",),
    )
    task = work.TaskFile.load(
        toolang_root / "agents" / "alice" / "tasks" / "review.md",
        persist_id=True,
    )
    bound = bind_run_request(
        context,
        RunRequest(
            group="pulse",
            origin="task",
            thread_id=task.thread_id(),
            thunk=task.body,
        ),
    )

    bundle = assemble_run_input(context, bound)

    assert bundle.snapshot.task == SnapshotTask(
        provider="local",
        ref=task.thread_id(),
        name="review",
        body=task.body,
        status="todo",
        requester="owner",
        thread_id=task.thread_id(),
        path=str(toolang_root / "agents" / "alice" / "tasks" / "review.md"),
    )
    assert bundle.snapshot.task_services == SnapshotTaskServices(
        provider="local",
        read=True,
        write=True,
        comment=True,
        path=str(toolang_root / "agents" / "alice" / "tasks" / "review.md"),
    )
    assert "Task execution protocol:" in bundle.instructions
    assert "Update the task file directly at:" in bundle.instructions
    assert "Move status from todo to doing when work starts." in bundle.instructions


def test_assemble_run_input_prefers_thunk_model_over_activation_default(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(
        toolang_root / "agents" / "alice" / "alice.too",
        "agent alice\n\nthunk chat:\n  model = openai/gpt-5\n\n  Reply directly.\n",
    )
    context = _build_context(
        toolang_root=toolang_root,
        agent_name="alice",
        enabled_loops=("chat",),
    )
    context.config.set("models.default_selector", "qwen/qwen3@ollama")
    bound = bind_run_request(
        context,
        RunRequest(group="chat", origin="chat", thunk="hello"),
    )

    bundle = assemble_run_input(context, bound)

    assert bundle.model == "openai/gpt-5"
    assert bundle.debug["activation_default_model"] == "qwen/qwen3@ollama"


def test_assemble_run_input_uses_activation_default_when_thunk_omits_one(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(
        toolang_root / "agents" / "alice" / "alice.too",
        "agent alice\n\nthunk chat:\n  Reply directly.\n",
    )
    context = _build_context(
        toolang_root=toolang_root,
        agent_name="alice",
        enabled_loops=("chat",),
    )
    context.config.set("models.default_selector", "qwen/qwen3@ollama")
    bound = bind_run_request(
        context,
        RunRequest(group="chat", origin="chat", thunk="hello"),
    )

    bundle = assemble_run_input(context, bound)

    assert bundle.model == "qwen/qwen3@ollama"
    assert bundle.debug["activation_default_model"] == "qwen/qwen3@ollama"


def test_execute_run_rejects_thunk_model_outside_activation_allowlist(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(
        toolang_root / "agents" / "alice" / "alice.too",
        "agent alice\n\nthunk chat:\n  model = openai/gpt-5\n\n  Reply directly.\n",
    )
    context = _build_context(
        toolang_root=toolang_root,
        agent_name="alice",
        enabled_loops=("chat",),
    )
    context.config.set("models.allowed_selectors", ("qwen/qwen3@ollama",))
    context.config.set("models.default_selector", "qwen/qwen3@ollama")

    outcome = asyncio.run(
        run_execute_module.execute_run(
            context,
            RunSubmission(
                request=RunRequest(group="chat", origin="chat", thunk="hello"),
                live=context.live,
            ),
            delay_sec=0.0,
            sleep=asyncio.sleep,
        )
    )

    assert outcome.status == "failed"
    assert outcome.error is not None
    assert "not allowed for this activation" in outcome.error


def test_execution_store_records_runs_steps_and_messages(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    store = ExecutionStore(execution_db_path(toolang_root, "alice"))
    try:
        created = store.append_update(
            kind="created",
            payload={"path": str(toolang_root / "agents" / "alice" / "alice.too")},
        )
        run = store.start_run(
            run_id="run-1",
            thread_id="thread-1",
            origin="chat",
            input=Message.user("hello"),
        )
        store.append_step(
            run_id=run.run_id,
            step_index=1,
            kind="model_call",
            status="finished",
            input=(RunInputRef(),),
            output=(TextPart(text="assistant:hello"),),
            payload=ModelCallStepPayload(
                model_ref="gpt-5",
                input_tokens=0,
                output_tokens=0,
            ),
            started_at="2026-01-01T00:00:01Z",
            finished_at="2026-01-01T00:00:02Z",
        )
        finished = store.finish_run(run_id=run.run_id)

        assert finished.status == "finished"
        assert [item.kind for item in store.list_steps(run_id=run.run_id)] == ["model_call"]
        assert [item.to_data() for item in store.recent_conversation_messages(thread_id="thread-1")] == [
            {"role": "user", "parts": [{"type": "text", "text": "hello"}]},
            {"role": "assistant", "parts": [{"type": "text", "text": "assistant:hello"}]},
        ]
        assert [item.kind for item in store.list_updates(limit=10)] == ["created"]
        assert created.payload["path"] == str(toolang_root / "agents" / "alice" / "alice.too")
    finally:
        store.close()


def test_execution_store_rebuilds_tool_history_from_steps(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    store = ExecutionStore(execution_db_path(toolang_root, "alice"))
    try:
        run = store.start_run(
            run_id="run-1",
            thread_id="thread-1",
            origin="chat",
            input=Message.user("sum 7 and 8"),
        )
        store.append_step(
            run_id=run.run_id,
            step_index=1,
            kind="model_call",
            status="finished",
            input=(RunInputRef(),),
            output=(
                ToolCallPart(
                    tool_call_id="tool-1",
                    tool_name="math_add",
                    tool_family="math_add",
                    input={"a": 7, "b": 8},
                ),
            ),
            payload=ModelCallStepPayload(
                model_ref="gpt-5",
                input_tokens=0,
                output_tokens=0,
            ),
            started_at="2026-01-01T00:00:01Z",
            finished_at="2026-01-01T00:00:02Z",
        )
        store.append_step(
            run_id=run.run_id,
            step_index=2,
            kind="tool_call",
            status="finished",
            input=(StepOutputRef(step_index=1, part_index=0),),
            output=(
                ToolResultPart(
                    tool_call_id="tool-1",
                    tool_name="math_add",
                    tool_family="math_add",
                    output={"value": 15},
                ),
            ),
            payload=ToolCallStepPayload(),
            started_at="2026-01-01T00:00:03Z",
            finished_at="2026-01-01T00:00:04Z",
        )
        store.append_step(
            run_id=run.run_id,
            step_index=3,
            kind="model_call",
            status="finished",
            input=(StepOutputRef(step_index=2),),
            output=(TextPart(text="15"),),
            payload=ModelCallStepPayload(
                model_ref="gpt-5",
                input_tokens=0,
                output_tokens=0,
            ),
            started_at="2026-01-01T00:00:05Z",
            finished_at="2026-01-01T00:00:06Z",
        )
        assert [item.to_data() for item in store.recent_conversation_messages(thread_id="thread-1")] == [
            {"role": "user", "parts": [{"type": "text", "text": "sum 7 and 8"}]},
            {
                "role": "assistant",
                "parts": [
                    {
                        "type": "tool_call",
                        "tool_call_id": "tool-1",
                        "tool_name": "math_add",
                        "tool_family": "math_add",
                        "input": {"a": 7, "b": 8},
                    }
                ],
            },
            {
                "role": "tool",
                "parts": [
                    {
                        "type": "tool_result",
                        "tool_call_id": "tool-1",
                        "tool_name": "math_add",
                        "tool_family": "math_add",
                        "output": {"value": 15},
                    }
                ],
            },
            {"role": "assistant", "parts": [{"type": "text", "text": "15"}]},
        ]
    finally:
        store.close()


@asynccontextmanager
async def _running_context(
    context: UptimeContext,
    *,
    enabled_loops: tuple[str, ...],
    loop_intervals_ms: dict[str, float] | None = None,
):
    @asynccontextmanager
    async def lifespan(_: FastAPI):
        stop_signal = asyncio.Event()
        reload_signal = asyncio.Event()
        if loop_intervals_ms is not None:
            for loop_name, interval_ms in loop_intervals_ms.items():
                context.config.set(f"loops.{loop_name}.interval_ms", interval_ms)

        background_tasks: list[asyncio.Task[None]] = []
        if "pulse" in enabled_loops:
            background_tasks.append(pulse.spawn(context, stop_signal=stop_signal))
        if "poll" in enabled_loops:
            background_tasks.append(poll.spawn(context, stop_signal=stop_signal))
        if "prepare" in enabled_loops:
            background_tasks.append(prepare.spawn(context, stop_signal=stop_signal, reload_signal=reload_signal))
        reload_task: asyncio.Task[None] | None = None
        if "reload" in enabled_loops:
            context.config.set("loops.reload.debounce_ms", 10.0)
            reload_task = reload.spawn(context, stop_signal=stop_signal, reload_signal=reload_signal)
        runner_task = None
        if any(loop in RUN_LOOPS for loop in enabled_loops):
            runner_task = context.runner.spawn(context)

        try:
            await asyncio.sleep(0)
            yield
        finally:
            stop_signal.set()
            if reload_task is not None:
                with suppress(asyncio.CancelledError):
                    await reload_task
            for task in background_tasks:
                with suppress(asyncio.CancelledError):
                    await task
            context.runner.close()
            if runner_task is not None:
                await runner_task
            context.store.close()

    async with lifespan(FastAPI()):
        yield context


def _create_test_app(context: UptimeContext) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI):
        enabled_loops = cast(tuple[str, ...], context.config.require("loops.enabled"))
        async with _running_context(context, enabled_loops=enabled_loops):
            yield

    return create_app(context, lifespan=lifespan)


def _build_context(
    *,
    toolang_root: Path,
    agent_name: str,
    enabled_loops: tuple[str, ...],
    runner: QueueRunner | None = None,
    channel_bindings: dict[str, ChannelBinding] | None = None,
    channel_plugins: dict[str, ChannelPlugin] | None = None,
) -> UptimeContext:
    durable = scan_durable_state(toolang_root, agent_name)
    prepared = prepare.build_prepared_state(durable)
    live = load_live_state(prepared, enabled_loops=enabled_loops)
    store = ExecutionStore(execution_db_path(toolang_root, agent_name))
    return UptimeContext(
        root=toolang_root,
        name=agent_name,
        live=live,
        tools=load_tool_plugins(),
        model_plugins=load_model_plugins(),
        model_profiles=load_model_profiles(toolang_root, agent_name),
        default_models=load_default_models(toolang_root, agent_name),
        model_environ={},
        channel_bindings=channel_bindings or {},
        channel_plugins=channel_plugins or {},
        runner=runner
        if runner is not None
        else QueueRunner(delay_sec=0.0),
        store=store,
        config=UptimeConfig(
            {
                "server.host": "127.0.0.1",
                "server.port": 8765,
                "loops.enabled": enabled_loops,
                "loops.pulse.interval_ms": pulse.DEFAULT_INTERVAL_MS,
                "loops.poll.interval_ms": poll.DEFAULT_INTERVAL_MS,
                "loops.prepare.interval_ms": prepare.DEFAULT_INTERVAL_MS,
                "loops.reload.debounce_ms": reload.DEFAULT_DEBOUNCE_MS,
            }
        ),
    )


def _wait_for_completed_runs(client: TestClient) -> dict[str, object]:
    for _ in range(50):
        snapshot = client.get("/api/v1/runs").json()
        if snapshot["items"]:
            return snapshot
        time.sleep(0.01)
    raise AssertionError("expected completed runs")


async def _wait_for_fingerprint_change(context: UptimeContext, fingerprint: str) -> bool:
    for _ in range(200):
        if context.live.fingerprint != fingerprint:
            return True
        await asyncio.sleep(0.01)
    return False


async def _wait_for_active_run(context: UptimeContext) -> None:
    for _ in range(100):
        live = cast(
            dict[str, object],
            inspect.snapshot_context(context, enabled_loops=context.live.enabled_loops)[
                "live"
            ],
        )
        if live["active_runs"]:
            return
        await asyncio.sleep(0.005)
    raise AssertionError("expected active run")


async def _wait_for_completed_count(context: UptimeContext, count: int) -> None:
    for _ in range(200):
        if len(context.runner.completed()) >= count:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("expected completed runs")


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _fake_run_input(bound):
    input_message = Message.user(bound.input_text or "hello")
    return type(
        "RunInputStub",
        (),
        {
            "run": bound,
            "model": None,
            "instructions": "",
            "input": input_message,
            "messages": [input_message],
            "snapshot": {},
            "tools": {},
            "debug": {},
        },
    )()


class _FakeStrategy:
    def __init__(self, *, run) -> None:
        self.name = "basic"
        self._run = run

    def run(self, context):
        return self._run(context)


def _started(
    step_index: int,
    *,
    run_id: str,
    thread_id: str,
    kind: Literal["model_call", "tool_call", "runtime"],
    input=(),
    instructions: str | None = None,
) -> StepStart:
    return StepStart(
        run_id=run_id,
        thread_id=thread_id,
        step_index=step_index,
        kind=kind,
        input=tuple(input) or _default_step_input(step_index=step_index, kind=kind),
        started_at="2026-01-01T00:00:00Z",
        instructions=instructions,
    )


def _completed(
    step_index: int,
    *,
    run_id: str,
    thread_id: str,
    kind: Literal["model_call", "tool_call", "runtime"],
    output=(),
    input_step_index: int | None = 0,
    input_part_index: int | None = None,
    error: str | None = None,
) -> StepEnd:
    del input_step_index, input_part_index
    if kind == "model_call":
        payload = ModelCallStepPayload(model_ref="gpt-5", input_tokens=0, output_tokens=0)
    elif kind == "tool_call":
        payload = ToolCallStepPayload()
    else:
        payload = RuntimeStepPayload()
    return StepEnd(
        run_id=run_id,
        thread_id=thread_id,
        step_index=step_index,
        kind=kind,
        status="failed" if error else "finished",
        output=tuple(output),
        payload=payload,
        started_at="2026-01-01T00:00:00Z",
        finished_at="2026-01-01T00:00:01Z",
        error=error,
    )


def _default_step_input(
    *,
    step_index: int,
    kind: Literal["model_call", "tool_call", "runtime"],
):
    if kind == "runtime":
        return ()
    if kind == "model_call":
        if step_index == 1:
            return (RunInputRef(),)
        return (StepOutputRef(step_index=step_index - 1),)
    return (StepOutputRef(step_index=max(step_index - 1, 1), part_index=0),)


@contextmanager
def _patched_runner_execution():
    current: dict[str, str] = {}

    def fake_assemble(_context: UptimeContext, bound):
        current["run_id"] = bound.run_id
        current["thread_id"] = bound.thread_id
        current["input_text"] = bound.input_text
        return _fake_run_input(bound)

    def fake_run(context) -> RunResult:
        output_text = f"assistant:{current['input_text']}"
        instructions = "You are a helpful assistant."
        if context.on_event is not None:
            context.on_event(
                _started(
                    1,
                    run_id=current["run_id"],
                    thread_id=current["thread_id"],
                    kind="model_call",
                    instructions=instructions,
                )
            )
            context.on_event(
                _completed(
                    1,
                    run_id=current["run_id"],
                    thread_id=current["thread_id"],
                    kind="model_call",
                    output=(TextPart(text=output_text),),
                )
            )
        return RunResult(output_text=output_text)

    with (
        patch.object(run_execute_module, "assemble_run_input", side_effect=fake_assemble),
        patch.object(
            run_execute_module,
            "load_run_strategy",
            return_value=_FakeStrategy(
                run=fake_run,
            ),
        ),
    ):
        yield


@contextmanager
def _patched_runner_execution_with_tools(*, output_text: str):
    current: dict[str, str] = {}

    def fake_assemble(_context: UptimeContext, bound):
        current["run_id"] = bound.run_id
        current["thread_id"] = bound.thread_id
        return _fake_run_input(bound)

    def fake_run(context) -> RunResult:
        if context.on_event is not None:
            context.on_event(
                _started(
                    1,
                    run_id=current["run_id"],
                    thread_id=current["thread_id"],
                    kind="model_call",
                )
            )
            context.on_event(
                _completed(
                    1,
                    run_id=current["run_id"],
                    thread_id=current["thread_id"],
                    kind="model_call",
                    output=(
                        ToolCallPart(
                            tool_call_id="call_1",
                            tool_name="math_add",
                            tool_family="math_add",
                            input={"a": 7, "b": 8},
                        ),
                    ),
                )
            )
            context.on_event(
                _started(
                    2,
                    run_id=current["run_id"],
                    thread_id=current["thread_id"],
                    kind="tool_call",
                    input=(StepOutputRef(step_index=1, part_index=0),),
                )
            )
            context.on_event(
                _completed(
                    2,
                    run_id=current["run_id"],
                    thread_id=current["thread_id"],
                    kind="tool_call",
                    input_step_index=1,
                    input_part_index=0,
                    output=(
                        ToolResultPart(
                            tool_call_id="call_1",
                            tool_name="math_add",
                            tool_family="math_add",
                            output={"value": 15},
                        ),
                    ),
                )
            )
        return RunResult(output_text=output_text)

    def fake_run_stream(context) -> RunResult:
        on_event = context.on_event
        if on_event is not None:
            on_event(
                _started(
                    1,
                    run_id=current["run_id"],
                    thread_id=current["thread_id"],
                    kind="model_call",
                )
            )
            on_event(
                PartStart(
                    run_id=current["run_id"],
                    thread_id=current["thread_id"],
                    step_index=1,
                    part_index=0,
                    kind="tool_call",
                )
            )
            on_event(
                PartDelta(
                    run_id=current["run_id"],
                    thread_id=current["thread_id"],
                    step_index=1,
                    part_index=0,
                    delta=ToolCallDelta(
                        text='{"a":7,"b":8}',
                        tool_call_id="call_1",
                    ),
                )
            )
            on_event(
                PartEnd(
                    run_id=current["run_id"],
                    thread_id=current["thread_id"],
                    step_index=1,
                    part_index=0,
                    data=ToolCallPart(
                        tool_call_id="call_1",
                        tool_name="math_add",
                        tool_family="math_add",
                        input={"a": 7, "b": 8},
                    ),
                )
            )
            on_event(
                _completed(
                    1,
                    run_id=current["run_id"],
                    thread_id=current["thread_id"],
                    kind="model_call",
                    output=(
                        ToolCallPart(
                            tool_call_id="call_1",
                            tool_name="math_add",
                            tool_family="math_add",
                            input={"a": 7, "b": 8},
                        ),
                    ),
                )
            )
            on_event(
                _started(
                    2,
                    run_id=current["run_id"],
                    thread_id=current["thread_id"],
                    kind="tool_call",
                    input=(StepOutputRef(step_index=1, part_index=0),),
                )
            )
            on_event(
                PartEnd(
                    run_id=current["run_id"],
                    thread_id=current["thread_id"],
                    step_index=2,
                    part_index=0,
                    data=ToolResultPart(
                        tool_call_id="call_1",
                        tool_name="math_add",
                        tool_family="math_add",
                        output={"value": 15},
                    ),
                )
            )
            on_event(
                _completed(
                    2,
                    run_id=current["run_id"],
                    thread_id=current["thread_id"],
                    kind="tool_call",
                    input_step_index=1,
                    input_part_index=0,
                    output=(
                        ToolResultPart(
                            tool_call_id="call_1",
                            tool_name="math_add",
                            tool_family="math_add",
                            output={"value": 15},
                        ),
                    ),
                )
            )
        if on_event is not None and output_text:
            on_event(
                _started(
                    3,
                    run_id=current["run_id"],
                    thread_id=current["thread_id"],
                    kind="model_call",
                )
            )
            on_event(
                PartStart(
                    run_id=current["run_id"],
                    thread_id=current["thread_id"],
                    step_index=3,
                    part_index=0,
                    kind="text",
                )
            )
            on_event(
                PartDelta(
                    run_id=current["run_id"],
                    thread_id=current["thread_id"],
                    step_index=3,
                    part_index=0,
                    delta=TextDelta(text=output_text),
                )
            )
            on_event(
                _completed(
                    3,
                    run_id=current["run_id"],
                    thread_id=current["thread_id"],
                    kind="model_call",
                    input_step_index=2,
                    input_part_index=None,
                    output=(TextPart(text=output_text),),
                )
            )
        return RunResult(output_text=output_text)

    with (
        patch.object(run_execute_module, "assemble_run_input", side_effect=fake_assemble),
        patch.object(
            run_execute_module,
            "load_run_strategy",
            return_value=_FakeStrategy(
                run=fake_run_stream,
            ),
        ),
    ):
        yield


@contextmanager
def _patched_runner_failure(message: str):
    def fake_assemble(_context: UptimeContext, bound):
        return _fake_run_input(bound)

    def fake_run(_context):
        raise RuntimeError(message)

    with (
        patch.object(run_execute_module, "assemble_run_input", side_effect=fake_assemble),
        patch.object(
            run_execute_module,
            "load_run_strategy",
            return_value=_FakeStrategy(
                run=fake_run,
            ),
        ),
    ):
        yield


@contextmanager
def _patched_runner_streaming_text(release: threading.Event):
    current: dict[str, str] = {}

    def fake_assemble(_context: UptimeContext, bound):
        current["run_id"] = bound.run_id
        current["thread_id"] = bound.thread_id
        return _fake_run_input(bound)

    def fake_run(context) -> RunResult:
        return RunResult(output_text="streaming hello")

    def fake_run_stream(context) -> RunResult:
        on_event = context.on_event
        if on_event is not None:
            on_event(
                _started(
                    1,
                    run_id=current["run_id"],
                    thread_id=current["thread_id"],
                    kind="model_call",
                )
            )
            on_event(
                PartStart(
                    run_id=current["run_id"],
                    thread_id=current["thread_id"],
                    step_index=1,
                    part_index=0,
                    kind="text",
                )
            )
            on_event(
                PartDelta(
                    run_id=current["run_id"],
                    thread_id=current["thread_id"],
                    step_index=1,
                    part_index=0,
                    delta=TextDelta(text="streaming hello"),
                )
            )
        release.wait(timeout=1.0)
        if on_event is not None:
            on_event(
                _completed(
                    1,
                    run_id=current["run_id"],
                    thread_id=current["thread_id"],
                    kind="model_call",
                    output=(TextPart(text="streaming hello"),),
                )
            )
        return RunResult(output_text="streaming hello")

    with (
        patch.object(run_execute_module, "assemble_run_input", side_effect=fake_assemble),
        patch.object(
            run_execute_module,
            "load_run_strategy",
            return_value=_FakeStrategy(
                run=fake_run_stream,
            ),
        ),
    ):
        yield


def _index_where(items: list[str], predicate) -> int:
    for index, item in enumerate(items):
        if predicate(item):
            return index
    raise AssertionError(f"no item matched predicate: {items!r}")
