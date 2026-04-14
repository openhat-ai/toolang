from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from toolang.base.types.tool import ToolContext
from toolang.tools.filesystem import create_tool as create_filesystem_tool
from toolang.tools.service_use import create_tool as create_service_use_tool
from toolang.tools.shell import create_tool as create_shell_tool
from toolang.tools.web_search import create_tool as create_web_search_tool


def _tool_context(home: Path, plugin_name: str) -> ToolContext:
    return ToolContext(
        run_id="run-1",
        home=home,
        room=home / ".runtime" / "tools" / plugin_name,
        wd=home,
    )


def test_experiments_filesystem_tool_reads_and_writes_within_agent_home(tmp_path: Path) -> None:
    home = tmp_path / "alice"
    home.mkdir()
    plugin = create_filesystem_tool({})
    tools = plugin.tools()

    written = tools["write_text"].invoke(
        {"path": "notes/todo.txt", "text": "hello"},
        _tool_context(home, "filesystem"),
    )
    loaded = tools["read_text"].invoke(
        {"path": "notes/todo.txt"},
        _tool_context(home, "filesystem"),
    )

    assert written["path"].endswith("notes/todo.txt")
    assert loaded["text"] == "hello"


def test_experiments_filesystem_tool_rejects_paths_outside_agent_home(tmp_path: Path) -> None:
    home = tmp_path / "alice"
    home.mkdir()
    tool = create_filesystem_tool({}).tools()["read_text"]

    with pytest.raises(Exception, match="escapes agent home"):
        tool.invoke(
            {"path": "../secret.txt"},
            _tool_context(home, "filesystem"),
        )


def test_experiments_shell_tool_runs_one_command(tmp_path: Path) -> None:
    home = tmp_path / "alice"
    home.mkdir()
    tool = create_shell_tool({}).tools()["execute"]

    result = tool.invoke({"command": "printf hi"}, _tool_context(home, "shell"))

    assert result["ok"] is True
    assert result["exit_code"] == 0
    assert result["stdout"] == "hi"


def test_experiments_web_search_tool_filters_domains(monkeypatch, tmp_path: Path) -> None:
    tool = create_web_search_tool({}).tools()["search"]

    monkeypatch.setattr(
        "toolang.tools.web_search._search_text",
        lambda query, *, max_results: [
            {
                "title": "Example",
                "href": "https://example.com/post",
                "body": "example body",
            },
            {
                "title": "Other",
                "href": "https://other.com/post",
                "body": "other body",
            },
        ],
    )

    result = tool.invoke(
        {"query": "toolang", "domains": ["example.com"]},
        _tool_context(tmp_path, "web_search"),
    )

    assert result["domains"] == ["example.com"]
    assert result["results"] == [
        {
            "title": "Example",
            "url": "https://example.com/post",
            "snippet": "example body",
        }
    ]


def test_experiments_service_use_tool_definition_uses_object_input_schema() -> None:
    plugin = create_service_use_tool(
        {
            "visible_services": [
                {
                    "name": "github",
                    "transport": "http",
                    "target": "https://mcp.github.com/mcp",
                }
            ]
        }
    )

    definition = plugin.tools()["tool_call"].definition()

    assert definition.name == "tool_call"
    assert definition.parameters["properties"]["input"]["type"] == "object"
    assert "tool_name" in definition.parameters["properties"]
    assert "service" in definition.parameters["properties"]


def test_experiments_service_use_tool_exposes_leaf_commands_only() -> None:
    plugin = create_service_use_tool(
        {
            "visible_services": [
                {
                    "name": "github",
                    "transport": "http",
                    "target": "https://mcp.github.com/mcp",
                }
            ]
        }
    )

    assert "auth_start" in plugin.tools()
    assert "auth_complete" in plugin.tools()
    assert "bridge_start" in plugin.tools()
    assert "bridge_stop" in plugin.tools()
    assert "init" in plugin.tools()
    assert "tool_list" in plugin.tools()
    assert "tool_call" in plugin.tools()
    assert "resource_list" in plugin.tools()
    assert "resource_template_list" in plugin.tools()
    assert "resource_read" in plugin.tools()
    assert "prompt_list" in plugin.tools()
    assert "prompt_get" in plugin.tools()
    assert "bridge_status" not in plugin.tools()
    assert "callback_target" not in plugin.tools()


