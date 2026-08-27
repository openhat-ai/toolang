from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import pytest

from toolang.base.errors import ToolangError
from toolang.base.protocols.tool import AgentTool
from toolang.base.types.tool import ToolContext, ToolService
from toolang.plugin.toolsets.filesystem import create_toolset as create_filesystem_tool
from toolang.plugin.toolsets.service_use import (
    create_toolset as create_service_use_tool,
)
from toolang.plugin.toolsets.shell import create_toolset as create_shell_tool
from toolang.plugin.toolsets.web_search import create_toolset as create_web_search_tool


def _tool_context(
    home: Path,
    plugin_name: str,
    *,
    run_id: str = "run-1",
    services: tuple[ToolService, ...] = (),
) -> ToolContext:
    return ToolContext(
        run_id=run_id,
        home=home,
        room=home / ".runtime" / "tools" / plugin_name,
        wd=home,
        services=services,
    )


def _service_context(
    home: Path,
    *,
    name: str = "github",
    transport: str = "http",
    target: str = "https://mcp.github.com/mcp",
    env_names: tuple[str, ...] = (),
    environ: dict[str, str] | None = None,
    run_id: str = "run-1",
) -> ToolContext:
    return _tool_context(
        home,
        "service",
        run_id=run_id,
        services=(
            ToolService(
                name=name,
                meta={
                    "transport": transport,
                    "target": target,
                    "env": env_names,
                },
                environ=environ or {},
            ),
        ),
    )


def _invoke(
    tool: AgentTool,
    arguments: dict[str, object],
    context: ToolContext,
) -> dict[str, Any]:
    return asyncio.run(tool.invoke(arguments, context))


def test_filesystem_tool_reads_and_writes_within_agent_home(tmp_path: Path) -> None:
    home = tmp_path / "alice"
    home.mkdir()
    plugin = create_filesystem_tool({})
    tools = plugin.tools()

    written = _invoke(
        tools["write"],
        {"path": "notes/todo.txt", "text": "hello"},
        _tool_context(home, "fs"),
    )
    loaded = _invoke(
        tools["read"],
        {"path": "notes/todo.txt"},
        _tool_context(home, "fs"),
    )

    assert written["path"].endswith("notes/todo.txt")
    assert loaded["text"] == "hello"


def test_filesystem_tool_appends_to_missing_file(tmp_path: Path) -> None:
    home = tmp_path / "alice"
    home.mkdir()
    plugin = create_filesystem_tool({})
    tools = plugin.tools()

    appended = _invoke(
        tools["append"],
        {"path": "outbox/index.md", "text": "- hello\n"},
        _tool_context(home, "fs"),
    )
    loaded = _invoke(
        tools["read"],
        {"path": "outbox/index.md"},
        _tool_context(home, "fs"),
    )

    assert appended["bytes_appended"] == len("- hello\n")
    assert loaded["text"] == "- hello\n"


def test_filesystem_tool_rejects_paths_outside_agent_home(tmp_path: Path) -> None:
    home = tmp_path / "alice"
    home.mkdir()
    tool = create_filesystem_tool({}).tools()["read"]

    with pytest.raises(Exception, match="escapes agent home"):
        _invoke(
            tool,
            {"path": "../secret.txt"},
            _tool_context(home, "fs"),
        )


def test_shell_tool_runs_one_command(tmp_path: Path) -> None:
    home = tmp_path / "alice"
    home.mkdir()
    tool = create_shell_tool({}).tools()["execute"]

    result = _invoke(
        tool,
        {"command": "printf hi"},
        _tool_context(home, "shell"),
    )

    assert result["ok"] is True
    assert result["exit_code"] == 0
    assert result["stdout"] == "hi"


def test_web_search_tool_filters_domains(monkeypatch, tmp_path: Path) -> None:
    tool = create_web_search_tool({}).tools()["search"]

    async def search(
        query: str,
        *,
        max_results: int,
        timeout: int,
    ) -> list[dict[str, str]]:
        del query, max_results, timeout
        return [
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
        ]

    monkeypatch.setattr(
        "toolang.plugin.toolsets.web_search._run_search",
        search,
    )

    result = _invoke(
        tool,
        {"query": "toolang", "domains": ["example.com"]},
        _tool_context(tmp_path, "web"),
    )

    assert result["domains"] == ["example.com"]
    assert result["results"] == [
        {
            "title": "Example",
            "url": "https://example.com/post",
            "snippet": "example body",
        }
    ]


