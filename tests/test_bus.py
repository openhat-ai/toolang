from __future__ import annotations

from toolang.bus.db import BusStore
from toolang.bus.events import (
    AgentChanged,
    AgentCreated,
    AgentRemoved,
    AgentStarted,
    AgentStopped,
    RunFinished,
    RunStarted,
)


def test_bus_store_projects_agent_and_run_events(tmp_path) -> None:
    store = BusStore(tmp_path / "events.db")
    agent_uri = "agent://alice/alice.too"
    agent_id = "b334d0bc00b1"

    created = store.append(
        AgentCreated(
            at="2026-03-19T09:59:00Z",
            agent_uri=agent_uri,
            agent_id=agent_id,
            name="alice",
            kind="resident",
            detail="agent created",
            agent_home="/tmp/toolang-root/agents/alice",
            source_file="alice.too",
        )
    )
    store.append(
        AgentStarted(
            at="2026-03-19T10:00:00Z",
            agent_uri=agent_uri,
            agent_id=agent_id,
            name="alice",
            kind="resident",
            sandbox="host",
            endpoint="http://127.0.0.1:8765",
            agent_home="/tmp/toolang-root/agents/alice",
            source_file="alice.too",
        )
    )
    store.append(
        AgentChanged(
            at="2026-03-19T10:01:00Z",
            agent_uri=agent_uri,
            agent_id=agent_id,
            name="alice",
            change_type="caps_updated",
            detail="sync completed",
            agent_home="/tmp/toolang-root/agents/alice",
            source_file="alice.too",
        )
    )
    store.append(
        RunStarted(
            at="2026-03-19T10:02:00Z",
            agent_uri=agent_uri,
            agent_id=agent_id,
            run_id="run-1",
            run_type="run",
            origin="invoke",
            summary="alice:summarize",
            thunk_name="summarize",
        )
    )
    store.append(
        RunFinished(
            at="2026-03-19T10:02:01Z",
            agent_uri=agent_uri,
            agent_id=agent_id,
            run_id="run-1",
            run_type="run",
            origin="invoke",
            summary="alice:summarize",
            thunk_name="summarize",
        )
    )
    store.append(
        AgentStopped(
            at="2026-03-19T10:03:00Z",
            agent_uri=agent_uri,
            agent_id=agent_id,
            name="alice",
            sandbox="host",
            detail="run stopped",
            endpoint="http://127.0.0.1:8765",
            agent_home="/tmp/toolang-root/agents/alice",
            source_file="alice.too",
        )
    )

    agent = store.get_agent(agent_uri)
    runs = store.list_runs(agent_uri=agent_uri)
    events = store.list_events(agent_uri=agent_uri)
    store.close()

    assert agent is not None
    assert agent.status == "stopped"
    assert agent.kind == "resident"
    assert agent.sandbox == "host"
    assert agent.detail == "run stopped"
    assert agent.created_event_id == created.event_id
    assert [run.run_id for run in runs] == ["run-1"]
    assert runs[0].status == "finished"
    assert runs[0].origin == "invoke"
    assert [event.event_type for event in events] == [
        "agent_created",
        "agent_started",
        "caps_updated",
        "run_started",
        "run_finished",
        "agent_stopped",
    ]


def test_bus_store_tracks_latest_agent_incarnation(tmp_path) -> None:
    store = BusStore(tmp_path / "events.db")
    agent_uri = "agent://alice/alice.too"
    agent_id = "b334d0bc00b1"

    store.append(
        AgentCreated(
            at="2026-03-19T09:59:00Z",
            agent_uri=agent_uri,
            agent_id=agent_id,
            name="alice",
            kind="resident",
            detail="agent created",
        )
    )
    store.append(
        AgentRemoved(
            at="2026-03-19T10:00:00Z",
            agent_uri=agent_uri,
            agent_id=agent_id,
            name="alice",
            kind="resident",
            detail="agent removed",
        )
    )
    recreated = store.append(
        AgentCreated(
            at="2026-03-19T10:05:00Z",
            agent_uri=agent_uri,
            agent_id=agent_id,
            name="alice",
            kind="resident",
            detail="agent created",
        )
    )
    store.append(
        AgentStarted(
            at="2026-03-19T10:06:00Z",
            agent_uri=agent_uri,
            agent_id=agent_id,
            name="alice",
            kind="resident",
            sandbox="host",
            endpoint="http://127.0.0.1:8765",
            agent_home="/tmp/toolang-root/agents/alice",
            source_file="alice.too",
        )
    )

    agent = store.get_agent(agent_uri)
    events = store.list_events(agent_uri=agent_uri)
    store.close()

    assert agent is not None
    assert agent.status == "started"
    assert agent.created_event_id == recreated.event_id
    assert [event.event_type for event in events] == [
        "agent_created",
        "agent_removed",
        "agent_created",
        "agent_started",
    ]
