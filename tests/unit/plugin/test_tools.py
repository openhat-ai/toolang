from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from toolang.base.types.message import Message, TextPart, message_text
from toolang.base.types.tool import ToolContext
from toolang.execution.store import RunStore, run_store_path
from toolang.execution.records import ThreadPeer
from toolang.plugin.tools.agent_chat import create_tool_set as create_agent_chat_tool
from toolang.plugin.tools.filesystem import create_tool_set as create_filesystem_tool
from toolang.plugin.tools.service_use import create_tool_set as create_service_use_tool
from toolang.plugin.tools.shell import create_tool_set as create_shell_tool
from toolang.plugin.tools.web_search import create_tool_set as create_web_search_tool
from tests.support.execution import project_run_start


def _tool_context(home: Path, plugin_name: str) -> ToolContext:
    return ToolContext(
        run_id="run-1",
        home=home,
        room=home / ".runtime" / "tools" / plugin_name,
        wd=home,
    )


def test_filesystem_tool_reads_and_writes_within_agent_home(tmp_path: Path) -> None:
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


def test_filesystem_tool_appends_to_missing_file(tmp_path: Path) -> None:
    home = tmp_path / "alice"
    home.mkdir()
    plugin = create_filesystem_tool({})
    tools = plugin.tools()

    appended = tools["append_text"].invoke(
        {"path": "outbox/index.md", "text": "- hello\n"},
        _tool_context(home, "filesystem"),
    )
    loaded = tools["read_text"].invoke(
        {"path": "outbox/index.md"},
        _tool_context(home, "filesystem"),
    )

    assert appended["bytes_appended"] == len("- hello\n")
    assert loaded["text"] == "- hello\n"


def test_filesystem_tool_rejects_paths_outside_agent_home(tmp_path: Path) -> None:
    home = tmp_path / "alice"
    home.mkdir()
    tool = create_filesystem_tool({}).tools()["read_text"]

    with pytest.raises(Exception, match="escapes agent home"):
        tool.invoke(
            {"path": "../secret.txt"},
            _tool_context(home, "filesystem"),
        )


def test_shell_tool_runs_one_command(tmp_path: Path) -> None:
    home = tmp_path / "alice"
    home.mkdir()
    tool = create_shell_tool({}).tools()["execute"]

    result = tool.invoke({"command": "printf hi"}, _tool_context(home, "shell"))

    assert result["ok"] is True
    assert result["exit_code"] == 0
    assert result["stdout"] == "hi"