def test_experiments_service_use_tool_calls_http_service_via_mcat(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / "alice"
    home.mkdir()
    (home / ".env").write_text("GITHUB_TOKEN=github-token\n", encoding="utf-8")
    plugin = create_service_use_tool(
        {
            "visible_services": [
                {
                    "name": "github",
                    "transport": "http",
                    "target": "https://mcp.github.com/mcp",
                    "env_vars": ["GITHUB_TOKEN"],
                }
            ]
        }
    )
    calls: list[tuple[str, object]] = []

    def fake_init_session(*, connection_file: str, sess_info_file: str):
        calls.append(("init", connection_file))
        assert os.environ["GITHUB_TOKEN"] == "github-token"
        Path(sess_info_file).parent.mkdir(parents=True, exist_ok=True)
        Path(sess_info_file).write_text(
            json.dumps({"endpoint": "https://mcp.github.com/mcp"}),
            encoding="utf-8",
        )
        return {"session_file": sess_info_file}

    def fake_list_tools(*, sess_info_file: str):
        calls.append(("list_tools", sess_info_file))
        assert os.environ["GITHUB_TOKEN"] == "github-token"
        return {"tools": [{"name": "list_issues"}]}

    monkeypatch.setattr("mcat_cli.mcp.init_session", fake_init_session)
    monkeypatch.setattr("mcat_cli.mcp.list_tools", fake_list_tools)

    plugin.tools()["bridge_start"].invoke(
        {"service": "github"},
        _tool_context(home, "service_use"),
    )
    plugin.tools()["init"].invoke(
        {"service": "github"},
        _tool_context(home, "service_use"),
    )
    result = plugin.tools()["tool_list"].invoke(
        {"service": "github"},
        _tool_context(home, "service_use"),
    )

    assert result["ok"] is True
    assert result["result"]["service"] == "github"
    assert result["result"]["transport"] == "http"
    assert result["result"]["result"] == {"tools": [{"name": "list_issues"}]}
    assert calls[0][0] == "init"
    assert calls[1][0] == "list_tools"


def test_experiments_service_use_tool_serializes_dict_input_for_tool_call(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / "alice"
    home.mkdir()
    plugin = create_service_use_tool(
        {
            "visible_services": [
                {
                    "name": "github",
                    "transport": "http",
                    "target": "https://mcp.github.com/mcp",
                }
            ]
        }
    )

    def fake_init_session(*, connection_file: str, sess_info_file: str):
        Path(sess_info_file).parent.mkdir(parents=True, exist_ok=True)
        Path(sess_info_file).write_text(
            json.dumps({"endpoint": "https://mcp.github.com/mcp"}),
            encoding="utf-8",
        )
        return {"session_file": sess_info_file}

    def fake_call_tool(*, tool_name: str, arguments: dict[str, object], sess_info_file: str):
        del sess_info_file
        assert tool_name == "search_issues"
        assert arguments == {"query": "toolang"}
        return {"content": [{"type": "text", "text": "ok"}]}

    monkeypatch.setattr("mcat_cli.mcp.init_session", fake_init_session)
    monkeypatch.setattr("mcat_cli.mcp.call_tool", fake_call_tool)

    plugin.tools()["bridge_start"].invoke(
        {"service": "github"},
        _tool_context(home, "service_use"),
    )
    plugin.tools()["init"].invoke(
        {"service": "github"},
        _tool_context(home, "service_use"),
    )
    result = plugin.tools()["tool_call"].invoke(
        {
            "service": "github",
            "tool_name": "search_issues",
            "input": {"query": "toolang"},
        },
        _tool_context(home, "service_use"),
    )

    assert result["ok"] is True
    assert result["result"]["result"] == {"content": [{"type": "text", "text": "ok"}]}


def test_experiments_service_use_tool_init_fails_without_bridge_state(tmp_path: Path) -> None:
    home = tmp_path / "alice"
    home.mkdir()
    plugin = create_service_use_tool(
        {
            "visible_services": [
                {
                    "name": "github",
                    "transport": "http",
                    "target": "https://mcp.github.com/mcp",
                }
            ]
        }
    )

    with pytest.raises(Exception):
        plugin.tools()["init"].invoke(
            {"service": "github"},
            _tool_context(home, "service_use"),
        )


def test_experiments_service_use_tool_call_fails_without_init(tmp_path: Path) -> None:
    home = tmp_path / "alice"
    home.mkdir()
    plugin = create_service_use_tool(
        {
            "visible_services": [
                {
                    "name": "github",
                    "transport": "http",
                    "target": "https://mcp.github.com/mcp",
                }
            ]
        }
    )

    plugin.tools()["bridge_start"].invoke(
        {"service": "github"},
        _tool_context(home, "service_use"),
    )

    with pytest.raises(Exception):
        plugin.tools()["tool_list"].invoke(
            {"service": "github"},
            _tool_context(home, "service_use"),
        )


def test_experiments_service_use_uses_run_scoped_session_file(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / "alice"
    home.mkdir()
    plugin = create_service_use_tool(
        {
            "visible_services": [
                {
                    "name": "github",
                    "transport": "http",
                    "target": "https://mcp.github.com/mcp",
                }
            ]
        }
    )
    session_files: list[str] = []

    def fake_init_session(*, connection_file: str, sess_info_file: str):
        del connection_file
        session_files.append(sess_info_file)
        Path(sess_info_file).parent.mkdir(parents=True, exist_ok=True)
        Path(sess_info_file).write_text("{}", encoding="utf-8")
        return {"session_file": sess_info_file}

    monkeypatch.setattr("mcat_cli.mcp.init_session", fake_init_session)
    monkeypatch.setattr("mcat_cli.mcp.list_tools", lambda *, sess_info_file: {"session_file": sess_info_file})

    plugin.tools()["bridge_start"].invoke(
        {"service": "github"},
        ToolContext(
            run_id="run-a",
            home=home,
            room=home / ".runtime" / "tools" / "service_use",
            wd=home,
        ),
    )
    plugin.tools()["init"].invoke(
        {"service": "github"},
        ToolContext(
            run_id="run-a",
            home=home,
            room=home / ".runtime" / "tools" / "service_use",
            wd=home,
        ),
    )
    plugin.tools()["init"].invoke(
        {"service": "github"},
        ToolContext(
            run_id="run-b",
            home=home,
            room=home / ".runtime" / "tools" / "service_use",
            wd=home,
        ),
    )

    assert session_files[0].endswith("/github/runs/run-a/session.json")
    assert session_files[1].endswith("/github/runs/run-b/session.json")
