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
from toolang.agent.refs import resolve_agent_ref
from toolang.agent.registry import get_running_agent
from toolang.bus.db import BusStore
from toolang.files.agent_run import ActivationState
from toolang.files.prompt_trace import PromptTrace
from toolang.layout import (
    agent_run_path,
    agent_run_prompt_path,
    agents_db_path,
    bus_events_db_path,
    resolve_toolang_root,
)
from toolang.runtime.server import create_agent_app

SOURCE_FIXTURE = Path(__file__).parent / "fixtures" / "source_only.too"


def test_create_agent_app_serves_webui_compatible_endpoints(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = resolve_toolang_root(tmp_path / "toolang-root")
    home = root / "agents" / "alice"
    home.mkdir(parents=True)
    (home / "alice.too").write_text(SOURCE_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")

    agent = resolve_agent_ref("alice", cwd=tmp_path, toolang_root=root)
    prepared = prepare_agent(agent)
    db_path = agents_db_path(root)
    events_path = bus_events_db_path(root)
    run_path = agent_run_path(home, "alice")

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
        assert cors_runtime.headers["access-control-allow-origin"] == "http://localhost:3000"

        cors_runtime_too_run = client.get(
            "/api/v1/runtime",
            headers={"Origin": "https://too.run"},
        )
        assert cors_runtime_too_run.headers["access-control-allow-origin"] == "https://too.run"

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

        active = get_running_agent(db_path, agent.agent_uri)
        assert active is not None
        assert active.status == "running"
        assert active.sandbox == "host"
        assert run_path.exists()
        assert ActivationState.load(run_path).status == "running"
        assert ActivationState.load(run_path).sandbox.spec() == "host"

        first_chat = client.post(
            "/api/v1/chat",
            json={"thread": "owner", "message": "hello"},
        )
        assert first_chat.status_code == 200
        first_chat_body = first_chat.json()
        assert first_chat_body["thread_id"] == "owner"
        assert first_chat_body["message"]["parts"][0]["text"] == "chat:1:hello:gpt-5"
        first_run_id = first_chat_body["turn_id"]
        first_trace = PromptTrace.load(agent_run_prompt_path(home, "alice", first_run_id))
        assert first_trace.message_context is not None
        assert first_trace.message_context["channel"] == "api"
        assert first_trace.sandbox == "host"
        assert first_trace.runtime_context["visible_caps"]["psyches"][0]["name"] == "reviewer"

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
            stream_text = "".join(chunk.decode("utf-8") for chunk in response.iter_raw())
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

    assert get_running_agent(db_path, agent.agent_uri) is None
    assert ActivationState.load(run_path).status == "stopped"
    store = BusStore(events_path)
    events = store.list_events(agent_uri=agent.agent_uri)
    store.close()
    assert events[-1].event_type == "agent_stopped"


def test_create_agent_app_reports_docker_sandbox_state(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = resolve_toolang_root(tmp_path / "toolang-root")
    home = root / "agents" / "alice"
    home.mkdir(parents=True)
    (home / "alice.too").write_text(SOURCE_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")

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

        run_state = ActivationState.load(run_path)
        assert run_state.sandbox.type == "docker"
        assert run_state.sandbox.image_name == "python:3.13-slim"
        assert run_state.sandbox.container_name is not None


def test_serve_process_writes_stopped_state_after_termination(tmp_path: Path) -> None:
    root = resolve_toolang_root(tmp_path / "toolang-root")
    home = root / "agents" / "alice"
    home.mkdir(parents=True)
    (home / "alice.too").write_text(SOURCE_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")

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
            "serve",
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
        assert ActivationState.load(run_path).status == "running"

        process.terminate()
        process.wait(timeout=5)
        _wait_for_stopped_state(run_path)

        assert get_running_agent(db_path, "agent://alice/alice.too") is None
        assert ActivationState.load(run_path).status == "stopped"
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
        if run_path.exists() and ActivationState.load(run_path).status == "stopped":
            return
        time.sleep(0.05)
    raise AssertionError(f"Timed out waiting for stopped state at {run_path}")
