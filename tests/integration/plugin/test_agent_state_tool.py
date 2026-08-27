from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from toolang.base.protocols.tool import AgentTool
from toolang.base.types.tool import ToolContext
from toolang.catalog import cap as caps
from toolang.catalog.job import AuthoredJobs
from toolang.execution.tools.agent_state import (
    create_toolset as create_agent_state_tool,
)


def _tool_context(toolang_root: Path, agent_name: str = "alice") -> ToolContext:
    home = toolang_root / "agents" / agent_name
    home.mkdir(parents=True, exist_ok=True)
    return ToolContext(
        run_id="run-1",
        home=home,
        room=home / ".runtime" / "tools" / "_me",
        wd=home,
    )


def _invoke(
    tool: AgentTool,
    arguments: dict[str, object],
    context: ToolContext,
) -> dict[str, Any]:
    return asyncio.run(tool.invoke(arguments, context))


def test_agent_state_tool_creates_lists_gets_and_updates_tasks(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    context = _tool_context(toolang_root)
    tools = create_agent_state_tool({}).tools()

    created = _invoke(
        tools["create_task"],
        {
            "title": "Review plan",
            "body": "Review the implementation plan.",
        },
        context,
    )
    task_id = created["task"]["id"]

    listed = _invoke(tools["list_tasks"], {}, context)
    loaded = _invoke(tools["get_task"], {"task_id": task_id}, context)
    updated = _invoke(
        tools["update_task"],
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

    created = _invoke(
        tools["create_chore"],
        {
            "title": "Check stale PRs",
            "body": "Report stale pull requests.",
            "schedule": "FREQ=HOURLY;INTERVAL=6",
        },
        context,
    )
    chore_id = created["chore"]["id"]

    updated = _invoke(
        tools["update_chore"],
        {
            "chore_id": chore_id,
            "schedule": "FREQ=DAILY;INTERVAL=1",
            "body": "Report stale pull requests and blockers.",
        },
        context,
    )
    listed = _invoke(tools["list_chores"], {}, context)

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

    created = _invoke(
        tools["create_skill"],
        {
            "name": "reviewer",
            "description": "Review code changes.",
            "body": "Check correctness and tests.",
        },
        context,
    )
    updated = _invoke(
        tools["update_skill"],
        {
            "name": "reviewer",
            "description": "Review implementation changes.",
            "body": "Check correctness, tests, and docs.",
        },
        context,
    )
    listed = _invoke(tools["list_skills"], {}, context)
    loaded = _invoke(tools["get_skill"], {"name": "reviewer"}, context)
    deleted = _invoke(tools["delete_skill"], {"name": "reviewer"}, context)

    assert created["skill"]["scope"] == "home"
    assert created["skill"]["form"] == "authored"
    assert created["skill"]["meta"]["description"] == "Review code changes."
    assert updated["skill"]["meta"]["description"] == "Review implementation changes."
    assert "Check correctness, tests, and docs." in loaded["skill"]["content"]
    assert listed["skills"][0]["name"] == "reviewer"
    assert deleted["deleted"] is True
    assert not (toolang_root / "agents" / "alice" / "skills" / "reviewer").exists()


def test_agent_state_cap_tools_expose_scope_instead_of_visibility() -> None:
    tools = create_agent_state_tool({}).tools()

    for name in (
        "list_psyches",
        "get_psyche",
        "create_psyche",
        "update_psyche",
        "delete_psyche",
        "list_skills",
        "get_skill",
        "create_skill",
        "update_skill",
        "delete_skill",
        "list_services",
        "get_service",
        "create_service",
        "update_service",
        "delete_service",
        "list_prompts",
        "get_prompt",
        "create_prompt",
        "update_prompt",
        "delete_prompt",
    ):
        properties = tools[name].definition().parameters["properties"]
        assert "scope" in properties
        assert "visibility" not in properties


def test_agent_state_tool_creates_updates_and_deletes_service(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    context = _tool_context(toolang_root)
    tools = create_agent_state_tool({}).tools()

    created = _invoke(
        tools["create_service"],
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
    updated = _invoke(
        tools["update_service"],
        {
            "name": "search",
            "target": "https://example.com/v2/mcp",
            "env": ["TOKEN"],
        },
        context,
    )
    deleted = _invoke(tools["delete_service"], {"name": "search"}, context)

    assert created["service"]["meta"]["headers"] == {"Authorization": "Bearer $TOKEN"}
    assert updated["service"]["meta"]["target"] == "https://example.com/v2/mcp"
    assert updated["service"]["meta"]["env"] == ["TOKEN"]
    assert deleted["deleted"] is True


def test_agent_state_tool_creates_psyche_and_prompt(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    context = _tool_context(toolang_root)
    tools = create_agent_state_tool({}).tools()

    psyche = _invoke(
        tools["create_psyche"],
        {"name": "direct", "body": "Prefer direct answers."},
        context,
    )
    prompt = _invoke(
        tools["create_prompt"],
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
        room=tmp_path / "alice" / ".runtime" / "tools" / "_me",
        wd=tmp_path / "alice",
    )
    tool = create_agent_state_tool({}).tools()["list_tasks"]

    with pytest.raises(Exception, match="requires an agent home"):
        _invoke(tool, {}, context)