def test_web_search_tool_filters_domains(monkeypatch, tmp_path: Path) -> None:
    tool = create_web_search_tool({}).tools()["search"]

    monkeypatch.setattr(
        "toolang.plugin.tools.web_search._search_text",
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


def test_agent_chat_tool_creates_child_thread_and_sends_peer_request(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "toolang"
    home = root / "agents" / "alice"
    home.mkdir(parents=True)
    store = RunStore(run_store_path(root, "alice"))
    project_run_start(store,
        run_id="run-1",
        thread_id="term_user",
        origin="chat",
        input=Message.user("ask bob"),
        created_at="2026-01-01T00:00:00Z",
        started_at="2026-01-01T00:00:00Z",
    )
    calls: list[dict[str, object]] = []

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {
                "thread_id": "term_bob",
                "run_id": "run-bob",
                "assistant": {
                    "role": "assistant",
                    "parts": [{"type": "text", "text": "bob says yes"}],
                },
            }

    def fake_post(url: str, *, json: dict[str, object], timeout: float):
        calls.append({"url": url, "json": json, "timeout": timeout})
        return FakeResponse()

    monkeypatch.setattr("toolang.plugin.tools.agent_chat.httpx.post", fake_post)
    tool = create_agent_chat_tool(
        {"peers": [{"name": "bob", "endpoint": "http://127.0.0.1:7002"}]}
    ).tools()["send"]

    try:
        result = tool.invoke(
            {"peer": "bob", "message": "please review"},
            _tool_context(home, "agent_chat"),
        )
        local = store.get_thread(thread_id=str(result["local_thread"]))
        local_runs = store.list_runs(thread_id=str(result["local_thread"]), limit=None)
        local_commands = [
            input
            for run in local_runs
            for input in store.list_commands(run_id=run.run_id)
            if input.input is not None
        ]
        local_steps = store.list_steps(run_id=str(result["local_run_id"]))
    finally:
        store.close()

    assert calls == [
        {
            "url": "http://127.0.0.1:7002/api/v1/chat",
            "json": {
                "client": "chat",
                "peer": {"type": "agent", "name": "alice", "thread": result["local_thread"]},
                "message": {
                    "role": "user",
                    "parts": [{"type": "text", "text": "please review"}],
                },
            },
            "timeout": 60.0,
        }
    ]
    assert result["peer_thread"] == "term_bob"
    assert result["local_run_id"].startswith("run_")
    assert result["assistant_text"] == "bob says yes"
    assert local is not None
    assert local.parent == "term_user"
    assert local.peer == ThreadPeer(type="agent", name="bob", thread="term_bob")
    assert [message_text(input.input.parts) for input in local_commands if input.input is not None] == ["please review"]
    assert [part.text for part in local_steps[0].output if isinstance(part, TextPart)] == ["bob says yes"]


def test_agent_chat_tool_accepts_direct_peer_object_without_config(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "toolang"
    home = root / "agents" / "eve"
    home.mkdir(parents=True)
    store = RunStore(run_store_path(root, "eve"))
    project_run_start(store,
        run_id="run-1",
        thread_id="term_user",
        origin="chat",
        input=Message.user("ask merkle"),
        created_at="2026-01-01T00:00:00Z",
        started_at="2026-01-01T00:00:00Z",
    )
    calls: list[dict[str, object]] = []

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {
                "thread_id": "term_merkle",
                "run_id": "run-merkle",
                "assistant": {
                    "role": "assistant",
                    "parts": [{"type": "text", "text": "pong"}],
                },
            }

    def fake_post(url: str, *, json: dict[str, object], timeout: float):
        calls.append({"url": url, "json": json, "timeout": timeout})
        return FakeResponse()

    monkeypatch.setattr("toolang.plugin.tools.agent_chat.httpx.post", fake_post)
    tool = create_agent_chat_tool({}).tools()["send"]

    try:
        result = tool.invoke(
            {
                "peer": {"name": "merkle", "endpoint": "http://127.0.0.1:7002/"},
                "message": "ping",
            },
            _tool_context(home, "agent_chat"),
        )
        local = store.get_thread(thread_id=str(result["local_thread"]))
        local_runs = store.list_runs(thread_id=str(result["local_thread"]), limit=None)
        local_commands = [
            input
            for run in local_runs
            for input in store.list_commands(run_id=run.run_id)
            if input.input is not None
        ]
    finally:
        store.close()

    assert calls[0]["url"] == "http://127.0.0.1:7002/api/v1/chat"
    assert calls[0]["json"] == {
        "client": "chat",
        "peer": {"type": "agent", "name": "eve", "thread": result["local_thread"]},
        "message": {
            "role": "user",
            "parts": [{"type": "text", "text": "ping"}],
        },
    }
    assert result["peer"] == "merkle"
    assert result["peer_thread"] == "term_merkle"
    assert result["local_run_id"].startswith("run_")
    assert result["assistant_text"] == "pong"
    assert local is not None
    assert local.peer == ThreadPeer(type="agent", name="merkle", thread="term_merkle")
    assert [message_text(input.input.parts) for input in local_commands if input.input is not None] == ["ping"]


def test_agent_chat_tool_can_call_streaming_peer_chat(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "toolang"
    home = root / "agents" / "eve"
    home.mkdir(parents=True)
    store = RunStore(run_store_path(root, "eve"))
    project_run_start(store,
        run_id="run-1",
        thread_id="term_user",
        origin="chat",
        input=Message.user("ask merkle"),
        created_at="2026-01-01T00:00:00Z",
        started_at="2026-01-01T00:00:00Z",
    )
    calls: list[dict[str, object]] = []

    class FakeStream:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def raise_for_status(self) -> None:
            return None

        def iter_lines(self):
            yield 'data: {"type":"start","messageMetadata":{"threadId":"term_merkle","runId":"run-merkle"}}'
            yield 'data: {"type":"text-delta","delta":"po"}'
            yield 'data: {"type":"text-delta","delta":"ng"}'
            yield "data: [DONE]"

    def fake_stream(method: str, url: str, *, json: dict[str, object], timeout: float):
        calls.append({"method": method, "url": url, "json": json, "timeout": timeout})
        return FakeStream()

    monkeypatch.setattr("toolang.plugin.tools.agent_chat.httpx.stream", fake_stream)
    tool = create_agent_chat_tool({}).tools()["send"]

    try:
        result = tool.invoke(
            {
                "peer": {"name": "merkle", "endpoint": "http://127.0.0.1:7002"},
                "message": "ping",
                "stream": True,
            },
            _tool_context(home, "agent_chat"),
        )
        local_steps = store.list_steps(run_id=str(result["local_run_id"]))
    finally:
        store.close()

    assert calls[0]["method"] == "POST"
    assert calls[0]["url"] == "http://127.0.0.1:7002/api/v1/chat/stream"
    assert result["streamed"] is True
    assert result["peer_thread"] == "term_merkle"
    assert result["run_id"] == "run-merkle"
    assert result["assistant_text"] == "pong"
    assert [part.text for part in local_steps[0].output if isinstance(part, TextPart)] == ["pong"]


def test_service_use_tool_definition_uses_object_input_schema() -> None:
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
    assert "input" in definition.parameters["required"]
    assert "tool_name" in definition.parameters["properties"]
    assert "service" in definition.parameters["properties"]


def test_service_use_tool_exposes_leaf_commands_only() -> None:
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


def test_service_use_tool_calls_http_service_via_mcat(monkeypatch, tmp_path: Path) -> None:
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


def test_service_use_tool_serializes_dict_input_for_tool_call(monkeypatch, tmp_path: Path) -> None:
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


def test_service_use_tool_definitions_explain_auth_and_input_contract(tmp_path: Path) -> None:
    del tmp_path
    plugin = create_service_use_tool(
        {
            "visible_services": [
                {
                    "name": "linear",
                    "transport": "http",
                    "target": "https://mcp.linear.app/mcp",
                }
            ]
        }
    )
    bridge_start_description = plugin.tools()["bridge_start"].definition().description
    init_description = plugin.tools()["init"].definition().description
    auth_start_description = plugin.tools()["auth_start"].definition().description
    auth_complete_description = plugin.tools()["auth_complete"].definition().description
    tool_list_description = plugin.tools()["tool_list"].definition().description
    tool_call = plugin.tools()["tool_call"].definition()

    assert "stdio service bridge" in bridge_start_description
    assert "HTTP services do not need bridge_start" in bridge_start_description
    assert "HTTP services do not need bridge_start" in init_description
    assert "call auth_start" in init_description
    assert "call auth_complete so the callback endpoint is listening" in init_description
    assert "reuse it and do not call init again" in init_description
    assert "expired or invalid session" in init_description
    assert "show that URL to the user" in auth_start_description
    assert "receives the token while the user approves the URL" in auth_start_description
    assert "opening the callback endpoint and waiting for the token" in auth_complete_description
    assert "After this succeeds, call init" in auth_complete_description
    assert "inputSchema" in tool_list_description
    assert "prior successful tool_list result" in tool_list_description
    assert "reuse that tool list and schemas" in tool_list_description
    assert "inside input" in tool_call.description
    assert "required inputSchema fields" in tool_call.description
    assert "pass input={}" in tool_call.description
    assert "previously returned schema" in tool_call.description
    assert "not with title/team at the top level" in tool_call.parameters["properties"]["input"]["description"]


def test_service_use_bridge_start_is_not_required_for_http_service(tmp_path: Path) -> None:
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

    result = plugin.tools()["bridge_start"].invoke(
        {"service": "github"},
        _tool_context(home, "service_use"),
    )

    assert result["ok"] is True
    assert result["result"]["result"]["status"] == "not_required"
    assert not (home / ".runtime" / "tools" / "service_use" / "github" / "connection.json").exists()


def test_service_use_auth_start_prepares_http_connection(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / "alice"
    home.mkdir()
    plugin = create_service_use_tool(
        {
            "visible_services": [
                {
                    "name": "linear",
                    "transport": "http",
                    "target": "https://mcp.linear.app/mcp",
                }
            ]
        }
    )

    def fake_run_auth(**kwargs):
        assert kwargs["endpoint"] is None
        connection_path = Path(kwargs["connection_file"])
        assert connection_path.is_file()
        assert "https://mcp.linear.app/mcp" in connection_path.read_text(encoding="utf-8")
        return {"status": "pending"}

    monkeypatch.setattr("mcat_cli.auth.run_auth", fake_run_auth)

    result = plugin.tools()["auth_start"].invoke(
        {"service": "linear"},
        _tool_context(home, "service_use"),
    )

    assert result["ok"] is True
    assert result["result"]["result"] == {"status": "pending"}


def test_service_use_tool_init_fails_without_bridge_state(tmp_path: Path) -> None:
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


def test_service_use_tool_call_fails_without_init(tmp_path: Path) -> None:
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


def test_service_use_reuses_service_scoped_session_file(monkeypatch, tmp_path: Path) -> None:
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

    assert session_files[0].endswith("/github/session.json")
    assert session_files[1].endswith("/github/session.json")
    assert session_files[0] == session_files[1]
