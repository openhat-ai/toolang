from __future__ import annotations

from pathlib import Path

import pytest

from toolang import work
from toolang.base.types.tool import ToolContext
from toolang.tools.jobs import create_tool as create_jobs_tool


def _tool_context(toolang_root: Path, agent_name: str = "alice") -> ToolContext:
    home = toolang_root / "agents" / agent_name
    home.mkdir(parents=True, exist_ok=True)
    return ToolContext(
        run_id="run-1",
        home=home,
        room=home / ".runtime" / "tools" / "jobs",
        wd=home,
    )


def test_jobs_tool_creates_lists_gets_and_updates_tasks(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    context = _tool_context(toolang_root)
    tools = create_jobs_tool({}).tools()

    created = tools["task_create"].invoke(
        {
            "title": "Review plan",
            "body": "Review the implementation plan.",
        },
        context,
    )
    task_id = created["task"]["id"]

    listed = tools["task_list"].invoke({}, context)
    loaded = tools["task_get"].invoke({"task_id": task_id}, context)
    updated = tools["task_update"].invoke(
        {
            "task_id": task_id,
            "body": "Review the merged implementation.",
            "state": "inactive",
            "stage": "running",
        },
        context,
    )

    assert listed["tasks"][0]["id"] == task_id
    assert loaded["task"]["title"] == "Review plan"
    assert updated["task"]["state"] == "inactive"
    assert updated["task"]["stage"] == "running"
    assert updated["task"]["body"] == "Review the merged implementation."
    task = work.find_task(toolang_root, "alice", task_id)
    assert task is not None
    assert task.document.state == "inactive"
    assert task.document.stage == "running"


def test_jobs_tool_creates_and_updates_chores(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    context = _tool_context(toolang_root)
    tools = create_jobs_tool({}).tools()

    created = tools["chore_create"].invoke(
        {
            "title": "Check stale PRs",
            "body": "Report stale pull requests.",
            "schedule": "FREQ=HOURLY;INTERVAL=6",
        },
        context,
    )
    chore_id = created["chore"]["id"]

    updated = tools["chore_update"].invoke(
        {
            "chore_id": chore_id,
            "schedule": "FREQ=DAILY;INTERVAL=1",
            "body": "Report stale pull requests and blockers.",
        },
        context,
    )
    listed = tools["chore_list"].invoke({}, context)

    assert listed["chores"][0]["id"] == chore_id
    assert updated["chore"]["schedule"] == "FREQ=DAILY;INTERVAL=1"
    assert updated["chore"]["body"] == "Report stale pull requests and blockers."
    chore = work.find_chore(toolang_root, "alice", chore_id)
    assert chore is not None
    assert chore.document.schedule == "FREQ=DAILY;INTERVAL=1"


def test_jobs_tool_rejects_non_agent_home(tmp_path: Path) -> None:
    context = ToolContext(
        run_id="run-1",
        home=tmp_path / "alice",
        room=tmp_path / "alice" / ".runtime" / "tools" / "jobs",
        wd=tmp_path / "alice",
    )
    tool = create_jobs_tool({}).tools()["task_list"]

    with pytest.raises(Exception, match="requires an agent home"):
        tool.invoke({}, context)
