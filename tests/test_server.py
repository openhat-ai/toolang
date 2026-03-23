from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from toolang.agent.prepared import prepare_agent
from toolang.agent.resolve import resolve_agent_ref
from toolang.agent.registry import get_running_agent
from toolang.bus.db import BusStore
from toolang.channels import ChannelState, DeliveryResult, PluginHealth, PollResult
from toolang.concepts.channel import InboundDelivery, OutboundMessage, ReplyTarget
from toolang.concepts.layout import AgentHome, ToolangRoot
from toolang.concepts.persisted import (
    ChannelBinding,
    ChannelsConfig,
    ChoreFile,
    HookBinding,
    HooksConfig,
    PollState,
    TaskFile,
    WillFile,
)
from toolang.concepts.persisted.run_state import RunState
from toolang.concepts.persisted.prompt_trace import PromptTrace
from toolang.runtime.execution_store import ExecutionStore
from toolang.runtime.server import create_agent_app


def resolve_toolang_root(root: Path) -> Path:
    return ToolangRoot.resolve(root).path


def agent_run_path(agent_home: Path, agent_name: str) -> Path:
    return AgentHome.resolve(agent_home).room(agent_name).run_path


def agent_run_prompt_path(agent_home: Path, agent_name: str, run_id: str) -> Path:
    return AgentHome.resolve(agent_home).room(agent_name).prompt_trace_path(run_id)


def agent_execution_db_path(agent_home: Path, agent_name: str) -> Path:
    return AgentHome.resolve(agent_home).room(agent_name).execution_db_path


def agents_db_path(root: Path) -> Path:
    return ToolangRoot.resolve(root).agents_db_path


def bus_events_db_path(root: Path) -> Path:
    return ToolangRoot.resolve(root).bus_events_db_path


SOURCE_FIXTURE = Path(__file__).parent / "fixtures" / "source_only.too"


