from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
import httpx

from toolang.bus.app import create_bus_app
from toolang.bus.db import BusStore
from toolang.bus.events import AgentStarted, RunFinished, RunStarted


def test_create_bus_app_serves_agents_runs_events_and_proxy_chat(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "bus" / "events.db"
    store = BusStore(db_path)
    agent_uri = "agent://alice/alice.too"
    agent_id = "b334d0bc00b1"
    store.append(
        AgentStarted(
            at="2026-03-19T10:00:00Z",
            agent_uri=agent_uri,
            agent_id=agent_id,
            name="alice",
            kind="resident",
            endpoint="http://127.0.0.1:8765",
            agent_home="/tmp/toolang-root/agents/alice",
            source_file="alice.too",
        )
    )
    store.append(
        RunStarted(
            at="2026-03-19T10:01:00Z",
            agent_uri=agent_uri,
            agent_id=agent_id,
            run_id="run-1",
            run_type="turn",
            origin="chat",
            summary="alice:chat",
            thunk_name="chat",
            thread_id="owner",
        )
    )
    store.append(
        RunFinished(
            at="2026-03-19T10:01:02Z",
            agent_uri=agent_uri,
            agent_id=agent_id,
            run_id="run-1",
            run_type="turn",
            origin="chat",
            summary="alice:chat",
            thunk_name="chat",
            thread_id="owner",
        )
    )
    store.close()

    class DummyAsyncClient:
        async def __aenter__(self) -> "DummyAsyncClient":
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def post(self, url: str, json: dict[str, object]) -> httpx.Response:
            request = httpx.Request("POST", url, json=json)
            return httpx.Response(
                200,
                json={
                    "thread_id": "owner",
                    "turn_id": "run-1",
                    "message": {
                        "id": 2,
                        "thread_id": "owner",
                        "turn_id": "run-1",
                        "seq": 2,
                        "role": "assistant",
                        "parts": [{"type": "text", "text": "hello"}],
                        "created_at": "2026-03-19T10:01:02Z",
                        "meta": {},
                    },
                    "assistant": {
                        "id": 2,
                        "thread_id": "owner",
                        "turn_id": "run-1",
                        "seq": 2,
                        "role": "assistant",
                        "parts": [{"type": "text", "text": "hello"}],
                        "created_at": "2026-03-19T10:01:02Z",
                        "meta": {},
                    },
                },
                request=request,
            )

    monkeypatch.setattr("toolang.bus.app.httpx.AsyncClient", DummyAsyncClient)

    app = create_bus_app(db_path)
    with TestClient(app) as client:
        health = client.get("/healthz")
        assert health.status_code == 200
        assert health.json() == {"ok": True}

        agents = client.get("/api/v1/agents")
        assert agents.status_code == 200
        assert agents.json()["items"][0]["id"] == agent_id
        assert agents.json()["items"][0]["port"] == 8765

        agent = client.get(f"/api/v1/agents/{agent_id}")
        assert agent.status_code == 200
        assert agent.json()["runtime_ref"] == agent_uri

        runs = client.get("/api/v1/runs")
        assert runs.status_code == 200
        assert runs.json()["items"][0]["id"] == "run-1"
        assert runs.json()["items"][0]["origin_kind"] == "direct"

        events = client.get(f"/api/v1/agents/{agent_id}/events")
        assert events.status_code == 200
        assert [item["event_type"] for item in events.json()["items"]] == [
            "agent_started",
            "run_started",
            "run_finished",
        ]

        proxied = client.post(
            f"/api/v1/agents/{agent_id}/chat",
            json={"thread": "owner", "message": "hello"},
        )
        assert proxied.status_code == 200
        assert proxied.json()["assistant"]["parts"][0]["text"] == "hello"
