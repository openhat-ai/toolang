from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from toolang.agent_refs import resolve_agent_ref
from toolang.bus.db import BusStore
from toolang.agent_registry import get_running_agent
from toolang.files.agent_run import AgentRunState
from toolang.layout import agent_run_path, agents_db_path, bus_events_db_path, resolve_toolang_root
from toolang.prepared import prepare_agent
from toolang.server import create_agent_app

SOURCE_FIXTURE = Path(__file__).parent / "fixtures" / "source_only.too"


def test_create_agent_app_registers_running_agent_and_serves_requests(
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
        "toolang.invoke.execute_thunk",
        lambda program, thunk, program_path, *, user_input, model=None: (
            f"ran:{thunk.name}:{user_input}:{model}"
        ),
    )

    app = create_agent_app(
        prepared,
        agents_db_path=db_path,
        bus_db_path=events_path,
        host="127.0.0.1",
        port=8765,
    )

    with TestClient(app) as client:
        health = client.get("/api/v1/health")
        assert health.status_code == 200
        assert health.json()["agent_uri"] == agent.agent_uri
        assert health.json()["agent_id"] == agent.agent_id[:12]

        info = client.get("/api/v1/agent")
        assert info.status_code == 200
        assert info.json()["status"] == "started"
        assert info.json()["kind"] == "resident"

        caps = client.get("/api/v1/caps")
        assert caps.status_code == 200
        assert [item["name"] for item in caps.json()["services"]] == ["github"]
        assert [item["name"] for item in caps.json()["prompts"]] == ["summarize"]
        assert [item["name"] for item in caps.json()["psyches"]] == ["reviewer"]

        active = get_running_agent(db_path, agent.agent_uri)
        assert active is not None
        assert active.status == "running"
        assert run_path.exists()
        assert AgentRunState.load(run_path).status == "running"

        run_response = client.post(
            "/api/v1/runs",
            json={"thunk": "summarize", "input": "hello", "model": "gpt-5.3"},
        )
        assert run_response.status_code == 200
        body = run_response.json()
        assert body["output"] == "ran:summarize:hello:gpt-5.3"
        assert len(body["run_id"]) == 32

        runs = client.get("/api/v1/runs")
        assert runs.status_code == 200
        assert runs.json()[0]["status"] == "finished"
        assert runs.json()[0]["origin"] == "invoke"

        events = client.get("/api/v1/events")
        assert events.status_code == 200
        assert [item["event_type"] for item in events.json()] == [
            "agent_started",
            "run_started",
            "run_finished",
        ]

    assert get_running_agent(db_path, agent.agent_uri) is None
    assert AgentRunState.load(run_path).status == "stopped"
    store = BusStore(events_path)
    events = store.list_events(agent_uri=agent.agent_uri)
    store.close()
    assert [event.event_type for event in events] == [
        "agent_started",
        "run_started",
        "run_finished",
        "agent_stopped",
    ]


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
        assert AgentRunState.load(run_path).status == "running"

        process.terminate()
        process.wait(timeout=5)
        _wait_for_stopped_state(run_path)

        assert get_running_agent(db_path, "agent://alice/alice.too") is None
        assert AgentRunState.load(run_path).status == "stopped"
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
        if run_path.exists() and AgentRunState.load(run_path).status == "stopped":
            return
        time.sleep(0.05)
    raise AssertionError(f"Timed out waiting for stopped state at {run_path}")