def test_create_agent_app_serves_webui_compatible_endpoints(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = resolve_toolang_root(tmp_path / "toolang-root")
    home = root / "agents" / "alice"
    home.mkdir(parents=True)
    (home / "alice.too").write_text(
        SOURCE_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8"
    )

    agent = resolve_agent_ref("alice", cwd=tmp_path, toolang_root=root)
    prepared = prepare_agent(agent)
    db_path = agents_db_path(root)
    events_path = bus_events_db_path(root)
    run_path = agent_run_path(home, "alice")
    execution_db_path = agent_execution_db_path(home, "alice")

    def fake_execute(build) -> str:
        thunk_name = build.runtime_context["program"]["thunk"]["name"] or "default"
        if build.message_context is not None:
            return f"chat:{len(build.messages) - 1}:{build.raw_input}:{build.model}"
        return f"invoke:{thunk_name}:{build.raw_input}:{build.model}"

    monkeypatch.setattr("toolang.runtime.invoke.execute_prompt_build", fake_execute)

    app = create_agent_app(
        prepared,
        agents_db_path=db_path,
        bus_db_path=events_path,
        host="127.0.0.1",
        port=8765,
        sandbox="host",
    )

    with TestClient(app) as client:
        healthz = client.get("/healthz")
        assert healthz.status_code == 200
        assert healthz.json() == {"ok": True, "agent": "alice"}

        runtime = client.get("/api/v1/runtime")
        assert runtime.status_code == 200
        assert runtime.json()["status"] == "online"
        assert runtime.json()["endpoint"] == "http://127.0.0.1:8765"
        assert runtime.json()["working_directory"] == str(home)
        assert runtime.json()["sandbox"] == "host"
        assert runtime.json()["model"] == "gpt-5"

        cors_runtime = client.get(
            "/api/v1/runtime",
            headers={"Origin": "http://localhost:3000"},
        )
        assert (
            cors_runtime.headers["access-control-allow-origin"]
            == "http://localhost:3000"
        )

        cors_runtime_too_run = client.get(
            "/api/v1/runtime",
            headers={"Origin": "https://too.run"},
        )
        assert (
            cors_runtime_too_run.headers["access-control-allow-origin"]
            == "https://too.run"
        )

        pna_runtime = client.options(
            "/api/v1/runtime",
            headers={
                "Origin": "https://too.run",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Private-Network": "true",
            },
        )
        assert pna_runtime.status_code == 200
        assert pna_runtime.headers["access-control-allow-origin"] == "https://too.run"
        assert pna_runtime.headers["access-control-allow-private-network"] == "true"

        profile = client.get("/api/v1/profile")
        assert profile.status_code == 200
        assert profile.json()["agent"] == "alice"

        caps = client.get("/api/v1/caps")
        assert caps.status_code == 200
        assert caps.json()["agent"] == "alice"
        assert [item["name"] for item in caps.json()["servers"]] == ["github"]
        assert [item["name"] for item in caps.json()["skills"]] == []
        assert [item["name"] for item in caps.json()["psyches"]] == ["reviewer"]
        assert caps.json()["counts"] == {
            "psyches": 1,
            "skills": 0,
            "servers": 1,
            "chores": 0,
        }

        active = get_running_agent(db_path, agent.uri)
        assert active is not None
        assert active.status == "running"
        assert active.sandbox == "host"
        assert run_path.exists()
        assert RunState.load(run_path).status == "running"
        assert RunState.load(run_path).sandbox.spec == "host"

        first_chat = client.post(
            "/api/v1/chat",
            json={"thread": "owner", "message": "hello"},
        )
        assert first_chat.status_code == 200
        first_chat_body = first_chat.json()
        assert first_chat_body["thread_id"] == "owner"
        assert first_chat_body["message"]["parts"][0]["text"] == "chat:1:hello:gpt-5"
        first_run_id = first_chat_body["turn_id"]
        first_trace = PromptTrace.load(
            agent_run_prompt_path(home, "alice", first_run_id)
        )
        assert first_trace.message_context is not None
        assert first_trace.message_context["channel"] == "api"
        assert first_trace.sandbox == "host"
        assert (
            first_trace.runtime_context["visible_caps"]["psyches"][0]["name"]
            == "reviewer"
        )

        second_chat = client.post(
            "/api/v1/chat",
            json={"thread": "owner", "message": "again"},
        )
        assert second_chat.status_code == 200
        second_chat_body = second_chat.json()
        assert second_chat_body["message"]["parts"][0]["text"] == "chat:3:again:gpt-5"

        with client.stream(
            "POST",
            "/api/v1/chat/stream",
            json={"thread": "owner", "message": "stream me"},
        ) as response:
            assert response.status_code == 200
            stream_text = "".join(
                chunk.decode("utf-8") for chunk in response.iter_raw()
            )
        assert '"type":"start"' in stream_text
        assert '"type":"text-delta"' in stream_text
        assert "chat:5:stream me:gpt-5" in stream_text
        assert "data: [DONE]" in stream_text

        threads = client.get("/api/v1/chats")
        assert threads.status_code == 200
        assert threads.json()["items"][0]["id"] == "owner"

        thread = client.get("/api/v1/chats/owner")
        assert thread.status_code == 200
        assert len(thread.json()["turns"]) == 3
        assert [item["role"] for item in thread.json()["turns"][0]["messages"]] == [
            "user",
            "assistant",
        ]

        runs = client.get("/api/v1/runs")
        assert runs.status_code == 200
        assert len(runs.json()["items"]) == 3
        assert runs.json()["items"][0]["origin_kind"] == "direct"

        detail = client.get(f"/api/v1/runs/{first_run_id}")
        assert detail.status_code == 200
        assert detail.json()["run"]["id"] == first_run_id
        assert [item["role"] for item in detail.json()["turn"]["messages"]] == [
            "user",
            "assistant",
        ]
        assert [item["event_type"] for item in detail.json()["events"]] == [
            "run_started",
            "run_finished",
        ]

        run_response = client.post(
            "/api/v1/runs",
            json={"thunk": "summarize", "input": "hello", "model": "gpt-5.3"},
        )
        assert run_response.status_code == 200
        assert run_response.json()["output"] == "invoke:summarize:hello:gpt-5.3"
        invoke_trace = PromptTrace.load(
            agent_run_prompt_path(home, "alice", run_response.json()["run_id"])
        )
        assert invoke_trace.raw_input == "hello"
        assert invoke_trace.expanded_input == "hello"
        assert invoke_trace.model == "gpt-5.3"

        events = client.get("/api/v1/events")
        assert events.status_code == 200
        assert [item["event_type"] for item in events.json()["items"]] == [
            "agent_started",
            "run_started",
            "run_finished",
            "run_started",
            "run_finished",
            "run_started",
            "run_finished",
            "run_started",
            "run_finished",
        ]

        execution = ExecutionStore(execution_db_path)
        runs = execution.list_runs(agent_uri=agent.uri)
        turns = execution.list_turns(run_id=runs[0].run_id)
        execution.close()
        assert len(runs) == 1
        assert runs[0].run_kind == "runtime"
        assert runs[0].status == "running"
        assert {turn.turn_id for turn in turns} >= {
            first_run_id,
            run_response.json()["run_id"],
        }

    assert get_running_agent(db_path, agent.uri) is None
    assert RunState.load(run_path).status == "stopped"
    execution = ExecutionStore(execution_db_path)
    runs = execution.list_runs(agent_uri=agent.uri)
    execution.close()
    assert runs[0].status == "stopped"
    store = BusStore(events_path)
    events = store.list_events(agent_uri=agent.uri)
    store.close()
    assert events[-1].event_type == "agent_stopped"


def test_create_agent_app_reports_docker_sandbox_state(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = resolve_toolang_root(tmp_path / "toolang-root")
    home = root / "agents" / "alice"
    home.mkdir(parents=True)
    (home / "alice.too").write_text(
        SOURCE_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8"
    )

    agent = resolve_agent_ref("alice", cwd=tmp_path, toolang_root=root)
    prepared = prepare_agent(agent)
    db_path = agents_db_path(root)
    events_path = bus_events_db_path(root)
    run_path = agent_run_path(home, "alice")

    monkeypatch.setattr(
        "toolang.runtime.invoke.execute_prompt_build",
        lambda build: "ok",
    )

    app = create_agent_app(
        prepared,
        agents_db_path=db_path,
        bus_db_path=events_path,
        host="0.0.0.0",
        port=8766,
        sandbox="docker:python:3.13-slim",
        public_host="127.0.0.1",
    )

    with TestClient(app) as client:
        runtime = client.get("/api/v1/runtime")
        assert runtime.status_code == 200
        assert runtime.json()["endpoint"] == "http://127.0.0.1:8766"
        assert runtime.json()["execution_host"] == "docker"
        assert runtime.json()["sandbox"] == "docker:python:3.13-slim"

        run_state = RunState.load(run_path)
        assert run_state.sandbox.type == "docker"
        assert run_state.sandbox.image_name == "python:3.13-slim"
        assert run_state.sandbox.container_name is not None


def test_create_agent_app_polls_channel_bindings_and_delivers_replies(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = resolve_toolang_root(tmp_path / "toolang-root")
    home = root / "agents" / "alice"
    home.mkdir(parents=True)
    (home / "alice.too").write_text(
        SOURCE_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8"
    )

    agent = resolve_agent_ref("alice", cwd=tmp_path, toolang_root=root)
    prepared = prepare_agent(agent)
    db_path = agents_db_path(root)
    events_path = bus_events_db_path(root)
    execution_db_path = agent_execution_db_path(home, "alice")
    poll_state_path = AgentHome.resolve(home).room("alice").poll_state_path("telegram")

    class FakeTelegramPlugin:
        def __init__(self) -> None:
            self._emitted = False
            self.deliveries: list[tuple[ReplyTarget, OutboundMessage]] = []

        def poll(self, state: ChannelState) -> PollResult:
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
                        reply_target=ReplyTarget(
                            channel="telegram", address="chat:123"
                        ),
                    )
                ],
                next_state=ChannelState(cursor="43"),
            )

        def decode_hook(self, request):
            return None

        def deliver(
            self, target: ReplyTarget, message: OutboundMessage
        ) -> DeliveryResult:
            self.deliveries.append((target, message))
            return DeliveryResult(ok=True, remote_id="99")

        def health(self) -> PluginHealth:
            return PluginHealth(ok=True)

    fake_plugin = FakeTelegramPlugin()
    monkeypatch.setattr(
        "toolang.runtime.host.create_channel_plugin",
        lambda plugin, *, config=None: fake_plugin,
    )
    monkeypatch.setattr(
        "toolang.runtime.invoke.execute_prompt_build",
        lambda build: f"polled:{build.raw_input}:{build.model}",
    )

    app = create_agent_app(
        prepared,
        agents_db_path=db_path,
        bus_db_path=events_path,
        host="127.0.0.1",
        port=8767,
        sandbox="host",
        runtime_loops=("server", "poll"),
        channels_config=ChannelsConfig(
            channels={
                "telegram": ChannelBinding(
                    plugin="telegram", config={"token": "secret"}
                )
            }
        ),
    )

    with TestClient(app):
        _wait_for(
            lambda: len(fake_plugin.deliveries) == 1, label="telegram reply delivery"
        )

        execution = ExecutionStore(execution_db_path)
        runs = execution.list_runs(agent_uri=agent.uri)
        turns = execution.list_turns(run_id=runs[0].run_id)
        steps = execution.list_steps(turn_id=turns[0].turn_id)
        execution.close()

        assert runs[0].runtime_loops == ("server", "poll")
        assert turns[0].thread_id == "telegram:123"
        assert turns[0].origin == "chat"
        assert turns[0].status == "finished"
        assert any(step.step_kind == "delivery" for step in steps)
        assert fake_plugin.deliveries[0][0].channel == "telegram"
        assert fake_plugin.deliveries[0][1].text == "polled:hello from poll:gpt-5"
        assert poll_state_path.exists()
        assert PollState.load(poll_state_path).cursor == "43"


