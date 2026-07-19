from __future__ import annotations

from pathlib import Path

import pytest

from toolang.catalog import cap as caps
from toolang.catalog.job import AuthoredJobs
from toolang.base.types.tool import ToolContext
from toolang.execution.tools.agent_state import create_tool_set as create_agent_state_tool


def _tool_context(toolang_root: Path, agent_name: str = "alice") -> ToolContext:
    home = toolang_root / "agents" / agent_name
    home.mkdir(parents=True, exist_ok=True)
    return ToolContext(
        run_id="run-1",
        home=home,
        room=home / ".runtime" / "tools" / "agent_state",
        wd=home,
    )


def test_agent_state_tool_creates_lists_gets_and_updates_tasks(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    context = _tool_context(toolang_root)
    tools = create_agent_state_tool({}).tools()

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
        },
        context,
    )

    assert listed["tasks"][0]["id"] == task_id
    assert loaded["task"]["title"] == "Review plan"
    assert updated["task"]["stage"] == "ready"
    assert updated["task"]["body"] == "Review the merged implementation."
    task = AuthoredJobs(toolang_root / "agents" / "alice").get("task", task_id)
    assert task is not None
    assert task.stage == "ready"


def test_agent_state_tool_creates_and_updates_chores(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    context = _tool_context(toolang_root)
    tools = create_agent_state_tool({}).tools()

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
    chore = AuthoredJobs(toolang_root / "agents" / "alice").get("chore", chore_id)
    assert chore is not None
    assert chore.schedule == "FREQ=DAILY;INTERVAL=1"


def test_agent_state_tool_creates_updates_gets_and_deletes_skill(
    tmp_path: Path,
) -> None:
    toolang_root = tmp_path / "toolang"
    context = _tool_context(toolang_root)
    tools = create_agent_state_tool({}).tools()

    created = tools["skill_create"].invoke(
        {
            "name": "reviewer",
            "description": "Review code changes.",
            "body": "Check correctness and tests.",
        },
        context,
    )
    updated = tools["skill_update"].invoke(
        {
            "name": "reviewer",
            "description": "Review implementation changes.",
            "body": "Check correctness, tests, and docs.",
        },
        context,
    )
    listed = tools["skill_list"].invoke({}, context)
    loaded = tools["skill_get"].invoke({"name": "reviewer"}, context)
    deleted = tools["skill_delete"].invoke({"name": "reviewer"}, context)

    assert created["skill"]["scope"] == "home"
    assert created["skill"]["form"] == "file"
    assert created["skill"]["meta"]["description"] == "Review code changes."
    assert updated["skill"]["meta"]["description"] == "Review implementation changes."
    assert "Check correctness, tests, and docs." in loaded["skill"]["content"]
    assert listed["skills"][0]["name"] == "reviewer"
    assert deleted["deleted"] is True
    assert not (toolang_root / "agents" / "alice" / "skills" / "reviewer").exists()


def test_agent_state_tool_creates_updates_and_deletes_service(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    context = _tool_context(toolang_root)
    tools = create_agent_state_tool({}).tools()

    created = tools["service_create"].invoke(
        {
            "name": "search",
            "description": "Search service.",
            "transport": "http",
            "target": "https://example.com/mcp",
            "headers": {"Authorization": "Bearer $TOKEN"},
            "body": "Use for search.",
        },
        context,
    )
    updated = tools["service_update"].invoke(
        {
            "name": "search",
            "target": "https://example.com/v2/mcp",
            "env": ["TOKEN"],
        },
        context,
    )
    deleted = tools["service_delete"].invoke({"name": "search"}, context)

    assert created["service"]["meta"]["headers"] == {"Authorization": "Bearer $TOKEN"}
    assert updated["service"]["meta"]["target"] == "https://example.com/v2/mcp"
    assert updated["service"]["meta"]["env"] == ["TOKEN"]
    assert deleted["deleted"] is True


def test_agent_state_tool_creates_psyche_and_prompt(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    context = _tool_context(toolang_root)
    tools = create_agent_state_tool({}).tools()

    psyche = tools["psyche_create"].invoke(
        {"name": "direct", "body": "Prefer direct answers."},
        context,
    )
    prompt = tools["prompt_create"].invoke(
        {"name": "summarize", "body": "Summarize: {{input}}"},
        context,
    )

    assert psyche["psyche"]["content"] == "Prefer direct answers.\n"
    assert prompt["prompt"]["content"] == "Summarize: {{input}}\n"
    authored = caps.AuthoredCaps(toolang_root / "agents" / "alice").get(
        "psyche", "direct"
    )
    assert authored is not None
    assert authored.content == "Prefer direct answers.\n"


def test_agent_state_tool_rejects_non_agent_home(tmp_path: Path) -> None:
    context = ToolContext(
        run_id="run-1",
        home=tmp_path / "alice",
        room=tmp_path / "alice" / ".runtime" / "tools" / "agent_state",
        wd=tmp_path / "alice",
    )
    tool = create_agent_state_tool({}).tools()["task_list"]

    with pytest.raises(Exception, match="requires an agent home"):
        tool.invoke({}, context)