def test_web_search_worker_is_process_isolated_and_cancellable(
    monkeypatch,
) -> None:
    observed: dict[str, object] = {}

    async def run_sync(func, *args, cancellable=False):
        observed["func"] = func
        observed["args"] = args
        observed["cancellable"] = cancellable
        return []

    monkeypatch.setattr(
        "toolang.plugin.toolsets.web_search.to_process.run_sync",
        run_sync,
    )

    from toolang.plugin.toolsets.web_search import _run_search, _search_text

    result = asyncio.run(_run_search("toolang", max_results=15, timeout=5))

    assert result == []
    assert observed == {
        "func": _search_text,
        "args": ("toolang", 15, 5),
        "cancellable": True,
    }


def test_web_search_enforces_an_outer_timeout(
    monkeypatch,
    tmp_path: Path,
) -> None:
    plugin = create_web_search_tool({"timeout": 1})
    tool = plugin.tools()["search"]

    async def stalled(
        query: str,
        *,
        max_results: int,
        timeout: int,
    ) -> list[dict[str, Any]]:
        del query, max_results, timeout
        await asyncio.sleep(30)
        return []

    monkeypatch.setattr(
        "toolang.plugin.toolsets.web_search._run_search",
        stalled,
    )

    with pytest.raises(ToolangError, match="web search timed out after 1s"):
        _invoke(
            tool,
            {"query": "toolang"},
            _tool_context(tmp_path, "web"),
        )


def test_web_search_validation_uses_the_canonical_namespace() -> None:
    with pytest.raises(ToolangError, match="^web integer argument is invalid$"):
        create_web_search_tool({"top_k": "invalid"})


def test_service_use_tool_definition_uses_object_input_schema() -> None:
    plugin = create_service_use_tool({})

    definition = plugin.tools()["call_tool"].definition()

    assert definition.name == "call_tool"
    assert definition.parameters["properties"]["input"]["type"] == "object"
    assert "input" in definition.parameters["required"]
    assert "tool_name" in definition.parameters["properties"]
    assert "service" in definition.parameters["properties"]


def test_service_use_tool_exposes_leaf_commands_only() -> None:
    plugin = create_service_use_tool({})

    assert "start_auth" in plugin.tools()
    assert "complete_auth" in plugin.tools()
    assert "start_bridge" in plugin.tools()
    assert "stop_bridge" in plugin.tools()
    assert "init" in plugin.tools()
    assert "list_tools" in plugin.tools()
    assert "call_tool" in plugin.tools()
    assert "list_resources" in plugin.tools()
    assert "list_resource_templates" in plugin.tools()
    assert "read_resource" in plugin.tools()
    assert "list_prompts" in plugin.tools()
    assert "get_prompt" in plugin.tools()
    assert "bridge_status" not in plugin.tools()
    assert "callback_target" not in plugin.tools()