def test_create_agent_app_polls_task_deliveries(tmp_path: Path, monkeypatch) -> None:
    root = resolve_toolang_root(tmp_path / "toolang-root")
    home = root / "agents" / "alice"
    home.mkdir(parents=True)
    (home / "alice.too").write_text(
        SOURCE_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8"
    )

    agent = resolve_agent_ref("alice", cwd=tmp_path, toolang_root=root)
    prepared = prepare_agent(agent)
    db_path = agents_db_path(root)
    events_path = bus_events_db_path(root)
    execution_db_path = agent_execution_db_path(home, "alice")

    class FakeTaskPlugin:
        def __init__(self) -> None:
            self._emitted = False

        def poll(self, state: ChannelState) -> PollResult:
            if self._emitted:
                return PollResult(next_state=state)
            self._emitted = True
            return PollResult(
                deliveries=[
                    InboundDelivery(
                        origin="task",
                        channel=None,
                        sender="service",
                        thread_id="task:linear/42",
                        text="Investigate the regression and report back.",
                    )
                ],
                next_state=ChannelState(cursor="next-task"),
            )

        def decode_hook(self, request):
            return None

        def deliver(
            self, target: ReplyTarget, message: OutboundMessage
        ) -> DeliveryResult:
            return DeliveryResult(ok=True)

        def health(self) -> PluginHealth:
            return PluginHealth(ok=True)

    monkeypatch.setattr(
        "toolang.runtime.host.create_channel_plugin",
        lambda plugin, *, config=None: FakeTaskPlugin(),
    )
    monkeypatch.setattr(
        "toolang.runtime.invoke.execute_prompt_build",
        lambda build: (
            f"tasked:{build.runtime_context['origin']}:{build.raw_input}:{build.model}"
        ),
    )

    app = create_agent_app(
        prepared,
        agents_db_path=db_path,
        bus_db_path=events_path,
        host="127.0.0.1",
        port=8768,
        sandbox="host",
        runtime_loops=("server", "poll"),
        channels_config=ChannelsConfig(
            channels={
                "linear": ChannelBinding(plugin="linear", config={"token": "secret"})
            }
        ),
    )

    with TestClient(app):
        execution = ExecutionStore(execution_db_path)
        try:
            _wait_for(
                lambda: _turn_origins(execution, agent.uri) >= {"task"},
                label="task poll turn",
                timeout_sec=5.0,
            )
            runs = execution.list_runs(agent_uri=agent.uri)
            turns = execution.list_turns(run_id=runs[0].run_id)
        finally:
            execution.close()

    task_turns = [turn for turn in turns if turn.origin == "task"]
    assert len(task_turns) == 1
    assert task_turns[0].thread_id == "task:linear/42"
    assert task_turns[0].sender == "service"
    assert (
        task_turns[0].output_text
        == "tasked:task:Investigate the regression and report back.:gpt-5"
    )


