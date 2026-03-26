from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from toolang.concepts.identity import AgentRef
from toolang.concepts.persisted import ToolsConfig
from toolang.concepts.tools import ToolCallResult, ToolDefinition
from toolang.runtime.build import PromptBuild
from toolang.runtime.model_exec import (
    ModelExecutionResult,
    TextDeltaEvent,
    ToolInputAvailableEvent,
    ToolInputDeltaEvent,
    ToolInputStartEvent,
    ToolOutputAvailableEvent,
    execute_prompt_build,
    execute_prompt_build_stream,
)
from toolang.tools import ToolRuntime, create_tool_runtime
from toolang.tools.contracts import ToolContext
from toolang.tools.plugins.filesystem import create_filesystem_tool
from toolang.tools.plugins.service_use import create_service_use_tool
from toolang.tools.plugins.shell import create_shell_tool
from toolang.tools.plugins.web_search import create_web_search_tool


def _agent_ref(home: Path) -> AgentRef:
    return AgentRef(
        selector="alice",
        kind="resident",
        uri="agent://alice/alice.too",
        id="abc123" * 10,
        root=home.parent.parent,
        home=home,
        name="alice",
        source=home / "alice.too",
    )


def _tool_context(home: Path) -> ToolContext:
    return ToolContext(
        agent=_agent_ref(home),
        working_directory=home,
        sandbox="host",
    )


def test_filesystem_tool_reads_and_writes_within_agent_home(tmp_path: Path) -> None:
    home = tmp_path / "alice"
    home.mkdir()
    provider = create_filesystem_tool({})
    context = _tool_context(home)

    written = provider.invoke(
        {"action": "write_text", "path": "notes/todo.txt", "text": "hello"},
        context,
    )
    loaded = provider.invoke(
        {"action": "read_text", "path": "notes/todo.txt"},
        context,
    )

    assert written["path"].endswith("notes/todo.txt")
    assert loaded["text"] == "hello"


def test_filesystem_tool_rejects_paths_outside_agent_home(tmp_path: Path) -> None:
    home = tmp_path / "alice"
    home.mkdir()
    provider = create_filesystem_tool({})

    with pytest.raises(Exception, match="escapes agent home"):
        provider.invoke(
            {"action": "read_text", "path": "../secret.txt"},
            _tool_context(home),
        )


def test_shell_tool_runs_one_command(tmp_path: Path) -> None:
    home = tmp_path / "alice"
    home.mkdir()
    provider = create_shell_tool({})

    result = provider.invoke({"command": "printf hi"}, _tool_context(home))

    assert result["ok"] is True
    assert result["exit_code"] == 0
    assert result["stdout"] == "hi"


