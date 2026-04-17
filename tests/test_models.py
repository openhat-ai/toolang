from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from toolang.base.protocols.model import ModelPlugin
from toolang.base.protocols.tool import Tool
from toolang.base.types.message import Message, ToolCallPart, ToolResultPart
from toolang.base.types.model import ModelBinding, ModelCapabilities, ResolvedModel
from toolang.base.types.run import (
    ModelCall,
    ModelCallResult,
    ToolCall,
)
from toolang.base.types.tool import ToolContext, ToolDefinition
from toolang.base.error import ToolangError
from toolang.execution.input import RunBinding, RunInput
from toolang.execution.snapshot import RunSnapshot, SnapshotAgent, SnapshotProgram, SnapshotRun
from toolang.execution.model import resolve_model, select_model_selectors
from toolang.execution.context import RunContext
from toolang.models._openai_compat import encode_message, response_payload
from toolang.strategies import load_run_strategy
from toolang.up import load_default_models, load_model_profiles


class _FakeTool(Tool):
    name = "shell_execute"
    plugin_name = "shell"

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description="Run a shell command.",
            parameters={"type": "object"},
        )

    def invoke(self, arguments, context: ToolContext) -> dict[str, Any]:
        del context
        return {"ok": True, "stdout": f"ran:{arguments['command']}"}


class _FakeModelPlugin(ModelPlugin):
    def __init__(
        self,
        *,
        name: str,
        responses: list[ModelCallResult] | None = None,
        resolved: dict[str, ResolvedModel] | None = None,
    ) -> None:
        self.name = name
        self.description = None
        self._responses = list(responses or [])
        self._resolved = dict(resolved or {})
        self.requests: list[ModelCall] = []

    def capabilities(self) -> ModelCapabilities:
        return ModelCapabilities()

    def resolve_selector(self, selector: str, *, environ) -> ResolvedModel | None:
        del environ
        return self._resolved.get(selector)

    def invoke(self, target: ResolvedModel, request: ModelCall) -> ModelCallResult:
        del target
        self.requests.append(request)
        return self._responses.pop(0)

    def stream(self, target: ResolvedModel, request: ModelCall, *, on_event) -> ModelCallResult:
        del on_event
        return self.invoke(target, request)


