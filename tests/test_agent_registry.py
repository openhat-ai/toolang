from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from toolang.agent_refs import resolve_agent_ref
from toolang.agent_registry import (
    KnownAgentRecord,
    RunningAgentRecord,
    delete_running_agent,
    find_known_agents_by_id_prefix,
    find_known_agents_by_name,
    get_running_agent,
    list_known_agents,
    list_running_agents,
    upsert_known_agent,
    upsert_running_agent,
)
from toolang.layout import agents_db_path, resolve_toolang_root

SOURCE_FIXTURE = Path(__file__).parent / "fixtures" / "source_only.too"


def test_known_agent_registry_supports_name_and_id_lookup(tmp_path: Path) -> None:
    root = resolve_toolang_root(tmp_path / "toolang-root")
    home = root / "agents" / "alice"
    home.mkdir(parents=True)
    (home / "reviewer.too").write_text(SOURCE_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")

    agent = resolve_agent_ref("alice/reviewer", cwd=tmp_path, toolang_root=root)
    record = KnownAgentRecord.from_resolved_agent(
        agent,
        updated_at=datetime(2026, 3, 19, 8, 0, 0, tzinfo=timezone.utc),
    )
    db_path = agents_db_path(root)

    upsert_known_agent(db_path, record)

    assert find_known_agents_by_name(db_path, "reviewer") == [record]
    assert find_known_agents_by_id_prefix(db_path, agent.agent_id[:8]) == [record]


def test_running_agent_registry_tracks_active_served_agents(tmp_path: Path) -> None:
    root = resolve_toolang_root(tmp_path / "toolang-root")
    home = root / "agents" / "alice"
    home.mkdir(parents=True)
    (home / "reviewer.too").write_text(SOURCE_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")

    agent = resolve_agent_ref("alice/reviewer", cwd=tmp_path, toolang_root=root)
    db_path = agents_db_path(root)
    updated_at = datetime(2026, 3, 19, 8, 0, 0, tzinfo=timezone.utc)
    started_at = datetime(2026, 3, 19, 8, 5, 0, tzinfo=timezone.utc)
    heartbeat_at = datetime(2026, 3, 19, 8, 6, 0, tzinfo=timezone.utc)

    upsert_known_agent(db_path, KnownAgentRecord.from_resolved_agent(agent, updated_at=updated_at))
    upsert_running_agent(
        db_path,
        RunningAgentRecord(
            agent_uri=agent.agent_uri,
            pid=12345,
            status="running",
            endpoint="http://127.0.0.1:8765",
            started_at=started_at,
            heartbeat_at=heartbeat_at,
        ),
    )

    running = get_running_agent(db_path, agent.agent_uri)
    snapshots = list_running_agents(db_path)

    assert running is not None
    assert running.pid == 12345
    assert len(snapshots) == 1
    assert snapshots[0].agent_name == "reviewer"
    assert snapshots[0].agent_id == agent.agent_id[:12]

    delete_running_agent(db_path, agent.agent_uri)

    assert get_running_agent(db_path, agent.agent_uri) is None


def test_known_agent_registry_lists_known_agents_with_optional_running_state(tmp_path: Path) -> None:
    root = resolve_toolang_root(tmp_path / "toolang-root")
    home = root / "agents" / "alice"
    home.mkdir(parents=True)
    (home / "reviewer.too").write_text(SOURCE_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")

    agent = resolve_agent_ref("alice/reviewer", cwd=tmp_path, toolang_root=root)
    db_path = agents_db_path(root)
    updated_at = datetime(2026, 3, 19, 8, 0, 0, tzinfo=timezone.utc)
    upsert_known_agent(db_path, KnownAgentRecord.from_resolved_agent(agent, updated_at=updated_at))

    snapshots = list_known_agents(db_path)

    assert len(snapshots) == 1
    assert snapshots[0].agent_name == "reviewer"
    assert snapshots[0].running_status is None

    upsert_running_agent(
        db_path,
        RunningAgentRecord(
            agent_uri=agent.agent_uri,
            pid=12345,
            status="running",
            endpoint="http://127.0.0.1:8765",
            started_at=datetime(2026, 3, 19, 8, 5, 0, tzinfo=timezone.utc),
            heartbeat_at=datetime(2026, 3, 19, 8, 6, 0, tzinfo=timezone.utc),
        ),
    )

    snapshots = list_known_agents(db_path)

    assert len(snapshots) == 1
    assert snapshots[0].running_status == "running"
    assert snapshots[0].endpoint == "http://127.0.0.1:8765"
