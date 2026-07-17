from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, contextmanager, suppress
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
import signal
import socket
import threading
import time
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
import frontmatter

from toolang.agent import local as agents
from toolang.catalog import agent as agent_catalog
from toolang.catalog.agent import AgentCatalog
from toolang.catalog import cap as caps
from toolang.catalog.job import JobCatalog
from toolang.work.scheduler import DEFAULT_INTERVAL_MS as DEFAULT_SCHEDULER_INTERVAL_MS
from toolang.work.scheduler import Scheduler
from toolang.work.state import AgentJobs
from toolang.work.store import JobStore, open_job_store
from toolang.work.watcher import JobWatcher
from toolang.common.error import ToolangError
from toolang.base.protocols.channel import AgentChannel
from toolang.base.protocols.sandbox import AgentSandbox
from toolang.base.types.channel import (
    ChannelState,
    DeliveryResult,
    InboundDelivery,
    PluginHealth,
    PollResult,
    ReplyTarget,
)
from toolang.base.types.run import ModelCallResult, RunResult
from toolang.base.types.message import (
    Message,
    TextDelta,
    TextPart,
    ToolCallDelta,
    ToolCallPart,
    ToolResultPart,
    message_text,
)
from toolang.execution.events import (
    PartDelta,
    PartEnd,
    PartBegin,
    RunEnd,
    RunBegin,
    RunStarting,
    StepEnd,
    StepBegin,
)
from toolang.execution.context import (
    RunSnapshot,
    SnapshotAgent,
    SnapshotProgram,
    SnapshotRun,
    SnapshotTask,
    SnapshotTaskServices,
)
from toolang.execution.records import (
    InputRef,
    OutputRef,
    RunRecord,
    StepKind,
    trace_child_path,
)
from toolang.base.types.sandbox import (
    SandboxPlan,
    SandboxSelector,
    SandboxStartRequest,
    SandboxStartResult,
    SandboxState,
)
from toolang.catalog.cap import (
    add_remote_entry,
    remove_remote_entry,
)
from toolang.state.caps import build_visibility_lock, list_entries
from toolang.state import caps as cap_state
from toolang.plugin.config import ChannelBinding
from toolang.config.log_spec import PY_LOG_ENV_VAR
from toolang.execution import executor as run_executor_module
from toolang.execution.assembly import RunInput
from toolang.execution.binding import _bind_run_request
from toolang.execution.executor import Executor
from toolang.execution.request import RunRequest
from toolang.execution.store import PersistSink, RunStore, run_store_path
from toolang.execution.reply import SseReplySink
from toolang.execution.setup import AgentSetup
from toolang.api import _views as inspect
from toolang.api import chat as chat_loop
from toolang.api._streaming import ShutdownAwareStreamingResponse
from toolang.agent.features import normalize_component_names
from toolang.work import inbox as files
from toolang.agent import channel_runtime as poll
from toolang.agent import state_updates as watch
from toolang.plugin.tools.loading import load_runtime_tools, runtime_tool_config
from toolang.work import files as file_requests
from toolang.state.durable import scan_durable_state
from toolang.state.agent import load_agent_state
from toolang.state import watcher as state_watcher
from toolang.state.prepared import (
    PreparedLocks,
    load_prepared_locks,
    write_prepared_lock,
)
from toolang.plugin.loops.basic import BasicLoop
from toolang.plugin.models.loading import load_model_adapters, load_model_providers
from toolang.agent import runtime as up_module
from toolang.api.context import ApiContext
from toolang.config.runtime import RuntimeConfig
from toolang.agent.channel_runtime import start_delivery
from toolang.agent.runtime import (
    load_default_models,
    up as run_experiments_up,
)
from toolang.api.app import create_app
from toolang.config.files import load_config_layers, load_named_config
from toolang.plugin.models.config import parse_model_aliases
from tests.support.execution import (
    emit_event,
    project_command,
    project_run_end,
    project_run_start,
    project_step,
)


def _put_cap(
    root: Path,
    agent: str,
    *,
    visibility: caps.Visibility,
    kind: caps.EntryKind,
    name: str,
    body: str = "",
    meta: dict[str, object] | None = None,
    text: str | None = None,
) -> Path:
    source = text or frontmatter.dumps(frontmatter.Post(body, **dict(meta or {})))
    return caps.CapCatalog(root, agent, visibility=visibility).create(kind, name, source)


def bind_run_request(context, request, *, state=None):
    return _bind_run_request(
        request,
        id_state_path=context.executor.id_state_path,
        state=state or context.get_agent_state(),
        setup=context.executor.setup,
        store=context.store,
    )


def _emit_trace(context: ApiContext, event) -> None:
    emit_event(context.store, event, agent_id=context.name)


def test_executor_stop_projects_command_and_canceled_run(tmp_path: Path) -> None:
    context = _build_context(
        toolang_root=tmp_path,
        agent_name="alice",
        enabled_features=("chat",),
    )
    project_run_start(context.store,
        run_id="run-1",
        thread_id="thread-1",
        origin="chat",
        input=Message.user("hello"),
    )

    command, run = asyncio.run(context.executor.stop(run_id="run-1"))

    assert command.kind == "stop"
    assert command.index == 1
    assert run.status == "canceled"
    context.store.close()


def test_store_reserves_command_indexes_across_connections(tmp_path: Path) -> None:
    path = tmp_path / "runs.db"
    stores = (RunStore(path), RunStore(path))
    project_run_start(stores[0],
        run_id="run-1",
        thread_id="thread-1",
        origin="chat",
        input=Message.user("hello"),
    )
    barrier = threading.Barrier(3)
    values: list[int] = []
    values_lock = threading.Lock()

    def reserve(store: RunStore) -> None:
        barrier.wait()
        reserved = [store.reserve_command_index(run_id="run-1") for _ in range(20)]
        with values_lock:
            values.extend(reserved)

    threads = [threading.Thread(target=reserve, args=(store,)) for store in stores]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()
    for store in stores:
        store.close()

    assert sorted(values) == list(range(1, 41))


def test_store_appends_event_cursors_across_connections(tmp_path: Path) -> None:
    path = tmp_path / "runs.db"
    stores = (RunStore(path), RunStore(path))
    barrier = threading.Barrier(3)

    def append(store: RunStore) -> None:
        barrier.wait()
        for index in range(20):
            store.append_event(
                domain="agent",
                domain_id="alice",
                type="test",
                payload={"index": index},
            )

    threads = [threading.Thread(target=append, args=(store,)) for store in stores]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()

    events = stores[0].list_events(domain="agent", domain_id="alice", limit=100)
    for store in stores:
        store.close()

    assert [event.seq for event in events] == list(range(1, 41))


def test_sse_response_sink_streams_canceled_run_end() -> None:
    async def run_test() -> None:
        response = SseReplySink(thread_id="thread-1")
        response.on_event(
            RunStarting(
                run="run-1",
                cmd=0,
                parent=None,
                thread="thread-1",
                input=Message.user("hello"),
                context={"origin": "chat"},
                created_at="2026-01-01T00:00:00Z",
            )
        )
        response.on_event(
            RunEnd(
                run="run-1",
                status="canceled",
                finished_at="2026-01-01T00:00:01Z",
            )
        )

        chunks = [chunk async for chunk in response.stream()]

        payloads = [_sse_data(chunk) for chunk in chunks if chunk.startswith("data: {")]
        assert [payload["type"] for payload in payloads] == [
            "start",
            "message-metadata",
            "run_end",
            "finish",
        ]
        assert payloads[2]["payload"]["status"] == "canceled"

    asyncio.run(run_test())