def test_create_agent_app_lists_local_work_documents(tmp_path: Path) -> None:
    root = resolve_toolang_root(tmp_path / "toolang-root")
    home = root / "agents" / "alice"
    home.mkdir(parents=True)
    (home / "alice.too").write_text(
        SOURCE_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8"
    )

    room = AgentHome.resolve(home).room("alice")
    (room.tasks_dir / "roadmap.md").parent.mkdir(parents=True, exist_ok=True)
    (room.tasks_dir / "roadmap.md").write_text(
        "---\nrequester: owner\nstatus: todo\n---\nRead the current milestone and comment.\n",
        encoding="utf-8",
    )
    ChoreFile(
        title="Sync backlog",
        body="Sync backlog from the project tool.",
        interval_sec=1800,
        thunk="chore",
    ).save(room.chores_dir / "sync.md")
    WillFile(
        title="Reflect",
        body="Think about the next milestone.",
        interval_sec=3600,
        thunk="will",
    ).save(room.will_path)

    agent = resolve_agent_ref("alice", cwd=tmp_path, toolang_root=root)
    prepared = prepare_agent(agent)

    app = create_agent_app(
        prepared,
        agents_db_path=agents_db_path(root),
        bus_db_path=bus_events_db_path(root),
        host="127.0.0.1",
        port=8769,
        sandbox="host",
    )

    with TestClient(app) as client:
        tasks = client.get("/api/v1/tasks")
        chores = client.get("/api/v1/chores")
        will = client.get("/api/v1/will")
    saved_task = TaskFile.load(room.tasks_dir / "roadmap.md", persist_id=True)

    assert tasks.status_code == 200
    assert tasks.json()["items"] == [
        {
            "id": saved_task.id,
            "name": "roadmap",
            "body": saved_task.body,
            "status": "todo",
            "requester": saved_task.requester,
            "thread_id": f"task:local:{saved_task.id}",
            "path": str(room.tasks_dir / "roadmap.md"),
            "last_enqueued_at": None,
            "last_started_at": None,
            "last_finished_at": None,
            "last_status": None,
            "last_run_id": None,
            "updated_at": tasks.json()["items"][0]["updated_at"],
            "paused": False,
        }
    ]
    assert chores.status_code == 200
    assert chores.json()["items"] == [
        {
            "id": "sync",
            "title": "Sync backlog",
            "thread_id": "chore:sync",
            "interval_sec": 1800,
            "thunk": "chore",
            "model": None,
            "path": str(room.chores_dir / "sync.md"),
            "last_enqueued_at": None,
            "last_started_at": None,
            "last_finished_at": None,
            "last_status": None,
            "last_run_id": None,
            "next_due_at": None,
            "updated_at": chores.json()["items"][0]["updated_at"],
            "paused": False,
        }
    ]
    assert will.status_code == 200
    assert will.json()["item"] == {
        "title": "Reflect",
        "thread_id": f"will:{agent.id}",
        "interval_sec": 3600,
        "thunk": "will",
        "model": None,
        "path": str(room.will_path),
        "last_enqueued_at": None,
        "last_started_at": None,
        "last_finished_at": None,
        "last_status": None,
        "last_run_id": None,
        "next_due_at": None,
        "updated_at": will.json()["item"]["updated_at"],
        "paused": False,
    }