def test_service_use_tool_calls_http_service_via_mcat(
    monkeypatch, tmp_path: Path
) -> None:
    home = tmp_path / "alice"
    home.mkdir()
    plugin = create_service_use_tool({})
    context = _service_context(
        home,
        env_names=("GITHUB_TOKEN",),
        environ={"GITHUB_TOKEN": "github-token"},
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

    _invoke(
        plugin.tools()["init"],
        {"service": "github"},
        context,
    )
    result = _invoke(
        plugin.tools()["list_tools"],
        {"service": "github"},
        context,
    )

    assert result["ok"] is True
    assert result["result"]["service"] == "github"
    assert result["result"]["transport"] == "http"
    assert result["result"]["result"] == {"tools": [{"name": "list_issues"}]}
    assert calls[0][0] == "init"
    assert calls[1][0] == "list_tools"


def test_service_use_requires_explicitly_resolved_environment(
    monkeypatch, tmp_path: Path
) -> None:
    home = tmp_path / "alice"
    home.mkdir()
    (home / ".env").write_text("GITHUB_TOKEN=dotenv-token\n", encoding="utf-8")
    monkeypatch.setenv("GITHUB_TOKEN", "process-token")
    plugin = create_service_use_tool({})

    with pytest.raises(ToolangError, match="service env var is missing: GITHUB_TOKEN"):
        _invoke(
            plugin.tools()["init"],
            {"service": "github"},
            _service_context(home, env_names=("GITHUB_TOKEN",)),
        )


def test_service_use_tool_serializes_dict_input_for_tool_call(
    monkeypatch, tmp_path: Path
) -> None:
    home = tmp_path / "alice"
    home.mkdir()
    plugin = create_service_use_tool({})
    context = _service_context(home)

    def fake_init_session(*, connection_file: str, sess_info_file: str):
        Path(sess_info_file).parent.mkdir(parents=True, exist_ok=True)
        Path(sess_info_file).write_text(
            json.dumps({"endpoint": "https://mcp.github.com/mcp"}),
            encoding="utf-8",
        )
        return {"session_file": sess_info_file}

    def fake_call_tool(
        *, tool_name: str, arguments: dict[str, object], sess_info_file: str
    ):
        del sess_info_file
        assert tool_name == "search_issues"
        assert arguments == {"query": "toolang"}
        return {"content": [{"type": "text", "text": "ok"}]}

    monkeypatch.setattr("mcat_cli.mcp.init_session", fake_init_session)
    monkeypatch.setattr("mcat_cli.mcp.call_tool", fake_call_tool)

    _invoke(
        plugin.tools()["init"],
        {"service": "github"},
        context,
    )
    result = _invoke(
        plugin.tools()["call_tool"],
        {
            "service": "github",
            "tool_name": "search_issues",
            "input": {"query": "toolang"},
        },
        context,
    )

    assert result["ok"] is True
    assert result["result"]["result"] == {"content": [{"type": "text", "text": "ok"}]}


def test_service_use_tool_definitions_explain_auth_and_input_contract(
    tmp_path: Path,
) -> None:
    del tmp_path
    plugin = create_service_use_tool({})
    bridge_start_description = plugin.tools()["start_bridge"].definition().description
    init_description = plugin.tools()["init"].definition().description
    auth_start_description = plugin.tools()["start_auth"].definition().description
    auth_complete_description = plugin.tools()["complete_auth"].definition().description
    tool_list_description = plugin.tools()["list_tools"].definition().description
    tool_call = plugin.tools()["call_tool"].definition()

    assert "stdio service bridge" in bridge_start_description
    assert "HTTP services do not need start_bridge" in bridge_start_description
    assert "HTTP services do not need start_bridge" in init_description
    assert "call start_auth" in init_description
    assert (
        "call complete_auth so the callback endpoint is listening" in init_description
    )
    assert "reuse it and do not call init again" in init_description
    assert "expired or invalid session" in init_description
    assert "show that URL to the user" in auth_start_description
    assert (
        "receives the token while the user approves the URL" in auth_start_description
    )
    assert (
        "opening the callback endpoint and waiting for the token"
        in auth_complete_description
    )
    assert "After this succeeds, call init" in auth_complete_description
    assert "inputSchema" in tool_list_description
    assert "prior successful list_tools result" in tool_list_description
    assert "reuse that tool list and schemas" in tool_list_description
    assert "inside input" in tool_call.description
    assert "required inputSchema fields" in tool_call.description
    assert "pass input={}" in tool_call.description
    assert "previously returned schema" in tool_call.description
    assert (
        "not with title/team at the top level"
        in tool_call.parameters["properties"]["input"]["description"]
    )


def test_service_use_bridge_start_is_not_required_for_http_service(
    tmp_path: Path,
) -> None:
    home = tmp_path / "alice"
    home.mkdir()
    plugin = create_service_use_tool({})

    result = _invoke(
        plugin.tools()["start_bridge"],
        {"service": "github"},
        _service_context(home),
    )

    assert result["ok"] is True
    assert result["result"]["result"]["status"] == "not_required"
    assert not (
        home / ".runtime" / "tools" / "service" / "github" / "connection.json"
    ).exists()


def test_service_use_bridge_start_parses_effective_stdio_target(
    monkeypatch, tmp_path: Path
) -> None:
    home = tmp_path / "alice"
    home.mkdir()
    plugin = create_service_use_tool({})
    calls: list[dict[str, object]] = []

    def fake_bridge_start(**kwargs):
        calls.append(kwargs)
        return {"status": "running"}

    monkeypatch.setattr("mcat_cli.bridge.bridge_start", fake_bridge_start)

    result = _invoke(
        plugin.tools()["start_bridge"],
        {"service": "local"},
        _service_context(
            home,
            name="local",
            transport="stdio",
            target="uvx mcp-server --quiet",
        ),
    )

    assert result["ok"] is True
    assert calls[0]["command"] == ["uvx", "mcp-server", "--quiet"]


def test_service_use_rejects_service_outside_effective_run(tmp_path: Path) -> None:
    home = tmp_path / "alice"
    home.mkdir()
    plugin = create_service_use_tool({})

    with pytest.raises(
        ToolangError,
        match="service is not visible to this agent: github",
    ):
        _invoke(
            plugin.tools()["init"],
            {"service": "github"},
            _tool_context(home, "service"),
        )


def test_service_use_auth_start_prepares_http_connection(
    monkeypatch, tmp_path: Path
) -> None:
    home = tmp_path / "alice"
    home.mkdir()
    plugin = create_service_use_tool({})

    def fake_run_auth(**kwargs):
        assert kwargs["endpoint"] is None
        connection_path = Path(kwargs["connection_file"])
        assert connection_path.is_file()
        assert "https://mcp.linear.app/mcp" in connection_path.read_text(
            encoding="utf-8"
        )
        return {"status": "pending"}

    monkeypatch.setattr("mcat_cli.auth.run_auth", fake_run_auth)

    result = _invoke(
        plugin.tools()["start_auth"],
        {"service": "linear"},
        _service_context(
            home,
            name="linear",
            target="https://mcp.linear.app/mcp",
        ),
    )

    assert result["ok"] is True
    assert result["result"]["result"] == {"status": "pending"}


def test_service_use_tool_init_fails_without_bridge_state(tmp_path: Path) -> None:
    home = tmp_path / "alice"
    home.mkdir()
    plugin = create_service_use_tool({})

    with pytest.raises(Exception):
        _invoke(
            plugin.tools()["init"],
            {"service": "github"},
            _service_context(home),
        )


def test_service_use_tool_call_fails_without_init(tmp_path: Path) -> None:
    home = tmp_path / "alice"
    home.mkdir()
    plugin = create_service_use_tool({})
    context = _service_context(home)

    _invoke(
        plugin.tools()["start_bridge"],
        {"service": "github"},
        context,
    )

    with pytest.raises(Exception):
        _invoke(
            plugin.tools()["list_tools"],
            {"service": "github"},
            context,
        )


def test_service_use_reuses_service_scoped_session_file(
    monkeypatch, tmp_path: Path
) -> None:
    home = tmp_path / "alice"
    home.mkdir()
    plugin = create_service_use_tool({})
    session_files: list[str] = []

    def fake_init_session(*, connection_file: str, sess_info_file: str):
        del connection_file
        session_files.append(sess_info_file)
        Path(sess_info_file).parent.mkdir(parents=True, exist_ok=True)
        Path(sess_info_file).write_text("{}", encoding="utf-8")
        return {"session_file": sess_info_file}

    monkeypatch.setattr("mcat_cli.mcp.init_session", fake_init_session)
    monkeypatch.setattr(
        "mcat_cli.mcp.list_tools",
        lambda *, sess_info_file: {"session_file": sess_info_file},
    )

    _invoke(
        plugin.tools()["init"],
        {"service": "github"},
        _service_context(home, run_id="run-a"),
    )
    _invoke(
        plugin.tools()["init"],
        {"service": "github"},
        _service_context(home, run_id="run-b"),
    )

    assert session_files[0].endswith("/github/session.json")
    assert session_files[1].endswith("/github/session.json")
    assert session_files[0] == session_files[1]