def test_file_request_store_deduplicates_same_fingerprint(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    store = file_requests.open_file_request_store(toolang_root, "alice")
    snapshot = file_requests.FileSnapshot(
        watch_root=str(tmp_path / "inbox"),
        relative_path="note.txt",
        absolute_path=str(tmp_path / "inbox" / "note.txt"),
        size=5,
        mtime_ns=123,
        fingerprint="abc123",
    )
    try:
        first = store.claim(
            snapshot,
            run_id="run_first",
            thread_id=file_requests.file_thread_id(snapshot.absolute_path),
        )
        second = store.claim(snapshot, run_id="run_second", thread_id="script_unused")
        finished = store.finish_run(run_id="run_first", run_status="finished")
    finally:
        store.close()

    assert first is not None
    assert first.thread_id == file_requests.file_thread_id(snapshot.absolute_path)
    assert second is None
    assert finished is not None
    assert finished.status == "finished"
    assert finished.processed_at is not None


def test_file_request_input_classifies_common_file_types(tmp_path: Path) -> None:
    text_path = tmp_path / "data.json"
    text_path.write_text('{"ok": true}\n', encoding="utf-8")

    text, parts = file_requests.render_file_input(text_path)

    assert text == '{"ok": true}\n'
    assert parts == [
        {"type": "text", "text": '{"ok": true}\n', "path": str(text_path.resolve())}
    ]
    assert file_requests.path_part_type(tmp_path / "photo.png") == "image"
    assert file_requests.path_part_type(tmp_path / "voice.wav") == "audio"
    assert file_requests.path_part_type(tmp_path / "clip.mp4") == "video"
    assert file_requests.path_part_type(tmp_path / "archive.zip") == "file"


def test_collect_file_submissions_scans_existing_inbox_files_once(
    tmp_path: Path,
) -> None:
    toolang_root = tmp_path / "toolang"
    inbox = tmp_path / "inbox"
    _write_text(
        toolang_root / "agents" / "alice" / "agent.too",
        "agent alice\n\nagic file(in: Part[]):\n  Process a file.\n",
    )
    _write_text(inbox / "note.txt", "hello")
    context = _build_context(
        toolang_root=toolang_root,
        agent_name="alice",
        enabled_features=("file",),
    )
    context.config.set("components.trigger.file.inboxes", (inbox,))
    context.config.set("components.trigger.file.stable_ms", 0.0)
    store = file_requests.open_file_request_store(toolang_root, "alice")
    try:
        first = files.collect_file_submissions(
            context.executor, store, inboxes=(inbox,), stable_ms=0.0
        )
        second = files.collect_file_submissions(
            context.executor, store, inboxes=(inbox,), stable_ms=0.0
        )
        rows = store.list()
    finally:
        store.close()
        context.store.close()

    note_path = str((inbox / "note.txt").resolve())
    assert len(first) == 1
    assert second == []
    assert first[0].text == "hello"
    assert first[0].parts == [{"type": "text", "text": "hello", "path": note_path}]
    assert len(rows) == 1
    assert rows[0].relative_path == "note.txt"
    assert rows[0].thread_id == file_requests.file_thread_id(inbox / "note.txt")
    assert rows[0].status == "running"


def test_create_app_mounts_only_enabled_routes(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(toolang_root / "agents" / "alice" / "agent.too", "agent alice\n")
    context = _build_context(
        toolang_root=toolang_root,
        agent_name="alice",
        enabled_features=("chat", "inspect"),
    )
    app = _create_test_app(context)

    with _patched_executor_execution():
        with TestClient(app) as client:
            response = client.post(
                "/api/v1/chat",
                json={"message": _chat_message("say hello")},
            )
            assert response.status_code == 200
            body = response.json()
            thread_id = body["thread_id"]
            assert thread_id.startswith("web_")
            assert body["message"]["parts"][0]["text"] == "say hello"
            assert body["assistant"]["parts"][0]["text"] == "assistant:say hello"
            assert (
                client.put(
                    "/api/v1/skills/reviewer/wired",
                    json={"visibility": "private", "ref": "acme/reviewer"},
                ).status_code
                == 404
            )

            runs = client.get("/api/v1/runs").json()["items"]
            profile = client.get("/api/v1/profile").json()
            caps_response = client.get("/api/v1/caps").json()
            threads = client.get("/api/v1/threads").json()["items"]
            snapshot = inspect.snapshot_context(
                context, enabled_features=("chat", "inspect")
            )
            durable = cast(dict[str, object], snapshot["durable"])
            prepared = cast(dict[str, object], snapshot["prepared"])
            state = cast(dict[str, object], snapshot["state"])
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
                "steps": {"total": 1, "model": 1, "tool": 0, "system": 0},
                "tokens": {"input": 0, "output": 0, "total": 0},
            }
            assert caps_response["agent"] == "alice"
            assert [item["input_text"] for item in runs] == ["say hello"]
            assert [item["id"] for item in threads] == [thread_id]
            assert threads[0] == {
                "id": thread_id,
                "title": "say hello",
                "origin": "chat",
                "channel": "web",
                "status": "idle",
                "peer": {"type": "user", "name": "user", "thread": None},
                "parent": None,
                "created_at": runs[0]["created_at"],
                "updated_at": runs[0]["updated_at"],
                "run_count": 1,
                "latest_run": {
                    "id": runs[0]["id"],
                    "origin": "chat",
                    "status": "finished",
                    "created_at": runs[0]["created_at"],
                    "started_at": runs[0]["started_at"],
                    "finished_at": runs[0]["finished_at"],
                    "updated_at": runs[0]["updated_at"],
                },
                "active_run": None,
            }
            thread_detail = client.get(f"/api/v1/threads/{thread_id}").json()
            run_detail = client.get(f"/api/v1/runs/{body['run_id']}").json()
            assert thread_detail["info"] == threads[0]
            assert [item["info"]["id"] for item in thread_detail["runs"]] == [
                body["run_id"]
            ]
            assert run_detail["info"] == thread_detail["runs"][0]["info"]
            assert run_detail["input"]["role"] == "user"
            assert run_detail["input"]["parts"][0]["text"] == "say hello"
            assert run_detail["output"]["status"] == "finished"
            assert [
                item["record"]["kind"] for item in run_detail["output"]["steps"]
            ] == ["model"]
            assert run_detail["output"]["steps"][0]["message"]["role"] == "assistant"
            assert (
                run_detail["output"]["steps"][0]["message"]["parts"][0]["text"]
                == "assistant:say hello"
            )
            record_context = run_detail["output"]["steps"][0]["record"]["context"]
            instruct_hash = record_context["instruct"]
            context_hash = record_context["prompt_context"]
            assert isinstance(instruct_hash, str) and instruct_hash
            assert isinstance(context_hash, str) and context_hash
            assert instruct_hash == "You are a helpful assistant."
            assert context_hash == "Context for this run."
            assert (
                client.get(f"/api/v1/instructions/{instruct_hash}").status_code == 404
            )
            assert definitions["program_source"] == "agents/alice/agent.too"
            assert definitions["private_entries"] == []
            assert state["fingerprint"] == context.get_agent_state().fingerprint
            assert operational_facts["completed_runs"] == 1
            assert operational_facts["prepared_fingerprint"] == prepared_fingerprint
            assert state["completed_runs"] == 1


def test_threads_api_reports_full_run_count_independent_of_recent_run_limit(
    tmp_path: Path,
) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(toolang_root / "agents" / "alice" / "agent.too", "agent alice\n")
    context = _build_context(
        toolang_root=toolang_root,
        agent_name="alice",
        enabled_features=("inspect",),
    )
    project_run_start(context.store,
        run_id="run-old",
        thread_id="thread-1",
        origin="chat",
        input=Message.user("first message"),
        created_at="2026-01-01T00:00:00Z",
        started_at="2026-01-01T00:00:00Z",
    )
    project_run_end(context.store,
        run_id="run-old",
        status="finished",
        finished_at="2026-01-01T00:00:01Z",
    )
    project_run_start(context.store,
        run_id="run-new",
        thread_id="thread-1",
        origin="chat",
        input=Message.user("second message"),
        created_at="2026-01-01T00:01:00Z",
        started_at="2026-01-01T00:01:00Z",
    )
    project_run_end(context.store,
        run_id="run-new",
        status="failed",
        error="boom",
        finished_at="2026-01-01T00:01:01Z",
    )
    app = _create_test_app(context)

    with TestClient(app) as client:
        recent_runs = client.get("/api/v1/runs?limit=1").json()["items"]
        thread = client.get("/api/v1/threads").json()["items"][0]

    assert [item["id"] for item in recent_runs] == ["run-new"]
    assert thread["id"] == "thread-1"
    assert thread["title"] == "first message"
    assert thread["channel"] == "terminal"
    assert thread["status"] == "idle"
    assert thread["run_count"] == 2
    assert thread["created_at"] == "2026-01-01T00:00:00Z"
    assert thread["latest_run"] == {
        "id": "run-new",
        "origin": "chat",
        "status": "failed",
        "created_at": "2026-01-01T00:01:00Z",
        "started_at": "2026-01-01T00:01:00Z",
        "finished_at": "2026-01-01T00:01:01Z",
        "updated_at": "2026-01-01T00:01:01Z",
    }
    assert thread["active_run"] is None
    assert thread["updated_at"] == "2026-01-01T00:01:01Z"


def test_threads_api_reports_active_run(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(toolang_root / "agents" / "alice" / "agent.too", "agent alice\n")
    context = _build_context(
        toolang_root=toolang_root,
        agent_name="alice",
        enabled_features=("inspect",),
    )
    project_run_start(context.store,
        run_id="run-active",
        thread_id="thread-1",
        origin="chat",
        input=Message.user("still running"),
        created_at="2026-01-01T00:00:00Z",
        started_at="2026-01-01T00:00:00Z",
    )
    app = _create_test_app(context)

    with TestClient(app) as client:
        thread = client.get("/api/v1/threads").json()["items"][0]
        detail = client.get("/api/v1/threads/thread-1").json()

    assert thread["active_run"] == {
        "id": "run-active",
        "origin": "chat",
        "status": "running",
        "created_at": "2026-01-01T00:00:00Z",
        "started_at": "2026-01-01T00:00:00Z",
        "finished_at": None,
        "updated_at": "2026-01-01T00:00:00Z",
    }
    assert thread["created_at"] == "2026-01-01T00:00:00Z"
    assert thread["channel"] == "terminal"
    assert thread["status"] == "running"
    assert detail["info"]["active_run"] == thread["active_run"]
    assert detail["event_cursor"] == 2


def test_run_events_api_returns_resource_scoped_events(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(toolang_root / "agents" / "alice" / "agent.too", "agent alice\n")
    context = _build_context(
        toolang_root=toolang_root,
        agent_name="alice",
        enabled_features=("inspect",),
    )
    project_run_start(context.store,
        run_id="run-1",
        thread_id="thread-1",
        origin="chat",
        input=Message.user("hello"),
        created_at="2026-01-01T00:00:00Z",
        started_at="2026-01-01T00:00:00Z",
    )
    context.store.append_event(
        domain="run",
        domain_id="run-1",
        type="part_end",
        payload={"run_id": "run-1", "thread_id": "thread-1", "step_index": 1},
    )
    app = _create_test_app(context)

    with TestClient(app) as client:
        response = client.get("/api/v1/runs/run-1/events?after=2").json()

    assert response["cursor"] == 3
    assert [item["type"] for item in response["items"]] == ["part_end"]
    assert response["items"][0]["cursor"] == 3


def test_persist_sink_projects_run_truth_and_events(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs.db")
    sink = PersistSink(store, agent_id="alice")

    sink.on_event(
        RunStarting(
            run="run-1",
            cmd=0,
            parent=None,
            thread="thread-1",
            input=Message.user("hello"),
            context={"origin": "chat"},
            created_at="2026-01-01T00:00:00Z",
        )
    )

    assert store.get_run(run_id="run-1") is not None
    assert [
        event.type for event in store.list_events(domain="run", domain_id="run-1")
    ] == ["run_starting"]
    store.close()


def test_agent_events_include_thread_updates_for_run_lifecycle(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(toolang_root / "agents" / "alice" / "agent.too", "agent alice\n")
    context = _build_context(
        toolang_root=toolang_root,
        agent_name="alice",
        enabled_features=("inspect",),
    )

    _emit_trace(
        context,
        RunStarting(
            run="run-1",
            cmd=0,
            parent=None,
            thread="thread-1",
            input=Message.user("hello"),
            context={"origin": "chat"},
            created_at="2026-01-01T00:00:00Z",
        ),
    )
    _emit_trace(
        context,
        RunBegin(
            run="run-1",
            input=InputRef(cmd=0),
            context={"origin": "chat"},
            started_at="2026-01-01T00:00:00Z",
        ),
    )
    _emit_trace(
        context,
        RunEnd(
            run="run-1",
            status="finished",
            finished_at="2026-01-01T00:00:01Z",
        ),
    )
    app = _create_test_app(context)

    with TestClient(app) as client:
        response = client.get("/api/v1/agent/events").json()

    assert response["cursor"] == 2
    assert [item["type"] for item in response["items"]] == [
        "thread_update",
        "thread_update",
    ]
    assert response["items"][0]["payload"]["run"] == "run-1"
    assert response["items"][0]["payload"]["status"] == "running"
    assert response["items"][1]["payload"]["status"] == "finished"


def test_run_starting_trace_precedes_run_begin(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(toolang_root / "agents" / "alice" / "agent.too", "agent alice\n")
    context = _build_context(
        toolang_root=toolang_root,
        agent_name="alice",
        enabled_features=("inspect",),
    )

    _emit_trace(
        context,
        RunStarting(
            run="run-1",
            cmd=0,
            parent=None,
            thread="thread-1",
            input=Message.user("hello"),
            context={"origin": "chat", "request_id": "req-start"},
            created_at="2026-01-01T00:00:00Z",
        ),
    )
    _emit_trace(
        context,
        RunBegin(
            run="run-1",
            input=InputRef(cmd=0),
            context={"origin": "chat", "request_id": "req-start"},
            started_at="2026-01-01T00:00:00Z",
        ),
    )

    events = context.store.list_events(domain="run", domain_id="run-1")

    assert [item.type for item in events] == ["run_starting", "run_begin"]
    assert [item.seq for item in events] == [1, 2]
    assert events[0].payload == {
        "run": "run-1",
        "cmd": 0,
        "parent": None,
        "thread": "thread-1",
        "input": {"role": "user", "parts": [{"type": "text", "text": "hello"}]},
        "context": {"origin": "chat", "request_id": "req-start"},
        "created_at": "2026-01-01T00:00:00Z",
    }


def test_steer_run_appends_run_steering_event(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(toolang_root / "agents" / "alice" / "agent.too", "agent alice\n")
    context = _build_context(
        toolang_root=toolang_root,
        agent_name="alice",
        enabled_features=("inspect",),
    )
    project_run_start(context.store,
        run_id="run-1",
        thread_id="thread-1",
        origin="chat",
        input=Message.user("hello"),
        created_at="2026-01-01T00:00:00Z",
        started_at="2026-01-01T00:00:00Z",
    )
    app = _create_test_app(context)

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/runs/run-1/steer",
            json={
                "request_id": "req-steer",
                "mode": "next_step",
                "message": {
                    "role": "user",
                    "parts": [{"type": "text", "text": "focus on events"}],
                },
            },
        ).json()
        events = client.get("/api/v1/runs/run-1/events").json()["items"]

    assert response["input"]["run"] == "run-1"
    assert response["input"]["type"] == "run_steering"
    assert response["input"]["cmd"] == 1
    assert response["input"]["kind"] == "steer"
    assert response["input"]["apply"] == "next_step"
    assert response["input"]["context"]["request_id"] == "req-steer"
    assert [item["type"] for item in events] == [
        "run_starting",
        "run_begin",
        "run_steering",
    ]
    assert events[2]["payload"]["cmd"] == 1
    assert events[2]["payload"]["context"]["request_id"] == "req-steer"
    assert events[2]["payload"]["input"]["parts"] == [
        {"type": "text", "text": "focus on events"}
    ]


def test_steer_run_event_precedes_consuming_step_event(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(toolang_root / "agents" / "alice" / "agent.too", "agent alice\n")
    context = _build_context(
        toolang_root=toolang_root,
        agent_name="alice",
        enabled_features=("inspect",),
    )
    project_run_start(context.store,
        run_id="run-1",
        thread_id="thread-1",
        origin="chat",
        input=Message.user("hello"),
        created_at="2026-01-01T00:00:00Z",
        started_at="2026-01-01T00:00:00Z",
    )
    app = _create_test_app(context)

    with TestClient(app) as client:
        client.post(
            "/api/v1/runs/run-1/steer",
            json={
                "request_id": "req-steer",
                "mode": "next_step",
                "message": {
                    "role": "user",
                    "parts": [{"type": "text", "text": "focus on events"}],
                },
            },
        )
        _emit_trace(
            context,
            StepBegin(
                step="run-1/2",
                kind="model",
                input=(OutputRef(step="run-1/1"), InputRef(cmd=1)),
                started_at="2026-01-01T00:00:01Z",
                context={"instruct": "call prompt", "prompt_context": "call context"},
            ),
        )
        events = client.get("/api/v1/threads/thread-1/events").json()["items"]

    assert [item["type"] for item in events] == [
        "run_starting",
        "run_begin",
        "run_steering",
        "step_begin",
    ]
    assert [item["cursor"] for item in events] == [1, 2, 3, 4]
    assert events[2]["payload"]["cmd"] == 1
    assert events[3]["payload"]["input"] == [
        {"step": "run-1/1"},
        {"cmd": 1},
    ]
    assert events[3]["payload"]["context"] == {
        "instruct": "call prompt",
        "prompt_context": "call context",
    }


def test_run_detail_preserves_step_input_ref_kinds(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(toolang_root / "agents" / "alice" / "agent.too", "agent alice\n")
    context = _build_context(
        toolang_root=toolang_root,
        agent_name="alice",
        enabled_features=("inspect",),
    )
    project_run_start(context.store,
        run_id="run-1",
        thread_id="thread-1",
        origin="chat",
        input=Message.user("hello"),
        created_at="2026-01-01T00:00:00Z",
        started_at="2026-01-01T00:00:00Z",
    )
    project_command(context.store,
        run_id="run-1",
        kind="steer",
        apply="next_step",
        request_id="req-steer",
        input=Message.user("focus on events"),
        created_at="2026-01-01T00:00:01Z",
    )
    instruct_hash = context.store.put_prompt(body="model instructions")
    context_hash = context.store.put_prompt(body="model context")
    project_step(context.store,
        run_id="run-1",
        step_index=2,
        kind="model",
        status="finished",
        input=(_output_ref("run-1", 1), InputRef(cmd=1)),
        output=(TextPart(text="ok"),),
        detail={"model_ref": "gpt-5", "usage": {"input_tokens": 0, "output_tokens": 0}},
        context={"instruct": instruct_hash, "prompt_context": context_hash},
        started_at="2026-01-01T00:00:02Z",
        finished_at="2026-01-01T00:00:03Z",
    )
    app = _create_test_app(context)

    with TestClient(app) as client:
        detail = client.get("/api/v1/runs/run-1").json()

    assert detail["output"]["steps"][0]["record"]["input"] == [
        {"step": "run-1/1"},
        {"cmd": 1},
    ]
    assert detail["output"]["steps"][0]["record"]["context"] == {
        "instruct": instruct_hash,
        "prompt_context": context_hash,
    }
    assert detail["prompts"] == {
        instruct_hash: "model instructions",
        context_hash: "model context",
    }


def test_basic_loop_continues_when_steer_arrives_before_finish() -> None:
    class FakeContext:
        instructions = ""
        messages = ()
        model = None
        tools = {}

        def __init__(self) -> None:
            self.model_calls = 0
            self.pending_checks = 0

        def call_model(self) -> ModelCallResult:
            self.model_calls += 1
            return ModelCallResult(
                message=Message.assistant(f"answer {self.model_calls}")
            )

        def call_tool(self, call):
            raise AssertionError(f"unexpected tool call: {call}")

        def call_tools(self, calls):
            raise AssertionError(f"unexpected tool calls: {calls}")

        def has_pending_inputs(self) -> bool:
            self.pending_checks += 1
            return self.pending_checks == 1

        def finish(self) -> RunResult:
            return RunResult(output_text=f"finished after {self.model_calls}")

    context = FakeContext()

    result = BasicLoop().run(cast(Any, context))

    assert context.model_calls == 2
    assert result.output_text == "finished after 2"


def test_trace_events_after_run_cancel_are_ignored(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(toolang_root / "agents" / "alice" / "agent.too", "agent alice\n")
    context = _build_context(
        toolang_root=toolang_root,
        agent_name="alice",
        enabled_features=("inspect",),
    )
    _emit_trace(
        context,
        RunStarting(
            run="run-1",
            cmd=0,
            parent=None,
            thread="thread-1",
            input=Message.user("hello"),
            context={"origin": "chat"},
            created_at="2026-01-01T00:00:00Z",
        ),
    )
    _emit_trace(
        context,
        RunBegin(
            run="run-1",
            input=InputRef(cmd=0),
            context={"origin": "chat"},
            started_at="2026-01-01T00:00:00Z",
        ),
    )
    project_run_end(
        context.store,
        run_id="run-1",
        status="canceled",
        error="User stopped the run.",
    )
    executor = run_executor_module.Executor(
        root=context.root,
        name=context.name,
        home=context.home,
        id_state_path=context.executor.id_state_path,
        setup=context.executor.setup,
        store=context.store,
        model_aliases=context.executor.model_aliases,
        default_models=context.executor.default_models,
        model_environ=context.executor.model_environ,
        config=context.config,
    )
    emit = executor._handler(None)
    emit(
        _started(1, run_id="run-1", thread_id="thread-1", kind="model"),
    )
    emit(
        _completed(
            1,
            run_id="run-1",
            thread_id="thread-1",
            kind="model",
            output=(TextPart(text="late output"),),
        ),
    )
    emit(
        RunEnd(
            run="run-1",
            status="canceled",
            error="User stopped the run.",
            finished_at="2026-01-01T00:00:02Z",
        ),
    )

    assert context.store.list_steps(run_id="run-1") == []
    assert [
        item.type
        for item in context.store.list_events(domain="thread", domain_id="thread-1")
    ] == ["run_starting", "run_begin", "run_end"]


def test_runtime_start_restores_ignored_termination_signals(monkeypatch) -> None:
    calls: list[tuple[int, signal.Handlers]] = []

    def fake_getsignal(signum: int) -> signal.Handlers:
        if signum in {signal.SIGTERM, signal.SIGINT}:
            return signal.SIG_IGN
        return signal.SIG_DFL

    def fake_signal(signum: int, handler: signal.Handlers) -> signal.Handlers:
        calls.append((signum, handler))
        return signal.SIG_IGN

    monkeypatch.setattr(up_module.signal, "getsignal", fake_getsignal)
    monkeypatch.setattr(up_module.signal, "signal", fake_signal)

    up_module._restore_termination_signal_defaults()

    assert calls == [
        (signal.SIGTERM, signal.SIG_DFL),
        (signal.SIGINT, signal.SIG_DFL),
    ]


def test_runtime_shutdown_cancels_stuck_tasks() -> None:
    async def run_test() -> None:
        cancelled = asyncio.Event()

        async def stuck() -> None:
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        task = asyncio.create_task(stuck())

        await up_module._finish_runtime_tasks([task], timeout_sec=0.01)

        assert task.cancelled()
        assert cancelled.is_set()

    asyncio.run(run_test())


def test_agent_events_include_cap_updates(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(toolang_root / "agents" / "alice" / "agent.too", "agent alice\n")
    context = _build_context(
        toolang_root=toolang_root,
        agent_name="alice",
        enabled_features=("manage", "inspect"),
    )
    app = _create_test_app(context)

    with TestClient(app) as client:
        put_response = client.put(
            "/api/v1/psyches/reviewer/file",
            json={"visibility": "private", "content": "Prefer direct answers."},
        )
        response = client.get("/api/v1/agent/events").json()

    assert put_response.status_code == 200
    assert response["cursor"] == 1
    assert [item["type"] for item in response["items"]] == ["psyche_changed"]
    assert response["items"][0]["payload"] == {
        "name": "reviewer",
        "visibility": "private",
    }


def test_chat_api_allocates_web_threads_by_default_and_rejects_unknown_thread_ids(
    tmp_path: Path,
) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(toolang_root / "agents" / "alice" / "agent.too", "agent alice\n")
    context = _build_context(
        toolang_root=toolang_root,
        agent_name="alice",
        enabled_features=("chat", "inspect"),
    )
    app = _create_test_app(context)

    with _patched_executor_execution():
        with TestClient(app) as client:
            rejected = client.post(
                "/api/v1/chat",
                json={
                    "thread": "web-client-created",
                    "message": _chat_message("hello"),
                },
            )
            first = client.post(
                "/api/v1/chat",
                json={"message": _chat_message("hello")},
            )
            thread_id = first.json()["thread_id"]
            second = client.post(
                "/api/v1/chat",
                json={"thread": thread_id, "message": _chat_message("again")},
            )
            thread = client.get(f"/api/v1/threads/{thread_id}").json()["info"]

    assert rejected.status_code == 404
    assert rejected.json()["detail"] == "chat thread not found: web-client-created"
    assert first.status_code == 200
    assert thread_id.startswith("web_")
    assert len(thread_id) == len("web_") + 8
    assert second.status_code == 200
    assert second.json()["thread_id"] == thread_id
    assert thread["run_count"] == 2


def test_chat_api_allocates_term_threads_for_term_client(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(toolang_root / "agents" / "alice" / "agent.too", "agent alice\n")
    context = _build_context(
        toolang_root=toolang_root,
        agent_name="alice",
        enabled_features=("chat", "inspect"),
    )
    app = _create_test_app(context)

    with _patched_executor_execution():
        with TestClient(app) as client:
            response = client.post(
                "/api/v1/chat",
                json={"client": "tui", "message": _chat_message("hello")},
            )
            thread_id = response.json()["thread_id"]

    assert response.status_code == 200
    assert thread_id.startswith("term_")
    assert len(thread_id) == len("term_") + 8


def test_chat_api_creates_empty_terminal_threads(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(toolang_root / "agents" / "alice" / "agent.too", "agent alice\n")
    context = _build_context(
        toolang_root=toolang_root,
        agent_name="alice",
        enabled_features=("chat", "inspect"),
    )
    app = _create_test_app(context)

    with TestClient(app) as client:
        response = client.post("/api/v1/threads", json={"client": "tui"})
        body = response.json()
        thread = client.get(f"/api/v1/threads/{body['thread_id']}").json()["info"]

    assert response.status_code == 200
    assert body["thread_id"].startswith("term_")
    assert body["thread"]["origin"] == "chat"
    assert body["thread"]["channel"] == "terminal"
    assert body["thread"]["status"] == "idle"
    assert body["thread"]["run_count"] == 0
    assert thread == body["thread"]


def test_chat_rewind_supersedes_previous_run_in_thread_projection(
    tmp_path: Path,
) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(toolang_root / "agents" / "alice" / "agent.too", "agent alice\n")
    context = _build_context(
        toolang_root=toolang_root,
        agent_name="alice",
        enabled_features=("chat", "inspect"),
    )
    app = _create_test_app(context)

    with _patched_executor_execution():
        with TestClient(app) as client:
            first = client.post(
                "/api/v1/chat",
                json={"message": _chat_message("first input")},
            )
            old_run_id = first.json()["run_id"]
            thread_id = first.json()["thread_id"]
            rewind = client.post(
                f"/api/v1/runs/{old_run_id}/rewind",
                json={"message": _chat_message("replacement input")},
            )
            new_run_id = rewind.json()["run_id"]

            for _ in range(100):
                thread_detail = client.get(f"/api/v1/threads/{thread_id}").json()
                if [item["info"]["id"] for item in thread_detail["runs"]] == [
                    new_run_id
                ]:
                    break
                time.sleep(0.01)
            old_detail = client.get(f"/api/v1/runs/{old_run_id}").json()
            runs = client.get(f"/api/v1/runs?thread_id={thread_id}").json()["items"]

    assert first.status_code == 200
    assert rewind.status_code == 200
    assert old_detail["info"]["superseded"] == {
        "type": "rewound",
        "by": new_run_id,
        "from_run_id": old_run_id,
    }
    assert [item["info"]["id"] for item in thread_detail["runs"]] == [new_run_id]
    assert thread_detail["runs"][0]["input"]["parts"][0]["text"] == "replacement input"
    assert thread_detail["info"]["run_count"] == 1
    assert [item["id"] for item in runs] == [new_run_id]


def test_chat_rewind_cancels_running_run_before_superseding(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(toolang_root / "agents" / "alice" / "agent.too", "agent alice\n")
    context = _build_context(
        toolang_root=toolang_root,
        agent_name="alice",
        enabled_features=("chat", "inspect"),
    )
    project_run_start(context.store,
        run_id="run_running",
        thread_id="term_running",
        origin="chat",
        input=Message.user("original input"),
    )
    app = _create_test_app(context)

    with _patched_executor_execution():
        with TestClient(app) as client:
            rewind = client.post(
                "/api/v1/runs/run_running/rewind",
                json={"message": _chat_message("replacement input")},
            )
            new_run_id = rewind.json()["run_id"]

            for _ in range(100):
                thread_detail = client.get("/api/v1/threads/term_running").json()
                if [item["info"]["id"] for item in thread_detail["runs"]] == [
                    new_run_id
                ]:
                    break
                time.sleep(0.01)
            old_detail = client.get("/api/v1/runs/run_running").json()

    assert rewind.status_code == 200
    assert old_detail["output"]["status"] == "canceled"
    assert old_detail["output"]["error"] == "Run was rewound."
    assert old_detail["info"]["superseded"] == {
        "type": "rewound",
        "by": new_run_id,
        "from_run_id": "run_running",
    }
    assert [item["info"]["id"] for item in thread_detail["runs"]] == [new_run_id]


def test_chat_fork_copies_prior_runs_into_new_thread(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(toolang_root / "agents" / "alice" / "agent.too", "agent alice\n")
    context = _build_context(
        toolang_root=toolang_root,
        agent_name="alice",
        enabled_features=("chat", "inspect"),
    )
    app = _create_test_app(context)

    with _patched_executor_execution():
        with TestClient(app) as client:
            first = client.post(
                "/api/v1/chat",
                json={"message": _chat_message("first input")},
            )
            source_thread_id = first.json()["thread_id"]
            first_run_id = first.json()["run_id"]
            second = client.post(
                "/api/v1/chat",
                json={
                    "thread": source_thread_id,
                    "message": _chat_message("second input"),
                },
            )
            fork = client.post(
                f"/api/v1/runs/{second.json()['run_id']}/fork",
                json={"message": _chat_message("fork input")},
            )
            fork_thread_id = fork.json()["thread_id"]
            fork_run_id = fork.json()["run_id"]

            for _ in range(100):
                thread_detail = client.get(f"/api/v1/threads/{fork_thread_id}").json()
                run_ids = [item["info"]["id"] for item in thread_detail["runs"]]
                if run_ids[-1:] == [fork_run_id] and len(run_ids) == 2:
                    break
                time.sleep(0.01)
            copied_run_id = fork.json()["copied_run_ids"][0]
            copied_detail = client.get(f"/api/v1/runs/{copied_run_id}").json()

    assert first.status_code == 200
    assert second.status_code == 200
    assert fork.status_code == 200
    assert fork.json()["source_thread_id"] == source_thread_id
    assert fork.json()["from_run_id"] == second.json()["run_id"]
    assert fork.json()["include_anchor"] is False
    assert fork.json()["copied_run_ids"] == [copied_run_id]
    assert copied_run_id != first_run_id
    assert copied_detail["info"]["thread_id"] == fork_thread_id
    assert copied_detail["input"]["parts"][0]["text"] == "first input"
    assert [item["input"]["parts"][0]["text"] for item in thread_detail["runs"]] == [
        "first input",
        "fork input",
    ]
    assert thread_detail["info"]["run_count"] == 2


def test_chat_fork_can_include_anchor_run_in_new_thread(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(toolang_root / "agents" / "alice" / "agent.too", "agent alice\n")
    context = _build_context(
        toolang_root=toolang_root,
        agent_name="alice",
        enabled_features=("chat", "inspect"),
    )
    app = _create_test_app(context)

    with _patched_executor_execution():
        with TestClient(app) as client:
            first = client.post(
                "/api/v1/chat",
                json={"message": _chat_message("first input")},
            )
            source_thread_id = first.json()["thread_id"]
            second = client.post(
                "/api/v1/chat",
                json={
                    "thread": source_thread_id,
                    "message": _chat_message("second input"),
                },
            )
            fork = client.post(
                f"/api/v1/runs/{second.json()['run_id']}/fork",
                json={
                    "include_anchor": True,
                    "message": _chat_message("fork input"),
                },
            )
            fork_thread_id = fork.json()["thread_id"]
            fork_run_id = fork.json()["run_id"]

            for _ in range(100):
                thread_detail = client.get(f"/api/v1/threads/{fork_thread_id}").json()
                run_ids = [item["info"]["id"] for item in thread_detail["runs"]]
                if run_ids[-1:] == [fork_run_id] and len(run_ids) == 3:
                    break
                time.sleep(0.01)

    assert first.status_code == 200
    assert second.status_code == 200
    assert fork.status_code == 200
    assert fork.json()["source_thread_id"] == source_thread_id
    assert fork.json()["from_run_id"] == second.json()["run_id"]
    assert fork.json()["include_anchor"] is True
    assert len(fork.json()["copied_run_ids"]) == 2
    assert [item["input"]["parts"][0]["text"] for item in thread_detail["runs"]] == [
        "first input",
        "second input",
        "fork input",
    ]
    assert thread_detail["info"]["run_count"] == 3


def test_chat_api_records_peer_for_new_thread_and_rejects_mismatch(
    tmp_path: Path,
) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(toolang_root / "agents" / "bob" / "agent.too", "agent bob\n")
    context = _build_context(
        toolang_root=toolang_root,
        agent_name="bob",
        enabled_features=("chat", "inspect"),
    )
    app = _create_test_app(context)

    with _patched_executor_execution():
        with TestClient(app) as client:
            first = client.post(
                "/api/v1/chat",
                json={
                    "peer": {"type": "agent", "name": "alice", "thread": "term_a"},
                    "message": _chat_message("Alice asks Bob"),
                },
            )
            thread_id = first.json()["thread_id"]
            accepted = client.post(
                "/api/v1/chat",
                json={
                    "thread": thread_id,
                    "peer": {"type": "agent", "name": "alice", "thread": "term_a"},
                    "message": _chat_message("follow up"),
                },
            )
            rejected = client.post(
                "/api/v1/chat",
                json={
                    "thread": thread_id,
                    "peer": {"type": "agent", "name": "carol", "thread": "term_c"},
                    "message": _chat_message("wrong peer"),
                },
            )
            thread = client.get(f"/api/v1/threads/{thread_id}").json()["info"]

    assert first.status_code == 200
    assert first.json()["thread"]["peer"] == {
        "type": "agent",
        "name": "alice",
        "thread": "term_a",
    }
    assert accepted.status_code == 200
    assert rejected.status_code == 409
    assert thread["peer"] == {"type": "agent", "name": "alice", "thread": "term_a"}
    assert thread["parent"] is None


def test_chat_models_lists_effective_selectors_for_term_agic(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(
        toolang_root / "agents" / "alice" / "agent.too",
        "agent alice\n\nagic chat:\n  models = openai/gpt-5, openai/o3\n\n  Reply directly.\n",
    )
    context = _build_context(
        toolang_root=toolang_root,
        agent_name="alice",
        enabled_features=("chat", "inspect"),
    )
    context.executor.model_environ = {"OPENAI_API_KEY": "secret"}
    context.config.set(
        "models.allowed_selectors", ("openai/o3[openai]", "openai/gpt-5[openai]")
    )
    app = _create_test_app(context)

    with TestClient(app) as client:
        response = client.get("/api/v1/chat/models")

    assert response.status_code == 200
    body = response.json()
    assert body == {
        "default": "openai/o3[openai]",
        "items": [
            {
                "selector": "openai/o3[openai]",
                "name": "o3",
                "ref": "openai/o3",
                "provider": "openai",
                "model": "o3",
                "adapter": "responses",
                "tools": True,
                "streaming": True,
            },
            {
                "selector": "openai/gpt-5[openai]",
                "name": "gpt-5",
                "ref": "openai/gpt-5",
                "provider": "openai",
                "model": "gpt-5",
                "adapter": "responses",
                "tools": True,
                "streaming": True,
            },
        ],
    }


def test_chat_models_returns_all_discoverable_when_unrestricted(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(toolang_root / "agents" / "alice" / "agent.too", "agent alice\n")
    context = _build_context(
        toolang_root=toolang_root,
        agent_name="alice",
        enabled_features=("chat", "inspect"),
    )
    context.executor.model_environ = {"OPENAI_API_KEY": "secret"}
    app = _create_test_app(context)

    with TestClient(app) as client:
        response = client.get("/api/v1/chat/models")

    assert response.status_code == 200
    body = response.json()
    assert body["default"] == "openai/gpt-5[openai]"
    assert body["items"][0] == {
        "selector": "openai/gpt-5[openai]",
        "name": "gpt-5",
        "ref": "openai/gpt-5",
        "provider": "openai",
        "model": "gpt-5",
        "adapter": "responses",
        "tools": True,
        "streaming": True,
    }
    items_by_ref = {item["ref"]: item for item in body["items"]}
    assert items_by_ref["openai/gpt-5.5"]["model"] == "gpt-5.5"
    assert items_by_ref["openai/o3"]["model"] == "o3"


def test_chat_executable_endpoints_list_agics_and_flows(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(
        toolang_root / "agents" / "alice" / "agent.too",
        (
            "agent alice\n\n"
            "agic chat:\n"
            "  Reply directly.\n\n"
            "agic summarize:\n"
            "  Summarize it.\n\n"
            "flow review:\n"
            "  run chat\n"
        ),
    )
    context = _build_context(
        toolang_root=toolang_root,
        agent_name="alice",
        enabled_features=("chat", "inspect"),
    )
    app = _create_test_app(context)

    with TestClient(app) as client:
        agics = client.get("/api/v1/chat/agics")
        flows = client.get("/api/v1/chat/flows")

    assert agics.status_code == 200
    assert agics.json() == {
        "default": "chat",
        "items": [{"name": "chat"}, {"name": "summarize"}, {"name": "default"}],
    }
    assert flows.status_code == 200
    assert flows.json() == {
        "default": None,
        "items": [{"name": "review"}],
    }


def test_chat_request_passes_selected_agic_and_flow_to_executor(
    tmp_path: Path, monkeypatch
) -> None:
    async def run_test() -> None:
        toolang_root = tmp_path / "toolang"
        _write_text(
            toolang_root / "agents" / "alice" / "agent.too",
            (
                "agent alice\n\n"
                "agic chat:\n"
                "  Reply directly.\n\n"
                "agic summarize:\n"
                "  Summarize it.\n\n"
                "flow review:\n"
                "  run chat\n"
            ),
        )
        context = _build_context(
            toolang_root=toolang_root,
            agent_name="alice",
            enabled_features=("chat", "inspect"),
        )
        captured: list[RunRequest] = []

        async def fake_run(
            request: RunRequest,
            state,
            *,
            reply: object | None = None,
        ) -> RunRecord:
            del reply, state
            captured.append(request)
            return RunRecord(
                id=request.run_id or "run_test",
                parent=None,
                thread=request.thread_id or "term_test",
                input=InputRef(cmd=0),
                output=None,
                status="finished",
            )

        monkeypatch.setattr(context.executor, "run", fake_run)
        message = chat_loop.ChatMessagePayload(
            role="user",
            parts=[{"type": "text", "text": "hello"}],
            meta={},
        )

        await chat_loop._submit_chat_run(
            context,
            chat_loop.ChatRequest(message=message, agic="summarize"),
            thread_id=None,
        )
        await chat_loop._submit_chat_run(
            context,
            chat_loop.ChatRequest(message=message, flow="review"),
            thread_id=None,
        )

        assert captured[0].executable_name == "summarize"
        assert captured[0].executable_kind == "agic"
        assert captured[1].executable_name == "review"
        assert captured[1].executable_kind == "flow"

    asyncio.run(run_test())


def test_profile_reports_activity_metrics(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(toolang_root / "agents" / "alice" / "agent.too", "agent alice\n")
    context = _build_context(
        toolang_root=toolang_root,
        agent_name="alice",
        enabled_features=("inspect",),
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

    term_run = project_run_start(store,
        run_id="run-chat",
        thread_id="thread-chat",
        origin="chat",
        input=Message.user("list tools"),
    )
    project_step(store,
        run_id=term_run.run_id,
        step_index=1,
        kind="model",
        status="finished",
        input=(InputRef(),),
        output=(
            ToolCallPart(
                tool_call_id="call-1",
                tool_name="shell",
                tool_family="shell",
                input={"command": "pwd"},
            ),
        ),
        detail=_model_detail(input_tokens=11, output_tokens=7),
        started_at="2026-01-01T00:00:01Z",
        finished_at="2026-01-01T00:00:02Z",
    )
    project_step(store,
        run_id=term_run.run_id,
        step_index=2,
        kind="tool",
        status="finished",
        input=(_output_ref(term_run.run_id, 1, 0),),
        output=(
            ToolResultPart(
                tool_call_id="call-1",
                tool_name="shell",
                tool_family="shell",
                output={"cwd": "/tmp"},
            ),
        ),
        detail={"tool": "shell"},
        started_at="2026-01-01T00:00:03Z",
        finished_at="2026-01-01T00:00:04Z",
    )
    project_step(store,
        run_id=term_run.run_id,
        step_index=3,
        kind="model",
        status="finished",
        input=(InputRef(), _output_ref(term_run.run_id, 2)),
        output=(TextPart(text="done"),),
        detail=_model_detail(input_tokens=3, output_tokens=5),
        started_at="2026-01-01T00:00:05Z",
        finished_at="2026-01-01T00:00:06Z",
    )
    project_run_end(store, run_id=term_run.run_id)

    task_run = project_run_start(store,
        run_id="run-task",
        thread_id="task_task-1",
        origin="task",
        input=Message.user("do the task"),
    )
    project_run_end(store, run_id=task_run.run_id)

    chore_run = project_run_start(store,
        run_id="run-chore",
        thread_id="chore_daily-sync",
        origin="chore",
        input=Message.user("run the chore"),
    )
    project_run_end(store, run_id=chore_run.run_id)

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
            "steps": {"total": 3, "model": 2, "tool": 1, "system": 0},
            "tokens": {"input": 14, "output": 12, "total": 26},
        }


def test_create_app_allows_webui_cors_origin(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(toolang_root / "agents" / "alice" / "agent.too", "agent alice\n")
    context = _build_context(
        toolang_root=toolang_root,
        agent_name="alice",
        enabled_features=("inspect",),
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
    _write_text(toolang_root / "agents" / "alice" / "agent.too", "agent alice\n")
    context = _build_context(
        toolang_root=toolang_root,
        agent_name="alice",
        enabled_features=("chat", "inspect"),
    )
    app = _create_test_app(context)

    with _patched_executor_failure("model boom"):
        with TestClient(app) as client:
            response = client.post(
                "/api/v1/chat",
                json={"message": _chat_message("say hello")},
            )
            runs = client.get("/api/v1/runs").json()["items"]
            run_detail = client.get(f"/api/v1/runs/{runs[0]['id']}").json()

    assert response.status_code == 200
    body = response.json()
    assert body["message"]["parts"][0]["text"] == "say hello"
    assert body["assistant"]["parts"][0]["text"] == "model boom"
    assert runs[0]["status"] == "failed"
    assert runs[0]["summary"] == "model boom"
    assert runs[0]["failure"] == {
        "reason": "model boom",
        "step_index": 0,
        "step_kind": "system",
        "step_error": "model boom",
    }
    assert run_detail["output"]["steps"] == [
        {
            "record": {
                "parent": runs[0]["id"],
                "index": 0,
                "path": f"{runs[0]['id']}/0",
                "kind": "system",
                "status": "failed",
                "input": [],
                "output": [{"type": "text", "text": "model boom"}],
                "context": {},
                "detail": {},
                "started_at": run_detail["info"]["finished_at"],
                "finished_at": run_detail["info"]["finished_at"],
                "error": "model boom",
            },
            "message": None,
        }
    ]


def test_runs_api_surfaces_failure_reason_when_summary_is_empty(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(toolang_root / "agents" / "alice" / "agent.too", "agent alice\n")
    context = _build_context(
        toolang_root=toolang_root,
        agent_name="alice",
        enabled_features=("inspect",),
    )
    project_run_start(context.store,
        run_id="run-loop",
        thread_id="chore_sync",
        origin="chore",
        input=Message.user("sync remote tasks"),
    )
    project_step(context.store,
        run_id="run-loop",
        step_index=1,
        kind="tool",
        status="finished",
        input=(InputRef(),),
        output=(
            ToolResultPart(
                tool_call_id="call-1",
                call_id="call-1",
                tool_name="service_use__tool_call",
                tool_family="service_use__tool_call",
                output={"ok": True},
            ),
        ),
        detail={"tool": "service_use__tool_call"},
        started_at="2026-01-01T00:00:01Z",
        finished_at="2026-01-01T00:00:02Z",
    )
    project_run_end(context.store,
        run_id="run-loop",
        status="failed",
        error="Model tool loop exceeded the maximum number of rounds.",
        finished_at="2026-01-01T00:00:03Z",
    )
    app = _create_test_app(context)

    with TestClient(app) as client:
        run_item = client.get("/api/v1/runs").json()["items"][0]
        detail = client.get("/api/v1/runs/run-loop").json()

    assert run_item["status"] == "failed"
    assert (
        run_item["summary"] == "Model tool loop exceeded the maximum number of rounds."
    )
    assert run_item["failure"] == {
        "reason": "Model tool loop exceeded the maximum number of rounds.",
        "step_index": 2,
        "step_kind": "system",
        "step_error": "Model tool loop exceeded the maximum number of rounds.",
    }
    assert (
        detail["output"]["error"]
        == "Model tool loop exceeded the maximum number of rounds."
    )
    assert detail["output"]["failure"] == {
        "reason": "Model tool loop exceeded the maximum number of rounds.",
        "step_index": 2,
        "step_kind": "system",
        "step_error": "Model tool loop exceeded the maximum number of rounds.",
    }
    assert detail["output"]["steps"][-1] == {
        "record": {
            "parent": "run-loop",
            "index": 2,
            "path": "run-loop/2",
            "kind": "system",
            "status": "failed",
            "input": [],
            "output": [
                {
                    "type": "text",
                    "text": "Model tool loop exceeded the maximum number of rounds.",
                }
            ],
            "context": {},
            "detail": {},
            "started_at": "2026-01-01T00:00:03Z",
            "finished_at": "2026-01-01T00:00:03Z",
            "error": "Model tool loop exceeded the maximum number of rounds.",
        },
        "message": None,
    }


def test_chat_projects_tool_parts_from_tool_call_steps(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(toolang_root / "agents" / "alice" / "agent.too", "agent alice\n")
    context = _build_context(
        toolang_root=toolang_root,
        agent_name="alice",
        enabled_features=("chat", "inspect"),
    )
    app = _create_test_app(context)

    with _patched_executor_execution_with_tools(output_text="assistant:tool me"):
        with TestClient(app) as client:
            response = client.post(
                "/api/v1/chat",
                json={"message": _chat_message("tool me")},
            )

    assert response.status_code == 200
    body = response.json()
    assistant_parts = body["assistant"]["parts"]
    assert assistant_parts == [{"type": "text", "text": "assistant:tool me"}]


def test_chat_stream_emits_tool_and_text_chunks(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(toolang_root / "agents" / "alice" / "agent.too", "agent alice\n")
    context = _build_context(
        toolang_root=toolang_root,
        agent_name="alice",
        enabled_features=("chat", "inspect"),
    )
    app = _create_test_app(context)

    with _patched_executor_execution_with_tools(output_text="assistant:tool me"):
        with TestClient(app) as client:
            with client.stream(
                "POST",
                "/api/v1/chat/stream",
                json={"message": _chat_message("tool me")},
            ) as response:
                assert response.status_code == 200
                stream_text = "".join(
                    chunk.decode("utf-8") for chunk in response.iter_raw()
                )

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
    assert (
        '"providerMetadata":{"toolang":{"toolFamily":"math_add","toolName":"math_add"}}'
        in stream_text
    )
    assert '"type":"text-delta"' in stream_text
    assert '"delta":"assistant:tool me"' in stream_text
    assert '"type":"finish-step"' in stream_text
    assert '"type":"finish"' in stream_text
    assert "data: [DONE]" in stream_text


def test_chat_stream_tui_client_emits_trace_events(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(toolang_root / "agents" / "alice" / "agent.too", "agent alice\n")
    context = _build_context(
        toolang_root=toolang_root,
        agent_name="alice",
        enabled_features=("chat", "inspect"),
    )
    app = _create_test_app(context)

    with _patched_executor_execution_with_tools(output_text="assistant:tool me"):
        with TestClient(app) as client:
            with client.stream(
                "POST",
                "/api/v1/chat/stream",
                json={"client": "tui", "message": _chat_message("tool me")},
            ) as response:
                assert response.status_code == 200
                stream_text = "".join(
                    chunk.decode("utf-8") for chunk in response.iter_raw()
                )

    assert '"type":"run_starting"' in stream_text
    assert '"type":"run_begin"' in stream_text
    assert '"type":"step_begin"' in stream_text
    assert '"type":"part_delta"' in stream_text
    assert '"type":"part_end"' in stream_text
    assert '"type":"step_end"' in stream_text
    assert '"type":"run_end"' in stream_text
    assert '"type":"text-delta"' not in stream_text
    assert '"type":"finish"' not in stream_text
    assert "data: [DONE]" in stream_text


def test_chat_stream_emits_before_run_completion(tmp_path: Path) -> None:
    async def run_test() -> None:
        toolang_root = tmp_path / "toolang"
        _write_text(toolang_root / "agents" / "alice" / "agent.too", "agent alice\n")
        context = _build_context(
            toolang_root=toolang_root,
            agent_name="alice",
            enabled_features=("chat", "inspect"),
        )

        release = threading.Event()
        timer = threading.Timer(1.0, release.set)
        timer.start()
        try:
            with _patched_executor_streaming_text(release):
                async with _running_context(
                    context, enabled_features=("chat", "inspect")
                ):
                    stream = chat_loop._stream_chat_run(
                        context,
                        chat_loop.ChatRequest(
                            message=chat_loop.ChatMessagePayload(
                                role="user",
                                parts=[{"type": "text", "text": "stream me"}],
                                meta={},
                            ),
                        ),
                        thread_id=None,
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


def test_chat_default_executable_uses_default_agic_not_single_flow(
    tmp_path: Path,
) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(
        toolang_root / "agents" / "alice" / "agent.too",
        """
agic expand(in: Part[]):
  Expand.

agic search(in: Part[]):
  Search.

flow research(in: Text):
  run expand
""",
    )
    context = _build_context(
        toolang_root=toolang_root,
        agent_name="alice",
        enabled_features=("chat",),
    )
    bound = bind_run_request(
        context,
        RunRequest(group="chat", origin="chat", message=Message.user("hello")),
        state=context.get_agent_state(),
    )

    executable = run_executor_module._resolve_executable(bound)

    assert executable.name == "default"


def test_chat_stream_executes_flow_runnable(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(
        toolang_root / "agents" / "alice" / "agent.too",
        """
agic expand(in: Part[]):
  Expand.

agic search(in: Part[]):
  Search.

flow research(in: Text):
  run expand
""",
    )
    context = _build_context(
        toolang_root=toolang_root,
        agent_name="alice",
        enabled_features=("chat", "inspect"),
    )
    app = _create_test_app(context)

    with _patched_executor_execution():
        with TestClient(app) as client:
            with client.stream(
                "POST",
                "/api/v1/chat/stream",
                json={"flow": "research", "message": _chat_message("flow me")},
            ) as response:
                assert response.status_code == 200
                stream_text = "".join(
                    chunk.decode("utf-8") for chunk in response.iter_raw()
                )

    assert stream_text.count('"type":"start"') == 1
    assert stream_text.count('"type":"message-metadata"') == 1
    assert '"type":"error"' not in stream_text
    assert '"type":"run_starting"' in stream_text
    assert '"type":"finish"' in stream_text
    assert "data: [DONE]" in stream_text


def test_chat_stream_pre_start_failure_emits_error_and_done(tmp_path: Path) -> None:
    async def run_test() -> None:
        toolang_root = tmp_path / "toolang"
        _write_text(
            toolang_root / "agents" / "alice" / "agent.too",
            """
agic expand(in: Part[]):
  Expand.

agic search(in: Part[]):
  Search.
""",
        )
        context = _build_context(
            toolang_root=toolang_root,
            agent_name="alice",
            enabled_features=("chat", "inspect"),
        )
        async with _running_context(context, enabled_features=("chat", "inspect")):
            stream = chat_loop._stream_chat_run(
                context,
                chat_loop.ChatRequest(
                    agic="missing",
                    message=chat_loop.ChatMessagePayload(
                        role="user",
                        parts=[{"type": "text", "text": "stream me"}],
                        meta={},
                    ),
                ),
                thread_id=None,
            )
            stream_text = "".join([chunk async for chunk in stream])

        assert '"type":"error"' in stream_text
        assert "Agic not found: missing" in stream_text
        assert '"type":"finish"' in stream_text
        assert "data: [DONE]" in stream_text

    asyncio.run(run_test())


def test_chat_guarded_stream_swallows_cancelled_error() -> None:
    async def _run() -> list[str]:
        async def broken():
            raise asyncio.CancelledError()
            yield ""

        return [chunk async for chunk in chat_loop._guarded_stream(broken())]

    assert asyncio.run(_run()) == []


def test_inspect_guarded_stream_swallows_cancelled_error() -> None:
    async def _run() -> list[str]:
        async def broken():
            raise asyncio.CancelledError()
            yield ""

        return [chunk async for chunk in inspect._guarded_stream(broken())]

    assert asyncio.run(_run()) == []


def test_shutdown_aware_streaming_response_stops_when_shutdown_starts() -> None:
    shutdown_signal = threading.Event()
    response = ShutdownAwareStreamingResponse(
        iter(()),
        shutdown_signal=shutdown_signal,
        disconnect_poll_sec=0.001,
    )

    async def receive() -> dict[str, str]:
        await asyncio.sleep(1)
        return {"type": "http.request"}

    async def _run() -> None:
        task = asyncio.create_task(response.listen_for_disconnect(receive))
        await asyncio.sleep(0.01)
        shutdown_signal.set()
        await asyncio.wait_for(task, timeout=0.1)

    asyncio.run(_run())


def test_chat_stream_allows_tool_only_turns(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(toolang_root / "agents" / "alice" / "agent.too", "agent alice\n")
    context = _build_context(
        toolang_root=toolang_root,
        agent_name="alice",
        enabled_features=("chat", "inspect"),
    )
    app = _create_test_app(context)

    with _patched_executor_execution_with_tools(output_text=""):
        with TestClient(app) as client:
            with client.stream(
                "POST",
                "/api/v1/chat/stream",
                json={"message": _chat_message("tool only")},
            ) as response:
                assert response.status_code == 200
                stream_text = "".join(
                    chunk.decode("utf-8") for chunk in response.iter_raw()
                )

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
        enabled_features=("chat", "inspect"),
    )

    app = create_app(context)

    assert app.router.lifespan_context is not None
    assert app.state.context is context
    context.store.close()


def test_hook_routes_are_not_component_enabled(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(toolang_root / "agents" / "alice" / "agent.too", "agent alice\n")
    context = _build_context(
        toolang_root=toolang_root,
        agent_name="alice",
        enabled_features=("inspect",),
    )
    app = _create_test_app(context)

    with TestClient(app) as client:
        response = client.post("/hook/runs", json={"agic": "decode webhook"})
        assert response.status_code == 404


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
                        thread_id="script_tg_123",
                        text="hello from poll",
                        reply_target=ReplyTarget(
                            channel="telegram", address="chat:123"
                        ),
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
    _write_text(toolang_root / "agents" / "alice" / "agent.too", "agent alice\n")
    plugin = FakeTelegramPlugin()
    context = _build_context(
        toolang_root=toolang_root,
        agent_name="alice",
        enabled_features=("poll", "chat", "inspect"),
        channel_bindings={
            "telegram": ChannelBinding(
                name="telegram",
                plugin="telegram",
                config={"token": "secret"},
            )
        },
        channel_plugins={"telegram": plugin},
    )

    with _patched_executor_execution():

        async def run_test() -> None:
            async with _running_context(
                context,
                enabled_features=("poll", "chat", "inspect"),
                loop_intervals_ms={"poll": 10.0},
            ):
                await _wait_for_completed_count(context, 1)
                run = context.store.list_runs(limit=1)[0]
                assert run.thread_id == "script_tg_123"
                assert run.origin == "chat"

        asyncio.run(run_test())

    assert plugin.deliveries == [
        (ReplyTarget(channel="telegram", address="chat:123"), ""),
        (
            ReplyTarget(channel="telegram", address="chat:123"),
            "assistant:hello from poll",
        ),
    ]
    state_path = agents.channel_room(toolang_root, "alice", "telegram") / "state.json"
    assert state_path.is_file()
    assert (
        ChannelState.from_data(
            json.loads(state_path.read_text(encoding="utf-8"))
        ).cursor
        == "43"
    )


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
    _write_text(toolang_root / "agents" / "alice" / "agent.too", "agent alice\n")
    plugin = FakeTelegramPlugin()
    context = _build_context(
        toolang_root=toolang_root,
        agent_name="alice",
        enabled_features=("poll", "chat"),
        channel_bindings={
            "telegram": ChannelBinding(
                name="telegram",
                plugin="telegram",
                config={"token": "secret"},
            )
        },
        channel_plugins={"telegram": plugin},
    )
    delivery = InboundDelivery(
        origin="chat",
        channel="telegram",
        sender="owner",
        thread_id="script_tg_123",
        text="hello from poll",
        reply_target=ReplyTarget(channel="telegram", address="chat:123"),
    )

    def fake_assemble(_context: ApiContext, bound):
        return _fake_run_input(bound)

    def fake_execute_stream(_bound, _model, *, on_event) -> RunResult:
        on_event(
            _started(
                1,
                run_id="run-1",
                thread_id="script_tg_123",
                kind="model",
            )
        )
        on_event(
            PartBegin(
                step="run-1/1",
                part=0,
                type_="tool_call",
            )
        )
        on_event(
            PartBegin(
                step="run-1/1",
                part=1,
                type_="text",
            )
        )
        on_event(
            PartDelta(
                step="run-1/1",
                part=1,
                delta=TextDelta(text="hel"),
            )
        )
        time.sleep(0.02)
        on_event(
            PartDelta(
                step="run-1/1",
                part=1,
                delta=TextDelta(text="lo"),
            )
        )
        time.sleep(0.02)
        on_event(
            PartDelta(
                step="run-1/1",
                part=1,
                delta=TextDelta(text=" world"),
            )
        )
        time.sleep(0.02)
        on_event(
            PartDelta(
                step="run-1/1",
                part=1,
                delta=TextDelta(text=" and more"),
            )
        )
        time.sleep(0.02)
        on_event(
            PartDelta(
                step="run-1/1",
                part=1,
                delta=TextDelta(text=" from telegram"),
            )
        )
        on_event(
            _completed(
                1,
                run_id="run-1",
                thread_id="script_tg_123",
                kind="model",
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
        _patched_run_input_assembly(fake_assemble),
        patch.object(
            run_executor_module,
            "_default_load_loop",
            return_value=_FakeLoop(
                run=lambda context: fake_execute_stream(
                    None, None, on_event=context.on_event
                ),
            ),
        ),
    ):
        asyncio.run(_run_delivery(context, "telegram", delivery))

    assert plugin.deliveries[0] == (
        ReplyTarget(channel="telegram", address="chat:123"),
        "",
        {"action": "typing"},
    )
    non_typing = [
        item for item in plugin.deliveries if item[2].get("action") != "typing"
    ]
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
    _write_text(toolang_root / "agents" / "alice" / "agent.too", "agent alice\n")
    plugin = FakeTelegramPlugin()
    context = _build_context(
        toolang_root=toolang_root,
        agent_name="alice",
        enabled_features=("poll", "chat"),
        channel_bindings={
            "telegram": ChannelBinding(
                name="telegram",
                plugin="telegram",
                config={"token": "secret"},
            )
        },
        channel_plugins={"telegram": plugin},
    )
    delivery = InboundDelivery(
        origin="chat",
        channel="telegram",
        sender="owner",
        thread_id="script_tg_123",
        text="hello from poll",
        reply_target=ReplyTarget(channel="telegram", address="chat:123"),
    )

    def fake_assemble(_context: ApiContext, bound):
        return _fake_run_input(bound)

    def fake_execute_stream(_bound, _model, *, on_event) -> RunResult:
        on_event(
            _started(
                1,
                run_id="run-1",
                thread_id="script_tg_123",
                kind="model",
            )
        )
        on_event(
            PartBegin(
                step="run-1/1",
                part=0,
                type_="text",
            )
        )
        on_event(
            PartDelta(
                step="run-1/1",
                part=0,
                delta=TextDelta(text="hello"),
            )
        )
        time.sleep(0.02)
        on_event(
            PartDelta(
                step="run-1/1",
                part=0,
                delta=TextDelta(text=" world"),
            )
        )
        on_event(
            _completed(
                1,
                run_id="run-1",
                thread_id="script_tg_123",
                kind="model",
                output=(TextPart(text="hello world"),),
            )
        )
        return RunResult(output_text="hello world")

    with (
        _patched_run_input_assembly(fake_assemble),
        patch.object(
            run_executor_module,
            "_default_load_loop",
            return_value=_FakeLoop(
                run=lambda context: fake_execute_stream(
                    None, None, on_event=context.on_event
                ),
            ),
        ),
    ):
        asyncio.run(_run_delivery(context, "telegram", delivery))

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


def test_control_routes_update_durable_only_without_prepare_reload(
    tmp_path: Path, monkeypatch
) -> None:
    toolang_root = tmp_path / "toolang"
    monkeypatch.setattr(caps, "_github_repo_default_branch", lambda owner, repo: "main")
    monkeypatch.setattr(caps, "_github_remote_exists", lambda _kind, _ref: True)
    _write_text(toolang_root / "agents" / "alice" / "agent.too", "agent alice\n")
    context = _build_context(
        toolang_root=toolang_root,
        agent_name="alice",
        enabled_features=("manage", "inspect"),
    )
    initial_state_fingerprint = context.get_agent_state().fingerprint
    initial_prepared_fingerprint = load_prepared_locks(
        toolang_root, "alice"
    ).fingerprint
    app = _create_test_app(context)

    with TestClient(app) as client:
        add_response = client.put(
            "/api/v1/skills/reviewer/wired",
            json={"visibility": "private", "ref": "acme/reviewer"},
        )
        assert add_response.status_code == 200
        assert add_response.json()["item"]["name"] == "reviewer"
        assert add_response.json()["item"]["origin"] == "remote"
        assert add_response.json()["item"]["form"] == "wired"
        assert add_response.json()["item"]["scope"] == "home"

        snapshot = inspect.snapshot_context(
            context, enabled_features=("manage", "inspect")
        )
        durable = cast(dict[str, object], snapshot["durable"])
        prepared = cast(dict[str, object], snapshot["prepared"])
        state = cast(dict[str, object], snapshot["state"])
        definitions = cast(dict[str, object], durable["definitions"])
        private_entries = cast(list[dict[str, object]], definitions["private_entries"])
        assert [item["name"] for item in private_entries] == ["reviewer"]
        assert prepared["fingerprint"] == initial_prepared_fingerprint
        assert state["fingerprint"] == initial_state_fingerprint
        assert state["caps"] == []
        assert client.get("/api/v1/skills").json()["items"] == []

        remove_response = client.delete(
            "/api/v1/skills/reviewer/wired?visibility=private"
        )
        assert remove_response.status_code == 200
        assert remove_response.json() == {"ok": True}

        snapshot = inspect.snapshot_context(
            context, enabled_features=("manage", "inspect")
        )
        durable = cast(dict[str, object], snapshot["durable"])
        definitions = cast(dict[str, object], durable["definitions"])
        assert definitions["private_entries"] == []

        local_response = client.put(
            "/api/v1/prompts/rewrite/file",
            json={"visibility": "private", "content": "Rewrite the request.\n"},
        )
        assert local_response.status_code == 200
        assert local_response.json()["item"]["name"] == "rewrite"
        assert local_response.json()["item"]["origin"] == "local"
        assert local_response.json()["item"]["form"] == "file"
        assert local_response.json()["item"]["scope"] == "home"

        delete_local_response = client.delete(
            "/api/v1/prompts/rewrite/file?visibility=private"
        )
        assert delete_local_response.status_code == 200
        assert delete_local_response.json() == {"ok": True}


def test_cap_template_api_lists_and_reads_templates(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(toolang_root / "agents" / "alice" / "agent.too", "agent alice\n")
    context = _build_context(
        toolang_root=toolang_root,
        agent_name="alice",
        enabled_features=("inspect",),
    )
    app = _create_test_app(context)

    with TestClient(app) as client:
        list_response = client.get("/api/v1/services/templates")
        assert list_response.status_code == 200
        items = list_response.json()["items"]
        assert [item["name"] for item in items] == ["default", "stdio"]
        assert items[0]["kind"] == "service"
        assert items[0]["description"] == (
            "Trigger this service when the agent needs this remote MCP server."
        )

        detail_response = client.get("/api/v1/services/templates/stdio")
        assert detail_response.status_code == 200
        item = detail_response.json()["item"]
        assert item["name"] == "stdio"
        assert item["kind"] == "service"
        assert "transport: stdio" in item["content"]
        assert "target: uvx example-mcp-server" in item["content"]
        assert "# env: API_TOKEN, ANOTHER_ENV_VAR" in item["content"]


def test_background_features_start_runs(tmp_path: Path) -> None:
    async def run_test() -> None:
        toolang_root = tmp_path / "toolang"
        _write_text(toolang_root / "agents" / "alice" / "agent.too", "agent alice\n")
        _write_text(
            toolang_root / "agents" / "alice" / "tasks" / "review.md",
            "---\ntitle: Review\n---\nReview the current plan.\n",
        )
        context = _build_context(
            toolang_root=toolang_root,
            agent_name="alice",
            enabled_features=("pulse",),
        )

        with _patched_executor_execution():
            async with _running_context(
                context, enabled_features=("pulse",), loop_intervals_ms={"pulse": 10.0}
            ):
                for _ in range(50):
                    completed = [
                        run
                        for run in context.store.list_runs(limit=None)
                        if run.origin == "task"
                        and run.status in {"finished", "failed", "canceled"}
                    ]
                    if completed:
                        break
                    await asyncio.sleep(0.01)
                assert completed
                run = completed[0]
                command = context.store.get_command(run_id=run.id, index=0)
                assert run.context["group"] == "scheduler:task"
                assert run.origin == "task"
                assert command is not None and command.input == Message.user(
                    "Review the current plan."
                )
                assert run.thread_id.startswith("task_")

    asyncio.run(run_test())


def test_job_store_claims_due_chores_and_tasks(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(toolang_root / "agents" / "alice" / "agent.too", "agent alice\n")
    _write_text(
        toolang_root / "agents" / "alice" / "chores" / "sync.md",
        "---\ntitle: Sync\nschedule: FREQ=MINUTELY;INTERVAL=1\n---\nSync remote tasks.\n",
    )
    _write_text(
        toolang_root / "agents" / "alice" / "tasks" / "review.md",
        "---\ntitle: Review\n---\nReview synced remote task.\n",
    )
    context = _build_context(
        toolang_root=toolang_root, agent_name="alice", enabled_features=()
    )
    definitions = AgentJobs.load(toolang_root, "alice", context.get_agent_state().program)
    store = open_job_store(toolang_root, "alice")
    try:
        store.reconcile(jobs=definitions, now=datetime(2026, 1, 1, tzinfo=timezone.utc))
        claimed = tuple(
            store.claim_due(
                jobs=definitions,
                kind=kind,
                run_id=f"run-{kind}",
                now=datetime(2026, 1, 1, tzinfo=timezone.utc),
            )
            for kind in ("chore", "task")
        )
    finally:
        store.close()

    assert {item.job.kind for item in claimed if item is not None} == {"chore", "task"}


def test_bind_run_request_allocates_normalized_local_ids(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(toolang_root / "agents" / "alice" / "agent.too", "agent alice\n")
    context = _build_context(
        toolang_root=toolang_root,
        agent_name="alice",
        enabled_features=("chat",),
    )

    bound = bind_run_request(
        context,
        RunRequest(group="chat", origin="chat", input="hello"),
    )

    assert bound.run_id.startswith("run_")
    assert bound.thread_id.startswith("term_")
    assert (toolang_root / "agents" / "alice" / ".runtime" / "ids.json").is_file()


def test_bind_run_request_uses_explicit_thread_kind_for_new_thread(
    tmp_path: Path,
) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(toolang_root / "agents" / "alice" / "agent.too", "agent alice\n")
    context = _build_context(
        toolang_root=toolang_root,
        agent_name="alice",
        enabled_features=("chat",),
    )

    bound = bind_run_request(
        context,
        RunRequest(group="chat", origin="chat", thread_kind="tui", input="hello"),
    )

    assert bound.origin == "chat"
    assert bound.thread_id.startswith("term_")
    assert len(bound.thread_id) == len("term_") + 8


def test_up_picks_free_port_when_unspecified(tmp_path: Path, monkeypatch) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(toolang_root / "agents" / "alice" / "agent.too", "agent alice\n")
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        "toolang.agent.runtime._pick_runtime_port",
        lambda host, *, toolang_root, agent_name, preferred_port=None: 43210,
    )

    def fake_run_uvicorn_app(
        app,
        *,
        host: str,
        port: int,
        log_config,
        shutdown_signal,
        on_starting=None,
        on_running=None,
        on_stopping=None,
        on_stopped=None,
    ) -> None:
        del on_starting, on_running, on_stopping, on_stopped
        captured["app"] = app
        captured["host"] = host
        captured["port"] = port
        captured["log_config"] = log_config
        captured["shutdown_signal"] = shutdown_signal

    monkeypatch.setattr("toolang.agent.runtime._run_uvicorn_app", fake_run_uvicorn_app)
    monkeypatch.setattr(
        cap_state,
        "_fetch_github_directory",
        lambda _ref: {"SKILL.md": b"---\ndescription: PDF work\n---\n# PDF\n"},
    )

    result = run_experiments_up(
        toolang_root=toolang_root,
        agent_name="alice",
        host="0.0.0.0",
        component_names=("inspect",),
        environ={},
    )

    assert result == 0
    assert captured["host"] == "0.0.0.0"
    assert captured["port"] == 43210
    assert isinstance(captured["shutdown_signal"], threading.Event)


def test_up_logs_runtime_urls_after_start_and_stop(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(toolang_root / "agents" / "alice" / "agent.too", "agent alice\n")
    _write_text(
        toolang_root / "config.toml",
        '[web]\nui_base_url = "https://agents.example.test"\n',
    )

    def fake_run_uvicorn_app(
        app,
        *,
        host: str,
        port: int,
        log_config,
        shutdown_signal,
        on_starting=None,
        on_running=None,
        on_stopping=None,
        on_stopped=None,
    ) -> None:
        del app, host, port, log_config, shutdown_signal
        if on_starting is not None:
            on_starting()
        if on_running is not None:
            on_running()
        if on_stopping is not None:
            on_stopping()
        if on_stopped is not None:
            on_stopped()

    monkeypatch.setattr("toolang.agent.runtime._run_uvicorn_app", fake_run_uvicorn_app)
    monkeypatch.setattr("toolang.agent.runtime.configure_logging", lambda **_kwargs: None)
    caplog.set_level(logging.INFO, logger="toolang.runtime")

    result = run_experiments_up(
        toolang_root=toolang_root,
        agent_name="alice",
        host="127.0.0.1",
        port=8765,
        feature_names=("inspect",),
        environ={},
    )

    assert result == 0
    messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == "toolang.runtime"
    ]
    assert len(messages) == 4
    assert (
        messages[0]
        == f"Agent starting root={toolang_root} trigger=none runner=none router=inspect"
    )
    assert messages[1] == "Agent started webui=https://agents.example.test/8765"
    assert messages[2] == "Agent stopping"
    assert messages[3] == "Agent stopped"
    color_messages = [
        record.__dict__.get("color_message")
        for record in caplog.records
        if record.name == "toolang.runtime"
    ]
    assert color_messages[0] == (
        "Agent starting root=\x1b[1m%s\x1b[0m trigger=\x1b[1m%s\x1b[0m runner=\x1b[1m%s\x1b[0m router=\x1b[1m%s\x1b[0m"
    )
    assert color_messages[1] == "Agent started webui=\x1b[1m%s\x1b[0m"


def test_local_runtime_configures_logging_before_state_loaded(
    tmp_path: Path, monkeypatch
) -> None:
    toolang_root = tmp_path / "toolang"
    AgentCatalog(toolang_root).create("alice")
    order: list[str] = []
    captured: dict[str, object] = {}

    def fake_configure_logging(
        *, spec: str | None, environ: dict[str, str], log_path=None
    ) -> None:
        del log_path
        order.append("configure")
        captured["spec"] = spec
        captured["environ"] = environ

    def fake_log_state_loaded(executor, config, state) -> None:
        del executor, config, state
        order.append("state")

    def fake_run_uvicorn_app(*args, **kwargs) -> None:
        del args, kwargs
        order.append("run")

    monkeypatch.setattr(up_module, "configure_logging", fake_configure_logging)
    monkeypatch.setattr(up_module, "_log_state_loaded", fake_log_state_loaded)
    monkeypatch.setattr(up_module, "_run_uvicorn_app", fake_run_uvicorn_app)

    result = up_module._up_local(
        toolang_root=toolang_root,
        agent_name="alice",
        host="127.0.0.1",
        endpoint_host="localhost",
        port=8765,
        enabled_components=("router.inspect",),
        environ={PY_LOG_ENV_VAR: "toolang.state=info"},
        sandbox_child=False,
        model_selectors=(),
        tool_selectors=None,
        cap_selectors=(),
        log_spec=None,
    )

    assert result == 0
    assert order[:2] == ["configure", "state"]
    assert captured["spec"] == "toolang.state=info"


def test_state_loaded_log_counts_selectable_models(tmp_path: Path, caplog) -> None:
    toolang_root = tmp_path / "toolang"
    AgentCatalog(toolang_root).create("alice")
    context = _build_context(
        toolang_root=toolang_root,
        agent_name="alice",
        enabled_features=("chat",),
    )

    caplog.set_level(logging.INFO, logger="toolang.state")
    up_module._log_state_loaded(
        context.executor, context.config, context.get_agent_state()
    )

    messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == "toolang.state"
    ]
    assert messages == [
        (
            f"Agent loaded state={context.get_agent_state().fingerprint[:12]} "
            f"models={up_module._model_count(context.executor, context.config)} "
            f"tools={len(context.executor.setup.tools)} "
            "psyches=0 skills=0 services=0"
        )
    ]
    assert up_module._model_count(context.executor, context.config) > 0
    unrestricted_count = up_module._model_count(context.executor, context.config)
    context.config.set("models.allowed_selectors", ("openai/gpt-5[openai]",))
    assert up_module._model_count(context.executor, context.config) == 1
    assert up_module._model_count(context.executor, context.config) < unrestricted_count


def test_assemble_execution_rejects_unmatched_tool_selector(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    AgentCatalog(toolang_root).create("alice")

    with pytest.raises(
        ValueError, match="tool selector matched no tools: missing/none"
    ):
        up_module.assemble_execution(
            toolang_root=toolang_root,
            agent_name="alice",
            enabled_components=("runner.chat",),
            environ={"OPENAI_API_KEY": "secret"},
            tool_selectors=("missing/none",),
        )


def test_assemble_execution_rejects_unmatched_cap_selector(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    AgentCatalog(toolang_root).create("alice")

    with pytest.raises(ValueError, match="cap selector matched no caps: skill/missing"):
        up_module.assemble_execution(
            toolang_root=toolang_root,
            agent_name="alice",
            enabled_components=("runner.chat",),
            environ={"OPENAI_API_KEY": "secret"},
            cap_selectors=("skill/missing",),
        )


def test_assemble_execution_applies_tool_and_cap_selectors(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    AgentCatalog(toolang_root).create("alice")
    _put_cap(
        toolang_root,
        "alice",
        visibility="private",
        kind="skill",
        name="local-reviewer",
        text="---\ndescription: Review local changes\n---\n# Local Reviewer\n",
    )
    _put_cap(
        toolang_root,
        "alice",
        visibility="private",
        kind="skill",
        name="extra-skill",
        text="---\ndescription: Extra\n---\n# Extra\n",
    )

    executor, watcher, _ = up_module.assemble_execution(
        toolang_root=toolang_root,
        agent_name="alice",
        enabled_components=("runner.chat",),
        environ={"OPENAI_API_KEY": "secret"},
        tool_selectors=("shell/*",),
        cap_selectors=("skill/local-reviewer",),
    )

    assert tuple(executor.setup.tools) == ("shell__execute",)
    assert [(entry.kind, entry.name) for entry in watcher.current().caps] == [
        ("skill", "local-reviewer")
    ]


def test_watch_reload_preserves_tool_and_cap_selectors(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    AgentCatalog(toolang_root).create("alice")
    _put_cap(
        toolang_root,
        "alice",
        visibility="private",
        kind="skill",
        name="local-reviewer",
        text="---\ndescription: Review local changes\n---\n# Local Reviewer\n",
    )
    executor, watcher, config = up_module.assemble_execution(
        toolang_root=toolang_root,
        agent_name="alice",
        enabled_components=("trigger.watch", "runner.chat"),
        environ={"OPENAI_API_KEY": "secret"},
        tool_selectors=("shell/*",),
        cap_selectors=("skill/local-reviewer",),
    )
    config.set("components.trigger.watch.debounce_ms", 1)
    binding = _bind_run_request(
        RunRequest(group="chat", origin="chat", input="hello"),
        id_state_path=executor.id_state_path,
        state=watcher.current(),
        setup=executor.setup,
        store=executor.store,
    )
    accepted_setup = binding.setup
    before = watcher.current().fingerprint
    _put_cap(
        toolang_root,
        "alice",
        visibility="private",
        kind="skill",
        name="extra-skill",
        text="---\ndescription: Extra\n---\n# Extra\n",
    )
    refreshed_state = watcher.refresh()
    expected_fingerprint = refreshed_state.fingerprint
    assert expected_fingerprint != before

    watch._apply_state(
        root=toolang_root,
        name="alice",
        previous=watcher.current(),
        state=refreshed_state,
        executor=executor,
        config=config,
    )

    assert watcher.current().fingerprint == expected_fingerprint
    assert executor.setup is not accepted_setup
    assert binding.setup is accepted_setup
    assert tuple(binding.setup.tools) == ("shell__execute",)
    assert tuple(executor.setup.tools) == ("shell__execute",)
    assert [(entry.kind, entry.name) for entry in watcher.current().caps] == [
        ("skill", "local-reviewer")
    ]


def test_trigger_logger_names_are_flat() -> None:
    assert watch.logger.name == "toolang.watch"
    assert state_watcher.prepare_logger.name == "toolang.prepare"
    assert poll.logger.name == "toolang.poll"


def test_up_reuses_previous_agent_port_when_unspecified(
    tmp_path: Path, monkeypatch
) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(toolang_root / "agents" / "alice" / "agent.too", "agent alice\n")
    agents.write_runtime_state(
        toolang_root,
        "alice",
        endpoint="http://127.0.0.1:53322",
        started_at="2026-04-07T11:00:00Z",
        pid=12345,
    )
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        "toolang.agent.runtime._pick_runtime_port",
        lambda host, *, toolang_root, agent_name, preferred_port=None: 43210,
    )

    def fake_run_uvicorn_app(
        app,
        *,
        host: str,
        port: int,
        log_config,
        shutdown_signal,
        on_starting=None,
        on_running=None,
        on_stopping=None,
        on_stopped=None,
    ) -> None:
        del on_starting, on_running, on_stopping, on_stopped
        captured["app"] = app
        captured["host"] = host
        captured["port"] = port
        captured["log_config"] = log_config
        captured["shutdown_signal"] = shutdown_signal

    monkeypatch.setattr("toolang.agent.runtime._run_uvicorn_app", fake_run_uvicorn_app)

    result = run_experiments_up(
        toolang_root=toolang_root,
        agent_name="alice",
        host="127.0.0.1",
        feature_names=("inspect",),
        environ={},
    )

    assert result == 0
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 53322
    assert isinstance(captured["shutdown_signal"], threading.Event)


def test_up_falls_back_when_previous_agent_port_is_unavailable(
    tmp_path: Path, monkeypatch
) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(toolang_root / "agents" / "alice" / "agent.too", "agent alice\n")
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

        monkeypatch.setattr(
            "toolang.agent.runtime._pick_runtime_port",
            lambda host, *, toolang_root, agent_name, preferred_port=None: 43210,
        )

        def fake_run_uvicorn_app(
            app,
            *,
            host: str,
            port: int,
            log_config,
            shutdown_signal,
            on_starting=None,
            on_running=None,
            on_stopping=None,
            on_stopped=None,
        ) -> None:
            del on_starting, on_running, on_stopping, on_stopped
            captured["app"] = app
            captured["host"] = host
            captured["port"] = port
            captured["log_config"] = log_config
            captured["shutdown_signal"] = shutdown_signal

        monkeypatch.setattr("toolang.agent.runtime._run_uvicorn_app", fake_run_uvicorn_app)

        result = run_experiments_up(
            toolang_root=toolang_root,
            agent_name="alice",
            host="127.0.0.1",
            feature_names=("inspect",),
            environ={},
        )

    assert result == 0
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 43210
    assert isinstance(captured["shutdown_signal"], threading.Event)


def test_up_falls_back_when_stopped_agent_port_is_unavailable(
    tmp_path: Path, monkeypatch
) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(toolang_root / "agents" / "alice" / "agent.too", "agent alice\n")
    agents.write_runtime_state(
        toolang_root,
        "alice",
        endpoint="http://127.0.0.1:53322",
        started_at="2026-04-07T11:00:00Z",
        pid=12345,
    )
    agents.stop_runtime_state(toolang_root, "alice")
    captured: dict[str, object] = {}

    def fake_port_is_available(host: str, port: int) -> bool:
        assert host == "127.0.0.1"
        assert port == 53322
        return False

    monkeypatch.setattr("toolang.agent.runtime._port_is_available", fake_port_is_available)
    monkeypatch.setattr(
        "toolang.agent.runtime._wait_for_port_available",
        lambda *_args, **_kwargs: pytest.fail(
            "stopped runtime ports should not be awaited"
        ),
    )
    monkeypatch.setattr(
        "toolang.agent.runtime._pick_runtime_port",
        lambda host, *, toolang_root, agent_name, preferred_port=None: 43210,
    )

    def fake_run_uvicorn_app(
        app,
        *,
        host: str,
        port: int,
        log_config,
        shutdown_signal,
        on_starting=None,
        on_running=None,
        on_stopping=None,
        on_stopped=None,
    ) -> None:
        del app, log_config, on_starting, on_running, on_stopping, on_stopped
        captured["host"] = host
        captured["port"] = port
        captured["shutdown_signal"] = shutdown_signal

    monkeypatch.setattr("toolang.agent.runtime._run_uvicorn_app", fake_run_uvicorn_app)

    result = run_experiments_up(
        toolang_root=toolang_root,
        agent_name="alice",
        host="127.0.0.1",
        feature_names=("inspect",),
        environ={},
    )

    assert result == 0
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 43210
    assert isinstance(captured["shutdown_signal"], threading.Event)


def test_resolve_runtime_port_does_not_wait_for_stopped_preferred_port(
    tmp_path: Path, monkeypatch
) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(toolang_root / "agents" / "alice" / "agent.too", "agent alice\n")
    agents.write_runtime_state(
        toolang_root,
        "alice",
        endpoint="http://127.0.0.1:53322",
        started_at="2026-04-07T11:00:00Z",
        pid=12345,
    )
    agents.stop_runtime_state(toolang_root, "alice")

    monkeypatch.setattr("toolang.agent.runtime._port_is_available", lambda host, port: False)
    monkeypatch.setattr(
        "toolang.agent.runtime._wait_for_port_available",
        lambda *_args, **_kwargs: pytest.fail(
            "stopped runtime ports should not be awaited"
        ),
    )
    monkeypatch.setattr(
        "toolang.agent.runtime._pick_runtime_port",
        lambda host, *, toolang_root, agent_name, preferred_port=None: 43210,
    )

    resolved = up_module.resolve_runtime_port(
        host="127.0.0.1",
        explicit_port=None,
        toolang_root=toolang_root,
        agent_name="alice",
    )

    assert resolved == 43210


def test_resolve_runtime_port_uses_temporary_picker_for_visiting_agents(
    tmp_path: Path, monkeypatch
) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(toolang_root / "agents" / "alice" / "agent.too", "agent alice\n")

    monkeypatch.setattr(
        "toolang.agent.runtime._pick_runtime_port",
        lambda *_args, **_kwargs: pytest.fail(
            "visiting agents should not use the resident port range"
        ),
    )
    monkeypatch.setattr("toolang.agent.runtime._pick_temporary_runtime_port", lambda host: 45678)

    resolved = up_module.resolve_runtime_port(
        host="127.0.0.1",
        explicit_port=None,
        toolang_root=toolang_root,
        agent_name="alice",
        temporary=True,
    )

    assert resolved == 45678


def test_resolve_runtime_port_reuses_visiting_agent_previous_port(
    tmp_path: Path, monkeypatch
) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(toolang_root / "agents" / "alice" / "agent.too", "agent alice\n")
    agents.write_runtime_state(
        toolang_root,
        "alice",
        endpoint="http://127.0.0.1:45679",
        started_at="2026-04-07T11:00:00Z",
        pid=None,
        status="stopped",
    )
    monkeypatch.setattr(
        "toolang.agent.runtime._port_is_available", lambda host, port: port == 45679
    )
    monkeypatch.setattr(
        "toolang.agent.runtime._pick_temporary_runtime_port",
        lambda *_args: pytest.fail(
            "available preferred visiting port should be reused"
        ),
    )

    resolved = up_module.resolve_runtime_port(
        host="127.0.0.1",
        explicit_port=None,
        toolang_root=toolang_root,
        agent_name="alice",
        temporary=True,
    )

    assert resolved == 45679


def test_pick_runtime_port_uses_first_available_auto_port(
    tmp_path: Path, monkeypatch
) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(toolang_root / "agents" / "bob" / "agent.too", "agent bob\n")
    _write_text(toolang_root / "agents" / "carol" / "agent.too", "agent carol\n")
    agents.write_runtime_state(
        toolang_root,
        "bob",
        endpoint="http://127.0.0.1:7001",
        started_at="2026-04-07T11:00:00Z",
        pid=None,
        status="stopped",
    )
    agents.write_runtime_state(
        toolang_root,
        "carol",
        endpoint="http://127.0.0.1:7002",
        started_at="2026-04-07T11:00:00Z",
        pid=12345,
    )

    seen: list[int] = []

    def fake_port_is_available(host: str, port: int) -> bool:
        assert host == "127.0.0.1"
        seen.append(port)
        return port == 7003

    monkeypatch.setattr("toolang.agent.runtime._port_is_available", fake_port_is_available)

    resolved = up_module._pick_runtime_port(
        "127.0.0.1",
        toolang_root=toolang_root,
        agent_name="alice",
    )

    assert resolved == 7003
    assert seen == [7003]


def test_up_uses_cors_origins_from_root_config(tmp_path: Path, monkeypatch) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(toolang_root / "agents" / "alice" / "agent.too", "agent alice\n")
    _write_text(
        toolang_root / "config.toml",
        '[web]\ncors_allowed_origins = ["http://localhost:3000", "https://too.run"]\n',
    )
    captured: dict[str, object] = {}

    def fake_run_uvicorn_app(
        app,
        *,
        host: str,
        port: int,
        log_config,
        shutdown_signal,
        on_starting=None,
        on_running=None,
        on_stopping=None,
        on_stopped=None,
    ) -> None:
        del on_starting, on_running, on_stopping, on_stopped
        captured["app"] = app
        captured["host"] = host
        captured["port"] = port
        captured["log_config"] = log_config
        captured["shutdown_signal"] = shutdown_signal

    monkeypatch.setattr("toolang.agent.runtime._run_uvicorn_app", fake_run_uvicorn_app)

    result = run_experiments_up(
        toolang_root=toolang_root,
        agent_name="alice",
        host="127.0.0.1",
        port=8765,
        feature_names=("inspect",),
        environ={},
    )

    assert result == 0
    assert isinstance(captured["shutdown_signal"], threading.Event)
    app = cast(FastAPI, captured["app"])
    with TestClient(app) as client:
        response = client.get(
            "/api/v1/profile",
            headers={"Origin": "http://localhost:3000"},
        )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:3000"


def test_up_starts_managed_sandbox_without_local_uvicorn(
    tmp_path: Path, monkeypatch
) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(toolang_root / "agents" / "alice" / "agent.too", "agent alice\n")
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
                run_command=("toolang",),
                state=SandboxState(
                    selector=request.selector,
                    runtime_id="sandbox-alice",
                ),
            )

        def start(self, plan: SandboxPlan) -> SandboxStartResult:
            captured["plan"] = plan
            return SandboxStartResult(
                state=cast(SandboxState, plan.state),
                endpoint="http://localhost:8765",
            )

        def alive(self, state: SandboxState) -> bool:
            del state
            return True

        def stop(self, state: SandboxState, *, force: bool = False) -> None:
            del state, force

    def fail_run_uvicorn_app(*args, **kwargs) -> None:
        raise AssertionError(
            "_run_uvicorn_app should not be called for managed sandboxes"
        )

    monkeypatch.setattr(
        "toolang.agent.runtime.create_sandbox_plugin", lambda name, config=None: FakeSandbox()
    )
    monkeypatch.setattr(
        "toolang.agent.runtime._wait_for_sandbox_ready",
        lambda **kwargs: captured.setdefault("ready", kwargs),
    )
    monkeypatch.setattr("toolang.agent.runtime._run_uvicorn_app", fail_run_uvicorn_app)

    result = run_experiments_up(
        toolang_root=toolang_root,
        agent_name="alice",
        host="127.0.0.1",
        port=8765,
        sandbox="docker:python:3.13-slim",
        feature_names=("inspect",),
        environ={"OPENAI_API_KEY": "secret"},
    )

    assert result == 0
    request = cast("SandboxStartRequest", captured["request"])
    assert request.selector == SandboxSelector(
        driver="docker", target="python:3.13-slim"
    )
    assert request.sandbox_root == Path("/root/.toolang")
    assert request.sandbox_home == Path("/root/.toolang/agents/alice")
    assert request.bind_host == "127.0.0.1"
    assert request.endpoint_host == "localhost"
    assert request.endpoint == "http://localhost:8765"
    assert request.env_vars["OPENAI_API_KEY"] == "secret"
    assert {
        mount.sandbox_path.relative_to(request.sandbox_root).as_posix()
        for mount in request.mounts
    } == {"config.toml", ".caps", "psyches", "skills", "services", "prompts"}
    assert request.run_command[:7] == (
        "toolang",
        "--root",
        "/root/.toolang",
        "run",
        "alice",
        "--host",
        "0.0.0.0",
    )
    assert "--endpoint-host" in request.run_command
    assert (
        request.run_command[request.run_command.index("--endpoint-host") + 1]
        == "localhost"
    )
    assert "--sandbox" in request.run_command
    assert request.run_command[request.run_command.index("--sandbox") + 1] == "none"
    assert "--sandbox-child" in request.run_command
    assert "--enable" in request.run_command
    assert (
        request.run_command[request.run_command.index("--enable") + 1]
        == "router.inspect"
    )
    runtime_state = json.loads(
        agents.agent_runtime_state_path(toolang_root, "alice").read_text(
            encoding="utf-8"
        )
    )
    assert runtime_state["status"] == "running"
    assert runtime_state["endpoint"] == "http://localhost:8765"
    assert runtime_state["sandbox"]["selector"]["driver"] == "docker"
    assert runtime_state["sandbox"]["runtime_id"] == "sandbox-alice"
    ready = cast(dict[str, object], captured["ready"])
    assert ready["host"] == "127.0.0.1"
    assert ready["port"] == 8765


def test_up_defaults_docker_target_when_selector_omits_one(
    tmp_path: Path, monkeypatch
) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(toolang_root / "agents" / "alice" / "agent.too", "agent alice\n")
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

    monkeypatch.setattr(
        "toolang.agent.runtime.create_sandbox_plugin", lambda name, config=None: FakeSandbox()
    )
    monkeypatch.setattr("toolang.agent.runtime._wait_for_sandbox_ready", lambda **kwargs: None)

    result = run_experiments_up(
        toolang_root=toolang_root,
        agent_name="alice",
        host="127.0.0.1",
        port=8765,
        sandbox="docker",
        feature_names=("inspect",),
        environ={},
    )

    assert result == 0
    request = cast("SandboxStartRequest", captured["request"])
    assert request.selector == SandboxSelector(
        driver="docker", target="python:3.13-slim"
    )


def test_up_marks_managed_sandbox_failed_when_ready_check_fails(
    tmp_path: Path, monkeypatch
) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(toolang_root / "agents" / "alice" / "agent.too", "agent alice\n")

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
                run_command=("too", "runner"),
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

    monkeypatch.setattr(
        "toolang.agent.runtime.create_sandbox_plugin", lambda name, config=None: FakeSandbox()
    )
    monkeypatch.setattr(
        "toolang.agent.runtime._wait_for_sandbox_ready",
        lambda **kwargs: (_ for _ in ()).throw(ValueError("sandbox failed")),
    )

    try:
        run_experiments_up(
            toolang_root=toolang_root,
            agent_name="alice",
            host="127.0.0.1",
            port=8765,
            sandbox="docker:python:3.13-slim",
            feature_names=("inspect",),
            environ={},
        )
    except ValueError as exc:
        assert str(exc) == "sandbox failed"
    else:
        raise AssertionError("expected managed sandbox startup failure")

    runtime_state = json.loads(
        agents.agent_runtime_state_path(toolang_root, "alice").read_text(
            encoding="utf-8"
        )
    )
    assert runtime_state["status"] == "failed"
    assert runtime_state["message"] == "sandbox failed"


def test_up_marks_managed_sandbox_failed_when_prepare_fails(
    tmp_path: Path, monkeypatch
) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(toolang_root / "agents" / "alice" / "agent.too", "agent alice\n")

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

    monkeypatch.setattr(
        "toolang.agent.runtime.create_sandbox_plugin", lambda name, config=None: FakeSandbox()
    )

    try:
        run_experiments_up(
            toolang_root=toolang_root,
            agent_name="alice",
            host="127.0.0.1",
            port=8765,
            sandbox="docker",
            feature_names=("inspect",),
            environ={},
        )
    except ValueError as exc:
        assert str(exc) == "prepare failed"
    else:
        raise AssertionError("expected managed sandbox prepare failure")

    runtime_state = json.loads(
        agents.agent_runtime_state_path(toolang_root, "alice").read_text(
            encoding="utf-8"
        )
    )
    assert runtime_state["status"] == "failed"
    assert runtime_state["message"] == "prepare failed"


def test_list_agent_statuses_surfaces_preparing_and_failed_states(
    tmp_path: Path,
) -> None:
    toolang_root = tmp_path / "toolang"
    AgentCatalog(toolang_root).create("alice")
    AgentCatalog(toolang_root).create("bob")
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

    statuses = {
        item.name: item
        for item in agents.AgentProcess.list(
            toolang_root, ui_base_url="http://localhost:3000"
        )
    }

    assert statuses["alice"].status == "preparing"
    assert statuses["alice"].endpoint == "http://127.0.0.1:8765"
    assert statuses["alice"].api_url == "http://127.0.0.1:8765/docs"
    assert statuses["alice"].webui_url is None
    assert statuses["bob"].status == "failed"
    assert statuses["bob"].endpoint is None
    assert statuses["bob"].api_url is None
    assert statuses["bob"].webui_url is None


def test_stop_runtime_state_requires_matching_owner_when_expected(
    tmp_path: Path,
) -> None:
    toolang_root = tmp_path / "toolang"
    AgentCatalog(toolang_root).create("alice")
    agents.write_runtime_state(
        toolang_root,
        "alice",
        endpoint="http://127.0.0.1:8765",
        started_at="2026-04-08T10:00:00Z",
        pid=111,
    )

    stopped = agents.stop_runtime_state(
        toolang_root,
        "alice",
        expected_pid=222,
        expected_started_at="2026-04-08T10:00:00Z",
    )

    runtime_state = cast(
        dict[str, object], agents.AgentProcess(toolang_root, "alice").state()
    )
    assert stopped is False
    assert runtime_state["status"] == "running"
    assert runtime_state["pid"] == 111

    stopped = agents.stop_runtime_state(
        toolang_root,
        "alice",
        expected_pid=111,
        expected_started_at="2026-04-08T10:00:00Z",
    )

    runtime_state = cast(
        dict[str, object], agents.AgentProcess(toolang_root, "alice").state()
    )
    assert stopped is True
    assert runtime_state["status"] == "stopped"
    assert runtime_state["pid"] is None


def test_agent_status_uses_matching_process_when_runtime_state_is_stale(
    tmp_path: Path, monkeypatch
) -> None:
    toolang_root = tmp_path / "toolang"
    AgentCatalog(toolang_root).create("alice")
    agents.write_runtime_state(
        toolang_root,
        "alice",
        endpoint="http://127.0.0.1:8765",
        started_at="2026-04-08T10:00:00Z",
        pid=None,
        status="stopped",
    )

    monkeypatch.setattr(
        "toolang.agent.local._agent_runtime_process_pids", lambda *_args: (43210,)
    )

    status = agents.AgentProcess(toolang_root, "alice").status(
        ui_base_url="http://localhost:3000"
    )

    assert status is not None
    assert status.status == "running"
    assert status.endpoint == "http://127.0.0.1:8765"
    assert status.api_url == "http://127.0.0.1:8765/docs"
    assert status.webui_url == "http://localhost:3000/8765"


def test_resolve_dev_artifact_picks_newest_wheel_recursively(tmp_path: Path) -> None:
    from toolang.agent import runtime as up_module

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


def test_stop_agent_terminates_local_pid_and_marks_state_stopped(
    tmp_path: Path, monkeypatch
) -> None:
    toolang_root = tmp_path / "toolang"
    AgentCatalog(toolang_root).create("alice")
    pid = 43210
    alive = {"running": True}

    def fake_kill(target_pid: int, signal_value: int) -> None:
        assert target_pid == pid
        if signal_value == 0:
            if alive["running"]:
                return
            raise OSError("dead")
        alive["running"] = False

    monkeypatch.setattr("toolang.agent.local.os.kill", fake_kill)
    agents.write_runtime_state(
        toolang_root,
        "alice",
        endpoint="http://127.0.0.1:8765",
        started_at="2026-04-08T10:00:00Z",
        pid=pid,
    )

    stopped = agents.AgentProcess(toolang_root, "alice").stop()

    assert stopped is True
    runtime_state = cast(
        dict[str, object], agents.AgentProcess(toolang_root, "alice").state()
    )
    assert runtime_state["status"] == "stopped"
    assert runtime_state["pid"] is None


def test_stop_agent_marks_state_stopped_without_waiting_for_endpoint_release(
    tmp_path: Path, monkeypatch
) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(toolang_root / "agents" / "alice" / "agent.too", "agent alice\n")
    agents.write_runtime_state(
        toolang_root,
        "alice",
        endpoint="http://127.0.0.1:53322",
        started_at="2026-04-08T10:00:00Z",
        pid=43210,
    )
    observed: dict[str, object] = {}

    monkeypatch.setattr("toolang.agent.local._pid_alive", lambda pid: pid == 43210)
    monkeypatch.setattr(
        "toolang.agent.local._stop_pid",
        lambda pid, *, force: observed.setdefault("stopped_pid", pid) and True,
    )

    process = agents.AgentProcess(toolang_root, "alice")
    stopped = process.stop()

    assert stopped is True
    assert observed["stopped_pid"] == 43210
    runtime_state = process.state()
    assert runtime_state is not None
    assert runtime_state["status"] == "stopped"


def test_stop_agent_terminates_matching_process_when_runtime_state_is_stale(
    tmp_path: Path, monkeypatch
) -> None:
    toolang_root = tmp_path / "toolang"
    AgentCatalog(toolang_root).create("alice")
    agents.write_runtime_state(
        toolang_root,
        "alice",
        endpoint="http://127.0.0.1:8765",
        started_at="2026-04-08T10:00:00Z",
        pid=None,
        status="stopped",
    )
    stopped_pids: list[int] = []

    monkeypatch.setattr(
        "toolang.agent.local._agent_runtime_process_pids", lambda *_args: (43210,)
    )
    monkeypatch.setattr(
        "toolang.agent.local._stop_pid",
        lambda pid, *, force: stopped_pids.append(pid) or True,
    )

    stopped = agents.AgentProcess(toolang_root, "alice").stop()

    assert stopped is True
    assert stopped_pids == [43210]
    runtime_state = cast(
        dict[str, object], agents.AgentProcess(toolang_root, "alice").state()
    )
    assert runtime_state["status"] == "stopped"
    assert runtime_state["pid"] is None


def test_stop_agent_rejects_stubborn_process_without_marking_state_stopped(
    tmp_path: Path, monkeypatch
) -> None:
    toolang_root = tmp_path / "toolang"
    AgentCatalog(toolang_root).create("alice")
    agents.write_runtime_state(
        toolang_root,
        "alice",
        endpoint="http://127.0.0.1:8765",
        started_at="2026-04-08T10:00:00Z",
        pid=None,
        status="running",
    )

    monkeypatch.setattr(
        "toolang.agent.local._agent_runtime_process_pids", lambda *_args: (43210,)
    )
    monkeypatch.setattr("toolang.agent.local._stop_pid", lambda _pid, *, force: False)

    with pytest.raises(ValueError, match="retry with --force"):
        agents.AgentProcess(toolang_root, "alice").stop()

    runtime_state = cast(
        dict[str, object], agents.AgentProcess(toolang_root, "alice").state()
    )
    assert runtime_state["status"] == "running"


def test_stop_agent_stops_managed_sandbox_and_marks_state_stopped(
    tmp_path: Path,
) -> None:
    class FakeSandbox:
        def __init__(self) -> None:
            self.runtime_id: str | None = None
            self.force = False

        def stop(self, state: SandboxState, *, force: bool = False) -> None:
            self.runtime_id = state.runtime_id
            self.force = force

    toolang_root = tmp_path / "toolang"
    AgentCatalog(toolang_root).create("alice")
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

    stopped = agents.AgentProcess(toolang_root, "alice").stop(
        sandbox_plugin=cast("AgentSandbox", plugin),
        force=True,
    )

    assert stopped is True
    assert plugin.runtime_id == "sandbox-alice"
    assert plugin.force is True
    runtime_state = cast(
        dict[str, object], agents.AgentProcess(toolang_root, "alice").state()
    )
    assert runtime_state["status"] == "stopped"
    assert runtime_state["pid"] is None


def test_up_reads_web_config_without_validating_experiments_caps(
    tmp_path: Path, monkeypatch
) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(toolang_root / "agents" / "alice" / "agent.too", "agent alice\n")
    (toolang_root / "config.toml").write_text(
        "[web]\n"
        'cors_allowed_origins = ["http://localhost:3000", "https://too.run"]\n'
        "\n"
        "[skills]\n"
        'pdf-processing = { ref = "github://by3gus/agent-skills/skills/pdf-processing@main" }\n',
        encoding="utf-8",
    )
    captured: dict[str, object] = {}

    def fake_run_uvicorn_app(
        app,
        *,
        host: str,
        port: int,
        log_config,
        shutdown_signal,
        on_starting=None,
        on_running=None,
        on_stopping=None,
        on_stopped=None,
    ) -> None:
        del host, port, log_config, on_starting, on_running, on_stopping, on_stopped
        captured["app"] = app
        captured["shutdown_signal"] = shutdown_signal

    monkeypatch.setattr("toolang.agent.runtime._run_uvicorn_app", fake_run_uvicorn_app)

    result = run_experiments_up(
        toolang_root=toolang_root,
        agent_name="alice",
        host="127.0.0.1",
        port=8765,
        feature_names=("inspect",),
        environ={},
    )

    assert result == 0
    assert isinstance(captured["shutdown_signal"], threading.Event)
    app = cast(FastAPI, captured["app"])
    with TestClient(app) as client:
        response = client.get(
            "/api/v1/profile",
            headers={"Origin": "https://too.run"},
        )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "https://too.run"


def test_prepare_reload_refreshes_prepared_and_agent_state(tmp_path: Path) -> None:
    async def run_test() -> None:
        toolang_root = tmp_path / "toolang"
        prompt_path = toolang_root / "agents" / "alice" / "prompts" / "rewrite.md"
        _write_text(prompt_path, "---\ndescription: v1\n---\nPrompt v1\n")
        context = _build_context(
            toolang_root=toolang_root,
            agent_name="alice",
            enabled_features=("watch",),
        )

        initial_fingerprint = context.get_agent_state().fingerprint
        initial_program = context.get_agent_state().program
        async with _running_context(
            context,
            enabled_features=("watch",),
            loop_intervals_ms={"watch": 10.0},
        ):
            _write_text(prompt_path, "---\ndescription: v2\n---\nPrompt v2\n")
            refreshed = await _wait_for_fingerprint_change(context, initial_fingerprint)
            assert refreshed
            prepared = load_prepared_locks(context.root, context.name)
            assert prepared.shared_lock.entries == ()
            assert not prepared.shared_lock.lock_path.is_file()
            assert prepared.private_lock.lock_path.is_file()
            assert context.get_agent_state().fingerprint == load_agent_state(prepared).fingerprint
            assert context.get_agent_state().program is initial_program
            assert any(
                entry.path == "agents/alice/prompts/rewrite.md"
                for entry in prepared.private_lock.entries
            )

    asyncio.run(run_test())


def test_prepare_reload_refreshes_service_use_visible_services(tmp_path: Path) -> None:
    async def run_test() -> None:
        toolang_root = tmp_path / "toolang"
        _write_text(toolang_root / "agents" / "alice" / "agent.too", "agent alice\n")
        context = _build_context(
            toolang_root=toolang_root,
            agent_name="alice",
            enabled_features=("watch",),
        )

        initial_fingerprint = context.get_agent_state().fingerprint
        service_schema = (
            context.executor.setup.tools["service_use__bridge_start"]
            .definition()
            .parameters["properties"]["service"]
        )
        assert "enum" not in service_schema

        async with _running_context(
            context,
            enabled_features=("watch",),
            loop_intervals_ms={"watch": 10.0},
        ):
            _write_text(
                toolang_root / "agents" / "alice" / "services" / "linear.md",
                "---\n"
                "description: Linear MCP\n"
                "transport: http\n"
                "target: https://mcp.linear.app/mcp\n"
                "---\n",
            )
            refreshed = await _wait_for_fingerprint_change(context, initial_fingerprint)
            assert refreshed

            service_schema = (
                context.executor.setup.tools["service_use__bridge_start"]
                .definition()
                .parameters["properties"]["service"]
            )
            assert service_schema["enum"] == ["linear"]

    asyncio.run(run_test())


def test_runtime_tool_plugin_config_maps_service_caps_to_service_use(
    tmp_path: Path,
) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(toolang_root / "agents" / "alice" / "agent.too", "agent alice\n")
    _write_text(
        toolang_root / "agents" / "alice" / "services" / "linear.md",
        "---\n"
        "description: Linear MCP\n"
        "transport: stdio\n"
        "target: uvx mcp-remote https://mcp.linear.app/sse\n"
        "env: LINEAR_API_KEY, API_KEY\n"
        "---\n",
    )
    durable = scan_durable_state(toolang_root, "alice")
    prepared = state_watcher.prepare_locks(durable)
    state = load_agent_state(prepared)

    config = runtime_tool_config(
        plugin_config=load_named_config(
            toolang_root,
            "alice",
            section="tools",
            environ={},
        ),
        entries=state.caps,
    )

    assert config["service_use"]["visible_services"] == [
        {
            "name": "linear",
            "description": "Linear MCP",
            "transport": "stdio",
            "target": "uvx mcp-remote https://mcp.linear.app/sse",
            "command": ["uvx", "mcp-remote", "https://mcp.linear.app/sse"],
            "env_vars": ["LINEAR_API_KEY", "API_KEY"],
        }
    ]


def test_prepare_materializes_remote_entries_from_config(
    tmp_path: Path, monkeypatch
) -> None:
    toolang_root = tmp_path / "toolang"
    monkeypatch.setattr(caps, "_github_repo_default_branch", lambda owner, repo: "main")
    monkeypatch.setattr(caps, "_github_remote_exists", lambda _kind, _ref: True)

    def fake_materialized_files(*, relative_entry_path, kind, name, ref):
        del kind, name, ref
        return {
            str(
                relative_entry_path
            ): b"---\ndescription: Rewrite\n---\nRewrite prompt.\n"
        }

    monkeypatch.setattr(
        cap_state, "_remote_materialized_files", fake_materialized_files
    )

    config_path = add_remote_entry(
        toolang_root,
        "alice",
        visibility="shared",
        kind="prompt",
        ref="acme/rewrite",
    )
    assert config_path == toolang_root / "config.toml"

    durable = scan_durable_state(toolang_root, "alice")
    prepared = state_watcher.prepare_locks(durable)
    state = load_agent_state(prepared)

    assert (toolang_root / ".caps" / "wired" / "prompts" / "rewrite.md").is_file()
    assert [entry.source.origin for entry in prepared.shared_lock.entries] == ["remote"]
    assert [entry.source.form for entry in prepared.shared_lock.entries] == ["wired"]
    assert prepared.shared_lock.entries[0].path == ".caps/wired/prompts/rewrite.md"
    assert (
        prepared.shared_lock.entries[0].ref
        == "github://acme/agents/prompts/rewrite.md@main"
    )
    assert [entry.path for entry in state.caps] == [".caps/wired/prompts/rewrite.md"]


def test_remote_skill_shorthand_probes_agent_skills_and_skills_repos(
    tmp_path: Path,
    monkeypatch,
) -> None:
    toolang_root = tmp_path / "toolang"
    probes: list[str] = []

    def fake_exists(_kind, ref):
        probes.append(ref)
        return ref == "github://anthropics/skills/skills/pdf@main"

    monkeypatch.setattr(caps, "_github_repo_default_branch", lambda owner, repo: "main")
    monkeypatch.setattr(caps, "_github_remote_exists", fake_exists)

    add_remote_entry(
        toolang_root,
        "alice",
        visibility="private",
        kind="skill",
        ref="anthropics/pdf",
    )

    config_text = (toolang_root / "agents" / "alice" / "config.toml").read_text(
        encoding="utf-8"
    )

    assert probes == [
        "github://anthropics/agents/skills/pdf@main",
        "github://anthropics/agent-skills/pdf@main",
        "github://anthropics/agent-skills/skills/pdf@main",
        "github://anthropics/skills/pdf@main",
        "github://anthropics/skills/skills/pdf@main",
    ]
    assert 'pdf = { ref = "github://anthropics/skills/skills/pdf@main" }' in config_text


def test_remote_cap_repo_shorthand_uses_named_repo_path_probes(
    tmp_path: Path, monkeypatch
) -> None:
    toolang_root = tmp_path / "toolang"
    probes: list[str] = []

    def fake_exists(_kind, ref):
        probes.append(ref)
        return ref == "github://anthropics/project/pdf@trunk"

    monkeypatch.setattr(
        caps, "_github_repo_default_branch", lambda owner, repo: "trunk"
    )
    monkeypatch.setattr(caps, "_github_remote_exists", fake_exists)

    add_remote_entry(
        toolang_root,
        "alice",
        visibility="private",
        kind="skill",
        ref="anthropics/project/pdf",
    )

    config_text = (toolang_root / "agents" / "alice" / "config.toml").read_text(
        encoding="utf-8"
    )

    assert probes == [
        "github://anthropics/project/skills/pdf@trunk",
        "github://anthropics/project/pdf@trunk",
    ]
    assert 'pdf = { ref = "github://anthropics/project/pdf@trunk" }' in config_text


def test_remote_skill_add_canonicalizes_github_tree_url(
    tmp_path: Path, monkeypatch
) -> None:
    toolang_root = tmp_path / "toolang"

    monkeypatch.setattr(caps, "_github_remote_exists", lambda _kind, _ref: True)
    monkeypatch.setattr(
        cap_state,
        "_fetch_github_directory",
        lambda ref: {
            "SKILL.md": f"---\ndescription: {ref.render()}\n---\n# Answers\n".encode()
        },
    )

    add_remote_entry(
        toolang_root,
        "alice",
        visibility="private",
        kind="skill",
        ref="https://github.com/brave/brave-search-skills/tree/main/skills/answers",
    )

    config_text = (toolang_root / "agents" / "alice" / "config.toml").read_text(
        encoding="utf-8"
    )
    durable = scan_durable_state(toolang_root, "alice")
    prepared = state_watcher.prepare_locks(durable)

    assert (
        'answers = { ref = "github://brave/brave-search-skills/skills/answers@main" }'
        in config_text
    )
    assert (
        prepared.private_lock.entries[0].ref
        == "github://brave/brave-search-skills/skills/answers@main"
    )
    assert (
        toolang_root
        / "agents"
        / "alice"
        / ".caps"
        / "wired"
        / "skills"
        / "answers"
        / "SKILL.md"
    ).is_file()


def test_remote_skill_add_canonicalizes_github_skill_file_url(
    tmp_path: Path, monkeypatch
) -> None:
    toolang_root = tmp_path / "toolang"

    monkeypatch.setattr(caps, "_github_remote_exists", lambda _kind, _ref: True)
    monkeypatch.setattr(
        cap_state,
        "_fetch_github_directory",
        lambda ref: {
            "SKILL.md": f"---\ndescription: {ref.render()}\n---\n# Agent Browser\n".encode()
        },
    )

    add_remote_entry(
        toolang_root,
        "alice",
        visibility="private",
        kind="skill",
        ref="https://github.com/vercel-labs/agent-browser/blob/main/skills/agent-browser/SKILL.md",
    )

    config_text = (toolang_root / "agents" / "alice" / "config.toml").read_text(
        encoding="utf-8"
    )
    durable = scan_durable_state(toolang_root, "alice")
    prepared = state_watcher.prepare_locks(durable)

    expected_ref = "github://vercel-labs/agent-browser/skills/agent-browser@main"
    assert f'agent-browser = {{ ref = "{expected_ref}" }}' in config_text
    assert prepared.private_lock.entries[0].ref == expected_ref


def test_remote_skill_add_canonicalizes_raw_refs_heads_skill_file_url(
    tmp_path: Path, monkeypatch
) -> None:
    toolang_root = tmp_path / "toolang"

    monkeypatch.setattr(caps, "_github_remote_exists", lambda _kind, _ref: True)
    monkeypatch.setattr(
        cap_state,
        "_fetch_github_directory",
        lambda ref: {
            "SKILL.md": f"---\ndescription: {ref.render()}\n---\n# Agent Browser\n".encode()
        },
    )

    add_remote_entry(
        toolang_root,
        "alice",
        visibility="private",
        kind="skill",
        ref="https://raw.githubusercontent.com/vercel-labs/agent-browser/refs/heads/main/skills/agent-browser/SKILL.md",
    )

    config_text = (toolang_root / "agents" / "alice" / "config.toml").read_text(
        encoding="utf-8"
    )
    durable = scan_durable_state(toolang_root, "alice")
    prepared = state_watcher.prepare_locks(durable)

    expected_ref = (
        "github://vercel-labs/agent-browser/skills/agent-browser@refs/heads/main"
    )
    assert f'agent-browser = {{ ref = "{expected_ref}" }}' in config_text
    assert prepared.private_lock.entries[0].ref == expected_ref


def test_state_watcher_refresh_records_remote_cap_updates(
    tmp_path: Path, monkeypatch
) -> None:
    toolang_root = tmp_path / "toolang"
    monkeypatch.setattr(caps, "_github_repo_default_branch", lambda owner, repo: "main")
    monkeypatch.setattr(caps, "_github_remote_exists", lambda _kind, _ref: True)
    monkeypatch.setattr(
        cap_state,
        "_fetch_github_directory",
        lambda ref: {
            "SKILL.md": f"---\ndescription: {ref.render()}\n---\n# Skill\n".encode()
        },
    )

    AgentCatalog(toolang_root).create("alice")
    add_remote_entry(
        toolang_root,
        "alice",
        visibility="private",
        kind="skill",
        ref="acme/pdf",
    )
    initial = state_watcher.prepare_locks(scan_durable_state(toolang_root, "alice"))
    state = load_agent_state(initial)
    add_remote_entry(
        toolang_root,
        "alice",
        visibility="private",
        kind="skill",
        ref="acme/review",
    )
    updates: list[tuple[str, dict[str, object]]] = []

    class Store:
        def append_update(self, *, kind, payload=None, created_at=None):
            del created_at
            updates.append((kind, dict(payload or {})))

    watcher = state_watcher.StateWatcher(toolang_root, "alice", state)
    next_state = watcher.refresh()
    watch._append_entry_change_updates(
        cast(RunStore, Store()), watcher.previous_locks, watcher.locks
    )

    assert ("skill_changed", {"name": "review", "visibility": "private"}) in updates
    assert next_state.fingerprint != state.fingerprint
    assert next_state.program is state.program


def test_state_watcher_reparses_changed_program(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    AgentCatalog(toolang_root).create("alice")
    initial = state_watcher.prepare_locks(scan_durable_state(toolang_root, "alice"))
    state = load_agent_state(initial)
    watcher = state_watcher.StateWatcher(toolang_root, "alice", state)

    _write_text(
        toolang_root / "agents" / "alice" / "agent.too",
        "agent alice\n\nagic chat:\n  Updated.\n",
    )
    next_state = watcher.refresh()

    assert next_state.program is not state.program
    assert next_state.program.agics[0].messages[0].content == "Updated."


def test_agent_state_config_is_deeply_immutable(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    AgentCatalog(toolang_root).create("alice")
    _write_text(
        toolang_root / "config.toml",
        '[runtime]\nsandbox = "none"\nmodels = ["root"]\n',
    )
    _write_text(
        toolang_root / "agents" / "alice" / "config.toml",
        '[runtime]\nmodels = ["home"]\n',
    )

    prepared = state_watcher.prepare_locks(scan_durable_state(toolang_root, "alice"))
    state = load_agent_state(prepared)
    runtime = cast(Any, state.config["runtime"])

    assert runtime["sandbox"] == "none"
    assert runtime["models"] == ("home",)
    with pytest.raises(TypeError):
        cast(Any, state.config)["runtime"] = {}
    with pytest.raises(TypeError):
        runtime["sandbox"] = "docker"


def test_prepare_reuses_remote_caps_when_visibility_inputs_and_outputs_match(
    tmp_path: Path,
    monkeypatch,
) -> None:
    toolang_root = tmp_path / "toolang"
    fetches: list[str] = []

    monkeypatch.setattr(caps, "_github_repo_default_branch", lambda owner, repo: "main")
    monkeypatch.setattr(caps, "_github_remote_exists", lambda _kind, _ref: True)

    def fake_fetch(ref):
        fetches.append(ref.render())
        return {"SKILL.md": b"---\ndescription: PDF\n---\n# PDF\n"}

    monkeypatch.setattr(cap_state, "_fetch_github_directory", fake_fetch)
    add_remote_entry(
        toolang_root,
        "alice",
        visibility="private",
        kind="skill",
        ref="acme/pdf",
    )

    first = state_watcher.prepare_locks(scan_durable_state(toolang_root, "alice"))
    monkeypatch.setattr(
        cap_state,
        "_fetch_github_directory",
        lambda ref: pytest.fail(f"unexpected remote fetch: {ref.render()}"),
    )
    second = state_watcher.prepare_locks(scan_durable_state(toolang_root, "alice"))

    assert fetches == ["github://acme/agents/skills/pdf@main"]
    assert second.fingerprint == first.fingerprint


def test_prepare_rebuilds_stale_lock_schema_as_cache_miss(
    tmp_path: Path,
    monkeypatch,
) -> None:
    toolang_root = tmp_path / "toolang"
    AgentCatalog(toolang_root).create("alice")
    config_path = toolang_root / "agents" / "alice" / "config.toml"
    config_path.write_text(
        '[skills]\npdf = { ref = "github://acme/agents/skills/pdf@main" }\n',
        encoding="utf-8",
    )
    lock_path = toolang_root / "agents" / "alice" / ".caps" / "lock.json"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(
        json.dumps(
            {
                "visibility": "private",
                "updated_at": "2026-04-18T00:00:00Z",
                "fingerprint": "stale",
                "input_fingerprint": "stale",
                "entries": [
                    {
                        "kind": "skill",
                        "name": "pdf",
                        "shape": "dir",
                        "ref": "github://acme/agents/skills/pdf@main",
                        "path": "agents/alice/.caps/wired/skills/pdf/SKILL.md",
                        "source": {
                            "origin": "remote",
                            "path": "agents/alice/config.toml",
                            "updated_at": "2026-04-18T00:00:00Z",
                            "fingerprint": "stale",
                        },
                        "meta": {"description": "stale"},
                    }
                ],
                "program": {
                    "agent_name": "alice",
                    "source_path": "agents/alice/agent.too",
                    "source_text": "agent alice\n",
                    "body_text": "agent alice\n",
                },
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        cap_state,
        "_fetch_github_directory",
        lambda ref: {"SKILL.md": b"---\ndescription: PDF\n---\n# PDF\n"},
    )

    prepared = state_watcher.prepare_locks(scan_durable_state(toolang_root, "alice"))

    assert prepared.private_lock.entries[0].source.form == "wired"
    assert '"form": "wired"' in lock_path.read_text(encoding="utf-8")


def test_prepare_reuses_private_remote_caps_when_shared_inputs_change(
    tmp_path: Path,
    monkeypatch,
) -> None:
    toolang_root = tmp_path / "toolang"
    fetches: list[str] = []

    monkeypatch.setattr(caps, "_github_repo_default_branch", lambda owner, repo: "main")
    monkeypatch.setattr(caps, "_github_remote_exists", lambda _kind, _ref: True)

    def fake_fetch(ref):
        fetches.append(ref.render())
        return {"SKILL.md": b"---\ndescription: PDF\n---\n# PDF\n"}

    monkeypatch.setattr(cap_state, "_fetch_github_directory", fake_fetch)
    add_remote_entry(
        toolang_root,
        "alice",
        visibility="private",
        kind="skill",
        ref="acme/pdf",
    )
    state_watcher.prepare_locks(scan_durable_state(toolang_root, "alice"))

    _put_cap(
        toolang_root,
        "alice",
        visibility="shared",
        kind="prompt",
        name="style",
        body="Use a direct style.",
    )
    state_watcher.prepare_locks(scan_durable_state(toolang_root, "alice"))

    assert fetches == ["github://acme/agents/skills/pdf@main"]


def test_prepare_reuses_private_remote_caps_when_local_cap_changes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    toolang_root = tmp_path / "toolang"
    fetches: list[str] = []

    monkeypatch.setattr(caps, "_github_repo_default_branch", lambda owner, repo: "main")
    monkeypatch.setattr(caps, "_github_remote_exists", lambda _kind, _ref: True)

    def fake_fetch(ref):
        fetches.append(ref.render())
        return {"SKILL.md": b"---\ndescription: PDF\n---\n# PDF\n"}

    monkeypatch.setattr(cap_state, "_fetch_github_directory", fake_fetch)
    add_remote_entry(
        toolang_root,
        "alice",
        visibility="private",
        kind="skill",
        ref="acme/pdf",
    )
    state_watcher.prepare_locks(scan_durable_state(toolang_root, "alice"))

    monkeypatch.setattr(
        cap_state,
        "_fetch_github_directory",
        lambda ref: pytest.fail(f"unexpected remote fetch: {ref.render()}"),
    )
    _put_cap(
        toolang_root,
        "alice",
        visibility="private",
        kind="prompt",
        name="style",
        body="Use a direct style.",
    )
    prepared = state_watcher.prepare_locks(scan_durable_state(toolang_root, "alice"))

    assert fetches == ["github://acme/agents/skills/pdf@main"]
    assert [(entry.kind, entry.name) for entry in prepared.private_lock.entries] == [
        ("prompt", "style"),
        ("skill", "pdf"),
    ]


def test_concurrent_agent_prepare_reuses_shared_lock_after_another_agent_updates(
    tmp_path: Path,
    monkeypatch,
) -> None:
    toolang_root = tmp_path / "toolang"
    fetches: list[str] = []
    fetch_lock = threading.Lock()

    monkeypatch.setattr(caps, "_github_repo_default_branch", lambda owner, repo: "main")
    monkeypatch.setattr(caps, "_github_remote_exists", lambda _kind, _ref: True)

    AgentCatalog(toolang_root).create("alice")
    AgentCatalog(toolang_root).create("bob")
    state_watcher.prepare_locks(scan_durable_state(toolang_root, "alice"))
    state_watcher.prepare_locks(scan_durable_state(toolang_root, "bob"))
    add_remote_entry(
        toolang_root,
        "default",
        visibility="shared",
        kind="prompt",
        ref="acme/style",
    )

    def fake_fetch(ref) -> bytes:
        with fetch_lock:
            fetches.append(ref.render())
        time.sleep(0.05)
        return b"Use direct language.\n"

    original_load_prepared_optional = state_watcher._load_prepared_optional
    ready = threading.Barrier(2)

    def delayed_load_prepared_optional(root: Path, agent_name: str):
        prepared = original_load_prepared_optional(root, agent_name)
        if agent_name in {"alice", "bob"}:
            ready.wait(timeout=2.0)
        return prepared

    monkeypatch.setattr(cap_state, "_fetch_github_file", fake_fetch)
    monkeypatch.setattr(
        state_watcher, "_load_prepared_optional", delayed_load_prepared_optional
    )

    results: list[object] = []
    errors: list[BaseException] = []

    def prepare(agent_name: str) -> None:
        try:
            results.append(
                state_watcher.prepare_locks(
                    scan_durable_state(toolang_root, agent_name)
                )
            )
        except BaseException as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=prepare, args=(agent_name,))
        for agent_name in ("alice", "bob")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2.0)

    assert not any(thread.is_alive() for thread in threads)
    assert errors == []
    assert fetches == ["github://acme/agents/prompts/style.md@main"]
    assert len(results) == 2
    assert all(
        len(cast(PreparedLocks, result).shared_lock.entries) == 1 for result in results
    )


def test_prepare_reuses_program_ref_caps_when_inline_program_changes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    toolang_root = tmp_path / "toolang"
    fetches: list[str] = []

    monkeypatch.setattr(caps, "_github_remote_exists", lambda _kind, _ref: True)

    def fake_fetch(ref):
        fetches.append(ref.render())
        return b"Remote psyche body.\n"

    monkeypatch.setattr(cap_state, "_fetch_github_file", fake_fetch)
    AgentCatalog(toolang_root).create("alice")
    program_path = toolang_root / "agents" / "alice" / "agent.too"
    program_path.write_text(
        "agent alice\n\nwith psyche github://acme/agents/psyches/steady.md@main\n",
        encoding="utf-8",
    )
    state_watcher.prepare_locks(scan_durable_state(toolang_root, "alice"))
    assert fetches == ["github://acme/agents/psyches/steady.md@main"]

    fetches.clear()
    program_path.write_text(
        program_path.read_text(encoding="utf-8") + "\npsyche local:\n  Inline body.\n",
        encoding="utf-8",
    )
    state_watcher.prepare_locks(scan_durable_state(toolang_root, "alice"))

    assert fetches == []


def test_prepare_fetches_only_changed_program_ref_cap(
    tmp_path: Path,
    monkeypatch,
) -> None:
    toolang_root = tmp_path / "toolang"
    fetches: list[str] = []

    monkeypatch.setattr(caps, "_github_remote_exists", lambda _kind, _ref: True)

    def fake_fetch(ref):
        fetches.append(ref.render())
        return b"Remote psyche body.\n"

    monkeypatch.setattr(cap_state, "_fetch_github_file", fake_fetch)
    AgentCatalog(toolang_root).create("alice")
    program_path = toolang_root / "agents" / "alice" / "agent.too"
    program_path.write_text(
        "agent alice\n\n"
        "with psyche github://acme/agents/psyches/steady.md@main\n"
        "with psyche github://acme/agents/psyches/change.md@main\n",
        encoding="utf-8",
    )
    state_watcher.prepare_locks(scan_durable_state(toolang_root, "alice"))
    assert sorted(fetches) == [
        "github://acme/agents/psyches/change.md@main",
        "github://acme/agents/psyches/steady.md@main",
    ]

    fetches.clear()
    program_path.write_text(
        program_path.read_text(encoding="utf-8").replace(
            "github://acme/agents/psyches/change.md@main",
            "github://acme/agents/psyches/changed.md@main",
        ),
        encoding="utf-8",
    )
    state_watcher.prepare_locks(scan_durable_state(toolang_root, "alice"))

    assert fetches == ["github://acme/agents/psyches/changed.md@main"]


def test_list_entries_reuses_prepared_program_ref_resolution(
    tmp_path: Path,
    monkeypatch,
) -> None:
    toolang_root = tmp_path / "toolang"

    monkeypatch.setattr(caps, "_github_repo_default_branch", lambda owner, repo: "main")
    monkeypatch.setattr(caps, "_github_remote_exists", lambda _kind, _ref: True)
    monkeypatch.setattr(
        cap_state, "_fetch_github_file", lambda ref: b"Remote psyche body.\n"
    )
    AgentCatalog(toolang_root).create("alice")
    program_path = toolang_root / "agents" / "alice" / "agent.too"
    program_path.write_text(
        "agent alice\n\nwith psyche acme/steady\n",
        encoding="utf-8",
    )
    state_watcher.prepare_locks(scan_durable_state(toolang_root, "alice"))

    monkeypatch.setattr(
        caps,
        "_github_repo_default_branch",
        lambda owner, repo: pytest.fail(
            f"unexpected remote branch lookup: {owner}/{repo}"
        ),
    )
    entries = cap_state.list_entries(
        toolang_root, "alice", visibility="private", kinds={"psyche"}
    )

    assert [(entry.name, entry.ref) for entry in entries] == [
        ("steady", "github://acme/agents/psyches/steady.md@main")
    ]


def test_prepare_refetches_remote_caps_when_prepared_output_does_not_match_lock(
    tmp_path: Path,
    monkeypatch,
) -> None:
    toolang_root = tmp_path / "toolang"
    fetch_count = 0

    monkeypatch.setattr(caps, "_github_repo_default_branch", lambda owner, repo: "main")
    monkeypatch.setattr(caps, "_github_remote_exists", lambda _kind, _ref: True)

    def fake_fetch(ref):
        nonlocal fetch_count
        fetch_count += 1
        return {
            "SKILL.md": f"---\ndescription: PDF {fetch_count}\n---\n# PDF\n".encode()
        }

    monkeypatch.setattr(cap_state, "_fetch_github_directory", fake_fetch)
    add_remote_entry(
        toolang_root,
        "alice",
        visibility="private",
        kind="skill",
        ref="acme/pdf",
    )
    state_watcher.prepare_locks(scan_durable_state(toolang_root, "alice"))
    prepared_file = (
        toolang_root
        / "agents"
        / "alice"
        / ".caps"
        / "wired"
        / "skills"
        / "pdf"
        / "SKILL.md"
    )
    prepared_file.write_text(
        "---\ndescription: Corrupt\n---\n# Corrupt\n", encoding="utf-8"
    )

    state_watcher.prepare_locks(scan_durable_state(toolang_root, "alice"))

    assert fetch_count == 2
    assert "PDF 2" in prepared_file.read_text(encoding="utf-8")


def test_remote_skill_add_rejects_missing_github_tree_url(
    tmp_path: Path, monkeypatch
) -> None:
    toolang_root = tmp_path / "toolang"
    monkeypatch.setattr(caps, "_github_remote_exists", lambda _kind, _ref: False)

    with pytest.raises(
        ValueError, match="remote skill not found or missing entry file"
    ):
        add_remote_entry(
            toolang_root,
            "alice",
            visibility="private",
            kind="skill",
            ref="https://github.com/brave/agent-skills/tree/main/skills/answers",
        )

    assert not (toolang_root / "agents" / "alice" / "config.toml").exists()


def test_prepare_materializes_remote_skill_directory(
    tmp_path: Path, monkeypatch
) -> None:
    toolang_root = tmp_path / "toolang"
    config_path = toolang_root / "agents" / "alice" / "config.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        '[skills]\npdf = { ref = "github://anthropics/skills/skills/pdf@main" }\n',
        encoding="utf-8",
    )

    def fake_directory(ref):
        assert ref.render() == "github://anthropics/skills/skills/pdf@main"
        return {
            "SKILL.md": b"---\ndescription: PDF work\n---\n# PDF\n",
            "REFERENCE.md": b"# Reference\n",
        }

    monkeypatch.setattr(cap_state, "_fetch_github_directory", fake_directory)

    durable = scan_durable_state(toolang_root, "alice")
    prepared = state_watcher.prepare_locks(durable)

    assert (
        toolang_root
        / "agents"
        / "alice"
        / ".caps"
        / "wired"
        / "skills"
        / "pdf"
        / "SKILL.md"
    ).read_text(encoding="utf-8") == "---\ndescription: PDF work\n---\n# PDF\n"
    assert (
        toolang_root
        / "agents"
        / "alice"
        / ".caps"
        / "wired"
        / "skills"
        / "pdf"
        / "REFERENCE.md"
    ).read_text(encoding="utf-8") == "# Reference\n"
    assert prepared.private_lock.entries[0].meta["description"] == "PDF work"
    assert prepared.private_lock.entries[0].source.form == "wired"


def test_prepare_materializes_remote_skill_from_program_use(
    tmp_path: Path, monkeypatch
) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(
        toolang_root / "agents" / "alice" / "agent.too",
        "agent alice\n\nwith skill https://github.com/coinbase/agentic-wallet-skills/tree/main/skills/fund\n",
    )

    monkeypatch.setattr(caps, "_github_remote_exists", lambda _kind, _ref: True)
    monkeypatch.setattr(
        cap_state,
        "_fetch_github_directory",
        lambda ref: {
            "SKILL.md": f"---\ndescription: {ref.render()}\n---\n# Fund\n".encode()
        },
    )

    durable = scan_durable_state(toolang_root, "alice")
    prepared = state_watcher.prepare_locks(durable)
    state = load_agent_state(prepared)

    skill_path = (
        toolang_root
        / "agents"
        / "alice"
        / ".caps"
        / "ref"
        / "skills"
        / "fund"
        / "SKILL.md"
    )
    assert skill_path.read_text(encoding="utf-8").startswith(
        "---\ndescription: github://coinbase/"
    )
    entry = prepared.private_lock.entries[0]
    assert entry.name == "fund"
    assert entry.ref == "github://coinbase/agentic-wallet-skills/skills/fund@main"
    assert entry.source.origin == "remote"
    assert entry.source.form == "ref"
    assert entry.source.path == "agents/alice/agent.too"
    assert entry.source.line == 3
    assert [entry.path for entry in state.caps] == [
        "agents/alice/.caps/ref/skills/fund/SKILL.md"
    ]
    with pytest.raises(TypeError):
        cast(Any, state.caps[0].meta)["description"] = "Changed"
    lock_data = json.loads(
        (toolang_root / "agents" / "alice" / ".caps" / "lock.json").read_text(
            encoding="utf-8"
        )
    )
    program = cast(
        dict[str, object], cast(dict[str, object], lock_data["prepared"])["program"]
    )
    uses = cast(list[dict[str, object]], program["uses"])
    assert uses[0]["cap"] == 0
    assert "prepared" not in uses[0]


def test_prepare_materializes_embedded_caps_for_caps_api(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(
        toolang_root / "agents" / "alice" / "agent.too",
        (
            "agent alice\n\n"
            "psyche reviewer:\n"
            "  Prefer concrete findings.\n\n"
            "service github:\n"
            "  description = Use when the agent needs GitHub MCP access.\n"
            "  protocol = http\n"
            "  target = https://mcp.github.com/mcp\n\n"
            "  Use this service when the agent needs GitHub access.\n\n"
            "prompt summarize:\n"
            "  params = style, audience?\n\n"
            "  Summarize the user's request in a {{style}} style.\n"
            "  Target audience: {{audience}}\n"
        ),
    )

    durable = scan_durable_state(toolang_root, "alice")
    prepared = state_watcher.prepare_locks(durable)
    state = load_agent_state(prepared)

    psyche_path = (
        toolang_root
        / "agents"
        / "alice"
        / ".caps"
        / "inline"
        / "psyches"
        / "reviewer.md"
    )
    assert psyche_path.read_text(encoding="utf-8") == "Prefer concrete findings."
    service_path = (
        toolang_root
        / "agents"
        / "alice"
        / ".caps"
        / "inline"
        / "services"
        / "github.md"
    )
    service_content = service_path.read_text(encoding="utf-8")
    assert "description: Use when the agent needs GitHub MCP access." in service_content
    assert "Use this service when the agent needs GitHub access." in service_content
    prompt_path = (
        toolang_root
        / "agents"
        / "alice"
        / ".caps"
        / "inline"
        / "prompts"
        / "summarize.md"
    )
    prompt_content = prompt_path.read_text(encoding="utf-8")
    assert (
        prompt_content
        == (
            "---\n"
            "params: style, audience?\n"
            "---\n\n"
            "Summarize the user's request in a {{style}} style.\n"
            "Target audience: {{audience}}\n"
        ).rstrip()
    )
    entries_by_kind = {entry.kind: entry for entry in prepared.private_lock.entries}
    assert set(entries_by_kind) == {"prompt", "psyche", "service"}
    assert entries_by_kind["psyche"].name == "reviewer"
    assert entries_by_kind["psyche"].ref == "inline://psyches/reviewer"
    assert entries_by_kind["psyche"].source.form == "inline"
    assert entries_by_kind["psyche"].source.line == 3
    assert entries_by_kind["service"].name == "github"
    assert entries_by_kind["service"].ref == "inline://services/github"
    assert entries_by_kind["service"].source.form == "inline"
    assert entries_by_kind["service"].source.line == 6
    assert entries_by_kind["prompt"].name == "summarize"
    assert entries_by_kind["prompt"].ref == "inline://prompts/summarize"
    assert entries_by_kind["prompt"].source.origin == "local"
    assert entries_by_kind["prompt"].source.form == "inline"
    assert entries_by_kind["prompt"].source.path == "agents/alice/agent.too"
    assert entries_by_kind["prompt"].source.line == 13
    assert tuple(entry.path for entry in state.caps) == (
        "agents/alice/.caps/inline/prompts/summarize.md",
        "agents/alice/.caps/inline/psyches/reviewer.md",
        "agents/alice/.caps/inline/services/github.md",
    )
    lock_data = json.loads(
        (toolang_root / "agents" / "alice" / ".caps" / "lock.json").read_text(
            encoding="utf-8"
        )
    )
    program = cast(
        dict[str, object], cast(dict[str, object], lock_data["prepared"])["program"]
    )
    program_caps = cast(list[dict[str, object]], program["caps"])
    assert {item["name"]: item["cap"] for item in program_caps} == {
        "github": 2,
        "reviewer": 1,
        "summarize": 0,
    }
    assert all("prepared" not in item for item in program_caps)

    context = _build_context(
        toolang_root=toolang_root,
        agent_name="alice",
        enabled_features=("inspect",),
    )
    app = _create_test_app(context)
    with TestClient(app) as client:
        psyche_response = client.get("/api/v1/psyches/reviewer")
        assert psyche_response.status_code == 200
        psyche_detail = psyche_response.json()["item"]
        assert psyche_detail["origin"] == "local"
        assert psyche_detail["form"] == "inline"
        assert psyche_detail["scope"] == "here"
        assert psyche_detail["definition_file"] == "agents/alice/agent.too"
        assert psyche_detail["line"] == 3
        assert psyche_detail["content"] == "Prefer concrete findings."

        service_response = client.get("/api/v1/services/github")
        assert service_response.status_code == 200
        service_detail = service_response.json()["item"]
        assert (
            service_detail["description"]
            == "Use when the agent needs GitHub MCP access."
        )
        assert service_detail["origin"] == "local"
        assert service_detail["form"] == "inline"
        assert service_detail["scope"] == "here"
        assert service_detail["definition_file"] == "agents/alice/agent.too"
        assert service_detail["line"] == 6
        assert service_detail["content"] == service_content

        list_response = client.get("/api/v1/prompts")
        assert list_response.status_code == 200
        assert list_response.json()["items"] == [
            {
                "name": "summarize",
                "description": None,
                "scope": "here",
                "origin": "local",
                "form": "inline",
                "ref": "inline://prompts/summarize",
                "definition_file": "agents/alice/agent.too",
                "editable": False,
                "line": 13,
            }
        ]

        detail_response = client.get("/api/v1/prompts/summarize")
        assert detail_response.status_code == 200
        detail = detail_response.json()["item"]
        assert detail["kind"] == "prompt"
        assert detail["content"] == prompt_content
        assert detail["files"] is None


def test_prepare_rejects_duplicate_embedded_cap_names(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(
        toolang_root / "agents" / "alice" / "agent.too",
        (
            "agent alice\n\n"
            "prompt summarize:\n"
            "  First body.\n\n"
            "prompt summarize:\n"
            "  Second body.\n"
        ),
    )

    durable = scan_durable_state(toolang_root, "alice")
    with pytest.raises(ToolangError, match=r"Duplicate prompt name 'summarize'"):
        state_watcher.prepare_locks(durable)

    assert not (
        toolang_root
        / "agents"
        / "alice"
        / ".caps"
        / "inline"
        / "prompts"
        / "summarize.md"
    ).exists()


def test_prepare_builds_program_into_private_lock(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(toolang_root / "agents" / "alice" / "agent.too", "agent alice\n")

    durable = scan_durable_state(toolang_root, "alice")
    prepared = state_watcher.prepare_locks(durable)
    lock_data = json.loads(
        (toolang_root / "agents" / "alice" / ".caps" / "lock.json").read_text(
            encoding="utf-8"
        )
    )

    assert prepared.program_source.agent_name == "alice"
    assert prepared.program_source.source_path == "agents/alice/agent.too"
    program_snapshot = cast(
        dict[str, object], prepared.private_lock.to_snapshot()["program"]
    )
    assert program_snapshot["agent_name"] == "alice"
    assert program_snapshot["agics"] == []
    assert lock_data["schema"] == 1
    assert "visibility" not in lock_data
    assert "entries" not in lock_data
    lock_program = cast(
        dict[str, object], cast(dict[str, object], lock_data["prepared"])["program"]
    )
    assert list(lock_program) == [
        "source",
        "source_text",
        "uses",
        "structs",
        "contexts",
        "instructs",
        "caps",
        "agics",
    ]


def test_prepare_preserves_optional_struct_fields_in_lock(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(
        toolang_root / "agents" / "alice" / "agent.too",
        "agent alice\n\nstruct Result:\n  summary?: Text\n",
    )

    state_watcher.prepare_locks(scan_durable_state(toolang_root, "alice"))
    lock_data = json.loads(
        (toolang_root / "agents" / "alice" / ".caps" / "lock.json").read_text(
            encoding="utf-8"
        )
    )
    program = cast(
        dict[str, object], cast(dict[str, object], lock_data["prepared"])["program"]
    )
    structs = cast(list[dict[str, object]], program["structs"])
    fields = cast(list[dict[str, object]], structs[0]["fields"])

    assert fields[0]["optional"] is True


def test_prepare_skips_shared_caps_dir_without_root_inputs(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(toolang_root / "agents" / "alice" / "agent.too", "agent alice\n")

    durable = scan_durable_state(toolang_root, "alice")
    prepared = state_watcher.prepare_locks(durable)

    assert not (toolang_root / ".caps").exists()
    assert (toolang_root / "agents" / "alice" / ".caps" / "lock.json").is_file()
    assert prepared.shared_lock.entries == ()
    assert prepared.private_lock.program_source is not None


def test_prepare_rewrites_legacy_private_lock_missing_program(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(toolang_root / "agents" / "alice" / "agent.too", "agent alice\n")

    durable = scan_durable_state(toolang_root, "alice")
    shared_lock, shared_files = build_visibility_lock(durable, visibility="shared")
    write_prepared_lock(toolang_root, shared_lock, files=shared_files)
    legacy_private_lock, legacy_private_files = build_visibility_lock(
        durable, visibility="private"
    )
    write_prepared_lock(toolang_root, legacy_private_lock, files=legacy_private_files)

    prepared = state_watcher.prepare_locks(durable)

    assert prepared.private_lock.program_source is not None
    assert prepared.private_lock.program_source.agent_name == "alice"


def test_prepare_fetches_remote_caps_with_bounded_concurrency(
    tmp_path: Path, monkeypatch
) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(
        toolang_root / "config.toml",
        "[psyches]\n"
        'alpha = { ref = "github://acme/agents/psyches/alpha.md@main" }\n'
        'bravo = { ref = "github://acme/agents/psyches/bravo.md@main" }\n',
    )
    _write_text(toolang_root / "agents" / "alice" / "agent.too", "agent alice\n")
    active = 0
    max_active = 0
    started = threading.Event()
    release = threading.Event()
    lock = threading.Lock()

    def fake_fetch(ref) -> bytes:
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
            if active == 2:
                started.set()
        assert started.wait(timeout=1.0)
        release.set()
        assert release.wait(timeout=1.0)
        with lock:
            active -= 1
        return f"---\ndescription: {ref.path}\n---\nBody\n".encode("utf-8")

    monkeypatch.setattr(cap_state, "_fetch_github_file", fake_fetch)

    durable = scan_durable_state(toolang_root, "alice")
    lock_record, files = build_visibility_lock(durable, visibility="shared")

    assert max_active == 2
    assert [entry.name for entry in lock_record.entries] == ["alpha", "bravo"]
    assert sorted(files) == [
        ".caps/wired/psyches/alpha.md",
        ".caps/wired/psyches/bravo.md",
    ]


def test_caps_list_and_remove_remote_entries(tmp_path: Path, monkeypatch) -> None:
    toolang_root = tmp_path / "toolang"
    monkeypatch.setattr(caps, "_github_repo_default_branch", lambda owner, repo: "main")
    monkeypatch.setattr(caps, "_github_remote_exists", lambda _kind, _ref: True)

    add_remote_entry(
        toolang_root,
        "alice",
        visibility="private",
        kind="skill",
        ref="acme/reviewer",
    )

    entries = list_entries(toolang_root, "alice", visibility="private", kinds={"skill"})

    assert [
        (entry.source.origin, entry.source.form, entry.path) for entry in entries
    ] == [("remote", "wired", "agents/alice/.caps/wired/skills/reviewer/SKILL.md")]
    assert (
        remove_remote_entry(
            toolang_root, "alice", visibility="private", kind="skill", name="reviewer"
        )
        is True
    )
    assert (
        list_entries(toolang_root, "alice", visibility="private", kinds={"skill"}) == ()
    )


def test_runs_bind_latest_agent_state(tmp_path: Path) -> None:
    async def run_test() -> None:
        toolang_root = tmp_path / "toolang"
        prompt_path = toolang_root / "agents" / "alice" / "prompts" / "rewrite.md"
        _write_text(prompt_path, "---\ndescription: v1\n---\nPrompt v1\n")
        context = _build_context(
            toolang_root=toolang_root,
            agent_name="alice",
            enabled_features=("chat", "watch"),
        )

        with _patched_executor_execution():
            async with _running_context(
                context,
                enabled_features=("chat", "watch"),
                loop_intervals_ms={"watch": 10.0},
            ):
                first_fingerprint = context.get_agent_state().fingerprint
                await context.executor.start(
                    RunRequest(
                        group="chat",
                        origin="chat",
                        thread_id="thread-1",
                        input="first",
                    ),
                    context.get_agent_state(),
                )
                _write_text(prompt_path, "---\ndescription: v2\n---\nPrompt v2\n")
                changed = await _wait_for_fingerprint_change(context, first_fingerprint)
                assert changed
                second_fingerprint = context.get_agent_state().fingerprint
                await context.executor.start(
                    RunRequest(
                        group="chat",
                        origin="chat",
                        thread_id="thread-1",
                        input="second",
                    ),
                    context.get_agent_state(),
                )
                fingerprints = {}
                for run in context.store.list_runs(thread_id="thread-1", limit=None):
                    command = context.store.get_command(run_id=run.id, index=0)
                    assert command is not None and command.input is not None
                    fingerprints[message_text(command.input.parts)] = run.context[
                        "state_fingerprint"
                    ]
                assert fingerprints == {
                    "first": first_fingerprint,
                    "second": second_fingerprint,
                }

    asyncio.run(run_test())


def test_new_task_reloads_into_agent_state_and_tasks_endpoint(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(toolang_root / "agents" / "alice" / "agent.too", "agent alice\n")
    context = _build_context(
        toolang_root=toolang_root,
        agent_name="alice",
        enabled_features=("inspect", "watch"),
    )
    app = _create_test_app(context)

    with TestClient(app) as client:
        first_fingerprint = context.get_agent_state().fingerprint
        _write_text(
            toolang_root / "agents" / "alice" / "tasks" / "review.md",
            "---\ntitle: Review\n---\nReview the current plan.\n",
        )
        for _ in range(200):
            tasks = client.get("/api/v1/tasks").json()["items"]
            if tasks:
                break
            time.sleep(0.01)

        tasks = client.get("/api/v1/tasks").json()["items"]

    assert context.get_agent_state().fingerprint == first_fingerprint
    assert len(tasks) == 1
    assert tasks[0]["kind"] == "task"
    assert tasks[0]["title"] == "Review"
    assert tasks[0]["lifecycle"] == "ready"
    assert tasks[0]["status"] == "todo"
    assert tasks[0]["remote_ref"] is None
    assert tasks[0]["remote_status"] is None
    assert tasks[0]["runtime"]["thread_id"] == f"task_{tasks[0]['id']}"
    assert tasks[0]["runtime"]["last_run"] is None
    assert tasks[0]["runtime"]["next_run"] is None
    assert tasks[0]["path"] == "tasks/review.md"


def test_jobs_api_supports_task_crud(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(toolang_root / "agents" / "alice" / "agent.too", "agent alice\n")
    context = _build_context(
        toolang_root=toolang_root,
        agent_name="alice",
        enabled_features=("inspect", "manage"),
    )
    app = _create_test_app(context)

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/tasks",
            json={
                "title": "Review API",
                "body": "Review the new API surface.",
            },
        )
        assert created.status_code == 201
        task = created.json()["item"]
        task_id = task["id"]

        assert task["kind"] == "task"
        assert task["lifecycle"] == "ready"
        assert task["status"] == "todo"
        assert task["body"] == "Review the new API surface."
        assert task["runtime"]["thread_id"] == f"task_{task_id}"

        jobs = client.get("/api/v1/jobs").json()["items"]
        assert [(item["kind"], item["id"]) for item in jobs] == [("task", task_id)]
        assert "body" not in jobs[0]
        assert client.delete(f"/api/v1/tasks/{task_id}").status_code == 405

        detail = client.get(f"/api/v1/tasks/{task_id}").json()["item"]
        assert detail["body"] == "Review the new API surface."

        updated = client.patch(
            f"/api/v1/tasks/{task_id}",
            json={
                "body": "Updated task body.",
            },
        )
        assert updated.status_code == 200
        task = updated.json()["item"]
        assert task["lifecycle"] == "ready"
        assert task["body"] == "Updated task body."

        archived = client.post(f"/api/v1/tasks/{task_id}/archive")
        assert archived.status_code == 200
        task = archived.json()["item"]
        assert task["lifecycle"] == "archived"
        assert task["path"].startswith("archive/tasks/")

        assert client.get("/api/v1/tasks").json()["items"] == []
        assert client.get(f"/api/v1/tasks/{task_id}").status_code == 404
        archived_tasks = client.get("/api/v1/tasks/archived").json()["items"]
        assert [item["id"] for item in archived_tasks] == [task_id]
        archived_detail = client.get(f"/api/v1/tasks/archived/{task_id}").json()["item"]
        assert archived_detail["body"] == "Updated task body."

        reopened = client.post(f"/api/v1/tasks/{task_id}/ready")
        assert reopened.status_code == 200
        task = reopened.json()["item"]
        assert task["lifecycle"] == "ready"
        assert task["path"] == f"tasks/{task_id}.md"
        assert [item["id"] for item in client.get("/api/v1/jobs").json()["items"]] == [
            task_id
        ]

        rearchived = client.post(f"/api/v1/tasks/{task_id}/archive")
        assert rearchived.status_code == 200

        deleted = client.delete(f"/api/v1/tasks/archived/{task_id}")
        assert deleted.status_code == 200
        assert deleted.json() == {"deleted": True, "id": task_id, "kind": "task"}
        assert client.get("/api/v1/tasks/archived").json()["items"] == []

        updates = client.get("/api/v1/events").json()["items"]

    assert [item["kind"] for item in updates] == [
        "task_changed",
        "task_changed",
        "task_changed",
        "task_changed",
        "task_changed",
        "task_changed",
    ]
    assert [item["payload"]["action"] for item in updates] == [
        "created",
        "updated",
        "archived",
        "ready",
        "archived",
        "deleted",
    ]


def test_jobs_api_projects_active_run_tasks_as_running(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(toolang_root / "agents" / "alice" / "agent.too", "agent alice\n")
    _write_text(
        toolang_root / "agents" / "alice" / "tasks" / "remote.md",
        "---\ntitle: XBY-26 - test\n---\n"
        "Link: https://linear.app/xby/issue/XBY-26/test\n"
        "Status: Backlog\n",
    )
    context = _build_context(
        toolang_root=toolang_root,
        agent_name="alice",
        enabled_features=("inspect", "manage"),
    )
    task = JobCatalog(toolang_root, "alice").list(kind="task")[0].document
    project_run_start(context.store,
        run_id="run-active-task",
        thread_id=task.thread_id(),
        origin="task",
        input=Message.user(task.body),
    )
    app = _create_test_app(context)

    with TestClient(app) as client:
        item = client.get("/api/v1/tasks").json()["items"][0]

    assert item["id"] == task.task_id()
    assert item["status"] == "todo"
    assert item["remote_ref"] == "XBY-26"
    assert item["remote_status"] == "Backlog"
    assert item["runtime"]["last_run"]["id"] == "run-active-task"
    assert item["runtime"]["last_run"]["status"] == "running"


def test_jobs_api_supports_chore_crud(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(toolang_root / "agents" / "alice" / "agent.too", "agent alice\n")
    context = _build_context(
        toolang_root=toolang_root,
        agent_name="alice",
        enabled_features=("inspect", "manage"),
    )
    app = _create_test_app(context)

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/chores",
            json={
                "title": "Check PRs",
                "body": "Check stale pull requests.",
                "schedule": "FREQ=HOURLY;INTERVAL=6",
            },
        )
        assert created.status_code == 201
        chore = created.json()["item"]
        chore_id = chore["id"]

        assert chore["kind"] == "chore"
        assert chore["lifecycle"] == "ready"
        assert chore["status"] == "todo"
        assert chore["schedule"] == "FREQ=HOURLY;INTERVAL=6"
        assert chore["body"] == "Check stale pull requests."
        assert chore["runtime"]["thread_id"] == f"chore_{chore_id}"

        jobs = client.get("/api/v1/jobs?kind=chore").json()["items"]
        assert [(item["kind"], item["id"]) for item in jobs] == [("chore", chore_id)]
        assert "body" not in jobs[0]
        assert client.delete(f"/api/v1/jobs/{chore_id}").status_code == 405

        invalid = client.patch(
            f"/api/v1/chores/{chore_id}",
            json={"schedule": "not an rrule"},
        )
        assert invalid.status_code == 400

        updated = client.patch(
            f"/api/v1/chores/{chore_id}",
            json={
                "schedule": "FREQ=DAILY",
                "body": "Updated chore body.",
            },
        )
        assert updated.status_code == 200
        chore = updated.json()["item"]
        assert chore["lifecycle"] == "ready"
        assert chore["schedule"] == "FREQ=DAILY"
        assert chore["body"] == "Updated chore body."

        archived = client.post(f"/api/v1/chores/{chore_id}/archive")
        assert archived.status_code == 200
        chore = archived.json()["item"]
        assert chore["lifecycle"] == "archived"
        assert chore["path"].startswith("archive/chores/")

        assert client.get("/api/v1/chores").json()["items"] == []
        assert client.get(f"/api/v1/chores/{chore_id}").status_code == 404
        archived_chores = client.get("/api/v1/jobs/archived?kind=chore").json()["items"]
        assert [item["id"] for item in archived_chores] == [chore_id]

        reopened = client.post(f"/api/v1/chores/{chore_id}/ready")
        assert reopened.status_code == 200
        chore = reopened.json()["item"]
        assert chore["lifecycle"] == "ready"
        assert chore["path"] == f"chores/{chore_id}.md"
        assert [
            item["id"] for item in client.get("/api/v1/chores").json()["items"]
        ] == [chore_id]

        rearchived = client.post(f"/api/v1/chores/{chore_id}/archive")
        assert rearchived.status_code == 200

        deleted = client.delete(f"/api/v1/chores/archived/{chore_id}")
        assert deleted.status_code == 200
        assert deleted.json() == {"deleted": True, "id": chore_id, "kind": "chore"}
        assert client.get("/api/v1/chores/archived").json()["items"] == []

        updates = client.get("/api/v1/events").json()["items"]

    assert [item["kind"] for item in updates] == [
        "chore_changed",
        "chore_changed",
        "chore_changed",
        "chore_changed",
        "chore_changed",
        "chore_changed",
    ]
    assert [item["payload"]["action"] for item in updates] == [
        "created",
        "updated",
        "archived",
        "ready",
        "archived",
        "deleted",
    ]


def test_new_task_reloads_and_pulse_runs_it(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(toolang_root / "agents" / "alice" / "agent.too", "agent alice\n")
    context = _build_context(
        toolang_root=toolang_root,
        agent_name="alice",
        enabled_features=("inspect", "watch", "pulse"),
    )
    context.config.set("components.trigger.watch.interval_ms", 10.0)
    context.config.set("components.trigger.watch.debounce_ms", 10.0)
    context.config.set("components.trigger.pulse.interval_ms", 10.0)
    app = _create_test_app(context)
    completed: list[RunRecord] = []

    with _patched_executor_execution():
        with TestClient(app):
            _write_text(
                toolang_root / "agents" / "alice" / "tasks" / "review.md",
                "---\ntitle: Review\n---\nReview the current plan.\n",
            )
            for _ in range(200):
                completed = [
                    run
                    for run in context.store.list_runs(limit=None)
                    if run.origin == "task"
                    and run.status in {"finished", "failed", "canceled"}
                ]
                if completed:
                    break
                time.sleep(0.01)
    assert completed
    run = completed[0]
    run_store = RunStore(run_store_path(toolang_root, "alice"))
    try:
        command = run_store.get_command(run_id=run.id, index=0)
    finally:
        run_store.close()
    assert run.context["group"] == "scheduler:task"
    assert run.origin == "task"
    assert command is not None and command.input == Message.user(
        "Review the current plan."
    )
    assert run.thread_id.startswith("task_")


def test_task_run_includes_local_task_protocol_in_prompt_bundle(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(toolang_root / "agents" / "alice" / "agent.too", "agent alice\n")
    _write_text(
        toolang_root / "agents" / "alice" / "services" / "linear.md",
        "---\n"
        "description: Linear MCP\n"
        "transport: http\n"
        "target: https://mcp.linear.app/mcp\n"
        "---\n",
    )
    _write_text(
        toolang_root / "agents" / "alice" / "tasks" / "review.md",
        "---\ntitle: Review\n---\nReview the current plan.\n",
    )
    context = _build_context(
        toolang_root=toolang_root,
        agent_name="alice",
        enabled_features=("pulse",),
    )
    task_entry = JobCatalog(toolang_root, "alice").list(kind="task")[0]
    task = task_entry.document
    definition = AgentJobs.load(toolang_root, "alice", context.get_agent_state().program).get(
        "task", task.task_id()
    )
    assert definition is not None
    bound = bind_run_request(
        context,
        RunRequest(
            group="pulse",
            origin="task",
            thread_id=task.thread_id(),
            input=task.body,
            metadata={"job": definition.run_metadata()},
        ),
    )
    task_entry.path.write_text("Changed after the run was bound.\n", encoding="utf-8")

    bundle = RunInput.from_binding(context.executor, bound)

    assert bundle.snapshot is not None
    assert bundle.snapshot.task == SnapshotTask(
        provider="local",
        ref=task.thread_id(),
        name="review",
        body=task.body,
        thread_id=task.thread_id(),
        path=str(toolang_root / "agents" / "alice" / "tasks" / "review.md"),
    )
    assert bundle.snapshot.task_services == SnapshotTaskServices(
        provider="local",
        read=True,
        write=True,
        comment=False,
        path=str(toolang_root / "agents" / "alice" / "tasks" / "review.md"),
    )
    instructions = bundle.instructions()
    assert "Treat the user's message as the current task input." in instructions
    assert "Current task:" in instructions
    assert (
        f"- Path: {toolang_root / 'agents' / 'alice' / 'tasks' / 'review.md'}"
        in instructions
    )
    assert "The runtime records completion status from the run outcome." in instructions
    assert "If this task mirrors a remote work item" in instructions
    assert (
        "Do not report the task complete just because you fetched or verified the remote item"
        in instructions
    )
    assert "update the remote status when supported" in instructions
    service_schema = (
        bundle.tools()["service_use__bridge_start"]
        .definition()
        .parameters["properties"]["service"]
    )
    assert service_schema["enum"] == ["linear"]


def test_chore_run_includes_remote_task_sync_protocol_in_prompt_bundle(
    tmp_path: Path,
) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(toolang_root / "agents" / "alice" / "agent.too", "agent alice\n")
    _write_text(
        toolang_root / "agents" / "alice" / "chores" / "sync.md",
        "---\ntitle: Sync remote tasks\nschedule: FREQ=MINUTELY;INTERVAL=1\n---\n"
        "Sync remote issues into local tasks.\n",
    )
    context = _build_context(
        toolang_root=toolang_root,
        agent_name="alice",
        enabled_features=("pulse",),
    )
    chore = JobCatalog(toolang_root, "alice").list(kind="chore")[0].document
    definition = AgentJobs.load(toolang_root, "alice", context.get_agent_state().program).get(
        "chore", chore.chore_id()
    )
    assert definition is not None
    bound = bind_run_request(
        context,
        RunRequest(
            group="pulse:chore",
            origin="chore",
            thread_id=chore.thread_id(),
            input=chore.body,
            metadata={"job": definition.run_metadata()},
        ),
    )

    instructions = RunInput.from_binding(context.executor, bound).instructions()

    assert "Treat the user's message as the current chore input." in instructions
    assert (
        "When creating or updating local tasks that mirror remote work items"
        in instructions
    )
    assert (
        "include the remote title, description, link, update timestamp, status"
        in instructions
    )
    assert "match by remote_ref, remote URL, or remote id" in instructions
    assert (
        "instead of creating another local task for the same remote item"
        in instructions
    )
    assert "update the remote status when supported" in instructions


def test_pulse_marks_finished_task_job_done(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(toolang_root / "agents" / "alice" / "agent.too", "agent alice\n")
    _write_text(
        toolang_root / "agents" / "alice" / "tasks" / "review.md",
        "---\ntitle: Review\n---\nReview the current plan.\n",
    )
    context = _build_context(
        toolang_root=toolang_root,
        agent_name="alice",
        enabled_features=("pulse",),
    )
    store = open_job_store(toolang_root, "alice")
    definitions = AgentJobs.load(toolang_root, "alice", context.get_agent_state().program)
    store.reconcile(jobs=definitions, kind="task")
    claimed = store.claim_due(
        jobs=definitions,
        kind="task",
        run_id="run-task-done",
        now=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    assert claimed is not None
    run_id = claimed.run_id
    project_run_start(context.store,
        run_id=run_id,
        thread_id=claimed.job.thread_id,
        origin="task",
        input=Message.user(claimed.definition.input),
    )
    project_run_end(context.store,
        run_id=run_id, status="finished", finished_at="2026-01-01T00:00:07Z"
    )

    run = context.store.get_run(run_id=run_id)
    assert run is not None
    asyncio.run(_record_pulse_result(context, store, run))

    updated = store.get(job_id=claimed.job.job_id, kind="task")
    store.close()
    assert updated is not None
    assert updated.status == "done"


def test_pulse_marks_failed_task_job_failed(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(toolang_root / "agents" / "alice" / "agent.too", "agent alice\n")
    _write_text(
        toolang_root / "agents" / "alice" / "tasks" / "review.md",
        "---\ntitle: Review\n---\nReview the current plan.\n",
    )
    context = _build_context(
        toolang_root=toolang_root,
        agent_name="alice",
        enabled_features=("pulse",),
    )
    store = open_job_store(toolang_root, "alice")
    definitions = AgentJobs.load(toolang_root, "alice", context.get_agent_state().program)
    store.reconcile(jobs=definitions, kind="task")
    claimed = store.claim_due(
        jobs=definitions,
        kind="task",
        run_id="run-task-failed",
        now=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    assert claimed is not None
    project_run_start(context.store,
        run_id=claimed.run_id,
        thread_id=claimed.job.thread_id,
        origin="task",
        input=Message.user(claimed.definition.input),
    )
    project_run_end(context.store,
        run_id=claimed.run_id, status="failed", finished_at="2026-01-01T00:00:07Z"
    )

    run = context.store.get_run(run_id=claimed.run_id)
    assert run is not None
    asyncio.run(_record_pulse_result(context, store, run))

    updated = store.get(job_id=claimed.job.job_id, kind="task")
    store.close()
    assert updated is not None
    assert updated.status == "failed"


def test_assemble_run_input_prefers_agic_model_over_activation_default(
    tmp_path: Path,
) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(
        toolang_root / "agents" / "alice" / "agent.too",
        "agent alice\n\nagic chat:\n  models = openai/gpt-5\n\n  Reply directly.\n",
    )
    context = _build_context(
        toolang_root=toolang_root,
        agent_name="alice",
        enabled_features=("chat",),
    )
    context.executor.model_environ = {"OPENAI_API_KEY": "secret"}
    context.config.set("models.default_selector", "openai/gpt-5[openai]")
    bound = bind_run_request(
        context,
        RunRequest(group="chat", origin="chat", input="hello"),
    )

    bundle = RunInput.from_binding(context.executor, bound)

    assert bundle.model_selector(context.executor) == "openai/gpt-5[openai]"
    assert bundle.debug["activation_default_model"] == "openai/gpt-5[openai]"


def test_assemble_run_input_accepts_explicit_run_model_within_allowed_set(
    tmp_path: Path,
) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(
        toolang_root / "agents" / "alice" / "agent.too",
        "agent alice\n\nagic chat:\n  models = openai/gpt-5, openai/o3\n\n  Reply directly.\n",
    )
    context = _build_context(
        toolang_root=toolang_root,
        agent_name="alice",
        enabled_features=("chat",),
    )
    context.executor.model_environ = {"OPENAI_API_KEY": "secret"}
    context.config.set(
        "models.allowed_selectors", ("openai/o3[openai]", "openai/gpt-5[openai]")
    )
    bound = bind_run_request(
        context,
        RunRequest(
            group="chat",
            origin="chat",
            input="hello",
            model_selector="openai/gpt-5[openai]",
        ),
    )

    bundle = RunInput.from_binding(context.executor, bound)
    runtime_context = cast(dict[str, object], bundle.system_template_context["runtime"])
    model_context = cast(dict[str, object], runtime_context["model"])

    assert bundle.effective_model_selectors(context.executor) == (
        "openai/o3[openai]",
        "openai/gpt-5[openai]",
    )
    assert bundle.model_selector(context.executor) == "openai/gpt-5[openai]"
    assert model_context["ref"] == "openai/gpt-5"
    assert bundle.debug["requested_model_selector"] == "openai/gpt-5[openai]"


def test_assemble_run_input_uses_activation_default_when_agic_omits_one(
    tmp_path: Path,
) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(
        toolang_root / "agents" / "alice" / "agent.too",
        "agent alice\n\nagic chat:\n  instruct:\n    Reply directly.\n",
    )
    context = _build_context(
        toolang_root=toolang_root,
        agent_name="alice",
        enabled_features=("chat",),
    )
    context.config.set("models.default_selector", "openai/gpt-5[openai]")
    bound = bind_run_request(
        context,
        RunRequest(group="chat", origin="chat", input="hello"),
    )

    bundle = RunInput.from_binding(context.executor, bound)

    assert bundle.model_selector(context.executor) == "openai/gpt-5[openai]"
    assert bundle.debug["activation_default_model"] == "openai/gpt-5[openai]"


def test_script_run_thread_id_uses_script_prefix(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(
        toolang_root / "agents" / "alice" / "agent.too",
        "agent alice\n\nagic summarize(in: Part[]):\n  Summarize it.\n",
    )
    context = _build_context(
        toolang_root=toolang_root,
        agent_name="alice",
        enabled_features=("chat",),
    )
    bound = bind_run_request(
        context,
        RunRequest(
            group="script",
            origin="script",
            executable_name="summarize",
            input="hello",
        ),
    )

    assert bound.thread_id.startswith("script_")
    assert len(bound.thread_id) == len("script_") + 8
    assert bound.thread_id != "script_summarize"


def test_assemble_file_run_input_includes_authored_file_agic_message(
    tmp_path: Path,
) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(
        toolang_root / "agents" / "alice" / "agent.too",
        (
            "agent alice\n\n"
            "agic file(in: Part[]):\n"
            "  tools = filesystem/*\n\n"
            "  user:\n"
            "    Write one short text summary to outbox/index.md.\n"
        ),
    )
    context = _build_context(
        toolang_root=toolang_root,
        agent_name="alice",
        enabled_features=("chat",),
    )
    context.config.set("models.default_selector", "openai/gpt-5[openai]")
    bound = bind_run_request(
        context,
        RunRequest(
            group="file",
            origin="file",
            executable_name="file",
            input="file body",
        ),
    )

    bundle = RunInput.from_binding(context.executor, bound)

    text = message_text(bundle.message.parts)
    assert "Write one short text summary to outbox/index.md." in text
    assert "file body" in text


def test_child_run_input_uses_authored_agic_message_and_current_input(
    tmp_path: Path,
) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(
        toolang_root / "agents" / "alice" / "agent.too",
        (
            "agent alice\n\n"
            "agic draft(in: Text) -> Text:\n"
            "  user: Draft {{in}}.\n\n"
            "flow pipeline(in: Text) -> Text:\n"
            "  run draft\n"
        ),
    )
    context = _build_context(
        toolang_root=toolang_root,
        agent_name="alice",
        enabled_features=("chat",),
    )
    root = bind_run_request(
        context,
        RunRequest(
            group="chat",
            origin="chat",
            executable_kind="flow",
            executable_name="pipeline",
            input="original input",
        ),
    )
    child = replace(
        root,
        run_id="run_child",
        executable_kind="agic",
        executable_name="draft",
        input_text="current input",
        message=Message.user("current input"),
        metadata={
            "root": root.run_id,
            "call": "run",
            "invoke_params": {"in": "stale input"},
        },
    )

    draft = context.get_agent_state().program.find_agic("draft")
    assert draft is not None
    bundle = RunInput.from_agic(context.executor, child, draft)
    text = message_text(bundle.messages()[0].parts)

    assert text.endswith("Draft current input.")
    assert "stale input" not in text


def test_top_level_chat_agic_uses_its_authored_message(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(
        toolang_root / "agents" / "alice" / "agent.too",
        ("agent alice\n\nagic probe(in: Text) -> Text:\n  user: CHAT_OK: {{in}}\n"),
    )
    context = _build_context(
        toolang_root=toolang_root,
        agent_name="alice",
        enabled_features=("chat",),
    )
    project_run_start(context.store,
        run_id="run-current",
        thread_id="thread-current",
        origin="chat",
        input=Message.user("current input"),
        executable_name="probe",
    )
    bound = bind_run_request(
        context,
        RunRequest(
            group="chat",
            origin="chat",
            run_id="run-current",
            thread_id="thread-current",
            executable_name="probe",
            input="current input",
        ),
    )

    bundle = RunInput.from_binding(context.executor, bound)

    messages = bundle.messages()
    assert len(messages) == 1
    assert message_text(messages[0].parts).endswith("CHAT_OK: current input")


def test_assemble_run_input_hides_tools_when_activation_has_no_tools(
    tmp_path: Path,
) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(
        toolang_root / "agents" / "alice" / "agent.too",
        "agent alice\n\nagic summarize(in: Part[]):\n  Reply directly.\n",
    )
    context = _build_context(
        toolang_root=toolang_root,
        agent_name="alice",
        enabled_features=("chat",),
        tool_selectors=(),
    )
    context.config.set("models.default_selector", "openai/gpt-5[openai]")
    invoke_bound = bind_run_request(
        context,
        RunRequest(
            group="script",
            origin="script",
            executable_name="summarize",
            input="hello",
        ),
    )
    term_bound = bind_run_request(
        context,
        RunRequest(
            group="chat",
            origin="chat",
            executable_name="summarize",
            input="hello",
        ),
    )

    invoke_bundle = RunInput.from_binding(context.executor, invoke_bound)
    term_bundle = RunInput.from_binding(context.executor, term_bound)

    assert invoke_bundle.tools() == {}
    assert invoke_bundle.snapshot is not None
    assert invoke_bundle.snapshot.tools == ()
    assert invoke_bundle.debug["tool_names"] == []
    assert term_bundle.tools() == {}
    assert term_bundle.snapshot is not None
    assert term_bundle.snapshot.tools == ()
    assert term_bundle.debug["tool_names"] == []


def test_assemble_run_input_uses_explicit_activation_tools_for_script_runs(
    tmp_path: Path,
) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(
        toolang_root / "agents" / "alice" / "agent.too",
        "agent alice\n\nagic summarize(in: Part[]):\n  Reply directly.\n",
    )
    context = _build_context(
        toolang_root=toolang_root,
        agent_name="alice",
        enabled_features=("chat",),
        tool_selectors=("shell/*",),
    )
    context.config.set("models.default_selector", "openai/gpt-5[openai]")
    invoke_bound = bind_run_request(
        context,
        RunRequest(
            group="script",
            origin="script",
            executable_name="summarize",
            input="hello",
        ),
    )

    invoke_bundle = RunInput.from_binding(context.executor, invoke_bound)

    assert tuple(invoke_bundle.tools()) == ("shell__execute",)
    assert invoke_bundle.snapshot is not None
    assert invoke_bundle.snapshot.tools == ("shell__execute",)
    assert invoke_bundle.debug["tool_names"] == ["shell__execute"]


def test_assemble_run_input_applies_run_resource_selectors(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(
        toolang_root / "agents" / "alice" / "agent.too",
        "agent alice\n\nagic chat:\n  Reply directly.\n",
    )
    _put_cap(
        toolang_root,
        "alice",
        visibility="private",
        kind="skill",
        name="local-reviewer",
        text="---\ndescription: Review local changes\n---\n# Local Reviewer\n",
    )
    _put_cap(
        toolang_root,
        "alice",
        visibility="private",
        kind="skill",
        name="extra-skill",
        text="---\ndescription: Extra\n---\n# Extra\n",
    )
    context = _build_context(
        toolang_root=toolang_root,
        agent_name="alice",
        enabled_features=("chat",),
    )
    context.config.set(
        "models.allowed_selectors", ("openai/gpt-5[openai]", "openai/o3[openai]")
    )
    bound = bind_run_request(
        context,
        RunRequest(
            group="chat",
            origin="chat",
            input="hello",
            model_selectors=("openai/o3",),
            tool_selectors=("shell/*",),
            cap_selectors=("skill/local-reviewer",),
        ),
    )

    bundle = RunInput.from_binding(context.executor, bound)

    assert bundle.effective_model_selectors(context.executor) == ("openai/o3[openai]",)
    assert tuple(bundle.tools()) == ("shell__execute",)
    assert [entry.name for entry in bundle.skills()] == ["local-reviewer"]
    assert bundle.debug["tool_names"] == ["shell__execute"]
    assert bundle.debug["skill_names"] == ["local-reviewer"]


def test_assemble_run_input_logs_activation_set_math(tmp_path: Path, caplog) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(
        toolang_root / "agents" / "alice" / "agent.too",
        (
            "agent alice\n\n"
            "agic summarize(in: Part[]):\n"
            "  models = openai/gpt-5\n"
            "  tools -= service_use/bridge_start, service_use/init, service_use/auth_start, service_use/tool_call\n"
            "  skills = local-reviewer\n\n"
            "  Reply directly.\n"
        ),
    )
    _put_cap(
        toolang_root,
        "alice",
        visibility="private",
        kind="skill",
        name="local-reviewer",
        text="---\ndescription: Review local changes\n---\n# Local Reviewer\n",
    )
    context = _build_context(
        toolang_root=toolang_root,
        agent_name="alice",
        enabled_features=("chat",),
    )
    context.config.set(
        "models.allowed_selectors", ("openai/gpt-5[openai]", "openai/o3[openai]")
    )
    bound = bind_run_request(
        context,
        RunRequest(
            group="script",
            origin="script",
            executable_name="summarize",
            input="hello",
        ),
    )

    with caplog.at_level(logging.DEBUG, logger="toolang.run"):
        bundle = RunInput.from_binding(context.executor, bound)

    set_math = cast(dict[str, object], bundle.debug["set_math"])
    model_math = cast(dict[str, object], set_math["models"])
    tool_math = cast(dict[str, object], set_math["tools"])
    skill_math = cast(dict[str, object], set_math["skills"])
    tool_steps = cast(list[dict[str, object]], tool_math["directive_steps"])
    skill_steps = cast(list[dict[str, object]], skill_math["directive_steps"])

    assert model_math["activation_ceiling"] == [
        "openai/gpt-5[openai]",
        "openai/o3[openai]",
    ]
    assert model_math["agic_selectors"] == ["openai/gpt-5"]
    assert model_math["effective"] == ["openai/gpt-5[openai]"]
    assert tool_steps[0]["op"] == "-="
    assert tool_steps[0]["selectors"] == [
        "service_use/bridge_start",
        "service_use/init",
        "service_use/auth_start",
        "service_use/tool_call",
    ]
    assert any(
        str(item).startswith("service_use__")
        for item in cast(list[object], tool_steps[0]["matches"])
    )
    for removed_tool in cast(list[object], tool_steps[0]["matches"]):
        assert removed_tool not in cast(list[object], tool_math["effective"])
    assert skill_steps == [
        {
            "op": "=",
            "line": 4,
            "selectors": ["local-reviewer"],
            "matches": ["skill/local-reviewer"],
            "before": ["skill/local-reviewer"],
            "after": ["skill/local-reviewer"],
        }
    ]

    messages = [
        record.getMessage() for record in caplog.records if record.name == "toolang.run"
    ]
    detail_line = next(
        message for message in messages if message.startswith("run.activation ")
    )
    assert "thread=script_" in detail_line
    assert " run=run_" in detail_line
    assert "summary=models 2 = openai/gpt-5 -> 1" in detail_line
    assert "skills 1 = local-reviewer -> 1" in detail_line
    logged_math = json.loads(detail_line.split(" math=", 1)[1])
    assert logged_math["models"]["effective"] == ["openai/gpt-5[openai]"]
    assert logged_math["skills"]["effective"] == ["skill/local-reviewer"]


def test_assemble_run_input_uses_agic_user_message_for_script_runs(
    tmp_path: Path,
) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(
        toolang_root / "agents" / "alice" / "agent.too",
        "agent alice\n\nagic rewrite(in: Part[]):\n  Rewrite the input for a technical audience.\n",
    )
    context = _build_context(
        toolang_root=toolang_root,
        agent_name="alice",
        enabled_features=("chat",),
    )
    bound = bind_run_request(
        context,
        RunRequest(
            group="script",
            origin="script",
            executable_name="rewrite",
            input="hello world",
        ),
    )

    bundle = RunInput.from_binding(context.executor, bound)

    messages = bundle.messages()
    assert [item.role for item in messages] == ["user"]
    text = message_text(messages[0].parts)
    assert text.startswith("<context>")
    assert f"agent_home: {toolang_root / 'agents' / 'alice'}" in text
    assert "model_provider: openai" in text
    assert "model_family: openai" in text
    assert "model_name: gpt-5" in text
    assert text.endswith("Rewrite the input for a technical audience.\n\nhello world")


@pytest.mark.parametrize("recall", ["none", "memory"])
def test_script_run_can_simulate_history_with_explicit_message_blocks(
    tmp_path: Path,
    recall: str,
) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(
        toolang_root / "agents" / "alice" / "agent.too",
        (
            "agent alice\n\n"
            "agic replay(in: Part[]):\n"
            f"  recall = {recall}\n"
            "  context none\n"
            "  instruct none\n\n"
            "  user:\n"
            "    My name is Ada.\n\n"
            "  assistant:\n"
            "    Nice to meet you, Ada.\n\n"
            "  user:\n"
            "    Answer from the simulated conversation: {{_}}\n"
        ),
    )
    context = _build_context(
        toolang_root=toolang_root,
        agent_name="alice",
        enabled_features=(),
    )
    previous = project_run_start(context.store,
        run_id="run-previous",
        thread_id="thread-1",
        origin="chat",
        input=Message.user("stored history should not appear"),
    )
    project_step(context.store,
        run_id=previous.run_id,
        step_index=1,
        kind="model",
        status="finished",
        input=(InputRef(),),
        output=(TextPart(text="stored answer should not appear"),),
        detail=_model_detail(),
        started_at="2026-01-01T00:00:01Z",
        finished_at="2026-01-01T00:00:02Z",
    )
    project_run_end(context.store, run_id=previous.run_id, finished_at="2026-01-01T00:00:03Z")
    bound = bind_run_request(
        context,
        RunRequest(
            group="script",
            origin="script",
            thread_id="thread-1",
            executable_name="replay",
            input="What is my name?",
        ),
    )

    bundle = RunInput.from_binding(context.executor, bound)

    assert bundle.history == ()
    assert bundle.debug["recall"] == [recall]
    assert [(item.role, message_text(item.parts)) for item in bundle.messages()] == [
        ("user", "My name is Ada."),
        ("assistant", "Nice to meet you, Ada."),
        ("user", "Answer from the simulated conversation: What is my name?"),
    ]


def test_script_run_keeps_implicit_user_block_as_single_invoke_message(
    tmp_path: Path,
) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(
        toolang_root / "agents" / "alice" / "agent.too",
        (
            "agent alice\n\n"
            "agic replay(in: Part[]):\n"
            "  recall = none\n"
            "  context none\n\n"
            "  Use one invoke message.\n"
        ),
    )
    context = _build_context(
        toolang_root=toolang_root,
        agent_name="alice",
        enabled_features=(),
    )
    bound = bind_run_request(
        context,
        RunRequest(
            group="script",
            origin="script",
            executable_name="replay",
            input="current input",
        ),
    )

    bundle = RunInput.from_binding(context.executor, bound)

    assert [(item.role, message_text(item.parts)) for item in bundle.messages()] == [
        ("user", "Use one invoke message.\n\ncurrent input"),
    ]


def test_assemble_run_input_keeps_thread_messages_out_of_system_instructions(
    tmp_path: Path,
) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(
        toolang_root / "agents" / "alice" / "agent.too",
        "agent alice\n\nagic chat:\n  instruct:\n    Reply directly.\n",
    )
    context = _build_context(
        toolang_root=toolang_root,
        agent_name="alice",
        enabled_features=("chat",),
    )
    bound = bind_run_request(
        context,
        RunRequest(group="chat", origin="chat", input="hello"),
    )

    bundle = RunInput.from_binding(context.executor, bound)

    messages = bundle.messages()
    assert [item.role for item in messages] == ["user"]
    text = message_text(messages[0].parts)
    assert text.startswith("<context>")
    assert "agent_name: alice" in text
    assert text.endswith("hello")
    instructions = bundle.instructions()
    assert "Reply directly." in instructions
    assert "<skills>" not in instructions
    assert "<services>" not in instructions
    assert "hello" not in instructions


def test_assemble_run_input_expands_embedded_prompt_for_chat_message(
    tmp_path: Path,
) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(
        toolang_root / "agents" / "alice" / "agent.too",
        (
            "agent alice\n\n"
            "prompt summarize:\n"
            "  params = style, audience?\n\n"
            "  Summarize the user's request in a {{style}} style.\n"
            "  Target audience: {{audience}}\n"
        ),
    )
    context = _build_context(
        toolang_root=toolang_root,
        agent_name="alice",
        enabled_features=("chat",),
    )
    bound = bind_run_request(
        context,
        RunRequest(
            group="chat",
            origin="chat",
            message=Message.user(
                "/summarize concise developers\n\nAdd a remote skill."
            ),
        ),
    )

    bundle = RunInput.from_binding(context.executor, bound)

    messages = bundle.messages()
    assert [item.role for item in messages] == ["user"]
    text = message_text(messages[0].parts)
    assert text.startswith("<context>")
    assert text.endswith(
        "Summarize the user's request in a concise style.\n"
        "Target audience: developers\n\n"
        "Add a remote skill."
    )


def test_run_input_prepends_selected_context_to_user_message(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(
        toolang_root / "agents" / "alice" / "agent.too",
        (
            "agent alice\n\n"
            "context report:\n"
            "  Agent {{runtime.agent.name}} is preparing a report.\n\n"
            "agic chat:\n"
            "  context report\n"
        ),
    )
    context = _build_context(
        toolang_root=toolang_root,
        agent_name="alice",
        enabled_features=("chat",),
    )
    bound = bind_run_request(
        context,
        RunRequest(group="chat", origin="chat", input="hello"),
    )

    bundle = RunInput.from_binding(context.executor, bound)

    assert [item.to_data() for item in bundle.messages()] == [
        {
            "role": "user",
            "parts": [
                {
                    "type": "text",
                    "text": "Agent alice is preparing a report.\n\nhello",
                }
            ],
        }
    ]
    assert bundle.debug["context_text"] == "Agent alice is preparing a report."


def test_run_input_debug_logs_computed_prompt_bundle(tmp_path: Path, caplog) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(
        toolang_root / "agents" / "alice" / "agent.too",
        "agent alice\n\nagic chat:\n  user: hello\n",
    )
    context = _build_context(
        toolang_root=toolang_root,
        agent_name="alice",
        enabled_features=("chat",),
    )
    bound = bind_run_request(
        context,
        RunRequest(group="chat", origin="chat", input="hi"),
    )

    with caplog.at_level(logging.DEBUG, logger="toolang.run"):
        RunInput.from_binding(context.executor, bound)

    messages = [
        record.getMessage() for record in caplog.records if record.name == "toolang.run"
    ]
    assert any(message.startswith("prompt.assembled thread=") for message in messages)
    assert any(message.startswith("prompt.tools thread=") for message in messages)
    assert any(
        message.startswith("prompt.instructions thread=")
        and "text=<runtime-instructions>" in message
        for message in messages
    )
    assert any(
        message.startswith("prompt.context thread=") and "text=<context>" in message
        for message in messages
    )
    assert any(
        message.startswith("prompt.messages thread=")
        and '"role": "user"' in message
        and '"text": "<context>' in message
        and "hello" in message
        for message in messages
    )


def test_chat_run_prefers_named_chat_agic_over_default(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(
        toolang_root / "agents" / "alice" / "agent.too",
        (
            "agent alice\n\n"
            "agic:\n"
            "  Script default.\n\n"
            "agic chat:\n"
            "  instruct:\n"
            "    Reply directly.\n"
        ),
    )
    context = _build_context(
        toolang_root=toolang_root,
        agent_name="alice",
        enabled_features=("chat",),
    )
    bound = bind_run_request(
        context,
        RunRequest(group="chat", origin="chat", input="hello"),
    )

    bundle = RunInput.from_binding(context.executor, bound)

    assert bundle.agic.name == "chat"
    instructions = bundle.instructions()
    assert "Reply directly." in instructions


def test_program_default_instruct_overrides_runtime_default(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(
        toolang_root / "agents" / "alice" / "agent.too",
        (
            "agent alice\n\n"
            "instruct:\n"
            "  Agent {{runtime.agent.name}} in sandbox {{runtime.sandbox}}.\n"
            "  Reply directly.\n\n"
            "agic chat:\n"
            "  user: hello\n"
        ),
    )
    context = _build_context(
        toolang_root=toolang_root,
        agent_name="alice",
        enabled_features=("chat",),
    )
    bound = bind_run_request(
        context,
        RunRequest(group="chat", origin="chat", input="hello"),
    )

    instructions = RunInput.from_binding(context.executor, bound).instructions()

    assert "Agent alice in sandbox none." in instructions
    assert "Reply directly." in instructions
    assert "You are the alice Toolang agent." not in instructions


def test_agic_instruct_can_select_named_instruct(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(
        toolang_root / "agents" / "alice" / "agent.too",
        (
            "agent alice\n\n"
            "instruct reviewer:\n"
            "  Review with {{runtime.agic.name}}.\n\n"
            "agic review:\n"
            "  instruct reviewer\n\n"
            "  Review the target carefully.\n"
        ),
    )
    context = _build_context(
        toolang_root=toolang_root,
        agent_name="alice",
        enabled_features=(),
    )
    bound = bind_run_request(
        context,
        RunRequest(group="script", origin="script", executable_name="review", input="input"),
    )

    bundle = RunInput.from_binding(context.executor, bound)

    instructions = bundle.instructions()
    assert "Review with review." in instructions
    assert "You are the alice Toolang agent." not in instructions


def test_agic_instruct_none_suppresses_agent_instruct_layer(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(
        toolang_root / "agents" / "alice" / "agent.too",
        ("agent alice\n\nagic quiet:\n  instruct none\n\n  Reply directly.\n"),
    )
    context = _build_context(
        toolang_root=toolang_root,
        agent_name="alice",
        enabled_features=(),
    )
    bound = bind_run_request(
        context,
        RunRequest(group="script", origin="script", executable_name="quiet", input="input"),
    )

    bundle = RunInput.from_binding(context.executor, bound)

    instructions = bundle.instructions()
    assert "<agent-instructions>" not in instructions
    assert instructions == ""
    assert "You are the alice Toolang agent." not in instructions


def test_agic_instruct_block_renders_as_agent_instruction(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(
        toolang_root / "agents" / "alice" / "agent.too",
        (
            "agent alice\n\n"
            "agic custom:\n"
            "  instruct:\n"
            "    Use {{runtime.agent.name}} and {{runtime.agic.name}}.\n\n"
            "  Reply directly.\n"
        ),
    )
    context = _build_context(
        toolang_root=toolang_root,
        agent_name="alice",
        enabled_features=(),
    )
    bound = bind_run_request(
        context,
        RunRequest(group="script", origin="script", executable_name="custom", input="input"),
    )

    bundle = RunInput.from_binding(context.executor, bound)

    instructions = bundle.instructions()
    assert "Use alice and custom." in instructions


def test_agent_markdown_psyche_files_change_default_behavior(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(toolang_root / "agents" / "alice" / "agent.too", "agent alice\n")
    _write_text(
        toolang_root / "psyches" / "root-style.md", "Use concise root defaults.\n"
    )
    _write_text(
        toolang_root / "agents" / "alice" / "psyches" / "home-style.md",
        "Prefer agent-home behavior.\n",
    )
    context = _build_context(
        toolang_root=toolang_root,
        agent_name="alice",
        enabled_features=("chat",),
    )
    bound = bind_run_request(
        context,
        RunRequest(group="chat", origin="chat", input="hello"),
    )

    instructions = RunInput.from_binding(context.executor, bound).instructions()

    assert "<agent-instructions>" in instructions
    assert "Use concise root defaults." in instructions
    assert "Prefer agent-home behavior." in instructions


def test_chat_run_uses_program_default_when_chat_agic_is_missing(
    tmp_path: Path,
) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(
        toolang_root / "agents" / "alice" / "agent.too",
        "agent alice\n\nagic:\n  Script default.\n",
    )
    _write_text(
        toolang_root / "psyches" / "reviewer.md",
        "Be precise.\n",
    )
    _write_text(
        toolang_root / "skills" / "reviewer" / "SKILL.md",
        "---\ndescription: Review carefully\n---\nUse this skill.\n",
    )
    _write_text(
        toolang_root / "services" / "github.md",
        (
            "---\n"
            "description: GitHub MCP\n"
            "transport: http\n"
            "target: https://example.com/mcp\n"
            "---\n"
            "Use this service.\n"
        ),
    )
    context = _build_context(
        toolang_root=toolang_root,
        agent_name="alice",
        enabled_features=("chat",),
    )
    (toolang_root / "psyches" / "reviewer.md").unlink()
    (toolang_root / "skills" / "reviewer" / "SKILL.md").unlink()
    (toolang_root / "services" / "github.md").unlink()
    bound = bind_run_request(
        context,
        RunRequest(group="chat", origin="chat", input="hello"),
    )

    bundle = RunInput.from_binding(context.executor, bound)
    instructions = bundle.instructions()

    assert bundle.agic.name == "default"
    assert "Script default." not in instructions
    assert "<psyches>" in instructions
    assert "Be precise." in instructions
    assert "<skills>" in instructions
    assert "Review carefully" in instructions
    assert "<services>" in instructions
    assert "GitHub MCP" in instructions
    assert "https://example.com/mcp" in instructions
    assert "<tools>" not in instructions
    assert "List configured peer agents available for agent_chat" not in instructions
    assert "agent_chat__peers" in bundle.tools()
    assert f"agent_home: {toolang_root / 'agents' / 'alice'}" in instructions
    assert "sandbox: none" in instructions
    assert (
        "Do not call tools or inspect files just to explore the environment."
        in instructions
    )
    assert "<tool-result-reuse>" in instructions
    assert (
        "Reuse applicable prior tool results instead of repeating the same tool call."
        in instructions
    )
    assert (
        "missing, failed, stale, expired, invalid for the current request"
        in instructions
    )


def test_execute_run_rejects_agic_model_outside_activation_allowlist(
    tmp_path: Path,
) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(
        toolang_root / "agents" / "alice" / "agent.too",
        "agent alice\n\nagic chat:\n  models = openai/gpt-5\n\n  Reply directly.\n",
    )
    context = _build_context(
        toolang_root=toolang_root,
        agent_name="alice",
        enabled_features=("chat",),
    )
    context.executor.model_environ = {"OPENAI_API_KEY": "secret"}
    context.config.set("models.allowed_selectors", ("openai/o3[openai]",))
    context.config.set("models.default_selector", "openai/o3[openai]")

    outcome = asyncio.run(
        context.executor.run(
            RunRequest(group="chat", origin="chat", input="hello"),
            context.get_agent_state(),
        )
    )

    assert outcome.status == "failed"
    assert outcome.error is not None
    assert "No matched models." in outcome.error
    assert "toolang model list --models <selector>" in outcome.error


def test_execute_run_pre_start_failure_persists_failed_accepted_run(
    tmp_path: Path, caplog
) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(
        toolang_root / "agents" / "alice" / "agent.too",
        "agent alice\n\nagic chat:\n  Reply directly.\n",
    )
    context = _build_context(
        toolang_root=toolang_root,
        agent_name="alice",
        enabled_features=("chat",),
    )
    context.config.set("models.default_selector", "claude")
    context.executor.model_environ = {}
    context.executor.model_environ = {}

    with caplog.at_level(logging.ERROR, logger="toolang.run"):
        outcome = asyncio.run(
            context.executor.run(
                RunRequest(group="chat", origin="chat", input="hello"),
                context.get_agent_state(),
            )
        )

    assert outcome.status == "failed"
    assert outcome.error is not None
    assert "No available models." in outcome.error
    assert "toolang model providers" in outcome.error
    runs = context.store.list_runs()
    assert len(runs) == 1
    assert runs[0].status == "failed"
    assert runs[0].error == outcome.error
    assert [
        (step.index, step.kind, step.status)
        for step in context.store.list_steps(run_id=runs[0].id)
    ] == [(0, "system", "failed")]
    assert "persist sink event handling failed" not in caplog.text


def test_script_executor_logs_lifecycle(tmp_path: Path, caplog) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(
        toolang_root / "agents" / "alice" / "agent.too",
        "agent alice\n\nagic:\n  Reply directly.\n",
    )
    context = _build_context(
        toolang_root=toolang_root,
        agent_name="alice",
        enabled_features=("chat",),
    )

    with (
        caplog.at_level(logging.INFO, logger="toolang.run"),
        _patched_executor_execution(),
    ):
        outcome = asyncio.run(
            context.executor.run(
                RunRequest(group="script", origin="script", input="hello"),
                context.get_agent_state(),
            )
        )

    messages = [
        record.getMessage() for record in caplog.records if record.name == "toolang.run"
    ]
    assert outcome.status == "finished"
    assert messages[0].startswith("Thread created id=script_")
    thread_id = messages[0].removeprefix("Thread created id=").split(" ", 1)[0]
    assert messages[1].startswith(f"Run started thread={thread_id}")
    assert " run=run_" in messages[1]
    assert messages[1].endswith(" input='hello'")
    assert messages[-1].startswith(f"Run finished thread={thread_id}")
    assert " run=run_" in messages[-1]
    assert " status=finished " in messages[-1]


def test_script_loop_cancel_does_not_wait_for_worker_thread() -> None:
    started = threading.Event()
    release = threading.Event()

    def blocking_run(_context) -> RunResult:
        started.set()
        release.wait(timeout=5.0)
        return RunResult(output_text="late")

    async def run_test() -> None:
        task = asyncio.create_task(
            run_executor_module._run_loop(
                blocking_run,
                cast(Any, object()),
                run_id="run_test",
            )
        )
        while not started.is_set():
            await asyncio.sleep(0.01)
        started_at = time.monotonic()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=0.5)
        assert time.monotonic() - started_at < 0.5

    try:
        asyncio.run(run_test())
    finally:
        release.set()


def test_execution_store_records_runs_steps_and_messages(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    store = RunStore(run_store_path(toolang_root, "alice"))
    try:
        created = store.append_update(
            kind="created",
            payload={"path": str(toolang_root / "agents" / "alice" / "agent.too")},
        )
        run = project_run_start(store,
            run_id="run-1",
            thread_id="thread-1",
            origin="chat",
            input=Message.user("hello"),
        )
        project_step(store,
            run_id=run.run_id,
            step_index=1,
            kind="model",
            status="finished",
            input=(InputRef(),),
            output=(TextPart(text="assistant:hello"),),
            detail=_model_detail(),
            started_at="2026-01-01T00:00:01Z",
            finished_at="2026-01-01T00:00:02Z",
        )
        finished = project_run_end(store, run_id=run.run_id)

        assert finished.status == "finished"
        assert [item.kind for item in store.list_steps(run_id=run.run_id)] == ["model"]
        assert [
            item.to_data()
            for item in store.recent_conversation_messages(thread_id="thread-1")
        ] == [
            {"role": "user", "parts": [{"type": "text", "text": "hello"}]},
            {
                "role": "assistant",
                "parts": [{"type": "text", "text": "assistant:hello"}],
            },
        ]
        assert [item.kind for item in store.list_updates(limit=10)] == ["created"]
        assert created.payload["path"] == str(
            toolang_root / "agents" / "alice" / "agent.too"
        )
    finally:
        store.close()


def test_chat_accepts_structured_message_parts_and_model_selector(
    tmp_path: Path,
) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(toolang_root / "agents" / "alice" / "agent.too", "agent alice\n")
    context = _build_context(
        toolang_root=toolang_root,
        agent_name="alice",
        enabled_features=("chat", "inspect"),
    )
    app = _create_test_app(context)

    with _patched_executor_execution():
        with TestClient(app) as client:
            response = client.post(
                "/api/v1/chat",
                json={
                    "model": "openai/gpt-5[openai]",
                    "message": {
                        "role": "user",
                        "parts": [
                            {"type": "text", "text": "summarize this"},
                            {
                                "type": "image",
                                "image_url": "https://example.com/image.png",
                            },
                            {
                                "type": "file",
                                "file_url": "https://example.com/report.pdf",
                                "filename": "report.pdf",
                            },
                        ],
                    },
                },
            )
            run_detail = client.get(f"/api/v1/runs/{response.json()['run_id']}").json()

    assert response.status_code == 200
    body = response.json()
    assert body["message"]["parts"] == [
        {"type": "text", "text": "summarize this"},
        {
            "type": "image",
            "detail": "auto",
            "image_url": "https://example.com/image.png",
        },
        {
            "type": "file",
            "file_url": "https://example.com/report.pdf",
            "filename": "report.pdf",
        },
    ]
    assert run_detail["input"]["parts"] == body["message"]["parts"]


def test_execution_store_rebuilds_tool_history_from_steps(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    store = RunStore(run_store_path(toolang_root, "alice"))
    try:
        run = project_run_start(store,
            run_id="run-1",
            thread_id="thread-1",
            origin="chat",
            input=Message.user("sum 7 and 8"),
        )
        project_step(store,
            run_id=run.run_id,
            step_index=1,
            kind="model",
            status="finished",
            input=(InputRef(),),
            output=(
                ToolCallPart(
                    tool_call_id="tool-1",
                    tool_name="math_add",
                    tool_family="math_add",
                    input={"a": 7, "b": 8},
                ),
            ),
            detail=_model_detail(),
            started_at="2026-01-01T00:00:01Z",
            finished_at="2026-01-01T00:00:02Z",
        )
        project_step(store,
            run_id=run.run_id,
            step_index=2,
            kind="tool",
            status="finished",
            input=(_output_ref(run.run_id, 1, 0),),
            output=(
                ToolResultPart(
                    tool_call_id="tool-1",
                    tool_name="math_add",
                    tool_family="math_add",
                    output={"value": 15},
                ),
            ),
            detail={"tool": "math_add"},
            started_at="2026-01-01T00:00:03Z",
            finished_at="2026-01-01T00:00:04Z",
        )
        project_step(store,
            run_id=run.run_id,
            step_index=3,
            kind="model",
            status="finished",
            input=(_output_ref(run.run_id, 2),),
            output=(TextPart(text="15"),),
            detail=_model_detail(),
            started_at="2026-01-01T00:00:05Z",
            finished_at="2026-01-01T00:00:06Z",
        )
        assert [
            item.to_data()
            for item in store.recent_conversation_messages(thread_id="thread-1")
        ] == [
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
        assert [
            item.to_data()
            for item in store.recent_text_conversation_messages(thread_id="thread-1")
        ] == [
            {"role": "user", "parts": [{"type": "text", "text": "sum 7 and 8"}]},
            {"role": "assistant", "parts": [{"type": "text", "text": "15"}]},
        ]
    finally:
        store.close()


def test_execution_store_does_not_return_orphan_tool_history_when_limited(
    tmp_path: Path,
) -> None:
    toolang_root = tmp_path / "toolang"
    store = RunStore(run_store_path(toolang_root, "alice"))
    try:
        run = project_run_start(store,
            run_id="run-1",
            thread_id="thread-1",
            origin="chat",
            input=Message.user("sum 7 and 8"),
        )
        project_step(store,
            run_id=run.run_id,
            step_index=1,
            kind="model",
            status="finished",
            input=(InputRef(),),
            output=(
                ToolCallPart(
                    tool_call_id="tool-1",
                    tool_name="math_add",
                    tool_family="math_add",
                    input={"a": 7, "b": 8},
                ),
            ),
            detail=_model_detail(),
            started_at="2026-01-01T00:00:01Z",
            finished_at="2026-01-01T00:00:02Z",
        )
        project_step(store,
            run_id=run.run_id,
            step_index=2,
            kind="tool",
            status="finished",
            input=(_output_ref(run.run_id, 1, 0),),
            output=(
                ToolResultPart(
                    tool_call_id="tool-1",
                    tool_name="math_add",
                    tool_family="math_add",
                    output={"value": 15},
                ),
            ),
            detail={"tool": "math_add"},
            started_at="2026-01-01T00:00:03Z",
            finished_at="2026-01-01T00:00:04Z",
        )
        project_step(store,
            run_id=run.run_id,
            step_index=3,
            kind="model",
            status="finished",
            input=(_output_ref(run.run_id, 2),),
            output=(TextPart(text="15"),),
            detail=_model_detail(),
            started_at="2026-01-01T00:00:05Z",
            finished_at="2026-01-01T00:00:06Z",
        )

        messages = store.recent_conversation_messages(thread_id="thread-1", limit=2)

        assert [item.to_data() for item in messages] == [
            {"role": "assistant", "parts": [{"type": "text", "text": "15"}]},
        ]
    finally:
        store.close()


def test_execution_store_replays_model_reasoning_content(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    store = RunStore(run_store_path(toolang_root, "alice"))
    try:
        run = project_run_start(store,
            run_id="run-1",
            thread_id="thread-1",
            origin="chat",
            input=Message.user("list files"),
        )
        project_step(store,
            run_id=run.run_id,
            step_index=1,
            kind="model",
            status="finished",
            input=(InputRef(),),
            output=(TextPart(text="Listing files now."),),
            detail=_model_detail(
                "deepseek/deepseek-v4-flash",
                provider="deepseek",
                model="deepseek-v4-flash",
                adapter="chat_completions",
                reasoning_content="The user asked for files, so list the current directory.",
            ),
            started_at="2026-01-01T00:00:01Z",
            finished_at="2026-01-01T00:00:02Z",
        )

        messages = store.recent_conversation_messages(thread_id="thread-1")

        assert messages[1].meta == {
            "reasoning_content": "The user asked for files, so list the current directory."
        }
    finally:
        store.close()


def test_run_input_uses_structured_thread_history(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(toolang_root / "agents" / "alice" / "agent.too", "agent alice\n")
    context = _build_context(
        toolang_root=toolang_root,
        agent_name="alice",
        enabled_features=("chat",),
    )
    previous = project_run_start(context.store,
        run_id="run-previous",
        thread_id="thread-1",
        origin="chat",
        input=Message.user("create a Linear issue"),
    )
    project_step(context.store,
        run_id=previous.run_id,
        step_index=1,
        kind="model",
        status="finished",
        input=(InputRef(),),
        output=(
            ToolCallPart(
                tool_call_id="tool-list-call",
                call_id="call-list",
                tool_name="service_use__tool_list",
                tool_family="service_use__tool_list",
                input={"service": "linear"},
            ),
        ),
        detail=_model_detail(),
        started_at="2026-01-01T00:00:01Z",
        finished_at="2026-01-01T00:00:02Z",
    )
    project_step(context.store,
        run_id=previous.run_id,
        step_index=2,
        kind="tool",
        status="finished",
        input=(_output_ref(previous.run_id, 1, 0),),
        output=(
            ToolResultPart(
                tool_call_id="tool-list-call",
                call_id="call-list",
                tool_name="service_use__tool_list",
                tool_family="service_use__tool_list",
                output={
                    "ok": True,
                    "result": {
                        "service": "linear",
                        "transport": "http",
                        "result": {
                            "tools": [
                                {
                                    "name": "save_issue",
                                    "inputSchema": {
                                        "type": "object",
                                        "properties": {
                                            "title": {"type": "string"},
                                            "team": {"type": "string"},
                                            "description": {"type": "string"},
                                        },
                                        "required": ["title", "team"],
                                    },
                                },
                                {
                                    "name": "list_teams",
                                    "inputSchema": {"type": "object", "properties": {}},
                                },
                            ]
                        },
                    },
                },
            ),
        ),
        detail={"tool": "service_use__tool_list"},
        started_at="2026-01-01T00:00:03Z",
        finished_at="2026-01-01T00:00:04Z",
    )
    project_step(context.store,
        run_id=previous.run_id,
        step_index=3,
        kind="model",
        status="finished",
        input=(_output_ref(previous.run_id, 2),),
        output=(
            ToolCallPart(
                tool_call_id="empty-save-call",
                call_id="call-save-empty",
                tool_name="service_use__tool_call",
                tool_family="service_use__tool_call",
                input={"service": "linear", "tool_name": "save_issue"},
            ),
        ),
        detail=_model_detail(),
        started_at="2026-01-01T00:00:05Z",
        finished_at="2026-01-01T00:00:06Z",
    )
    project_step(context.store,
        run_id=previous.run_id,
        step_index=4,
        kind="tool",
        status="finished",
        input=(_output_ref(previous.run_id, 3, 0),),
        output=(
            ToolResultPart(
                tool_call_id="empty-save-call",
                call_id="call-save-empty",
                tool_name="service_use__tool_call",
                tool_family="service_use__tool_call",
                output={
                    "ok": True,
                    "result": {
                        "service": "linear",
                        "transport": "http",
                        "result": {
                            "content": [
                                {
                                    "type": "text",
                                    "text": "Error: title is required when creating an issue",
                                }
                            ],
                            "isError": True,
                        },
                    },
                },
            ),
        ),
        detail={"tool": "service_use__tool_call"},
        started_at="2026-01-01T00:00:07Z",
        finished_at="2026-01-01T00:00:08Z",
    )
    project_step(context.store,
        run_id=previous.run_id,
        step_index=5,
        kind="model",
        status="finished",
        input=(_output_ref(previous.run_id, 4),),
        output=(TextPart(text="created XBY-31"),),
        detail=_model_detail(),
        started_at="2026-01-01T00:00:09Z",
        finished_at="2026-01-01T00:00:10Z",
    )
    project_run_end(context.store, run_id=previous.run_id, finished_at="2026-01-01T00:00:11Z")

    bound = bind_run_request(
        context,
        RunRequest(group="chat", origin="chat", thread_id="thread-1", input="again"),
    )
    bundle = RunInput.from_binding(context.executor, bound)
    messages = bundle.messages()
    instructions = bundle.instructions()

    assert [item.role for item in messages] == [
        "user",
        "assistant",
        "tool",
        "assistant",
        "tool",
        "assistant",
        "user",
    ]
    assert message_text(messages[0].parts) == "create a Linear issue"
    assert isinstance(messages[1].parts[0], ToolCallPart)
    assert isinstance(messages[2].parts[0], ToolResultPart)
    assert isinstance(messages[3].parts[0], ToolCallPart)
    assert isinstance(messages[4].parts[0], ToolResultPart)
    assert message_text(messages[5].parts) == "created XBY-31"
    current_message = message_text(messages[6].parts)
    assert current_message.startswith("<context>")
    assert current_message.endswith("again")
    assert message_text(bundle.input_message().parts) == "again"
    assert "Prior Tool Results:" not in instructions
    assert "service_use__tool_list service=linear succeeded" not in instructions


def test_run_input_recall_none_disables_thread_history_and_tool_context(
    tmp_path: Path,
) -> None:
    toolang_root = tmp_path / "toolang"
    _write_text(
        toolang_root / "agents" / "alice" / "agent.too",
        "agent alice\n\nagic chat:\n  recall = none\n",
    )
    context = _build_context(
        toolang_root=toolang_root,
        agent_name="alice",
        enabled_features=("chat",),
    )
    previous = project_run_start(context.store,
        run_id="run-previous",
        thread_id="thread-1",
        origin="chat",
        input=Message.user("previous"),
    )
    project_step(context.store,
        run_id=previous.run_id,
        step_index=1,
        kind="model",
        status="finished",
        input=(InputRef(),),
        output=(TextPart(text="old answer"),),
        detail=_model_detail(),
        started_at="2026-01-01T00:00:01Z",
        finished_at="2026-01-01T00:00:02Z",
    )
    project_run_end(context.store, run_id=previous.run_id, finished_at="2026-01-01T00:00:03Z")

    bound = bind_run_request(
        context,
        RunRequest(group="chat", origin="chat", thread_id="thread-1", input="again"),
    )
    bundle = RunInput.from_binding(context.executor, bound)

    messages = bundle.messages()
    assert [item.role for item in messages] == ["user"]
    text = message_text(messages[0].parts)
    assert text.startswith("<context>")
    assert text.endswith("again")
    assert message_text(bundle.input_message().parts) == "again"
    assert bundle.history == ()
    assert bundle.debug["recall"] == ["none"]


@asynccontextmanager
async def _running_context(
    context: ApiContext,
    *,
    enabled_features: tuple[str, ...],
    loop_intervals_ms: dict[str, float] | None = None,
):
    enabled_components = normalize_component_names(enabled_features)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        stop_signal = asyncio.Event()
        if loop_intervals_ms is not None:
            for feature_name, interval_ms in loop_intervals_ms.items():
                context.config.set(
                    f"components.trigger.{feature_name}.interval_ms",
                    interval_ms,
                )

        background_tasks: list[asyncio.Task[None]] = []
        scheduler_store = None
        if "trigger.pulse" in enabled_components:
            interval_value = context.config.require(
                "components.trigger.pulse.interval_ms"
            )
            assert isinstance(interval_value, int | float)
            scheduler_store = open_job_store(context.root, context.name)
            job_watcher = JobWatcher(context.root, context.name)
            scheduler = Scheduler(
                job_store=scheduler_store,
                executor=context.executor,
                get_home_jobs=job_watcher.current,
                get_agent_state=context.get_agent_state,
                interval_ms=float(interval_value),
            )
            background_tasks.extend(
                [
                    job_watcher.start(stop_signal=stop_signal),
                    scheduler.start(stop_signal=stop_signal),
                ]
            )
        if "trigger.poll" in enabled_components:
            interval = context.config.require("components.trigger.poll.interval_ms")
            assert isinstance(interval, int | float)
            background_tasks.append(
                poll.spawn(
                    name=context.name,
                    home=context.home,
                    bindings=context.channel_bindings,
                    plugins=context.channel_plugins,
                    executor=context.executor,
                    get_agent_state=context.get_agent_state,
                    enabled_components=enabled_components,
                    interval_ms=float(interval),
                    stop_signal=stop_signal,
                )
            )
        if "trigger.watch" in enabled_components:
            context.config.set("components.trigger.watch.debounce_ms", 10.0)
            watcher = state_watcher.StateWatcher(
                context.root, context.name, context.get_agent_state()
            )
            context.get_agent_state = watcher.current
            background_tasks.append(
                watch.spawn(
                    root=context.root,
                    name=context.name,
                    watcher=watcher,
                    executor=context.executor,
                    store=context.store,
                    config=context.config,
                    stop_signal=stop_signal,
                )
            )
        if "trigger.file" in enabled_components:
            interval = context.config.require("components.trigger.file.interval_ms")
            stable = context.config.require("components.trigger.file.stable_ms")
            inboxes = context.config.require("components.trigger.file.inboxes")
            assert isinstance(interval, int | float)
            assert isinstance(stable, int | float)
            assert isinstance(inboxes, tuple)
            assert all(isinstance(inbox, Path) for inbox in inboxes)
            background_tasks.append(
                files.spawn(
                    root=context.root,
                    name=context.name,
                    executor=context.executor,
                    get_agent_state=context.get_agent_state,
                    inboxes=cast(tuple[Path, ...], inboxes),
                    interval_ms=float(interval),
                    stable_ms=float(stable),
                    stop_signal=stop_signal,
                )
            )
        try:
            await asyncio.sleep(0)
            yield
        finally:
            stop_signal.set()
            for task in background_tasks:
                with suppress(asyncio.CancelledError):
                    await task
            if scheduler_store is not None:
                scheduler_store.close()
            await context.executor.close()
            context.store.close()

    async with lifespan(FastAPI()):
        yield context


def _create_test_app(context: ApiContext) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI):
        enabled_features = cast(
            tuple[str, ...], context.config.require("components.enabled")
        )
        async with _running_context(context, enabled_features=enabled_features):
            yield

    return create_app(context, lifespan=lifespan)


def _build_context(
    *,
    toolang_root: Path,
    agent_name: str,
    enabled_features: tuple[str, ...],
    channel_bindings: dict[str, ChannelBinding] | None = None,
    channel_plugins: dict[str, AgentChannel] | None = None,
    tool_selectors: tuple[str, ...] | None = None,
) -> ApiContext:
    durable = scan_durable_state(toolang_root, agent_name)
    prepared = state_watcher.prepare_locks(durable)
    state = load_agent_state(prepared)
    store = RunStore(run_store_path(toolang_root, agent_name))
    setup = AgentSetup(
        tools=load_runtime_tools(
            plugin_config=load_named_config(
                toolang_root,
                agent_name,
                section="tools",
                environ={},
            ),
            entries=state.caps,
            selectors=tool_selectors,
        ),
        model_providers={
            name: provider
            for name, provider in load_model_providers().items()
            if name == "openai"
        },
        model_adapters=load_model_adapters(),
    )
    config = RuntimeConfig(
        {
            "server.host": "127.0.0.1",
            "server.port": 8765,
            "server.endpoint": "http://127.0.0.1:8765",
            "components.enabled": normalize_component_names(enabled_features),
            "components.trigger.pulse.interval_ms": DEFAULT_SCHEDULER_INTERVAL_MS,
            "components.trigger.poll.interval_ms": poll.DEFAULT_INTERVAL_MS,
            "components.trigger.watch.interval_ms": state_watcher.DEFAULT_INTERVAL_MS,
            "components.trigger.watch.debounce_ms": state_watcher.DEFAULT_DEBOUNCE_MS,
            "components.trigger.file.interval_ms": files.DEFAULT_INTERVAL_MS,
            "components.trigger.file.stable_ms": files.DEFAULT_STABLE_MS,
            "components.trigger.file.inboxes": (),
            "runtime.sandbox": "none",
        }
    )
    model_aliases = parse_model_aliases(
        load_config_layers(toolang_root, agent_name)
    )
    default_models = load_default_models(toolang_root, agent_name)
    model_environ = {"OPENAI_API_KEY": "secret"}
    executor = Executor(
        root=toolang_root,
        name=agent_name,
        home=agent_catalog.agent_home(toolang_root, agent_name),
        id_state_path=agents.agent_id_state_path(toolang_root, agent_name),
        setup=setup,
        store=store,
        model_aliases=model_aliases,
        default_models=default_models,
        model_environ=model_environ,
        config=config,
    )
    watcher = state_watcher.StateWatcher(toolang_root, agent_name, state)
    return ApiContext(
        root=toolang_root,
        name=agent_name,
        home=agent_catalog.agent_home(toolang_root, agent_name),
        room=agents.agent_room(toolang_root, agent_name),
        get_agent_state=watcher.current,
        channel_bindings=channel_bindings or {},
        channel_plugins=channel_plugins or {},
        executor=executor,
        store=store,
        config=config,
        enabled_components=normalize_component_names(enabled_features),
    )


def _wait_for_completed_runs(client: TestClient) -> dict[str, object]:
    for _ in range(50):
        snapshot = client.get("/api/v1/runs").json()
        if snapshot["items"]:
            return snapshot
        time.sleep(0.01)
    raise AssertionError("expected completed runs")


async def _run_delivery(
    context: ApiContext,
    binding_name: str,
    delivery: InboundDelivery,
) -> RunRecord:
    return await start_delivery(
        executor=context.executor,
        get_agent_state=context.get_agent_state,
        plugins=context.channel_plugins,
        home=context.home,
        component_name="poll",
        binding_name=binding_name,
        delivery=delivery,
    )


async def _record_pulse_result(
    context: ApiContext,
    store: JobStore,
    run: RunRecord,
) -> None:
    store.finish_run(
        jobs=AgentJobs.load(context.root, context.name, context.get_agent_state().program),
        run_id=run.id,
        run_status=run.status,
        now=datetime.now(timezone.utc),
    )


async def _wait_for_fingerprint_change(
    context: ApiContext, fingerprint: str
) -> bool:
    for _ in range(200):
        if context.get_agent_state().fingerprint != fingerprint:
            return True
        await asyncio.sleep(0.01)
    return False


async def _wait_for_active_run(context: ApiContext) -> None:
    for _ in range(100):
        state = cast(
            dict[str, object],
            inspect.snapshot_context(
                context,
                enabled_features=cast(
                    tuple[str, ...], context.config.require("components.enabled")
                ),
            )["state"],
        )
        if state["active_runs"]:
            return
        await asyncio.sleep(0.005)
    raise AssertionError("expected active run")


async def _wait_for_completed_count(context: ApiContext, count: int) -> None:
    for _ in range(200):
        runs = context.store.list_runs(limit=None)
        if (
            sum(run.status in {"finished", "failed", "canceled"} for run in runs)
            >= count
        ):
            return
        await asyncio.sleep(0.01)
    raise AssertionError("expected completed runs")


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _sse_data(chunk: str) -> dict[str, Any]:
    data_line = next(line for line in chunk.splitlines() if line.startswith("data: "))
    return cast(dict[str, Any], json.loads(data_line.removeprefix("data: ")))


def _chat_message(text: str) -> dict[str, object]:
    return {
        "role": "user",
        "parts": [{"type": "text", "text": text}],
    }


def _fake_run_input(bound):
    input_message = bound.message or Message.user(bound.input_text or "hello")

    class RunInputStub:
        def __init__(self) -> None:
            self.run = bound
            self.message = input_message
            self.snapshot = RunSnapshot(
                agent=SnapshotAgent(name="alice", root="/tmp/root", home="/tmp/home"),
                run=SnapshotRun(
                    run_id=bound.run_id,
                    group=bound.group,
                    origin=bound.origin,
                    thread_id=bound.thread_id,
                    run_loop=bound.run_loop,
                    state_fingerprint="",
                ),
                program=SnapshotProgram(source_path="", agic={}),
            )
            self.debug = {}

        def instructions(self) -> str:
            return ""

        def context(self) -> str:
            return "Context for this run."

        def input_message(self) -> Message:
            return input_message

        def messages(self):
            return (input_message,)

        def tools(self):
            return {}

        def model_selector(self, _context):
            return None

        def effective_model_selectors(self, _context):
            return ()

    return RunInputStub()


class _FakeLoop:
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
    kind: StepKind,
    input=(),
    instructions: str | None = None,
    context: str | None = None,
) -> StepBegin:
    del thread_id
    step_context: dict[str, str] = {}
    if instructions is not None:
        step_context["instruct"] = instructions
    if context is not None:
        step_context["prompt_context"] = context
    return StepBegin(
        step=trace_child_path(run_id, step_index),
        kind=kind,
        input=tuple(input)
        or _default_step_input(run_id=run_id, step_index=step_index, kind=kind),
        started_at="2026-01-01T00:00:00Z",
        context=step_context,
    )


def _completed(
    step_index: int,
    *,
    run_id: str,
    thread_id: str,
    kind: StepKind,
    output=(),
    input_step_index: int | None = 0,
    input_part_index: int | None = None,
    error: str | None = None,
) -> StepEnd:
    del thread_id, input_step_index, input_part_index
    if kind == "model":
        detail = {
            "model_ref": "gpt-5",
            "usage": {"input_tokens": 0, "output_tokens": 0},
        }
    elif kind == "tool":
        detail = {"tool": "tool"}
    else:
        detail = {}
    return StepEnd(
        step=trace_child_path(run_id, step_index),
        kind=kind,
        status="failed" if error else "finished",
        output=tuple(output),
        detail=detail,
        started_at="2026-01-01T00:00:00Z",
        finished_at="2026-01-01T00:00:01Z",
        error=error,
    )


def test_model_step_detail_preserves_target_metadata(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs.db")
    try:
        project_run_start(store,
            run_id="run-1",
            thread_id="thread-1",
            origin="chat",
            input=Message.user("hi"),
        )
        detail = {
            "model_ref": "openai/gpt-5",
            "usage": {"input_tokens": 11, "output_tokens": 7},
            "provider": "openrouter",
            "model": "openai/gpt-5",
            "adapter": "responses",
            "base_url": "https://openrouter.ai/api/v1",
            "adapter_request": {
                "instructions": "system",
                "messages": [
                    {"role": "user", "parts": [{"type": "text", "text": "hi"}]}
                ],
                "tools": [
                    {"name": "search", "description": "Search.", "parameters": {}}
                ],
                "state": {"thread": "state"},
            },
        }
        project_step(store,
            run_id="run-1",
            step_index=1,
            kind="model",
            status="finished",
            input=(InputRef(),),
            output=(TextPart(text="ok"),),
            detail=detail,
            started_at="2026-01-01T00:00:01Z",
            finished_at="2026-01-01T00:00:02Z",
        )
        step = store.list_steps(run_id="run-1")[0]
    finally:
        store.close()

    assert step.detail == detail


def test_prompt_store_uses_content_hash_for_all_prompt_kinds(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs.db")
    try:
        instruct_hash = store.put_prompt(body="shared body")
        context_hash = store.put_prompt(body="shared body")
        body = store.get_prompt(prompt_hash=instruct_hash)
    finally:
        store.close()

    assert instruct_hash == context_hash
    assert instruct_hash == hashlib.sha256(b"shared body").hexdigest()
    assert body == "shared body"


def _default_step_input(
    *,
    run_id: str,
    step_index: int,
    kind: StepKind,
):
    if kind == "system":
        return ()
    if kind == "model":
        if step_index == 1:
            return (InputRef(),)
        return (OutputRef(step=trace_child_path(run_id, step_index - 1)),)
    return (OutputRef(step=trace_child_path(run_id, max(step_index - 1, 1)), part=0),)


def _output_ref(
    run_id: str,
    step_index: int,
    part_index: int | None = None,
) -> OutputRef:
    return OutputRef(step=trace_child_path(run_id, step_index), part=part_index)


def _model_detail(
    model_ref: str = "gpt-5",
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    **extra: object,
) -> dict[str, object]:
    return {
        "model_ref": model_ref,
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
        **extra,
    }


@contextmanager
def _patched_run_input_assembly(fake_assemble):
    def fake_from_agic(context: ApiContext, bound, _agic):
        return fake_assemble(context, bound)

    with (
        patch.object(
            run_executor_module.RunInput, "from_binding", side_effect=fake_assemble
        ),
        patch.object(
            run_executor_module.RunInput, "from_agic", side_effect=fake_from_agic
        ),
    ):
        yield


@contextmanager
def _patched_executor_execution():
    current: dict[str, str] = {}

    def fake_assemble(_context: ApiContext, bound):
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
                    kind="model",
                    instructions=instructions,
                    context="Context for this run.",
                )
            )
            context.on_event(
                _completed(
                    1,
                    run_id=current["run_id"],
                    thread_id=current["thread_id"],
                    kind="model",
                    output=(TextPart(text=output_text),),
                )
            )
        return RunResult(output_text=output_text)

    with (
        _patched_run_input_assembly(fake_assemble),
        patch.object(
            run_executor_module,
            "_default_load_loop",
            return_value=_FakeLoop(
                run=fake_run,
            ),
        ),
    ):
        yield


@contextmanager
def _patched_executor_execution_with_tools(*, output_text: str):
    current: dict[str, str] = {}

    def fake_assemble(_context: ApiContext, bound):
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
                    kind="model",
                )
            )
            context.on_event(
                _completed(
                    1,
                    run_id=current["run_id"],
                    thread_id=current["thread_id"],
                    kind="model",
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
                    kind="tool",
                    input=(_output_ref(current["run_id"], 1, 0),),
                )
            )
            context.on_event(
                _completed(
                    2,
                    run_id=current["run_id"],
                    thread_id=current["thread_id"],
                    kind="tool",
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
                    kind="model",
                )
            )
            on_event(
                PartBegin(
                    step=trace_child_path(current["run_id"], 1),
                    part=0,
                    type_="tool_call",
                )
            )
            on_event(
                PartDelta(
                    step=trace_child_path(current["run_id"], 1),
                    part=0,
                    delta=ToolCallDelta(
                        text='{"a":7,"b":8}',
                        tool_call_id="call_1",
                    ),
                )
            )
            on_event(
                PartEnd(
                    step=trace_child_path(current["run_id"], 1),
                    part=0,
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
                    kind="model",
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
                    kind="tool",
                    input=(_output_ref(current["run_id"], 1, 0),),
                )
            )
            on_event(
                PartEnd(
                    step=trace_child_path(current["run_id"], 2),
                    part=0,
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
                    kind="tool",
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
                    kind="model",
                )
            )
            on_event(
                PartBegin(
                    step=trace_child_path(current["run_id"], 3),
                    part=0,
                    type_="text",
                )
            )
            on_event(
                PartDelta(
                    step=trace_child_path(current["run_id"], 3),
                    part=0,
                    delta=TextDelta(text=output_text),
                )
            )
            on_event(
                _completed(
                    3,
                    run_id=current["run_id"],
                    thread_id=current["thread_id"],
                    kind="model",
                    input_step_index=2,
                    input_part_index=None,
                    output=(TextPart(text=output_text),),
                )
            )
        return RunResult(output_text=output_text)

    with (
        _patched_run_input_assembly(fake_assemble),
        patch.object(
            run_executor_module,
            "_default_load_loop",
            return_value=_FakeLoop(
                run=fake_run_stream,
            ),
        ),
    ):
        yield


@contextmanager
def _patched_executor_failure(message: str):
    def fake_assemble(_context: ApiContext, bound):
        return _fake_run_input(bound)

    def fake_run(_context):
        raise RuntimeError(message)

    with (
        _patched_run_input_assembly(fake_assemble),
        patch.object(
            run_executor_module,
            "_default_load_loop",
            return_value=_FakeLoop(
                run=fake_run,
            ),
        ),
    ):
        yield


@contextmanager
def _patched_executor_streaming_text(release: threading.Event):
    current: dict[str, str] = {}

    def fake_assemble(_context: ApiContext, bound):
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
                    kind="model",
                )
            )
            on_event(
                PartBegin(
                    step=trace_child_path(current["run_id"], 1),
                    part=0,
                    type_="text",
                )
            )
            on_event(
                PartDelta(
                    step=trace_child_path(current["run_id"], 1),
                    part=0,
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
                    kind="model",
                    output=(TextPart(text="streaming hello"),),
                )
            )
        return RunResult(output_text="streaming hello")

    with (
        _patched_run_input_assembly(fake_assemble),
        patch.object(
            run_executor_module,
            "_default_load_loop",
            return_value=_FakeLoop(
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