def test_web_search_tool_filters_domains(monkeypatch) -> None:
    provider = create_web_search_tool({})

    monkeypatch.setattr(
        "toolang.tools.plugins.web_search._search_text",
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

    result = provider.invoke(
        {"query": "toolang", "domains": ["example.com"]},
        _tool_context(Path.cwd()),
    )

    assert result["domains"] == ["example.com"]
    assert result["results"] == [
        {
            "title": "Example",
            "url": "https://example.com/post",
            "snippet": "example body",
        }
    ]


def test_create_tool_runtime_enables_service_use_when_services_are_visible(
    tmp_path: Path,
) -> None:
    home = tmp_path / "alice"
    home.mkdir()

    runtime = create_tool_runtime(
        _agent_ref(home),
        sandbox="host",
        tools_config=ToolsConfig(),
        working_directory=home,
        visible_services=[
            {
                "name": "github",
                "transport": "http",
                "target": "https://mcp.github.com/mcp",
                "description": "GitHub MCP server",
            }
        ],
    )

    assert runtime.enabled_families() == ["filesystem", "service_use", "shell", "web_search"]


def test_service_use_tool_calls_http_service_via_mcat(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / "alice"
    home.mkdir()
    provider = create_service_use_tool(
        {
            "visible_services": [
                {
                    "name": "github",
                    "transport": "http",
                    "target": "https://mcp.github.com/mcp",
                    "description": "GitHub MCP server",
                }
            ],
            "services": {
                "github": {
                    "key_ref": "env://GITHUB_TOKEN",
                }
            },
        }
    )
    calls: list[list[str]] = []

    def fake_init_session(*, runner, endpoint, session_path, key_ref, cwd):
        calls.append(["init", endpoint, key_ref or ""])
        session_path.write_text(json.dumps({"endpoint": endpoint}), encoding="utf-8")

    def fake_run_mcat_json(runner, args, *, cwd):
        calls.append(args)
        return {"tools": [{"name": "list_issues"}]}

    monkeypatch.setattr("toolang.tools.plugins.service_use._init_session", fake_init_session)
    monkeypatch.setattr("toolang.tools.plugins.service_use._run_mcat_json", fake_run_mcat_json)

    result = provider.invoke(
        {"service": "github", "action": "tool_list"},
        _tool_context(home),
    )

    assert result["service"] == "github"
    assert result["transport"] == "http"
    assert result["result"] == {"tools": [{"name": "list_issues"}]}
    assert calls[0] == ["init", "https://mcp.github.com/mcp", "env://GITHUB_TOKEN"]
    assert calls[1][0:2] == ["tool", "list"]


def test_service_use_tool_calls_stdio_service_via_mcat_proxy(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / "alice"
    home.mkdir()
    provider = create_service_use_tool(
        {
            "runner": ["mcat"],
            "visible_services": [
                {
                    "name": "localdocs",
                    "description": "Local docs MCP server",
                }
            ],
            "services": {
                "localdocs": {
                    "transport": "stdio",
                    "command": ["uvx", "localdocs-mcp"],
                    "port": 6110,
                }
            },
        }
    )
    calls: list[list[str]] = []

    def fake_run_mcat_json(runner, args, *, cwd):
        calls.append(args)
        if args[:2] == ["proxy", "up"]:
            return {"endpoint": "http://127.0.0.1:6110/mcp"}
        if args[0] == "init":
            session_path = Path(args[args.index("-o") + 1])
            session_path.write_text(
                json.dumps({"endpoint": "http://127.0.0.1:6110/mcp"}),
                encoding="utf-8",
            )
            return {}
        if args[:2] == ["tool", "call"]:
            return {"content": [{"type": "text", "text": "done"}]}
        raise AssertionError(args)

    monkeypatch.setattr("toolang.tools.plugins.service_use._run_mcat_json", fake_run_mcat_json)

    result = provider.invoke(
        {
            "service": "localdocs",
            "action": "tool_call",
            "tool_name": "search_docs",
            "input": {"query": "toolang"},
        },
        _tool_context(home),
    )

    assert result["service"] == "localdocs"
    assert result["transport"] == "stdio"
    assert result["result"] == {"content": [{"type": "text", "text": "done"}]}
    assert calls[0][0:3] == ["proxy", "up", "6110"]
    assert calls[1][0] == "init"
    assert calls[2][0:3] == ["tool", "call", "search_docs"]


def test_execute_prompt_build_runs_local_tool_loop(monkeypatch, tmp_path: Path) -> None:
    class FakeShellProvider:
        family = "shell"

        def definition(self) -> ToolDefinition:
            return ToolDefinition(
                family="shell",
                name="shell",
                description="Run shell commands.",
                parameters={"type": "object", "properties": {"command": {"type": "string"}}},
            )

        def invoke(self, arguments, context):
            return {"ok": True, "stdout": f"ran:{arguments['command']}"}

    class FakeFunctionCall:
        type = "function_call"

        def __init__(self) -> None:
            self.id = "tool_item_1"
            self.name = "shell"
            self.arguments = json.dumps({"command": "pwd"})
            self.call_id = "call_1"

    class FakeResponse:
        def __init__(self, response_id: str, *, output, output_text: str | None = None) -> None:
            self.id = response_id
            self.output = output
            self.output_text = output_text

    responses = [
        FakeResponse("resp_1", output=[FakeFunctionCall()]),
        FakeResponse("resp_2", output=[], output_text="done"),
    ]
    calls: list[dict[str, Any]] = []

    def fake_create_response(client, *, model, messages, tools=None, previous_response_id=None):
        calls.append(
            {
                "model": model,
                "messages": messages,
                "tools": tools,
                "previous_response_id": previous_response_id,
            }
        )
        return responses.pop(0)

    monkeypatch.setattr("toolang.runtime.model_exec._create_openai_client", lambda: object())
    monkeypatch.setattr("toolang.runtime.model_exec._create_response", fake_create_response)

    home = tmp_path / "alice"
    home.mkdir()
    build = PromptBuild(
        model="gpt-5",
        raw_input="hello",
        expanded_input=None,
        message_context=None,
        runtime_context={},
        developer_message="dev",
        messages=[{"role": "user", "content": "hello"}],
        source_text="source",
        tool_runtime=ToolRuntime(
            context=_tool_context(home),
            providers={"shell": FakeShellProvider()},
        ),
    )

    result = execute_prompt_build(build)

    assert result.output_text == "done"
    assert result.tool_calls[0].family == "shell"
    assert result.tool_calls[0].arguments == {"command": "pwd"}
    assert result.tool_calls[0].output == {"ok": True, "stdout": "ran:pwd"}
    assert calls[0]["previous_response_id"] is None
    assert calls[1]["previous_response_id"] == "resp_1"
    followup_messages = calls[1]["messages"]
    assert isinstance(followup_messages, list)
    first_message = followup_messages[0]
    assert first_message["type"] == "function_call_output"


def test_execute_prompt_build_stream_emits_text_and_tool_events(
    monkeypatch,
    tmp_path: Path,
) -> None:
    class FakeShellProvider:
        family = "shell"

        def definition(self) -> ToolDefinition:
            return ToolDefinition(
                family="shell",
                name="shell",
                description="Run shell commands.",
                parameters={"type": "object", "properties": {"command": {"type": "string"}}},
            )

        def invoke(self, arguments, context):
            return {"ok": True, "stdout": f"ran:{arguments['command']}"}

    class FakeTextDelta:
        type = "response.output_text.delta"

        def __init__(self, delta: str) -> None:
            self.delta = delta

    class FakeToolArgsDelta:
        type = "response.function_call_arguments.delta"

        def __init__(self, delta: str) -> None:
            self.delta = delta
            self.item_id = "tool_item_1"
            self.output_index = 0

    class FakeFunctionCall:
        type = "function_call"

        def __init__(self) -> None:
            self.id = "tool_item_1"
            self.name = "shell"
            self.arguments = json.dumps({"command": "pwd"})
            self.call_id = "call_1"

    class FakeResponse:
        def __init__(self, response_id: str, *, output, output_text: str | None = None) -> None:
            self.id = response_id
            self.output = output
            self.output_text = output_text

    class FakeStream:
        def __init__(self, events, final_response) -> None:
            self._events = events
            self._final_response = final_response

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, exc_tb) -> None:
            return None

        def __iter__(self):
            return iter(self._events)

        def get_final_response(self):
            return self._final_response

    streams = [
        FakeStream(
            [
                FakeTextDelta("hel"),
                FakeTextDelta("lo"),
                FakeToolArgsDelta('{"command":"pwd"}'),
            ],
            FakeResponse("resp_1", output=[FakeFunctionCall()], output_text="hello"),
        ),
        FakeStream(
            [FakeTextDelta("done")],
            FakeResponse("resp_2", output=[], output_text="done"),
        ),
    ]

    monkeypatch.setattr("toolang.runtime.model_exec._create_openai_client", lambda: object())
    monkeypatch.setattr(
        "toolang.runtime.model_exec._create_response_stream",
        lambda client, **kwargs: streams.pop(0),
    )

    home = tmp_path / "alice"
    home.mkdir()
    build = PromptBuild(
        model="gpt-5",
        raw_input="hello",
        expanded_input=None,
        message_context=None,
        runtime_context={},
        developer_message="dev",
        messages=[{"role": "user", "content": "hello"}],
        source_text="source",
        tool_runtime=ToolRuntime(
            context=_tool_context(home),
            providers={"shell": FakeShellProvider()},
        ),
    )

    streamed_events: list[
        TextDeltaEvent
        | ToolInputStartEvent
        | ToolInputDeltaEvent
        | ToolInputAvailableEvent
        | ToolOutputAvailableEvent
    ] = []
    result = execute_prompt_build_stream(
        build,
        on_event=streamed_events.append,
    )

    assert result == ModelExecutionResult(
        output_text="done",
        tool_calls=[
            ToolCallResult(
                family="shell",
                name="shell",
                arguments={"command": "pwd"},
                output={"ok": True, "stdout": "ran:pwd"},
                error=None,
            )
        ],
    )
    assert streamed_events == [
        TextDeltaEvent(delta="hel"),
        TextDeltaEvent(delta="lo"),
        ToolInputStartEvent(
            tool_call_id="tool_item_1",
        ),
        ToolInputDeltaEvent(
            tool_call_id="tool_item_1",
            delta='{"command":"pwd"}',
        ),
        ToolInputAvailableEvent(
            tool_call_id="tool_item_1",
            family="shell",
            name="shell",
            arguments={"command": "pwd"},
        ),
        ToolOutputAvailableEvent(
            tool_call_id="tool_item_1",
            result=ToolCallResult(
                family="shell",
                name="shell",
                arguments={"command": "pwd"},
                output={"ok": True, "stdout": "ran:pwd"},
                error=None,
            ),
        ),
        TextDeltaEvent(delta="done"),
    ]


def test_execute_prompt_build_stream_allows_tool_only_results(
    monkeypatch,
    tmp_path: Path,
) -> None:
    class FakeShellProvider:
        family = "shell"

        def definition(self) -> ToolDefinition:
            return ToolDefinition(
                family="shell",
                name="shell",
                description="Run shell commands.",
                parameters={"type": "object", "properties": {"command": {"type": "string"}}},
            )

        def invoke(self, arguments, context):
            return {"ok": True, "stdout": f"ran:{arguments['command']}"}

    class FakeFunctionCall:
        type = "function_call"

        def __init__(self) -> None:
            self.id = "tool_item_1"
            self.name = "shell"
            self.arguments = json.dumps({"command": "pwd"})
            self.call_id = "call_1"

    class FakeResponse:
        def __init__(self, response_id: str, *, output, output_text: str | None = None) -> None:
            self.id = response_id
            self.output = output
            self.output_text = output_text

    class FakeStream:
        def __init__(self, events, final_response) -> None:
            self._events = events
            self._final_response = final_response

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, exc_tb) -> None:
            return None

        def __iter__(self):
            return iter(self._events)

        def get_final_response(self):
            return self._final_response

    streams = [
        FakeStream([], FakeResponse("resp_1", output=[FakeFunctionCall()])),
        FakeStream([], FakeResponse("resp_2", output=[], output_text="")),
    ]

    monkeypatch.setattr("toolang.runtime.model_exec._create_openai_client", lambda: object())
    monkeypatch.setattr(
        "toolang.runtime.model_exec._create_response_stream",
        lambda client, **kwargs: streams.pop(0),
    )

    home = tmp_path / "alice"
    home.mkdir()
    build = PromptBuild(
        model="gpt-5",
        raw_input="hello",
        expanded_input=None,
        message_context=None,
        runtime_context={},
        developer_message="dev",
        messages=[{"role": "user", "content": "hello"}],
        source_text="source",
        tool_runtime=ToolRuntime(
            context=_tool_context(home),
            providers={"shell": FakeShellProvider()},
        ),
    )

    streamed_events: list[
        TextDeltaEvent
        | ToolInputStartEvent
        | ToolInputDeltaEvent
        | ToolInputAvailableEvent
        | ToolOutputAvailableEvent
    ] = []
    result = execute_prompt_build_stream(
        build,
        on_event=streamed_events.append,
    )

    assert result == ModelExecutionResult(
        output_text="",
        tool_calls=[
            ToolCallResult(
                family="shell",
                name="shell",
                arguments={"command": "pwd"},
                output={"ok": True, "stdout": "ran:pwd"},
                error=None,
            )
        ],
    )
    assert streamed_events == [
        ToolInputStartEvent(
            tool_call_id="tool_item_1",
        ),
        ToolInputAvailableEvent(
            tool_call_id="tool_item_1",
            family="shell",
            name="shell",
            arguments={"command": "pwd"},
        ),
        ToolOutputAvailableEvent(
            tool_call_id="tool_item_1",
            result=ToolCallResult(
                family="shell",
                name="shell",
                arguments={"command": "pwd"},
                output={"ok": True, "stdout": "ran:pwd"},
                error=None,
            ),
        )
    ]
