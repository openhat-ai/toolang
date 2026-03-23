from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from toolang.agent.prepared import prepare_agent
from toolang.agent.resolve import resolve_agent_ref
from toolang.concepts.layout import AgentHome, ToolangRoot
from toolang.concepts.persisted import ChoreFile, PulseState, TaskFile, WillFile
from toolang.runtime.execution_store import ExecutionStore
from toolang.runtime.pulse import collect_pulse_submissions
from toolang.runtime.server import create_agent_app


def resolve_toolang_root(root: Path) -> Path:
    return ToolangRoot.resolve(root).path


def agent_execution_db_path(agent_home: Path, agent_name: str) -> Path:
    return AgentHome.resolve(agent_home).room(agent_name).execution_db_path


def pulse_state_path(agent_home: Path, agent_name: str) -> Path:
    return AgentHome.resolve(agent_home).room(agent_name).pulse_state_path


SOURCE_FIXTURE = Path(__file__).parent / "fixtures" / "source_only.too"


def test_collect_pulse_submissions_detects_task_chore_and_will(tmp_path: Path) -> None:
    root = resolve_toolang_root(tmp_path / "toolang-root")
    home = root / "agents" / "alice"
    home.mkdir(parents=True)
    source_path = home / "alice.too"
    source_path.write_text(SOURCE_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")

    agent = resolve_agent_ref("alice", cwd=tmp_path, toolang_root=root)
    room = AgentHome.resolve(home).room("alice")
    (room.tasks_dir / "review.md").parent.mkdir(parents=True, exist_ok=True)
    (room.tasks_dir / "review.md").write_text(
        "---\nstatus: todo\nrequester: owner\n---\nLook at the latest changes.\n",
        encoding="utf-8",
    )
    ChoreFile(title="Refresh tasks", body="Refresh local tasks.", interval_sec=3600).save(
        room.chores_dir / "refresh.md"
    )
    WillFile(title="Reflect", body="Think about the next step.", interval_sec=3600).save(room.will_path)

    state, submissions = collect_pulse_submissions(room, agent, PulseState())
    task = TaskFile.load(room.tasks_dir / "review.md", persist_id=True)
    task_id = task.task_id()

    assert state.tasks[task_id].content_hash is not None
    assert state.chores["refresh"].next_due_at is not None
    assert state.will.next_due_at is not None
    assert [(item.kind, item.thread_id) for item in submissions] == [
        ("task", f"task:local:{task_id}"),
        ("chore", "chore:refresh"),
        ("will", f"will:{agent.id}"),
    ]

    next_state, next_submissions = collect_pulse_submissions(room, agent, state)

    assert next_state == state
    assert next_submissions == []


def test_create_agent_app_processes_pulse_work_files(tmp_path: Path, monkeypatch) -> None:
    root = resolve_toolang_root(tmp_path / "toolang-root")
    home = root / "agents" / "alice"
    home.mkdir(parents=True)
    source_path = home / "alice.too"
    source_path.write_text(SOURCE_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")

    agent = resolve_agent_ref("alice", cwd=tmp_path, toolang_root=root)
    room = AgentHome.resolve(home).room("alice")
    (room.tasks_dir / "review.md").parent.mkdir(parents=True, exist_ok=True)
    (room.tasks_dir / "review.md").write_text(
        "---\nstatus: todo\nrequester: owner\n---\nLook at the latest changes.\n",
        encoding="utf-8",
    )
    ChoreFile(title="Refresh tasks", body="Refresh local tasks.", interval_sec=3600).save(
        room.chores_dir / "refresh.md"
    )
    WillFile(title="Reflect", body="Think about the next step.", interval_sec=3600).save(room.will_path)

    builds: list[Any] = []

    def fake_execute(build) -> str:
        builds.append(build)
        return f"{build.runtime_context['origin']}:{build.raw_input}:{build.model}"

    monkeypatch.setattr("toolang.runtime.invoke.execute_prompt_build", fake_execute)

    prepared = prepare_agent(agent)
    app = create_agent_app(
        prepared,
        agents_db_path=ToolangRoot.resolve(root).agents_db_path,
        bus_db_path=ToolangRoot.resolve(root).bus_events_db_path,
        host="127.0.0.1",
        port=8765,
        sandbox="host",
        runtime_loops=("server", "pulse"),
    )

    with TestClient(app):
        execution = ExecutionStore(agent_execution_db_path(home, "alice"))
        try:
            _wait_for(
                lambda: _turn_origins(execution, agent.uri) >= {"task", "chore", "will"},
                timeout_sec=5.0,
            )
            runs = execution.list_runs(agent_uri=agent.uri)
            turns = execution.list_turns(run_id=runs[0].run_id)
        finally:
            execution.close()

    assert {turn.origin for turn in turns} >= {"task", "chore", "will"}
    assert {turn.sender for turn in turns if turn.origin in {"task", "chore", "will"}} == {"self"}
    assert pulse_state_path(home, "alice").exists()
    pulse_state = PulseState.load(pulse_state_path(home, "alice"))
    task = TaskFile.load(room.tasks_dir / "review.md", persist_id=True)
    task_id = task.task_id()
    task_build = next(
        build for build in builds if build.runtime_context["origin"] == "task"
    )
    assert pulse_state.tasks[task_id].last_started_at is not None
    assert pulse_state.tasks[task_id].last_finished_at is not None
    assert pulse_state.tasks[task_id].last_status == "finished"
    assert pulse_state.tasks[task_id].last_run_id is not None
    assert pulse_state.chores["refresh"].last_status == "finished"
    assert pulse_state.will.last_status == "finished"
    assert task_build.runtime_context["task"] == {
        "provider": "local",
        "ref": f"task:local:{task_id}",
        "name": "review",
        "body": task.body,
        "status": "todo",
        "requester": "owner",
        "thread_id": f"task:local:{task_id}",
        "path": str(room.tasks_dir / "review.md"),
    }
    assert task_build.runtime_context["task_services"] == {
        "provider": "local",
        "read": True,
        "write": True,
        "comment": True,
        "path": str(room.tasks_dir / "review.md"),
    }
    assert "Task execution protocol:" in task_build.developer_message
    assert "Update the task file directly at:" in task_build.developer_message


def _turn_origins(execution: ExecutionStore, agent_uri: str) -> set[str]:
    runs = execution.list_runs(agent_uri=agent_uri)
    if not runs:
        return set()
    turns = execution.list_turns(run_id=runs[0].run_id)
    return {turn.origin for turn in turns}


def _wait_for(predicate, *, timeout_sec: float) -> None:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("condition not met before timeout")