def test_create_agent_app_puts_and_patches_local_tasks(tmp_path: Path) -> None:
    root = resolve_toolang_root(tmp_path / "toolang-root")
    home = root / "agents" / "alice"
    home.mkdir(parents=True)
    (home / "alice.too").write_text(
        SOURCE_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8"
    )

    agent = resolve_agent_ref("alice", cwd=tmp_path, toolang_root=root)
    prepared = prepare_agent(agent)
    room = AgentHome.resolve(home).room("alice")

    app = create_agent_app(
        prepared,
        agents_db_path=agents_db_path(root),
        bus_db_path=bus_events_db_path(root),
        host="127.0.0.1",
        port=8770,
        sandbox="host",
    )

    with TestClient(app) as client:
        created = client.put(
            "/api/v1/tasks/planning/roadmap",
            json={
                "body": "Draft the roadmap update.",
                "status": "todo",
                "requester": "owner",
            },
        )
        assert created.status_code == 200
        created_id = created.json()["id"]
        assert created_id
        assert created.json()["name"] == "roadmap"
        assert created.json()["thread_id"] == f"task:local:{created_id}"
        assert created.json()["path"] == str(room.tasks_dir / "planning" / "roadmap.md")

        patched = client.patch(
            "/api/v1/tasks/planning/roadmap",
            json={
                "status": "doing",
                "body_append": "Started working on the outline.",
                "paused": True,
            },
        )
        assert patched.status_code == 200
        assert patched.json()["status"] == "doing"
        assert patched.json()["paused"] is True

        tasks = client.get("/api/v1/tasks")
        assert tasks.status_code == 200
        assert tasks.json()["items"][0]["id"] == created_id
        assert tasks.json()["items"][0]["status"] == "doing"
        assert tasks.json()["items"][0]["paused"] is True

    saved = TaskFile.load(room.tasks_dir / "planning" / "roadmap.md", persist_id=True)
    assert saved.id == created_id
    assert saved.requester == "owner"
    assert saved.status == "doing"
    assert saved.paused is True
    assert "Started working on the outline." in saved.body