def test_model_resolution_resolves_named_profile(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    (toolang_root / "agents" / "alice").mkdir(parents=True, exist_ok=True)
    (toolang_root / "config.toml").write_text(
        '[models]\n'
        'default = ["fast"]\n'
        '\n'
        '[models.fast]\n'
        'ref = "openai/gpt-5"\n'
        'plugin = "openai"\n'
        'model = "gpt-5"\n'
        'api_key_env = "OPENAI_API_KEY"\n',
        encoding="utf-8",
    )
    plugin = _FakeModelPlugin(name="openai")
    context = SimpleNamespace(
        model_plugins={"openai": plugin},
        model_profiles=load_model_profiles(toolang_root, "alice"),
        default_models=load_default_models(toolang_root, "alice"),
        model_environ={"OPENAI_API_KEY": "secret"},
    )
    resolved = resolve_model(context, selector="fast")

    assert resolved.target.ref == "openai/gpt-5"
    assert resolved.target.plugin == "openai"
    assert resolved.target.model == "gpt-5"
    assert resolved.target.api_key == "secret"
    assert resolved.plugin is plugin


def test_model_resolution_resolves_explicit_plugin_route() -> None:
    plugin = _FakeModelPlugin(
        name="openrouter",
        resolved={
            "openai/gpt-5": ResolvedModel(
                ref="openai/gpt-5",
                plugin="openrouter",
                model="openai/gpt-5",
            )
        },
    )
    context = SimpleNamespace(
        model_plugins={"openrouter": plugin},
        model_profiles={},
        default_models=(),
        model_environ={},
    )

    resolved = resolve_model(context, selector="openai/gpt-5@openrouter")

    assert resolved.target.plugin == "openrouter"
    assert resolved.target.model == "openai/gpt-5"


def test_model_resolution_rejects_ambiguous_selector() -> None:
    context = SimpleNamespace(
        model_plugins={
            "openai": _FakeModelPlugin(
                name="openai",
                resolved={"openai/gpt-5": ResolvedModel(ref="openai/gpt-5", plugin="openai", model="gpt-5")},
            ),
            "openrouter": _FakeModelPlugin(
                name="openrouter",
                resolved={"openai/gpt-5": ResolvedModel(ref="openai/gpt-5", plugin="openrouter", model="openai/gpt-5")},
            ),
        },
        model_profiles={},
        default_models=(),
        model_environ={},
    )

    with pytest.raises(ToolangError, match="ambiguous"):
        resolve_model(context, selector="openai/gpt-5")


def test_model_resolution_uses_first_allowed_selector_as_default() -> None:
    plugin = _FakeModelPlugin(
        name="openrouter",
        resolved={
            "gpt-5": ResolvedModel(ref="openai/gpt-5", plugin="openrouter", model="gpt-5"),
            "o3": ResolvedModel(ref="openai/o3", plugin="openrouter", model="o3"),
        },
    )
    context = SimpleNamespace(
        model_plugins={"openrouter": plugin},
        model_profiles={},
        default_models=(),
        model_environ={},
    )

    resolved = resolve_model(
        context,
        selector=None,
        default_selector="gpt-5@openrouter",
        allowed_selectors=("gpt-5@openrouter", "o3@openrouter"),
    )

    assert resolved.target.ref == "openai/gpt-5"
    assert resolved.target.model == "gpt-5"


def test_model_resolution_allows_thunk_selector_within_allowed_set() -> None:
    plugin = _FakeModelPlugin(
        name="openrouter",
        resolved={
            "gpt-5": ResolvedModel(ref="openai/gpt-5", plugin="openrouter", model="gpt-5"),
            "o3": ResolvedModel(ref="openai/o3", plugin="openrouter", model="o3"),
        },
    )
    context = SimpleNamespace(
        model_plugins={"openrouter": plugin},
        model_profiles={},
        default_models=(),
        model_environ={},
    )

    resolved = resolve_model(
        context,
        selector="o3@openrouter",
        default_selector="gpt-5@openrouter",
        allowed_selectors=("gpt-5@openrouter", "o3@openrouter"),
    )

    assert resolved.target.ref == "openai/o3"
    assert resolved.target.model == "o3"


def test_model_resolution_rejects_thunk_selector_outside_allowed_set() -> None:
    plugin = _FakeModelPlugin(
        name="openrouter",
        resolved={
            "gpt-5": ResolvedModel(ref="openai/gpt-5", plugin="openrouter", model="gpt-5"),
            "o3": ResolvedModel(ref="openai/o3", plugin="openrouter", model="o3"),
        },
    )
    context = SimpleNamespace(
        model_plugins={"openrouter": plugin},
        model_profiles={},
        default_models=(),
        model_environ={},
    )

    with pytest.raises(ToolangError, match="not allowed for this activation"):
        resolve_model(
            context,
            selector="o3@openrouter",
            default_selector="gpt-5@openrouter",
            allowed_selectors=("gpt-5@openrouter",),
        )


def test_select_model_selectors_preserves_activation_order_for_intersection() -> None:
    plugin = _FakeModelPlugin(
        name="openrouter",
        resolved={
            "gpt-5": ResolvedModel(ref="openai/gpt-5", plugin="openrouter", model="gpt-5"),
            "o3": ResolvedModel(ref="openai/o3", plugin="openrouter", model="o3"),
        },
    )
    context = SimpleNamespace(
        model_plugins={"openrouter": plugin},
        model_profiles={},
        default_models=(),
        model_environ={},
    )

    selectors = select_model_selectors(
        context,
        thunk_selectors=("gpt-5@openrouter", "o3@openrouter"),
        activation_selectors=("o3@openrouter", "gpt-5@openrouter"),
    )

    assert selectors == ("o3@openrouter", "gpt-5@openrouter")


def test_execute_run_input_reuses_plugin_state_for_followups() -> None:
    plugin = _FakeModelPlugin(
        name="openai",
        responses=[
            ModelCallResult(
                message=Message(
                    role="assistant",
                    parts=(
                        ToolCallPart(
                            tool_call_id="tool-1",
                            call_id="call-1",
                            tool_name="shell_execute",
                            tool_family="shell_execute",
                            input={"command": "pwd"},
                        ),
                    ),
                ),
                tool_calls=(
                    ToolCall(
                        tool_call_id="tool-1",
                        call_id="call-1",
                        name="shell_execute",
                        input={"command": "pwd"},
                    ),
                ),
                state={"previous_response_id": "resp-1", "baseline_count": 2},
            ),
            ModelCallResult(message=Message.assistant("done")),
        ],
    )
    run_input = _run_input()

    result = load_run_strategy("basic").run(
        RunContext(
            run_input,
            ModelBinding(
                target=ResolvedModel(
                    ref="openai/gpt-5",
                    plugin=plugin.name,
                    model="gpt-5",
                ),
                plugin=plugin,
            ),
        )
    )

    assert result.output_text == "done"
    assert plugin.requests[0].state is None
    assert plugin.requests[1].state == {"previous_response_id": "resp-1", "baseline_count": 2}
    assert [item.to_data() for item in plugin.requests[1].messages] == [
        {"role": "user", "parts": [{"type": "text", "text": "hello"}]},
        {
            "role": "assistant",
            "parts": [
                {
                    "type": "tool_call",
                    "tool_call_id": "tool-1",
                    "call_id": "call-1",
                    "tool_name": "shell_execute",
                    "tool_family": "shell_execute",
                    "input": {"command": "pwd"},
                }
            ],
        },
        {
            "role": "tool",
            "parts": [
                {
                    "type": "tool_result",
                    "tool_call_id": "tool-1",
                    "call_id": "call-1",
                    "tool_name": "shell_execute",
                    "tool_family": "shell_execute",
                    "output": {"ok": True, "stdout": "ran:pwd"},
                }
            ],
        },
    ]


def test_execute_run_input_appends_plugin_messages_for_stateless_plugins() -> None:
    plugin = _FakeModelPlugin(
        name="ollama",
        responses=[
            ModelCallResult(
                message=Message(
                    role="assistant",
                    parts=(
                        ToolCallPart(
                            tool_call_id="tool-1",
                            call_id="call-1",
                            tool_name="shell_execute",
                            tool_family="shell_execute",
                            input={"command": "pwd"},
                        ),
                    ),
                ),
                tool_calls=(
                    ToolCall(
                        tool_call_id="tool-1",
                        call_id="call-1",
                        name="shell_execute",
                        input={"command": "pwd"},
                    ),
                ),
            ),
            ModelCallResult(message=Message.assistant("done")),
        ],
    )
    run_input = _run_input()

    result = load_run_strategy("basic").run(
        RunContext(
            run_input,
            ModelBinding(
                target=ResolvedModel(
                    ref="qwen/qwen3",
                    plugin=plugin.name,
                    model="qwen3",
                ),
                plugin=plugin,
            ),
        )
    )

    assert result.output_text == "done"
    assert plugin.requests[0].state is None
    assert plugin.requests[1].state is None
    assert [item.to_data() for item in plugin.requests[1].messages] == [
        {"role": "user", "parts": [{"type": "text", "text": "hello"}]},
        {
            "role": "assistant",
            "parts": [
                {
                    "type": "tool_call",
                    "tool_call_id": "tool-1",
                    "call_id": "call-1",
                    "tool_name": "shell_execute",
                    "tool_family": "shell_execute",
                    "input": {"command": "pwd"},
                }
            ],
        },
        {
            "role": "tool",
            "parts": [
                {
                    "type": "tool_result",
                    "tool_call_id": "tool-1",
                    "call_id": "call-1",
                    "tool_name": "shell_execute",
                    "tool_family": "shell_execute",
                    "output": {"ok": True, "stdout": "ran:pwd"},
                }
            ],
        },
    ]


def test_openai_compat_preserves_structured_message_content() -> None:
    encoded = encode_message(
        Message(role="user", parts=(Message.user("hello").parts[0],))
    )

    assert encoded == {
        "role": "user",
        "content": [
            {
                "type": "input_text",
                "text": "hello",
            }
        ],
    }


def test_openai_compat_skips_historical_tool_items_without_previous_response_id() -> None:
    payload = response_payload(
        ResolvedModel(ref="openai/gpt-5", plugin="openai", model="gpt-5"),
        ModelCall(
            instructions="dev",
            messages=[
                Message.user("hello"),
                Message(
                    role="assistant",
                    parts=(
                        ToolCallPart(
                            tool_call_id="fc_1",
                            call_id="call_1",
                            tool_name="shell_execute",
                            tool_family="shell_execute",
                            input={"command": "pwd"},
                        ),
                    ),
                ),
                Message(
                    role="tool",
                    parts=(
                        ToolResultPart(
                            tool_call_id="fc_1",
                            call_id="call_1",
                            tool_name="shell_execute",
                            tool_family="shell_execute",
                            output={"ok": True, "stdout": "/tmp"},
                        ),
                    ),
                ),
                Message.assistant("done"),
            ],
        ),
        stateful=True,
    )

    assert "previous_response_id" not in payload
    assert payload["input"] == [
        {"role": "developer", "content": "dev"},
        {"role": "user", "content": [{"type": "input_text", "text": "hello"}]},
        {"role": "assistant", "content": [{"type": "output_text", "text": "done"}]},
    ]


def _run_input() -> RunInput:
    tool = _FakeTool()
    return RunInput(
        run=RunBinding(
            run_id="run-1",
            group="chat",
            origin="chat",
            thread_id="thread-1",
            thunk_name=None,
            input_text="hello",
            run_strategy="basic",
            metadata={},
            live=cast(Any, None),
            created_at="2026-04-10T00:00:00Z",
        ),
        model="openai/gpt-5",
        input=Message.user("hello"),
        instructions="dev",
        messages=[Message.user("hello")],
        snapshot=RunSnapshot(
            agent=SnapshotAgent(name="alice", root="/tmp/root", home="/tmp/home"),
            run=SnapshotRun(
                run_id="run-1",
                group="chat",
                origin="chat",
                thread_id="thread-1",
                run_strategy="basic",
                live_fingerprint="",
            ),
            program=SnapshotProgram(source_path="", thunk={}),
        ),
        tools={tool.name: tool},
        debug={},
    )
