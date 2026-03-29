from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx
from fastapi.testclient import TestClient

from toolang.agent.prepared import prepare_agent
from toolang.agent.resolve import resolve_agent_ref
from toolang.agent.registry import get_running_agent
from toolang.bus.db import BusStore
from toolang.bus.events import AgentCreated, AgentRemoved
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
from toolang.concepts.tools import ToolCallResult
from toolang.runtime.execution_store import ExecutionStore
from toolang.runtime.model_exec import (
    ModelExecutionResult,
    TextDeltaEvent,
    ToolInputAvailableEvent,
    ToolInputDeltaEvent,
    ToolInputStartEvent,
    ToolOutputAvailableEvent,
)
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
REMOTE_SERVICE_FIXTURE = Path(__file__).parent / "fixtures" / "remote-service" / "github.md"
REMOTE_PSYCHE_FIXTURE = Path(__file__).parent / "fixtures" / "remote-psyche" / "reviewer.md"


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

    def fake_execute_stream(build, *, on_event) -> ModelExecutionResult:
        output = fake_execute(build)
        on_event(TextDeltaEvent(delta=output))
        return ModelExecutionResult(output_text=output)

    monkeypatch.setattr("toolang.runtime.invoke.execute_prompt_build", fake_execute)
    monkeypatch.setattr(
        "toolang.runtime.invoke.execute_prompt_build_stream",
        fake_execute_stream,
    )

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
        assert runtime.json()["security"] == {
            "sandbox": {
                "image": None,
                "volumes": [],
                "network_mode": "host",
                "bridge": None,
                "dns": [],
                "host_reachability": True,
            },
            "tools": {
                "filesystem": True,
                "shell": True,
                "browser_use": False,
                "computer_use": False,
                "service_use": True,
                "web_search": True,
                "mem_search": False,
                "file_search": False,
            },
            "autonomy": {
                "chores_enabled": False,
                "tasks_enabled": False,
                "will_enabled": False,
                "will_path_exists": False,
            },
            "self_modification": {
                "can_add_caps": True,
                "can_edit_will": True,
                "can_write_source": False,
                "can_persist_changes": True,
            },
        }

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
        assert [item["name"] for item in caps.json()["prompts"]] == ["summarize"]
        assert [item["name"] for item in caps.json()["services"]] == ["github"]
        assert [item["name"] for item in caps.json()["skills"]] == []
        assert [item["name"] for item in caps.json()["psyches"]] == ["reviewer"]
        assert caps.json()["counts"] == {
            "psyches": 1,
            "prompts": 1,
            "skills": 0,
            "services": 1,
        }

        psyches = client.get("/api/v1/psyches")
        assert psyches.status_code == 200
        assert [item["name"] for item in psyches.json()["items"]] == ["reviewer"]

        prompt_detail = client.get("/api/v1/prompts/summarize")
        assert prompt_detail.status_code == 200
        assert prompt_detail.json()["item"]["kind"] == "prompt"
        assert prompt_detail.json()["item"]["name"] == "summarize"
        assert prompt_detail.json()["item"]["scope"] == "agent"
        assert prompt_detail.json()["item"]["content"] == (
            "Summarize the request in a {{style}} style.\n"
            "Audience: {{audience}}\n\n"
            "{{input}}"
        )
        assert prompt_detail.json()["item"]["params"] == [
            {"name": "style", "optional": False},
            {"name": "audience", "optional": True},
        ]

        service_detail = client.get("/api/v1/services/github")
        assert service_detail.status_code == 200
        assert service_detail.json()["item"]["kind"] == "service"
        assert service_detail.json()["item"]["scope"] == "agent"
        assert (
            service_detail.json()["item"]["content"]
            == REMOTE_SERVICE_FIXTURE.read_text(encoding="utf-8").rstrip("\n")
        )

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
        first_run_id = first_chat_body["run_id"]
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

        threads = client.get("/api/v1/threads")
        assert threads.status_code == 200
        assert threads.json()["items"][0]["id"] == "owner"
        assert threads.json()["items"][0]["title"] == "hello"
        assert threads.json()["items"][0]["preview"] == "chat:5:stream me:gpt-5"
        assert threads.json()["items"][0]["channel"] == "api"
        assert threads.json()["items"][0]["kind"] == "chat"

        thread = client.get("/api/v1/threads/owner")
        assert thread.status_code == 200
        assert len(thread.json()["messages"]) == 6
        latest_messages = thread.json()["messages"][-2:]
        assert [item["role"] for item in latest_messages] == [
            "user",
            "assistant",
        ]
        assert latest_messages[0]["parts"] == [
            {
                "id": f'{latest_messages[0]["run_id"]}:user:text:1',
                "type": "text",
                "text": "stream me",
                "state": "done",
            }
        ]
        assert thread.json()["runs"][0]["origin"] == "chat"

        runs = client.get("/api/v1/runs")
        assert runs.status_code == 200
        assert len(runs.json()["items"]) == 3
        assert runs.json()["items"][0]["origin"] == "chat"

        detail = client.get(f"/api/v1/runs/{first_run_id}")
        assert detail.status_code == 200
        assert detail.json()["run"]["id"] == first_run_id
        assert [item["role"] for item in detail.json()["messages"]] == [
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
        activations = execution.list_activations(agent_uri=agent.uri)
        runs = execution.list_runs(activation_id=activations[0].activation_id)
        execution.close()
        assert len(activations) == 1
        assert activations[0].activation_kind == "runtime"
        assert activations[0].status == "running"
        assert {run.run_id for run in runs} >= {
            first_run_id,
            run_response.json()["run_id"],
        }

    assert get_running_agent(db_path, agent.uri) is None
    assert RunState.load(run_path).status == "stopped"
    execution = ExecutionStore(execution_db_path)
    activations = execution.list_activations(agent_uri=agent.uri)
    execution.close()
    assert activations[0].status == "stopped"
    store = BusStore(events_path)
    events = store.list_events(agent_uri=agent.uri)
    store.close()
    assert events[-1].event_type == "agent_stopped"


def test_chat_stream_emits_tool_call_events(tmp_path: Path, monkeypatch) -> None:
    root = resolve_toolang_root(tmp_path / "toolang-root")
    home = root / "agents" / "alice"
    home.mkdir(parents=True)
    (home / "alice.too").write_text(
        SOURCE_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8"
    )

    agent = resolve_agent_ref("alice", cwd=tmp_path, toolang_root=root)
    prepared = prepare_agent(agent)

    def fake_execute_stream(build, *, on_event) -> ModelExecutionResult:
        on_event(
            ToolInputStartEvent(
                tool_call_id="call_1",
            )
        )
        on_event(
            ToolInputDeltaEvent(
                tool_call_id="call_1",
                delta='{"command":"pwd"}',
            )
        )
        on_event(
            ToolInputAvailableEvent(
                tool_call_id="call_1",
                family="shell",
                name="shell",
                arguments={"command": "pwd"},
            )
        )
        on_event(
            ToolOutputAvailableEvent(
                tool_call_id="call_1",
                result=ToolCallResult(
                    family="shell",
                    name="shell",
                    arguments={"command": "pwd"},
                    output={"ok": True, "stdout": "/tmp/alice"},
                    error=None,
                ),
            )
        )
        on_event(TextDeltaEvent(delta="done"))
        return ModelExecutionResult(output_text="done")

    monkeypatch.setattr(
        "toolang.runtime.invoke.execute_prompt_build_stream",
        fake_execute_stream,
    )

    app = create_agent_app(
        prepared,
        agents_db_path=agents_db_path(root),
        bus_db_path=bus_events_db_path(root),
        host="127.0.0.1",
        port=8765,
        sandbox="host",
    )

    with TestClient(app) as client:
        with client.stream(
            "POST",
            "/api/v1/chat/stream",
            json={"thread": "owner", "message": "stream tool"},
        ) as response:
            assert response.status_code == 200
            stream_text = "".join(
                chunk.decode("utf-8") for chunk in response.iter_raw()
            )

    assert '"type":"tool-input-start"' in stream_text
    assert '"type":"tool-input-delta"' in stream_text
    assert '"type":"tool-input-available"' in stream_text
    assert '"type":"tool-output-available"' in stream_text
    assert '"toolCallId":"call_1"' in stream_text
    assert '"inputTextDelta":"{\\"command\\":\\"pwd\\"}"' in stream_text
    assert '"toolName":"shell"' in stream_text
    assert '"input":{"command":"pwd"}' in stream_text
    assert '"providerMetadata":{"toolang":{"toolFamily":"shell"}}' in stream_text
    assert '"type":"text-delta"' in stream_text
    assert '"delta":"done"' in stream_text


def test_chat_stream_allows_tool_only_turns(tmp_path: Path, monkeypatch) -> None:
    root = resolve_toolang_root(tmp_path / "toolang-root")
    home = root / "agents" / "alice"
    home.mkdir(parents=True)
    (home / "alice.too").write_text(
        SOURCE_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8"
    )

    agent = resolve_agent_ref("alice", cwd=tmp_path, toolang_root=root)
    prepared = prepare_agent(agent)

    def fake_execute_stream(build, *, on_event) -> ModelExecutionResult:
        on_event(
            ToolInputStartEvent(
                tool_call_id="call_1",
            )
        )
        on_event(
            ToolInputAvailableEvent(
                tool_call_id="call_1",
                family="shell",
                name="shell",
                arguments={"command": "pwd"},
            )
        )
        on_event(
            ToolOutputAvailableEvent(
                tool_call_id="call_1",
                result=ToolCallResult(
                    family="shell",
                    name="shell",
                    arguments={"command": "pwd"},
                    output={"ok": True, "stdout": "/tmp/alice"},
                    error=None,
                ),
            )
        )
        return ModelExecutionResult(output_text="")

    monkeypatch.setattr(
        "toolang.runtime.invoke.execute_prompt_build_stream",
        fake_execute_stream,
    )

    app = create_agent_app(
        prepared,
        agents_db_path=agents_db_path(root),
        bus_db_path=bus_events_db_path(root),
        host="127.0.0.1",
        port=8765,
        sandbox="host",
    )

    with TestClient(app) as client:
        with client.stream(
            "POST",
            "/api/v1/chat/stream",
            json={"thread": "owner", "message": "tool only"},
        ) as response:
            assert response.status_code == 200
            stream_text = "".join(
                chunk.decode("utf-8") for chunk in response.iter_raw()
            )

    assert '"type":"tool-input-start"' in stream_text
    assert '"type":"tool-input-available"' in stream_text
    assert '"type":"tool-output-available"' in stream_text
    assert '"toolCallId":"call_1"' in stream_text
    assert '"toolName":"shell"' in stream_text
    assert '"type":"text-delta"' not in stream_text
    assert '"type":"finish"' in stream_text
    assert "data: [DONE]" in stream_text


def test_chat_turn_and_run_detail_include_ordered_assistant_parts(
    tmp_path: Path, monkeypatch
) -> None:
    root = resolve_toolang_root(tmp_path / "toolang-root")
    home = root / "agents" / "alice"
    home.mkdir(parents=True)
    (home / "alice.too").write_text(
        SOURCE_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8"
    )

    agent = resolve_agent_ref("alice", cwd=tmp_path, toolang_root=root)
    prepared = prepare_agent(agent)

    monkeypatch.setattr(
        "toolang.runtime.invoke.execute_prompt_build",
        lambda build: ModelExecutionResult(
            output_text="done",
            tool_calls=[
                ToolCallResult(
                    family="shell",
                    name="shell",
                    arguments={"command": "pwd"},
                    output={"ok": True, "stdout": "/tmp/alice"},
                    error=None,
                )
            ],
        ),
    )

    app = create_agent_app(
        prepared,
        agents_db_path=agents_db_path(root),
        bus_db_path=bus_events_db_path(root),
        host="127.0.0.1",
        port=8765,
        sandbox="host",
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/chat",
            json={"thread": "owner", "message": "tool me"},
        )
        assert response.status_code == 200
        run_id = response.json()["run_id"]

        thread = client.get("/api/v1/threads/owner")
        assert thread.status_code == 200
        assert thread.json()["messages"][1]["parts"] == [
            {
                "id": f"{run_id}:assistant:tool:1",
                "type": "tool",
                "tool_call_id": f"{run_id}:assistant:tool:1",
                "tool_name": "shell",
                "tool_family": "shell",
                "state": "output-available",
                "input": {"command": "pwd"},
                "output": {"ok": True, "stdout": "/tmp/alice"},
                "error_text": None,
                "provider_metadata": {
                    "toolang": {
                        "toolFamily": "shell",
                        "toolName": "shell",
                    }
                },
            },
            {
                "id": f"{run_id}:assistant:text:2",
                "type": "text",
                "text": "done",
                "state": "done",
            }
        ]

        detail = client.get(f"/api/v1/runs/{run_id}")
        assert detail.status_code == 200
        assert detail.json()["messages"][1]["parts"] == [
            {
                "id": f"{run_id}:assistant:tool:1",
                "type": "tool",
                "tool_call_id": f"{run_id}:assistant:tool:1",
                "tool_name": "shell",
                "tool_family": "shell",
                "state": "output-available",
                "input": {"command": "pwd"},
                "output": {"ok": True, "stdout": "/tmp/alice"},
                "error_text": None,
                "provider_metadata": {
                    "toolang": {
                        "toolFamily": "shell",
                        "toolName": "shell",
                    }
                },
            },
            {
                "id": f"{run_id}:assistant:text:2",
                "type": "text",
                "text": "done",
                "state": "done",
            }
        ]


def test_agent_events_only_return_current_incarnation(tmp_path: Path, monkeypatch) -> None:
    root = resolve_toolang_root(tmp_path / "toolang-root")
    home = root / "agents" / "alice"
    home.mkdir(parents=True)
    (home / "alice.too").write_text(
        SOURCE_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8"
    )

    agent = resolve_agent_ref("alice", cwd=tmp_path, toolang_root=root)
    prepared = prepare_agent(agent)
    events_path = bus_events_db_path(root)
    store = BusStore(events_path)
    store.append(
        AgentCreated(
            at="2026-03-20T10:00:00Z",
            agent_uri=agent.uri,
            agent_id=agent.id[:12],
            name=agent.name,
            kind=agent.kind,
            detail="agent created",
            agent_home=str(agent.home),
            source_file=agent.source.name,
        )
    )
    store.append(
        AgentRemoved(
            at="2026-03-20T10:05:00Z",
            agent_uri=agent.uri,
            agent_id=agent.id[:12],
            name=agent.name,
            kind=agent.kind,
            detail="agent removed",
            agent_home=str(agent.home),
            source_file=agent.source.name,
        )
    )
    store.append(
        AgentCreated(
            at="2026-03-20T10:10:00Z",
            agent_uri=agent.uri,
            agent_id=agent.id[:12],
            name=agent.name,
            kind=agent.kind,
            detail="agent created",
            agent_home=str(agent.home),
            source_file=agent.source.name,
        )
    )
    store.close()

    monkeypatch.setattr(
        "toolang.runtime.invoke.execute_prompt_build",
        lambda build: ModelExecutionResult(output_text="done"),
    )
    monkeypatch.setattr(
        "toolang.runtime.invoke.execute_prompt_build_stream",
        lambda build, *, on_event: ModelExecutionResult(output_text="done"),
    )

    app = create_agent_app(
        prepared,
        agents_db_path=agents_db_path(root),
        bus_db_path=events_path,
        host="127.0.0.1",
        port=8765,
        sandbox="host",
    )

    with TestClient(app) as client:
        events = client.get("/api/v1/events")
        assert events.status_code == 200
        assert [item["event_type"] for item in events.json()["items"]] == [
            "agent_created",
            "agent_started",
        ]


def test_runtime_endpoints_and_chat_fallback_to_started_snapshot_when_source_is_missing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = resolve_toolang_root(tmp_path / "toolang-root")
    home = root / "agents" / "alice"
    home.mkdir(parents=True)
    source_path = home / "alice.too"
    source_path.write_text(
        SOURCE_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8"
    )

    agent = resolve_agent_ref("alice", cwd=tmp_path, toolang_root=root)
    prepared = prepare_agent(agent)

    monkeypatch.setattr(
        "toolang.runtime.invoke.execute_prompt_build",
        lambda build: ModelExecutionResult(output_text="done"),
    )
    monkeypatch.setattr(
        "toolang.runtime.invoke.execute_prompt_build_stream",
        lambda build, *, on_event: ModelExecutionResult(output_text="done"),
    )

    app = create_agent_app(
        prepared,
        agents_db_path=agents_db_path(root),
        bus_db_path=bus_events_db_path(root),
        host="127.0.0.1",
        port=8765,
        sandbox="host",
    )

    with TestClient(app) as client:
        source_path.unlink()

        runtime = client.get("/api/v1/runtime")
        caps = client.get("/api/v1/caps")
        chat = client.post(
            "/api/v1/chat",
            json={"thread": "owner", "message": "hello"},
        )

    assert runtime.status_code == 200
    assert caps.status_code == 200
    assert chat.status_code == 200
    assert chat.json()["assistant"]["parts"][0]["text"] == "done"


def test_create_agent_app_mutates_authored_caps_through_runtime_api(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = resolve_toolang_root(tmp_path / "toolang-root")
    home = root / "agents" / "alice"
    home.mkdir(parents=True)
    (home / "alice.too").write_text(
        """
thunk review:
    Review the issue.
""".strip(),
        encoding="utf-8",
    )

    agent = resolve_agent_ref("alice", cwd=tmp_path, toolang_root=root)
    prepared = prepare_agent(agent)

    def fake_resolve(kind: str, ref: str):
        assert kind == "psyche"
        assert ref == "by3gus/reviewer"
        from toolang.concepts.caps import CapRef

        return CapRef(
            kind="psyche",
            name="reviewer",
            ref=ref,
            repo="by3gus/agent-psyches",
            path="psyches/reviewer.md",
            rev="rev-by3gus",
        )

    def fake_fetch(resolved):
        import shutil

        fetched_file = tmp_path / "fetched" / resolved.repo.replace("/", "__") / REMOTE_PSYCHE_FIXTURE.name
        fetched_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REMOTE_PSYCHE_FIXTURE, fetched_file)
        return fetched_file, [REMOTE_PSYCHE_FIXTURE.name]

    monkeypatch.setattr("toolang.caps.github.resolve_github_cap_ref", fake_resolve)
    monkeypatch.setattr("toolang.caps.github.fetch_github_artifact", fake_fetch)
    monkeypatch.setattr(
        "toolang.runtime.invoke.execute_prompt_build",
        lambda build: ModelExecutionResult(output_text="done"),
    )
    monkeypatch.setattr(
        "toolang.runtime.invoke.execute_prompt_build_stream",
        lambda build, *, on_event: ModelExecutionResult(output_text="done"),
    )

    app = create_agent_app(
        prepared,
        agents_db_path=agents_db_path(root),
        bus_db_path=bus_events_db_path(root),
        host="127.0.0.1",
        port=8765,
        sandbox="host",
    )

    local_path = home / ".toolang" / "psyches" / "reviewer-local.md"
    shared_source = home / "agents.too"
    local_psyche = (
        "---\n"
        "description: Shared reviewer guidance\n"
        "---\n\n"
        "Prefer direct and concrete language.\n"
    )

    with TestClient(app) as client:
        runtime = client.get("/api/v1/runtime")
        assert runtime.status_code == 200
        assert runtime.json()["security"]["self_modification"] == {
            "can_add_caps": True,
            "can_edit_will": True,
            "can_write_source": False,
            "can_persist_changes": True,
        }

        put_local = client.put(
            "/api/v1/psyches/reviewer-local",
            json={
                "scope": "shared",
                "content": local_psyche,
            },
        )
        assert put_local.status_code == 200
        assert put_local.json()["item"] == {
            "kind": "psyche",
            "name": "reviewer-local",
            "scope": "shared",
            "source": "local",
            "locator": str(local_path),
            "path": str(local_path),
            "ref": None,
        }
        assert local_path.exists()
        assert local_path.read_text(encoding="utf-8") == local_psyche

        put_remote = client.put(
            "/api/v1/psyches/reviewer",
            json={
                "scope": "shared",
                "ref": "by3gus/reviewer",
            },
        )
        assert put_remote.status_code == 200
        assert put_remote.json()["item"] == {
            "kind": "psyche",
            "name": "reviewer",
            "scope": "shared",
            "source": "remote",
            "locator": "by3gus/reviewer",
            "path": str(shared_source),
            "ref": "by3gus/reviewer",
        }
        assert shared_source.read_text(encoding="utf-8") == "use psyche by3gus/reviewer\n"

        caps = client.get("/api/v1/caps")
        assert caps.status_code == 200
        assert sorted(item["name"] for item in caps.json()["psyches"]) == [
            "reviewer",
            "reviewer-local",
        ]
        by_name = {item["name"]: item for item in caps.json()["psyches"]}
        assert by_name["reviewer"]["scope"] == "shared"
        assert by_name["reviewer"]["editable"] is True
        assert by_name["reviewer"]["ref"] == "by3gus/reviewer"
        assert by_name["reviewer-local"]["scope"] == "shared"
        assert by_name["reviewer-local"]["editable"] is True
        assert by_name["reviewer-local"]["path"] == "psyches/reviewer-local.md"
        assert by_name["reviewer-local"]["description"] == "Shared reviewer guidance"
        assert caps.json()["counts"]["psyches"] == 2

        psyche_list = client.get("/api/v1/psyches")
        assert psyche_list.status_code == 200
        assert sorted(item["name"] for item in psyche_list.json()["items"]) == [
            "reviewer",
            "reviewer-local",
        ]

        remote_psyche_detail = client.get("/api/v1/psyches/reviewer")
        assert remote_psyche_detail.status_code == 200
        assert remote_psyche_detail.json()["item"]["ref"] == "by3gus/reviewer"
        assert remote_psyche_detail.json()["item"]["scope"] == "shared"
        assert (
            remote_psyche_detail.json()["item"]["content"]
            == REMOTE_PSYCHE_FIXTURE.read_text(encoding="utf-8")
        )

        local_psyche_detail = client.get("/api/v1/psyches/reviewer-local")
        assert local_psyche_detail.status_code == 200
        assert local_psyche_detail.json()["item"]["scope"] == "shared"
        assert local_psyche_detail.json()["item"]["ref"] is None
        assert local_psyche_detail.json()["item"]["content"] == local_psyche

        delete_remote = client.delete(
            "/api/v1/psyches/reviewer",
            params={"scope": "shared", "source": "remote"},
        )
        assert delete_remote.status_code == 200
        assert delete_remote.json() == {"ok": True}
        assert not shared_source.exists()

        delete_local = client.delete(
            "/api/v1/psyches/reviewer-local",
            params={"scope": "shared", "source": "local"},
        )
        assert delete_local.status_code == 200
        assert delete_local.json() == {"ok": True}
        assert not local_path.exists()

        caps_after_delete = client.get("/api/v1/caps")
        assert caps_after_delete.status_code == 200
        assert caps_after_delete.json()["psyches"] == []


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
        assert runtime.json()["security"]["sandbox"]["image"] == "python:3.13-slim"
        assert runtime.json()["security"]["sandbox"]["network_mode"] == "bridge"
        assert runtime.json()["security"]["sandbox"]["bridge"] == "default"
        assert runtime.json()["security"]["sandbox"]["host_reachability"] is False
        assert len(runtime.json()["security"]["sandbox"]["volumes"]) == 2

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
        activations = execution.list_activations(agent_uri=agent.uri)
        runs = execution.list_runs(agent_uri=agent.uri)
        steps = execution.list_steps(run_id=runs[0].run_id)
        execution.close()

        assert activations[0].runtime_loops == ("server", "poll")
        assert runs[0].thread_id == "telegram:123"
        assert runs[0].origin == "chat"
        assert runs[0].status == "finished"
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
    builds: list[Any] = []

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
                        meta={
                            "ref": "linear:42",
                            "name": "Regression triage",
                            "status": "todo",
                            "requester": "service:linear",
                        },
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

    def fake_execute(build) -> str:
        builds.append(build)
        return f"tasked:{build.runtime_context['origin']}:{build.raw_input}:{build.model}"

    monkeypatch.setattr("toolang.runtime.invoke.execute_prompt_build", fake_execute)

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
                lambda: _run_origins(execution, agent.uri) >= {"task"},
                label="task poll run",
                timeout_sec=5.0,
            )
            runs = execution.list_runs(agent_uri=agent.uri)
        finally:
            execution.close()

    task_runs = [run for run in runs if run.origin == "task"]
    task_build = next(build for build in builds if build.runtime_context["origin"] == "task")
    assert len(task_runs) == 1
    assert task_runs[0].thread_id == "task:linear/42"
    assert task_runs[0].sender == "service"
    assert (
        task_runs[0].output_text
        == "tasked:task:Investigate the regression and report back.:gpt-5"
    )
    assert task_build.runtime_context["task"] == {
        "provider": "linear",
        "ref": "linear:42",
        "name": "Regression triage",
        "body": "Investigate the regression and report back.",
        "status": "todo",
        "requester": "service:linear",
        "thread_id": "task:linear/42",
        "path": None,
    }
    assert task_build.runtime_context["task_services"] == {
        "provider": "linear",
        "read": False,
        "write": False,
        "comment": False,
        "path": None,
    }
    assert "Task execution protocol:" in task_build.developer_message
    assert "Task provider: linear." in task_build.developer_message
    assert "Task read available: no." in task_build.developer_message


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
        rrule="FREQ=MINUTELY;INTERVAL=30",
    ).save(room.chores_dir / "sync.md")
    WillFile(
        title="Reflect",
        body="Think about the next milestone.",
        rrule="FREQ=HOURLY;INTERVAL=1",
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
            "mirrored": False,
            "provider": None,
            "remote_ref": None,
            "thread_id": f"task:local:{saved_task.id}",
            "path": str(room.tasks_dir / "roadmap.md"),
            "updated_at": tasks.json()["items"][0]["updated_at"],
            "paused": False,
        }
    ]
    assert chores.status_code == 200
    assert chores.json()["items"] == [
        {
            "id": "sync",
            "title": "Sync backlog",
            "rrule": "FREQ=MINUTELY;INTERVAL=30",
            "paused": False,
        }
    ]
    assert will.status_code == 200
    assert will.json()["item"] == {
        "id": "will",
        "title": "Reflect",
        "rrule": "FREQ=HOURLY;INTERVAL=1",
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
        assert created.json()["mirrored"] is False
        assert created.json()["provider"] is None
        assert created.json()["remote_ref"] is None
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
                "rrule": "FREQ=MINUTELY;INTERVAL=30",
            },
        )
        assert created_chore.status_code == 200
        assert created_chore.json()["id"] == "maintenance/sync"
        assert created_chore.json()["rrule"] == "FREQ=MINUTELY;INTERVAL=30"

        patched_chore = client.patch(
            "/api/v1/chores/maintenance/sync",
            json={
                "rrule": "FREQ=MINUTELY;INTERVAL=15",
                "body_append": "Also refresh labels.",
                "paused": True,
            },
        )
        assert patched_chore.status_code == 200
        assert patched_chore.json()["rrule"] == "FREQ=MINUTELY;INTERVAL=15"
        assert patched_chore.json()["paused"] is True

        created_will = client.put(
            "/api/v1/will",
            json={
                "title": "Stay aligned",
                "body": "Review the current milestone and choose the next move.",
                "rrule": "FREQ=HOURLY;INTERVAL=1",
            },
        )
        assert created_will.status_code == 200
        assert created_will.json()["item"]["id"] == "will"
        assert created_will.json()["item"]["rrule"] == "FREQ=HOURLY;INTERVAL=1"

        patched_will = client.patch(
            "/api/v1/will",
            json={
                "rrule": "FREQ=HOURLY;INTERVAL=2",
                "body_append": "Prefer quieter work in the afternoon.",
                "paused": True,
            },
        )
        assert patched_will.status_code == 200
        assert patched_will.json()["item"]["rrule"] == "FREQ=HOURLY;INTERVAL=2"
        assert patched_will.json()["item"]["paused"] is True

        chores = client.get("/api/v1/chores")
        assert chores.status_code == 200
        assert chores.json()["items"][0]["id"] == "maintenance/sync"
        assert chores.json()["items"][0]["rrule"] == "FREQ=MINUTELY;INTERVAL=15"
        assert chores.json()["items"][0]["paused"] is True

        will = client.get("/api/v1/will")
        assert will.status_code == 200
        assert will.json()["item"]["rrule"] == "FREQ=HOURLY;INTERVAL=2"
        assert will.json()["item"]["paused"] is True

    saved_chore = ChoreFile.load(room.chores_dir / "maintenance" / "sync.md")
    assert saved_chore.title == "Sync maintenance"
    assert saved_chore.rrule == "FREQ=MINUTELY;INTERVAL=15"
    assert saved_chore.paused is True
    assert "Also refresh labels." in saved_chore.body

    saved_will = WillFile.load(room.will_path)
    assert saved_will.title == "Stay aligned"
    assert saved_will.rrule == "FREQ=HOURLY;INTERVAL=2"
    assert saved_will.paused is True
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
    assert body["security"] == {
        "sandbox": {
            "image": None,
            "volumes": [],
            "network_mode": "host",
            "bridge": None,
            "dns": [],
            "host_reachability": True,
        },
        "tools": {
            "filesystem": True,
            "shell": True,
            "browser_use": False,
            "computer_use": False,
            "service_use": True,
            "web_search": True,
            "mem_search": False,
            "file_search": False,
        },
        "autonomy": {
            "chores_enabled": True,
            "tasks_enabled": True,
            "will_enabled": True,
            "will_path_exists": False,
        },
        "self_modification": {
            "can_add_caps": True,
            "can_edit_will": True,
            "can_write_source": False,
            "can_persist_changes": True,
        },
    }
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


def _run_origins(execution: ExecutionStore, agent_uri: str) -> set[str]:
    runs = execution.list_runs(agent_uri=agent_uri)
    if not runs:
        return set()
    return {run.origin for run in runs}