def test_create_agent_app_puts_and_patches_local_chores_and_will(
    tmp_path: Path,
) -> None:
    root = resolve_toolang_root(tmp_path / "toolang-root")
    home = root / "agents" / "alice"
    home.mkdir(parents=True)
    (home / "alice.too").write_text(
        SOURCE_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8"
    )

    agent = resolve_agent_ref("alice", cwd=tmp_path, toolang_root=root)
    prepared = prepare_agent(agent)
    room = AgentHome.resolve(home).room("alice")

    app = create_agent_app(
        prepared,
        agents_db_path=agents_db_path(root),
        bus_db_path=bus_events_db_path(root),
        host="127.0.0.1",
        port=8771,
        sandbox="host",
    )

    with TestClient(app) as client:
        created_chore = client.put(
            "/api/v1/chores/maintenance/sync",
            json={
                "title": "Sync maintenance",
                "body": "Refresh the maintenance plan.",
                "interval_sec": 1800,
                "thunk": "chore",
                "model": "gpt-5.3",
            },
        )
        assert created_chore.status_code == 200
        assert created_chore.json()["id"] == "maintenance/sync"
        assert created_chore.json()["thread_id"] == "chore:maintenance/sync"
        assert created_chore.json()["path"] == str(
            room.chores_dir / "maintenance" / "sync.md"
        )

        patched_chore = client.patch(
            "/api/v1/chores/maintenance/sync",
            json={
                "interval_sec": 900,
                "body_append": "Also refresh labels.",
                "paused": True,
                "thread_id": "chore:maintenance/custom",
            },
        )
        assert patched_chore.status_code == 200
        assert patched_chore.json()["interval_sec"] == 900
        assert patched_chore.json()["paused"] is True
        assert patched_chore.json()["thread_id"] == "chore:maintenance/custom"

        created_will = client.put(
            "/api/v1/will",
            json={
                "title": "Stay aligned",
                "body": "Review the current milestone and choose the next move.",
                "interval_sec": 3600,
                "thunk": "will",
            },
        )
        assert created_will.status_code == 200
        assert created_will.json()["item"]["path"] == str(room.will_path)
        assert created_will.json()["item"]["thread_id"] == f"will:{agent.id}"

        patched_will = client.patch(
            "/api/v1/will",
            json={
                "interval_sec": 7200,
                "body_append": "Prefer quieter work in the afternoon.",
                "paused": True,
                "thread_id": "will:custom",
                "model": "gpt-5.3",
            },
        )
        assert patched_will.status_code == 200
        assert patched_will.json()["item"]["interval_sec"] == 7200
        assert patched_will.json()["item"]["paused"] is True
        assert patched_will.json()["item"]["thread_id"] == "will:custom"
        assert patched_will.json()["item"]["model"] == "gpt-5.3"

        chores = client.get("/api/v1/chores")
        assert chores.status_code == 200
        assert chores.json()["items"][0]["id"] == "maintenance/sync"
        assert chores.json()["items"][0]["interval_sec"] == 900
        assert chores.json()["items"][0]["paused"] is True

        will = client.get("/api/v1/will")
        assert will.status_code == 200
        assert will.json()["item"]["interval_sec"] == 7200
        assert will.json()["item"]["paused"] is True

    saved_chore = ChoreFile.load(room.chores_dir / "maintenance" / "sync.md")
    assert saved_chore.title == "Sync maintenance"
    assert saved_chore.interval_sec == 900
    assert saved_chore.thread_id == "chore:maintenance/custom"
    assert saved_chore.paused is True
    assert "Also refresh labels." in saved_chore.body

    saved_will = WillFile.load(room.will_path)
    assert saved_will.title == "Stay aligned"
    assert saved_will.interval_sec == 7200
    assert saved_will.thread_id == "will:custom"
    assert saved_will.paused is True
    assert saved_will.model == "gpt-5.3"
    assert "Prefer quieter work in the afternoon." in saved_will.body


def test_create_agent_app_exposes_prompt_trace_and_runtime_diagnostics(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = resolve_toolang_root(tmp_path / "toolang-root")
    home = root / "agents" / "alice"
    home.mkdir(parents=True)
    (home / "alice.too").write_text(
        SOURCE_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8"
    )

    room = AgentHome.resolve(home).room("alice")
    AgentHome.resolve(home).ensure_layout(agent_name="alice")
    HooksConfig(
        hooks={
            "incoming": HookBinding(
                path="/hooks/incoming",
                plugin="webhook",
            )
        }
    ).save(AgentHome.resolve(home).hooks_config_path)

    agent = resolve_agent_ref("alice", cwd=tmp_path, toolang_root=root)
    prepared = prepare_agent(agent)
    poll_state_path = room.poll_state_path("telegram")

    class HealthyTelegramPlugin:
        def poll(self, state: ChannelState) -> PollResult:
            return PollResult(
                next_state=ChannelState(cursor="cursor-1", meta={"seen": 1}),
            )

        def decode_hook(self, request):
            return None

        def deliver(
            self, target: ReplyTarget, message: OutboundMessage
        ) -> DeliveryResult:
            return DeliveryResult(ok=True)

        def health(self) -> PluginHealth:
            return PluginHealth(ok=True, detail="ready", meta={"binding": "telegram"})

    fake_plugin = HealthyTelegramPlugin()
    monkeypatch.setattr(
        "toolang.runtime.host.create_channel_plugin",
        lambda plugin, *, config=None: fake_plugin,
    )
    monkeypatch.setattr(
        "toolang.runtime.invoke.execute_prompt_build",
        lambda build: f"invoke:{build.raw_input}:{build.model}",
    )

    app = create_agent_app(
        prepared,
        agents_db_path=agents_db_path(root),
        bus_db_path=bus_events_db_path(root),
        host="127.0.0.1",
        port=8772,
        sandbox="host",
        runtime_loops=("server", "poll", "pulse"),
        channels_config=ChannelsConfig(
            channels={
                "telegram": ChannelBinding(
                    plugin="telegram", config={"token": "secret"}
                )
            }
        ),
    )

    with TestClient(app) as client:
        run_response = client.post("/api/v1/runs", json={"input": "hello"})
        assert run_response.status_code == 200
        run_id = run_response.json()["run_id"]

        _wait_for(
            lambda: (
                poll_state_path.exists()
                and PollState.load(poll_state_path).cursor == "cursor-1"
            ),
            label="telegram poll state",
        )

        prompt = client.get(f"/api/v1/runs/{run_id}/prompt")
        assert prompt.status_code == 200
        assert prompt.json()["run_id"] == run_id
        assert prompt.json()["raw_input"] == "hello"
        assert prompt.json()["model"] == "gpt-5"

        diagnostics = client.get("/api/v1/diagnostics")
        assert diagnostics.status_code == 200
        diagnostics_alias = client.get("/api/v1/runtime/diagnostics")
        assert diagnostics_alias.status_code == 200
        assert diagnostics_alias.json() == diagnostics.json()

    body = diagnostics.json()
    assert body["runtime_loops"] == ["server", "poll", "pulse"]
    assert body["hook_loop_enabled"] is False
    assert {item["kind"] for item in body["scheduler"]["thread_groups"]} == {
        "invoke",
        "chat",
        "task",
        "chore",
        "will",
    }
    assert body["channels"] == [
        {
            "name": "telegram",
            "plugin": "telegram",
            "ok": True,
            "detail": "ready",
            "meta": {"binding": "telegram"},
            "poll_state_path": str(poll_state_path),
            "poll_cursor": "cursor-1",
            "poll_meta": {"seen": 1},
        }
    ]
    assert body["hooks"] == [
        {
            "name": "incoming",
            "path": "/hooks/incoming",
            "method": "POST",
            "plugin": "webhook",
        }
    ]
    assert body["pulse"] == {
        "state_path": str(room.pulse_state_path),
        "pending": [],
    }


def test_run_process_writes_stopped_state_after_termination(tmp_path: Path) -> None:
    root = resolve_toolang_root(tmp_path / "toolang-root")
    home = root / "agents" / "alice"
    home.mkdir(parents=True)
    (home / "alice.too").write_text(
        SOURCE_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8"
    )

    run_path = agent_run_path(home, "alice")
    db_path = agents_db_path(root)
    port = _pick_free_port()
    env = os.environ.copy()
    env["TOOLANG_ROOT"] = str(root)
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "from toolang.cli import main; raise SystemExit(main())",
            "run",
            "alice",
            "--port",
            str(port),
        ],
        cwd=str(tmp_path),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    try:
        _wait_for_server(port)
        assert RunState.load(run_path).status == "running"

        process.terminate()
        process.wait(timeout=5)
        _wait_for_stopped_state(run_path)

        assert get_running_agent(db_path, "agent://alice/alice.too") is None
        assert RunState.load(run_path).status == "stopped"
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_server(port: int) -> None:
    deadline = time.monotonic() + 5.0
    url = f"http://127.0.0.1:{port}/api/v1/health"
    while time.monotonic() < deadline:
        try:
            response = httpx.get(url, timeout=0.2)
        except httpx.HTTPError:
            time.sleep(0.05)
            continue
        if response.status_code == 200:
            return
        time.sleep(0.05)
    raise AssertionError(f"Timed out waiting for server at {url}")


def _wait_for_stopped_state(run_path: Path) -> None:
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if run_path.exists() and RunState.load(run_path).status == "stopped":
            return
        time.sleep(0.05)
    raise AssertionError(f"Timed out waiting for stopped state at {run_path}")


def _wait_for(predicate, *, label: str, timeout_sec: float = 3.0) -> None:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(f"Timed out waiting for {label}")


def _turn_origins(execution: ExecutionStore, agent_uri: str) -> set[str]:
    runs = execution.list_runs(agent_uri=agent_uri)
    if not runs:
        return set()
    turns = execution.list_turns(run_id=runs[0].run_id)
    return {turn.origin for turn in turns}
