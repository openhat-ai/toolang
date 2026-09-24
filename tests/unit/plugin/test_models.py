from __future__ import annotations

from collections.abc import Mapping
import asyncio
from dataclasses import replace
import logging
from pathlib import Path
import tomllib
from types import SimpleNamespace
from typing import Any, cast

import pytest

from toolang.base.errors import ModelResponseError
from toolang.base.protocols.model import ModelAdapter
from toolang.base.protocols.tool import Tool
from toolang.base.types.message import (
    AudioPart,
    DocumentPart,
    ImagePart,
    Message,
    TextPart,
    ReasoningPart,
    ToolCallPart,
    ToolResultPart,
)
from toolang.base.types.model import (
    Model,
    ModelRoute,
    ModelToolang,
    Reasoning,
)
from toolang.base.types.policy import RunBindings
from toolang.base.types.run import (
    ModelCall,
    ModelCallResult,
    ModelStreamHandler,
    ModelUsage,
    ToolCall,
)
from toolang.base.types.tool import ToolContext, ToolDefinition, ToolResult
from toolang.common.errors import ToolangError
from toolang.common.layout import AgentLayout
from toolang.execution.events import RunEvent, StepEnd
from toolang.execution.executor.common import BoundRun
from toolang.execution.executor.frame import _AgicFrame
from toolang.execution.executor.runs.agic import _AgicState, _OutputBinding, _execute
from toolang.execution.executor.steps.model import _candidate
from toolang.execution.assembly.message_buffer import MessageBuffer
from toolang.lang.types import Array
from toolang.plugin.toolsets.loading import load_tools
from toolang.execution.records import ControlRecord, SteerControlPayload
from toolang.execution.types import (
    AgentResources,
    AgentToolResource,
    ControlRef,
    Local,
    Output,
    StepRef,
)
from toolang.plugin.models.resolution import (
    model_reasoning_effort_applicable,
    model_reasoning_efforts,
    resolve_model_reasoning,
)
from toolang.plugin.models.views import _format_decimal_unit
from toolang.setup import AgentSetup, ModelCollection, ToolCollection
from toolang.plugin.catalogs.models_dev.catalog import read_model_catalog_snapshot
from toolang.plugin.catalogs.models_dev.path import PACKAGED_MODEL_CATALOG
from toolang.plugin.loading import load_model_adapters
from toolang.plugin.adapters import chat_completions as chat_completions_models
from toolang.plugin.adapters import messages as messages_models
from toolang.plugin.adapters import responses as responses_models
from toolang.plugin.adapters.responses import encode_message, response_payload
from toolang.lang.ast import AgicDecl, Message as AstMessage, Parameter, Program, Span
from toolang.lang.input import CallInput, RunnableInput


def load_config_layers(root: Path, agent_name: str) -> tuple[dict[str, object], ...]:
    layers: list[dict[str, object]] = []
    for path in (root / "config.toml", root / "agents" / agent_name / "config.toml"):
        if path.is_file():
            layers.append(tomllib.loads(path.read_text(encoding="utf-8")))
    return tuple(layers)


def _reasoning_model(options: list[dict[str, object]]) -> Model:
    return Model(
        id="m",
        name="m",
        _toolang=ModelToolang(provider="p", ready=True),
        reasoning=True,
        reasoning_options=tuple(options),
    )


def test_reasoning_parameters_reject_effort_and_budget_together() -> None:
    with pytest.raises(ValueError, match="either effort or budget_tokens"):
        Reasoning("high", 2048)


def test_toggle_only_reasoning_advertises_none() -> None:
    model = _reasoning_model([{"type": "toggle"}])

    assert model_reasoning_efforts(model) == ()
    assert model_reasoning_effort_applicable(model) is True
    assert resolve_model_reasoning(model, Reasoning("none")) == Reasoning("none")


def test_budget_only_reasoning_makes_effort_applicable() -> None:
    model = _reasoning_model([{"type": "budget_tokens"}])

    assert model_reasoning_effort_applicable(model) is True
    assert resolve_model_reasoning(model, Reasoning(budget_tokens=1000)) == Reasoning(
        budget_tokens=1000
    )


def test_exhaustive_effort_enumeration_rejects_an_unlisted_level() -> None:
    model = _reasoning_model(
        [{"type": "effort", "values": ["low", "high"], "exhaustive": True}]
    )

    assert model_reasoning_efforts(model) == ("low", "high")
    with pytest.raises(ToolangError, match="does not advertise"):
        resolve_model_reasoning(model, Reasoning("medium"))


def test_non_exhaustive_effort_passes_unlisted_levels_through() -> None:
    model = _reasoning_model([{"type": "effort", "values": ["low"]}])

    assert resolve_model_reasoning(model, Reasoning("high")) == Reasoning("high")


def test_effort_none_disables_reasoning_for_a_toggle_only_model() -> None:
    model = _reasoning_model([{"type": "toggle"}])

    assert resolve_model_reasoning(model, Reasoning("none")) == Reasoning("none")


class _FakeTool(Tool):
    name = "shell__execute"
    plugin_name = "shell"
    toolset = "shell"

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description="Run a shell command.",
            parameters={"type": "object"},
        )

    async def invoke(self, arguments, context: ToolContext) -> ToolResult:
        del context
        return ToolResult({"ok": True, "stdout": f"ran:{arguments['command']}"})


class _FakeModels(ModelAdapter):
    def __init__(
        self,
        *,
        name: str,
        responses: list[ModelCallResult] | None = None,
        default_base_url: str | None = None,
    ) -> None:
        self.name = name
        self.description = None
        self.default_api = default_base_url
        self._responses = list(responses or [])
        self.requests: list[ModelCall] = []

    async def invoke(
        self,
        model: Model,
        request: ModelCall,
        *,
        environ: Mapping[str, str],
    ) -> ModelCallResult:
        del model, environ
        self.requests.append(request)
        return self._responses.pop(0)

    async def stream(
        self,
        model: Model,
        request: ModelCall,
        *,
        environ: Mapping[str, str],
        on_event: ModelStreamHandler,
    ) -> ModelCallResult:
        del on_event
        return await self.invoke(model, request, environ=environ)


def _route(
    provider: str = "test",
    adapter: str = "responses",
    *,
    api: str | None = None,
    options: dict[str, object] | None = None,
    env: tuple[str, ...] = (),
) -> ModelRoute:
    return ModelRoute(
        adapter=adapter,
        api=api,
        env=env,
        options=options or {},
    )


def _model(
    model_id: str = "model",
    *,
    provider: str = "test",
    name: str | None = None,
    tool_call: bool | None = None,
    structured_output: bool | None = None,
    reasoning: bool | None = None,
) -> Model:
    return Model(
        id=model_id,
        name=name or model_id,
        _toolang=ModelToolang(provider=provider, ready=True),
        tool_call=tool_call,
        structured_output=structured_output,
        reasoning=reasoning,
    )


async def _ignore_event(_event: object) -> None:
    return None


def test_packaged_catalog_includes_mainstream_remote_providers() -> None:
    snapshot = read_model_catalog_snapshot(PACKAGED_MODEL_CATALOG)

    assert {"anthropic", "deepseek", "google", "openai", "openrouter"} <= set(
        snapshot.providers
    )
    assert snapshot.providers["deepseek"].env == ("DEEPSEEK_API_KEY",)
    assert "GOOGLE_GENERATIVE_AI_API_KEY" in snapshot.providers["google"].env
    assert snapshot.providers["openrouter"].env == ("OPENROUTER_API_KEY",)


def test_package_registers_catalogs_without_legacy_model_provider_entry_points() -> (
    None
):
    pyproject = tomllib.loads(
        (Path(__file__).parents[3] / "pyproject.toml").read_text(encoding="utf-8")
    )
    entry_points = pyproject["project"]["entry-points"]

    assert "toolang.model_provider" not in entry_points
    assert entry_points["toolang.model_catalog"] == {
        "models_dev": "toolang.plugin.catalogs.models_dev:create_model_catalog",
        "ollama": "toolang.plugin.catalogs.ollama:create_model_catalog",
        "llama_cpp": "toolang.plugin.catalogs.llama_cpp:create_model_catalog",
    }


def test_builtin_model_adapter_loader_includes_all_protocol_adapters() -> None:
    adapters = load_model_adapters()

    assert tuple(sorted(adapters)) == (
        "chat_completions",
        "generate_content",
        "messages",
        "responses",
    )


def test_decimal_unit_formatting_accepts_integer_values() -> None:
    assert _format_decimal_unit(1) == "1"


def test_messages_adapter_replays_signed_thinking_before_tool_use() -> None:
    result = messages_models.parse_message_response(
        {
            "content": [
                {
                    "type": "thinking",
                    "thinking": "I should inspect the files.",
                    "signature": "signed-thinking",
                },
                {
                    "type": "tool_use",
                    "id": "call_1",
                    "name": "fs__list",
                    "input": {"path": "."},
                },
            ],
            "usage": {"input_tokens": 10, "output_tokens": 5},
        },
        model=_model("claude", provider="anthropic"),
    )
    assert result.message is not None

    payload = messages_models.messages_payload(
        _model("claude", provider="anthropic", name="Claude").with_route(
            _route(provider="anthropic", adapter="messages", api=None, options={})
        ),
        ModelCall(
            instructions="",
            messages=[
                result.message,
                Message(
                    role="tool",
                    parts=(
                        ToolResultPart(
                            tool_call_id="call_1",
                            call_id="call_1",
                            tool_name="fs__list",
                            tool_family="fs__list",
                            output={"entries": []},
                        ),
                    ),
                ),
            ],
            continuation=result.continuation,
            max_output_tokens=4096,
        ),
        stream=False,
    )

    assistant = cast(list[dict[str, object]], payload["messages"])[0]
    content = cast(list[dict[str, object]], assistant["content"])
    assert content[0] == {
        "type": "thinking",
        "thinking": "I should inspect the files.",
        "signature": "signed-thinking",
    }
    assert content[1]["type"] == "tool_use"
    assert content[1]["id"] == "call_1"


def test_messages_adapter_requires_an_allowance_above_the_thinking_budget() -> None:
    route = _route(provider="anthropic", adapter="messages", api=None, options={})
    model = _model("claude", provider="anthropic", name="Claude")

    with pytest.raises(ToolangError, match="output allowance"):
        messages_models.messages_payload(
            model.with_route(route),
            ModelCall(
                instructions="",
                messages=[Message.user("hello")],
                reasoning=Reasoning(budget_tokens=8_000),
            ),
            stream=False,
        )

    bounded = messages_models.messages_payload(
        model.with_route(route),
        ModelCall(
            instructions="",
            messages=[Message.user("hello")],
            max_output_tokens=20_000,
        ),
        stream=False,
    )
    assert bounded["max_tokens"] == 20_000

    with pytest.raises(ToolangError, match="lower than max_tokens"):
        messages_models.messages_payload(
            model.with_route(route),
            ModelCall(
                instructions="",
                messages=[Message.user("hello")],
                max_output_tokens=4_096,
                reasoning=Reasoning(budget_tokens=8_000),
            ),
            stream=False,
        )


def test_chat_completions_adapter_invokes_openai_compatible_client(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class _Completions:
        async def create(self, **payload):
            captured["payload"] = payload
            return SimpleNamespace(
                choices=(
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content="done",
                            tool_calls=(
                                SimpleNamespace(
                                    id="call_1",
                                    function=SimpleNamespace(
                                        name="shell__execute",
                                        arguments='{"command":"pwd"}',
                                    ),
                                ),
                            ),
                        )
                    ),
                ),
                usage=SimpleNamespace(prompt_tokens=11, completion_tokens=7),
            )

    class _Client:
        chat = SimpleNamespace(completions=_Completions())

    monkeypatch.setattr(
        chat_completions_models, "create_client", lambda route, *, environ: _Client()
    )
    adapter = chat_completions_models.create_model_adapter({})
    route = _route(
        provider="deepseek",
        adapter="chat_completions",
        api=None,
        options={"temperature": 0},
    )
    model = _model("deepseek-v4-pro", provider="deepseek", name="deepseek-v4-pro")
    request = ModelCall(
        instructions="dev",
        messages=[Message.user("hello")],
        tools=(
            ToolDefinition(
                name="shell__execute",
                description="Run a shell command.",
                parameters={"type": "object"},
            ),
        ),
    )

    result = asyncio.run(adapter.invoke(model.with_route(route), request, environ={}))

    assert captured["payload"] == {
        "model": "deepseek-v4-pro",
        "messages": [
            {"role": "system", "content": "dev"},
            {
                "role": "user",
                "content": [{"type": "text", "text": "hello"}],
            },
        ],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "shell__execute",
                    "description": "Run a shell command.",
                    "parameters": {"type": "object"},
                },
            }
        ],
        "temperature": 0,
        "stream": False,
    }
    assert result.message == Message(
        role="assistant",
        parts=(
            Message.assistant("done").parts[0],
            ToolCallPart(
                tool_call_id="call_1",
                call_id="call_1",
                tool_name="shell__execute",
                tool_family="shell__execute",
                input={"command": "pwd"},
            ),
        ),
    )
    assert result.tool_calls == (
        ToolCall(
            tool_call_id="call_1",
            call_id="call_1",
            name="shell__execute",
            input={"command": "pwd"},
        ),
    )
    assert result.usage == ModelUsage(input_tokens=11, output_tokens=7)


@pytest.mark.parametrize(
    (
        "provider",
        "options",
        "reasoning",
        "expected_effort",
        "expected_extra_body",
    ),
    (
        (
            "openrouter",
            {},
            Reasoning("low"),
            None,
            {"reasoning": {"effort": "low"}},
        ),
        (
            "deepseek",
            {"extra_body": {"thinking": {"type": "disabled"}}},
            Reasoning("high"),
            "high",
            {"thinking": {"type": "enabled"}},
        ),
    ),
)
def test_chat_completions_sends_provider_reasoning_as_sdk_extra_body(
    monkeypatch,
    provider: str,
    options: dict[str, object],
    reasoning: Reasoning,
    expected_effort: str | None,
    expected_extra_body: dict[str, object],
) -> None:
    captured: dict[str, object] = {}

    class _Completions:
        async def create(
            self,
            *,
            model: str,
            messages: list[dict[str, object]],
            stream: bool,
            reasoning_effort: str | None = None,
            extra_body: dict[str, object] | None = None,
        ):
            captured.update(
                model=model,
                messages=messages,
                stream=stream,
                reasoning_effort=reasoning_effort,
                extra_body=extra_body,
            )
            return SimpleNamespace(
                choices=(SimpleNamespace(message=SimpleNamespace(content="done")),),
                usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
            )

    monkeypatch.setattr(
        chat_completions_models,
        "create_client",
        lambda route, *, environ: SimpleNamespace(
            chat=SimpleNamespace(completions=_Completions())
        ),
    )
    route = _route(
        provider=provider,
        adapter="chat_completions",
        api="https://example.com/v1",
        options=options,
    )
    model = _model("model", provider=provider, name="model")

    asyncio.run(
        chat_completions_models.invoke_chat_completion(
            model.with_route(route),
            ModelCall(
                instructions="",
                messages=[Message.user("hello")],
                reasoning=reasoning,
            ),
            environ={},
        )
    )

    assert captured["reasoning_effort"] == expected_effort
    assert captured["extra_body"] == expected_extra_body


def test_chat_completions_stream_sends_openrouter_reasoning_as_sdk_extra_body(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    class _Stream:
        async def __aiter__(self):
            yield SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop")])

        async def close(self) -> None:
            return None

    class _Completions:
        async def create(
            self,
            *,
            model: str,
            messages: list[dict[str, object]],
            stream: bool,
            stream_options: dict[str, object],
            extra_body: dict[str, object],
        ):
            captured.update(
                model=model,
                messages=messages,
                stream=stream,
                stream_options=stream_options,
                extra_body=extra_body,
            )
            return _Stream()

    monkeypatch.setattr(
        chat_completions_models,
        "create_client",
        lambda route, *, environ: SimpleNamespace(
            chat=SimpleNamespace(completions=_Completions())
        ),
    )

    asyncio.run(
        chat_completions_models.stream_chat_completion(
            _model("model", provider="openrouter", name="model").with_route(
                _route(
                    provider="openrouter",
                    adapter="chat_completions",
                    api="https://openrouter.ai/api/v1",
                    options={},
                )
            ),
            ModelCall(
                instructions="",
                messages=[Message.user("hello")],
                reasoning=Reasoning("low"),
            ),
            environ={},
            on_event=_ignore_event,
        )
    )

    assert captured["stream"] is True
    assert captured["extra_body"] == {"reasoning": {"effort": "low"}}


def test_chat_completions_adapter_replays_deepseek_reasoning_content() -> None:
    response = SimpleNamespace(
        choices=(
            SimpleNamespace(
                message=SimpleNamespace(
                    content="I need to inspect the directory.",
                    reasoning_content="The user asked for the directory, so list the current folder.",
                    tool_calls=(
                        SimpleNamespace(
                            id="call_1",
                            function=SimpleNamespace(
                                name="fs__list",
                                arguments='{"path":"."}',
                            ),
                        ),
                    ),
                )
            ),
        ),
        usage=SimpleNamespace(prompt_tokens=11, completion_tokens=7),
    )

    result = chat_completions_models.parse_chat_completion(
        response, model=_model("deepseek-v4-flash", provider="deepseek")
    )

    assert result.message is not None
    reasoning_part = next(
        part for part in result.message.parts if isinstance(part, ReasoningPart)
    )
    assert reasoning_part.text == (
        "The user asked for the directory, so list the current folder."
    )

    payload = chat_completions_models.chat_completion_payload(
        _model(
            "deepseek-v4-flash", provider="deepseek", name="deepseek-v4-flash"
        ).with_route(
            _route(
                provider="deepseek", adapter="chat_completions", api=None, options={}
            )
        ),
        ModelCall(
            instructions="",
            messages=[
                result.message,
                Message(
                    role="tool",
                    parts=(
                        ToolResultPart(
                            tool_call_id="call_1",
                            call_id="call_1",
                            tool_name="fs__list",
                            tool_family="fs__list",
                            output={"entries": []},
                        ),
                    ),
                ),
            ],
        ),
        stream=False,
    )

    assert payload["messages"][0]["reasoning_content"] == (
        "The user asked for the directory, so list the current folder."
    )
    assert payload["messages"][0]["tool_calls"][0]["function"]["name"] == "fs__list"


def test_chat_completions_adapter_rejects_tool_calls_without_names() -> None:
    raw_tool_calls = (
        SimpleNamespace(
            id=None,
            function=SimpleNamespace(name=None, arguments='{"path":"."}'),
        ),
    )

    with pytest.raises(ToolangError, match="tool call without a function name"):
        chat_completions_models.parse_tool_calls(raw_tool_calls)


@pytest.mark.parametrize("adapter", [chat_completions_models, responses_models])
@pytest.mark.parametrize("arguments", ["", "  "])
def test_empty_tool_arguments_are_an_empty_object(adapter, arguments):
    assert adapter.parse_tool_arguments(arguments) == {}


def test_chat_output_limit_precedes_json_parsing_and_preserves_usage():
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason="length",
                message=SimpleNamespace(
                    content="Working.",
                    tool_calls=[
                        SimpleNamespace(
                            id="call_1",
                            function=SimpleNamespace(
                                name="shell__execute", arguments='{"command":'
                            ),
                        )
                    ],
                ),
            )
        ],
        usage=SimpleNamespace(prompt_tokens=100, completion_tokens=4096),
    )
    with pytest.raises(ModelResponseError, match="output limit") as caught:
        chat_completions_models.parse_chat_completion(response, model=_model())
    assert caught.value.usage == ModelUsage(input_tokens=100, output_tokens=4096)
    assert caught.value.partial_text == "Working."


def test_chat_completions_stream_rejects_tool_deltas_without_names(monkeypatch) -> None:
    class _Stream:
        async def __aiter__(self):
            yield SimpleNamespace(
                choices=(
                    SimpleNamespace(
                        finish_reason="stop",
                        delta=SimpleNamespace(
                            tool_calls=(
                                SimpleNamespace(
                                    index=0,
                                    id=None,
                                    function=SimpleNamespace(
                                        name=None, arguments='{"path":"."}'
                                    ),
                                ),
                            )
                        ),
                    ),
                )
            )

        async def close(self) -> None:
            return None

    class _Completions:
        async def create(self, **payload):
            del payload
            return _Stream()

    class _Client:
        chat = SimpleNamespace(completions=_Completions())

    monkeypatch.setattr(
        chat_completions_models, "create_client", lambda route, *, environ: _Client()
    )
    adapter = chat_completions_models.create_model_adapter({})

    with pytest.raises(ToolangError, match="tool call without a function name"):
        asyncio.run(
            adapter.stream(
                _model(
                    "deepseek-reasoner", provider="deepseek", name="deepseek-reasoner"
                ).with_route(
                    _route(
                        provider="deepseek",
                        adapter="chat_completions",
                        api=None,
                        options={},
                    )
                ),
                ModelCall(instructions="", messages=[Message.user("hello")]),
                environ={},
                on_event=_ignore_event,
            )
        )


@pytest.mark.parametrize("provider", ["deepseek", "ollama", "llama_cpp"])
def test_chat_completions_stream_collects_usage(monkeypatch, provider: str) -> None:
    captured: dict[str, object] = {}
    events: list[str] = []

    async def record_event(event: object) -> None:
        await asyncio.sleep(0)
        events.append(type(event).__name__)

    class _Stream:
        async def __aiter__(self):
            yield SimpleNamespace(
                choices=(
                    SimpleNamespace(
                        finish_reason="stop",
                        delta=SimpleNamespace(
                            reasoning_content="Thinking.",
                            content=None,
                            tool_calls=(),
                        ),
                    ),
                ),
                usage=None,
            )
            yield SimpleNamespace(
                choices=(
                    SimpleNamespace(
                        finish_reason="stop",
                        delta=SimpleNamespace(
                            reasoning_content=None,
                            content="done",
                            tool_calls=(),
                        ),
                    ),
                ),
                usage=None,
            )
            yield SimpleNamespace(
                choices=(),
                usage=SimpleNamespace(prompt_tokens=13, completion_tokens=8),
            )

        async def close(self) -> None:
            return None

    class _Completions:
        async def create(self, **payload):
            captured["payload"] = payload
            return _Stream()

    class _Client:
        chat = SimpleNamespace(completions=_Completions())

    monkeypatch.setattr(
        chat_completions_models, "create_client", lambda route, *, environ: _Client()
    )
    adapter = chat_completions_models.create_model_adapter({})

    result = asyncio.run(
        adapter.stream(
            _model("test-model", provider=provider, name="test-model").with_route(
                _route(
                    provider=provider, adapter="chat_completions", api=None, options={}
                )
            ),
            ModelCall(instructions="", messages=[Message.user("hello")]),
            environ={},
            on_event=record_event,
        )
    )

    payload = cast(dict[str, object], captured["payload"])

    assert payload["stream_options"] == {"include_usage": True}
    assert result.message is not None
    assert [
        part.text for part in result.message.parts if isinstance(part, TextPart)
    ] == ["done"]
    assert [
        part.text for part in result.message.parts if isinstance(part, ReasoningPart)
    ] == ["Thinking."]
    assert result.usage == ModelUsage(input_tokens=13, output_tokens=8)
    assert events.count("ModelPartStart") == events.count("ModelPartEnd") == 2
    assert events.count("ModelPartDelta") == (2 if provider == "deepseek" else 1)


def test_responses_adapter_rejects_openai_audio_inputs_for_non_audio_models(
    monkeypatch,
) -> None:
    def fail_invoke_response(*args, **kwargs):
        raise AssertionError("responses.invoke_response should not be called")

    monkeypatch.setattr(responses_models, "invoke_response", fail_invoke_response)
    adapter = responses_models.create_model_adapter({})
    route = _route(provider="openai", adapter="responses", api=None, options={})
    model = _model("gpt-5", provider="openai", name="gpt-5")
    request = ModelCall(
        instructions="dev",
        messages=[
            Message(
                role="user",
                parts=(
                    Message.user("hello").parts[0],
                    AudioPart(data="ZGF0YQ==", format="mp3"),
                ),
            )
        ],
    )

    with pytest.raises(
        ToolangError, match="audio input is not supported for OpenAI model 'gpt-5'"
    ):
        asyncio.run(adapter.invoke(model.with_route(route), request, environ={}))


def test_responses_adapter_rejects_openai_audio_inputs_for_non_audio_models_in_streaming(
    monkeypatch,
) -> None:
    def fail_stream_response(*args, **kwargs):
        raise AssertionError("responses.stream_response should not be called")

    monkeypatch.setattr(responses_models, "stream_response", fail_stream_response)
    adapter = responses_models.create_model_adapter({})
    route = _route(provider="openai", adapter="responses", api=None, options={})
    model = _model("gpt-5", provider="openai", name="gpt-5")
    request = ModelCall(
        instructions="dev",
        messages=[
            Message(
                role="user",
                parts=(
                    Message.user("hello").parts[0],
                    AudioPart(data="ZGF0YQ==", format="mp3"),
                ),
            )
        ],
    )

    with pytest.raises(
        ToolangError, match="audio input is not supported for OpenAI model 'gpt-5'"
    ):
        asyncio.run(
            adapter.stream(
                model.with_route(route), request, environ={}, on_event=_ignore_event
            )
        )


def test_responses_payload_uses_typed_input_items() -> None:
    payload = response_payload(
        _model("openai/gpt-5", provider="openrouter", name="gpt-5").with_route(
            _route(provider="openrouter", adapter="responses", api=None, options={})
        ),
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
                            tool_name="shell__execute",
                            tool_family="shell__execute",
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
                            tool_name="shell__execute",
                            tool_family="shell__execute",
                            output={"ok": True, "stdout": "/tmp"},
                        ),
                    ),
                ),
                Message.assistant("done"),
            ],
        ),
        stateful=False,
    )

    assert payload["input"] == [
        {
            "type": "message",
            "role": "developer",
            "content": [{"type": "input_text", "text": "dev"}],
        },
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "hello"}],
        },
        {
            "type": "function_call",
            "id": "fc_1",
            "call_id": "call_1",
            "name": "shell__execute",
            "arguments": '{"command":"pwd"}',
        },
        {
            "type": "function_call_output",
            "call_id": "call_1",
            "output": '{"ok":true,"name":"shell__execute","output":{"ok":true,"stdout":"/tmp"}}',
        },
        {
            "type": "message",
            "role": "assistant",
            "id": "msg_3",
            "status": "completed",
            "content": [{"type": "output_text", "text": "done"}],
        },
    ]


def test_protocol_payloads_apply_normalized_reasoning_controls() -> None:
    request = ModelCall(
        instructions="",
        messages=[Message.user("hello")],
        reasoning=Reasoning("high"),
    )
    route = _route(provider="openai", adapter="responses", api=None, options={})
    model = _model("gpt-5", provider="openai", name="gpt-5")

    responses_payload = response_payload(
        model.with_route(route), request, stateful=False
    )
    chat_payload = chat_completions_models.chat_completion_payload(
        model.with_route(route),
        request,
        stream=False,
    )

    assert responses_payload["reasoning"] == {"effort": "high"}
    assert chat_payload["reasoning_effort"] == "high"


def test_protocol_usage_normalizes_cache_reasoning_audio_and_reported_cost() -> None:
    chat = chat_completions_models.chat_usage(
        SimpleNamespace(
            usage=SimpleNamespace(
                prompt_tokens=100,
                completion_tokens=40,
                prompt_tokens_details=SimpleNamespace(
                    cached_tokens=60,
                    audio_tokens=10,
                ),
                completion_tokens_details=SimpleNamespace(
                    reasoning_tokens=30,
                    audio_tokens=5,
                ),
                cost="0.03",
            )
        )
    )
    responses = responses_models.response_usage(
        SimpleNamespace(
            usage=SimpleNamespace(
                input_tokens=100,
                output_tokens=40,
                input_tokens_details=SimpleNamespace(cached_tokens=60),
                output_tokens_details=SimpleNamespace(reasoning_tokens=30),
            )
        )
    )

    assert chat == ModelUsage(
        input_tokens=100,
        output_tokens=40,
        input_uncached_tokens=40,
        input_cache_read_tokens=60,
        input_audio_tokens=10,
        output_visible_tokens=10,
        output_reasoning_tokens=30,
        output_audio_tokens=5,
        reported_cost=0.03,
        reported_currency="USD",
    )
    assert responses == ModelUsage(
        input_tokens=100,
        output_tokens=40,
        input_uncached_tokens=40,
        input_cache_read_tokens=60,
        output_visible_tokens=10,
        output_reasoning_tokens=30,
    )


def test_execute_run_input_reuses_provider_state_for_followups() -> None:
    provider = _FakeModels(
        name="openai",
        responses=[
            ModelCallResult(
                message=Message(
                    role="assistant",
                    parts=(
                        ToolCallPart(
                            tool_call_id="tool-1",
                            call_id="call-1",
                            tool_name="shell__execute",
                            tool_family="shell__execute",
                            input={"command": "pwd"},
                        ),
                    ),
                ),
                tool_calls=(
                    ToolCall(
                        tool_call_id="tool-1",
                        call_id="call-1",
                        name="shell__execute",
                        input={"command": "pwd"},
                    ),
                ),
                continuation={"previous_response_id": "resp-1", "baseline_count": 2},
            ),
            ModelCallResult(message=Message.assistant("done")),
        ],
    )
    route = _route(provider=provider.name, adapter="responses", api=None, options={})
    model = _model("gpt-5", provider=provider.name, name="gpt-5")

    result = _run_agic(_prepared_agic(provider, route, model))

    assert result == Message.assistant("done")
    assert provider.requests[0].continuation is None
    assert provider.requests[1].continuation == {
        "previous_response_id": "resp-1",
        "baseline_count": 2,
    }
    assert [item.to_data() for item in provider.requests[1].messages] == [
        {"role": "user", "parts": [{"type": "text", "text": "hello"}]},
        {
            "role": "assistant",
            "parts": [
                {
                    "type": "tool_call",
                    "tool_call_id": "tool-1",
                    "call_id": "call-1",
                    "tool_name": "shell__execute",
                    "tool_family": "shell__execute",
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
                    "tool_name": "shell__execute",
                    "tool_family": "shell__execute",
                    "output": {"ok": True, "stdout": "ran:pwd"},
                }
            ],
        },
    ]


def test_execute_run_input_appends_provider_messages_for_stateless_providers() -> None:
    provider = _FakeModels(
        name="ollama",
        responses=[
            ModelCallResult(
                message=Message(
                    role="assistant",
                    parts=(
                        ToolCallPart(
                            tool_call_id="tool-1",
                            call_id="call-1",
                            tool_name="shell__execute",
                            tool_family="shell__execute",
                            input={"command": "pwd"},
                        ),
                    ),
                ),
                tool_calls=(
                    ToolCall(
                        tool_call_id="tool-1",
                        call_id="call-1",
                        name="shell__execute",
                        input={"command": "pwd"},
                    ),
                ),
            ),
            ModelCallResult(message=Message.assistant("done")),
        ],
    )
    route = _route(provider=provider.name, adapter="responses", api=None, options={})
    model = _model("qwen3", provider=provider.name, name="qwen3")

    result = _run_agic(_prepared_agic(provider, route, model))

    assert result == Message.assistant("done")
    assert provider.requests[0].continuation is None
    assert provider.requests[1].continuation is None
    assert [item.to_data() for item in provider.requests[1].messages] == [
        {"role": "user", "parts": [{"type": "text", "text": "hello"}]},
        {
            "role": "assistant",
            "parts": [
                {
                    "type": "tool_call",
                    "tool_call_id": "tool-1",
                    "call_id": "call-1",
                    "tool_name": "shell__execute",
                    "tool_family": "shell__execute",
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
                    "tool_name": "shell__execute",
                    "tool_family": "shell__execute",
                    "output": {"ok": True, "stdout": "ran:pwd"},
                }
            ],
        },
    ]


def test_agic_omits_tools_for_model_without_tool_support() -> None:
    provider = _FakeModels(
        name="ollama",
        responses=[ModelCallResult(message=Message.assistant("done"))],
    )
    route = _route(provider=provider.name, adapter="responses", api=None, options={})
    model = _model("gemma4:latest", provider=provider.name, name="gemma4:latest")

    result = _run_agic(
        replace(
            _prepared_agic(provider, route, model),
            tools=load_tools(queries=("_toolang/*",)),
        )
    )

    assert result == Message.assistant("done")
    assert provider.requests[0].tools == ()


@pytest.mark.parametrize("model_tools", [False, True])
@pytest.mark.parametrize("repairing", [False, True])
def test_model_call_keeps_content_separate_and_schema_detached(
    model_tools: bool, repairing: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _FakeModels(name="test")
    route = _route(provider="test", adapter="responses", api=None, options={})
    model = _model("model", provider="test", name="model", tool_call=model_tools)
    prepared = replace(
        _prepared_agic(provider, route, model),
        instructions="Resident instructions {{literal}}",
        output_budget=123,
    )
    schema: dict[str, object] = {"type": "array", "items": {"type": "string"}}
    continuation = {"adapter": {"id": "response-1"}}
    state = _AgicState(
        prepared=prepared,
        layout=prepared.run.setup.layout,
        emit=_ignore_event,
        pending_inputs=tuple,
        steer_before_next_step=lambda: False,
        immediate_steer=lambda: False,
        before_call=lambda: None,
        messages=MessageBuffer(),
        output_binding=_OutputBinding(type_name="Text[]", output_schema=schema),
        continuation=continuation,
        repairing_output=repairing,
    )
    definition = ToolDefinition(
        "shell__execute", "Unique description", {"type": "object"}
    )
    calls = []

    def tool_definition():
        calls.append(True)
        return definition

    monkeypatch.setattr(prepared.tools["shell__execute"], "definition", tool_definition)
    _, buffer, _, request, recorded = _candidate(
        state, prepared.run.state, prepared.run.state_ref
    )

    assert request.instructions == prepared.instructions
    assert request.messages == list(prepared.inputs.rendered_input[1])
    assert request.messages is not buffer.messages
    assert request.messages[0] is buffer.messages[0]
    assert recorded.head == StepRef.from_local(prepared.run.run_id, (0,))
    assert len(recorded.delta) == len(request.messages)
    assert request.tools == ((definition,) if model_tools and not repairing else ())
    assert len(calls) == int(model_tools and not repairing)
    if request.tools:
        assert request.tools[0] is definition
    assert request.output_schema == schema
    assert request.output_schema is not schema
    assert request.output_schema is not None
    assert request.output_schema["items"] is not schema["items"]
    assert request.continuation is continuation
    assert request.max_output_tokens == 123


def test_responses_adapter_logs_api_request_and_response_at_debug(
    caplog, monkeypatch
) -> None:
    class _FakeResponse:
        id = "resp_123"
        output_text = "done"
        output = ()
        usage = SimpleNamespace(input_tokens=11, output_tokens=7)

        def model_dump(self, *, mode="json", exclude_none=True) -> dict[str, object]:
            del mode, exclude_none
            return {
                "id": self.id,
                "output_text": self.output_text,
                "usage": {
                    "input_tokens": self.usage.input_tokens,
                    "output_tokens": self.usage.output_tokens,
                },
            }

    captured: dict[str, object] = {}

    class _FakeResponses:
        async def create(self, **kwargs):
            captured["payload"] = kwargs
            return _FakeResponse()

    monkeypatch.setattr(
        responses_models,
        "create_client",
        lambda route, *, environ: SimpleNamespace(responses=_FakeResponses()),
    )
    route = _route(
        provider="openai",
        adapter="responses",
        api="https://api.openai.com/v1",
        options={},
    )
    model = _model("gpt-5", provider="openai", name="gpt-5")
    request = ModelCall(
        instructions="Rewrite the input.",
        messages=[Message.user("hello")],
    )

    with caplog.at_level(
        logging.DEBUG,
        logger="toolang.plugin.adapters.responses",
    ):
        result = asyncio.run(
            responses_models.invoke_response(
                model.with_route(route),
                request,
                stateful=True,
                environ={},
            )
        )

    assert result.message == Message.assistant("done")
    assert result.usage == ModelUsage(input_tokens=11, output_tokens=7)
    assert captured["payload"] == response_payload(
        model.with_route(route), request, stateful=True
    )
    assert "adapter.request provider=openai ref=openai/gpt-5" in caplog.text
    assert '"model": "gpt-5"' in caplog.text
    assert '"text": "Rewrite the input."' in caplog.text
    assert "adapter.result provider=openai ref=openai/gpt-5" in caplog.text
    assert '"id": "resp_123"' in caplog.text
    assert '"output_text": "done"' in caplog.text
    assert "secret" not in caplog.text


def test_agic_logs_model_and_tool_io_at_debug(caplog) -> None:
    provider = _FakeModels(
        name="openai",
        responses=[
            ModelCallResult(
                message=Message(
                    role="assistant",
                    parts=(
                        ToolCallPart(
                            tool_call_id="tool-1",
                            call_id="call-1",
                            tool_name="shell__execute",
                            tool_family="shell__execute",
                            input={"command": "pwd"},
                        ),
                    ),
                ),
                tool_calls=(
                    ToolCall(
                        tool_call_id="tool-1",
                        call_id="call-1",
                        name="shell__execute",
                        input={"command": "pwd"},
                    ),
                ),
                usage=ModelUsage(input_tokens=11, output_tokens=7),
                continuation={"previous_response_id": "resp-1"},
            ),
            ModelCallResult(
                message=Message.assistant("done"),
                usage=ModelUsage(input_tokens=13, output_tokens=3),
            ),
        ],
    )

    with caplog.at_level(
        logging.DEBUG,
        logger="toolang.execution.executor.diagnostics",
    ):
        result = _run_agic(
            _prepared_agic(
                provider,
                _route(
                    provider=provider.name, adapter="responses", api=None, options={}
                ),
                _model("gpt-5", provider=provider.name, name="gpt-5"),
            )
        )

    assert result == Message.assistant("done")
    assert "model.request thread=thread-1 run=run_1 step=0 instructions=" in caplog.text
    assert '"command": "pwd"' in caplog.text
    assert "model.result thread=thread-1 run=run_1 step=0 message=" in caplog.text
    assert '"output_tokens": 7' in caplog.text
    assert "tool.request thread=thread-1 run=run_1 step=1 plugin=" in caplog.text
    assert "tool=shell__execute" in caplog.text
    assert "tool.result thread=thread-1 run=run_1 step=1 plugin=" in caplog.text
    assert '"stdout": "ran:pwd"' in caplog.text


def test_chat_completions_encode_multimodal_user_parts() -> None:
    encoded = chat_completions_models.encode_message(
        _model().with_route(
            _route(provider="openai", adapter="chat_completions", api=None, options={})
        ),
        Message(
            role="user",
            parts=(
                Message.user("describe").parts[0],
                ImagePart(
                    image_url="https://example.com/image.png",
                    detail="high",
                ),
                AudioPart(data="ZGF0YQ==", format="mp3"),
                DocumentPart(
                    data="data:application/pdf;base64,JVBERi0xLjc=",
                    filename="report.pdf",
                ),
            ),
        ),
    )

    assert encoded == {
        "role": "user",
        "content": [
            {"type": "text", "text": "describe"},
            {
                "type": "image_url",
                "image_url": {
                    "url": "https://example.com/image.png",
                    "detail": "high",
                },
            },
            {
                "type": "input_audio",
                "input_audio": {"data": "ZGF0YQ==", "format": "mp3"},
            },
            {
                "type": "file",
                "file": {
                    "file_data": "data:application/pdf;base64,JVBERi0xLjc=",
                    "filename": "report.pdf",
                },
            },
        ],
    }


def test_chat_completions_reject_document_url() -> None:
    route = _route(provider="openai", adapter="chat_completions", api=None, options={})

    with pytest.raises(
        ToolangError,
        match="does not accept a URL",
    ):
        chat_completions_models.encode_message(
            _model().with_route(route),
            Message(
                role="user",
                parts=(DocumentPart(url="https://example.com/report.pdf"),),
            ),
        )


def test_chat_completions_audio_response_keeps_transcript_on_audio_part() -> None:
    result = chat_completions_models.parse_chat_completion(
        SimpleNamespace(
            choices=(
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=None,
                        audio=SimpleNamespace(
                            data="ZGF0YQ==",
                            transcript="hello",
                        ),
                        tool_calls=(),
                    )
                ),
            ),
            usage=None,
        ),
        audio_format="mp3",
        model=_model("test"),
    )

    assert result.message == Message(
        role="assistant",
        parts=(
            AudioPart(
                data="ZGF0YQ==",
                format="mp3",
                transcript="hello",
            ),
        ),
    )


def test_chat_completions_replays_assistant_multimodal_output_as_text() -> None:
    encoded = chat_completions_models.encode_message(
        _model().with_route(
            _route(provider="openai", adapter="chat_completions", api=None, options={})
        ),
        Message(
            role="assistant",
            parts=(
                ImagePart(
                    image_url="data:image/png;base64,aW1hZ2U=",
                    filename="chart.png",
                ),
                AudioPart(
                    data="ZGF0YQ==",
                    format="mp3",
                    transcript="spoken result",
                ),
                DocumentPart(file_id="file-1", filename="report.pdf"),
            ),
        ),
    )

    assert encoded == {
        "role": "assistant",
        "content": "[image:chart.png]\nspoken result\n[document:report.pdf]",
    }


def test_chat_completions_audio_stream_does_not_open_duplicate_text_part(
    monkeypatch,
) -> None:
    events: list[object] = []

    async def record_event(event: object) -> None:
        events.append(event)

    class _Stream:
        async def __aiter__(self):
            yield SimpleNamespace(
                choices=(
                    SimpleNamespace(
                        finish_reason="stop",
                        delta=SimpleNamespace(
                            reasoning_content=None,
                            content="hello",
                            audio=SimpleNamespace(
                                data="ZGF0YQ==",
                                transcript="hello",
                            ),
                            tool_calls=(),
                        ),
                    ),
                ),
                usage=None,
            )

        async def close(self) -> None:
            return None

    class _Completions:
        async def create(self, **payload):
            del payload
            return _Stream()

    class _Client:
        chat = SimpleNamespace(completions=_Completions())

    monkeypatch.setattr(
        chat_completions_models, "create_client", lambda route, *, environ: _Client()
    )

    result = asyncio.run(
        chat_completions_models.stream_chat_completion(
            _model("gpt-audio", provider="openai", name="gpt-audio").with_route(
                _route(
                    provider="openai",
                    adapter="chat_completions",
                    api=None,
                    options={
                        "modalities": ["text", "audio"],
                        "audio": {"format": "mp3", "voice": "alloy"},
                    },
                )
            ),
            ModelCall(instructions="", messages=[Message.user("hello")]),
            environ={},
            on_event=record_event,
        )
    )

    assert result.message == Message(
        role="assistant",
        parts=(
            AudioPart(
                data="ZGF0YQ==",
                format="mp3",
                transcript="hello",
            ),
        ),
    )
    assert [
        (type(event).__name__, getattr(event, "kind", None)) for event in events
    ] == [
        ("ModelPartStart", "audio"),
        ("ModelPartEnd", None),
    ]


def test_responses_encode_message_preserves_structured_content() -> None:
    encoded = encode_message(
        Message(role="user", parts=(Message.user("hello").parts[0],))
    )

    assert encoded == {
        "type": "message",
        "role": "user",
        "content": [
            {
                "type": "input_text",
                "text": "hello",
            }
        ],
    }


def test_responses_encode_message_supports_multimodal_user_parts() -> None:
    encoded = encode_message(
        Message(
            role="user",
            parts=(
                Message.user("describe this").parts[0],
                ImagePart(image_url="https://example.com/image.png", detail="high"),
                AudioPart(data="ZGF0YQ==", format="mp3"),
                DocumentPart(
                    url="https://example.com/report.pdf",
                    filename="report.pdf",
                ),
            ),
        )
    )

    assert encoded == {
        "type": "message",
        "role": "user",
        "content": [
            {"type": "input_text", "text": "describe this"},
            {
                "type": "input_image",
                "image_url": "https://example.com/image.png",
                "detail": "high",
            },
            {
                "type": "input_audio",
                "input_audio": {"data": "ZGF0YQ==", "format": "mp3"},
            },
            {
                "type": "input_file",
                "file_url": "https://example.com/report.pdf",
                "filename": "report.pdf",
            },
        ],
    }


def test_audio_part_accepts_data_url_in_data_field() -> None:
    part = Message.from_data(
        {
            "role": "user",
            "parts": [
                {
                    "type": "audio",
                    "data": "data:audio/mpeg;base64,ZGF0YQ==",
                }
            ],
        }
    ).parts[0]

    assert isinstance(part, AudioPart)
    assert part.data == "ZGF0YQ=="
    assert part.format == "mp3"
    assert part.media_type == "audio/mpeg"


def test_document_part_preserves_data_url() -> None:
    part = Message.from_data(
        {
            "role": "user",
            "parts": [
                {
                    "type": "document",
                    "data": "data:application/pdf;base64,JVBERi0xLjc=",
                    "filename": "report.pdf",
                }
            ],
        }
    ).parts[0]

    assert isinstance(part, DocumentPart)
    assert part.data == "data:application/pdf;base64,JVBERi0xLjc="
    assert part.media_type == "application/pdf"
    assert part.filename == "report.pdf"


def test_responses_audio_response_keeps_transcript_on_audio_part() -> None:
    result = responses_models.assistant_message(
        SimpleNamespace(
            output=(
                SimpleNamespace(
                    type="message",
                    content=(
                        SimpleNamespace(
                            type="output_audio",
                            data="ZGF0YQ==",
                            format="wav",
                            transcript="hello",
                        ),
                    ),
                ),
            ),
            output_text="hello",
        ),
        tool_calls=(),
        model=_model("test"),
    )

    assert result == Message(
        role="assistant",
        parts=(
            AudioPart(
                data="ZGF0YQ==",
                format="wav",
                transcript="hello",
            ),
        ),
    )


def test_responses_image_generation_output_becomes_image_part() -> None:
    result = responses_models.assistant_message(
        SimpleNamespace(
            output=(
                SimpleNamespace(
                    type="image_generation_call",
                    result="aW1hZ2U=",
                ),
            ),
            output_text="",
        ),
        tool_calls=(),
        model=_model("test"),
    )

    assert result == Message(
        role="assistant",
        parts=(
            ImagePart(
                image_url="data:image/png;base64,aW1hZ2U=",
                media_type="image/png",
            ),
        ),
    )


def test_agic_preserves_multimodal_steer_and_model_output() -> None:
    image = ImagePart(file_id="image-1")
    audio = AudioPart(
        data="ZGF0YQ==",
        format="wav",
        transcript="done",
    )
    steer = Message(
        role="user",
        parts=(Message.user("inspect").parts[0], image),
    )
    provider = _FakeModels(
        name="openai",
        responses=[
            ModelCallResult(
                message=Message(role="assistant", parts=(audio,)),
            ),
        ],
    )
    prepared = _prepared_agic(
        provider,
        _route(provider="openai", adapter="chat_completions", api=None, options={}),
        _model("gpt-audio", provider="openai", name="gpt-audio"),
    )
    pending = [
        ControlRecord(
            id="run_1@1",
            kind="steer",
            timing="next_call",
            payload=SteerControlPayload(
                CallInput({"_": Array("Part[]", tuple(steer.parts))})
            ),
        )
    ]
    events: list[RunEvent] = []

    async def emit(event: RunEvent) -> None:
        events.append(event)

    def pending_inputs() -> tuple[ControlRecord, ...]:
        current = tuple(pending)
        pending.clear()
        return current

    result = asyncio.run(
        _execute(
            _AgicState(
                prepared=prepared,
                layout=AgentLayout.resident(Path("/tmp"), "home"),
                emit=emit,
                pending_inputs=pending_inputs,
                steer_before_next_step=lambda: False,
                immediate_steer=lambda: False,
                before_call=lambda: None,
                messages=MessageBuffer(prepared.inputs.rendered_input[1]),
            )
        )
    )

    assert result == Message(role="assistant", parts=(audio,))
    assert provider.requests[0].messages[-1] == Message(
        "user",
        (
            TextPart(
                '<toolang:steer description="The user supplied updated input for the current task.">'
            ),
            *steer.parts,
            TextPart("</toolang:steer>"),
        ),
    )
    step_end = next(event for event in events if isinstance(event, StepEnd))
    assert step_end.output == Output(Local.typed("Part[]", (audio,)), "_")
    assert [event.type for event in events] == [
        "step_begin",
        "part_begin",
        "part_end",
        "step_end",
    ]


def test_agic_commits_steer_messages_after_step_begin() -> None:
    steer = Message.user("inspect")
    provider = _FakeModels(
        name="openai",
        responses=[ModelCallResult(message=Message.assistant("unused"))],
    )
    prepared = _prepared_agic(
        provider,
        _route(provider="openai", adapter="chat_completions", api=None, options={}),
        _model("gpt-test", provider="openai", name="gpt-test"),
    )
    control = ControlRecord(
        id="run_1@1",
        kind="steer",
        timing="next_call",
        payload=SteerControlPayload(
            CallInput({"_": Array("Part[]", tuple(steer.parts))})
        ),
    )
    original_messages = list(prepared.inputs.rendered_input[1])

    async def emit(_event: RunEvent) -> None:
        assert state.messages.messages == original_messages
        raise RuntimeError("step begin persistence failed")

    state = _AgicState(
        prepared=prepared,
        layout=AgentLayout.resident(Path("/tmp"), "home"),
        emit=emit,
        pending_inputs=lambda: (control,),
        steer_before_next_step=lambda: False,
        immediate_steer=lambda: False,
        before_call=lambda: None,
        messages=MessageBuffer(original_messages),
    )

    with pytest.raises(RuntimeError, match="step begin persistence failed"):
        asyncio.run(_execute(state))

    assert state.messages.messages == original_messages
    assert provider.requests == []


def test_responses_replays_assistant_multimodal_output_as_text() -> None:
    assert encode_message(
        Message(
            role="assistant",
            parts=(
                ImagePart(
                    image_url="data:image/png;base64,aW1hZ2U=",
                    filename="chart.png",
                ),
                AudioPart(
                    data="ZGF0YQ==",
                    format="wav",
                    transcript="spoken result",
                ),
                DocumentPart(file_id="file-1", filename="report.pdf"),
            ),
        )
    ) == {
        "type": "message",
        "role": "assistant",
        "content": [
            {"type": "output_text", "text": "[image:chart.png]"},
            {"type": "output_text", "text": "spoken result"},
            {"type": "output_text", "text": "[document:report.pdf]"},
        ],
        "id": "msg_current",
        "status": "completed",
    }


def test_responses_non_audio_model_accepts_assistant_audio_history(
    monkeypatch,
) -> None:
    async def fake_invoke_response(model, request, *, stateful, environ):
        del model, request, stateful, environ
        return ModelCallResult(message=Message.assistant("done"))

    monkeypatch.setattr(
        responses_models,
        "invoke_response",
        fake_invoke_response,
    )
    adapter = responses_models.create_model_adapter({})
    result = asyncio.run(
        adapter.invoke(
            _model("gpt-5", provider="openai", name="gpt-5").with_route(
                _route(provider="openai", adapter="responses", api=None, options={})
            ),
            ModelCall(
                instructions="",
                messages=[
                    Message(
                        role="assistant",
                        parts=(
                            AudioPart(
                                data="ZGF0YQ==",
                                format="wav",
                                transcript="previous",
                            ),
                        ),
                    ),
                    Message.user("continue"),
                ],
            ),
            environ={},
        )
    )

    assert result.message == Message.assistant("done")


def test_responses_audio_stream_does_not_open_duplicate_text_part(
    monkeypatch,
) -> None:
    events: list[object] = []

    async def record_event(event: object) -> None:
        events.append(event)

    response = SimpleNamespace(
        id="resp-1",
        output=(
            SimpleNamespace(
                type="message",
                content=(
                    SimpleNamespace(
                        type="output_audio",
                        data="ZGF0YQ==",
                        format="wav",
                        transcript="hello",
                    ),
                ),
            ),
        ),
        output_text="hello",
        usage=None,
    )

    class _Stream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            del exc_type, exc, traceback

        async def __aiter__(self):
            yield SimpleNamespace(
                type="response.output_text.delta",
                delta="hello",
            )

            yield SimpleNamespace(type="response.completed", response=response)

    class _Responses:
        def stream(self, **payload):
            del payload
            return _Stream()

    class _Client:
        responses = _Responses()

    monkeypatch.setattr(
        responses_models, "create_client", lambda route, *, environ: _Client()
    )

    result = asyncio.run(
        responses_models.stream_response(
            _model("gpt-audio", provider="openai", name="gpt-audio").with_route(
                _route(provider="openai", adapter="responses", api=None, options={})
            ),
            ModelCall(instructions="", messages=[Message.user("hello")]),
            stateful=True,
            environ={},
            on_event=record_event,
        )
    )

    assert result.message == Message(
        role="assistant",
        parts=(
            AudioPart(
                data="ZGF0YQ==",
                format="wav",
                transcript="hello",
            ),
        ),
    )
    assert [
        (type(event).__name__, getattr(event, "kind", None)) for event in events
    ] == [
        ("ModelPartStart", "audio"),
        ("ModelPartEnd", None),
    ]


def test_responses_encode_historical_tool_items_without_previous_response_id() -> None:
    payload = response_payload(
        _model("gpt-5", provider="openai", name="gpt-5").with_route(
            _route(provider="openai", adapter="responses", api=None, options={})
        ),
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
                            tool_name="shell__execute",
                            tool_family="shell__execute",
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
                            tool_name="shell__execute",
                            tool_family="shell__execute",
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
        {
            "type": "message",
            "role": "developer",
            "content": [{"type": "input_text", "text": "dev"}],
        },
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "hello"}],
        },
        {
            "type": "function_call",
            "id": "fc_1",
            "call_id": "call_1",
            "name": "shell__execute",
            "arguments": '{"command":"pwd"}',
        },
        {
            "type": "function_call_output",
            "call_id": "call_1",
            "output": '{"ok":true,"name":"shell__execute","output":{"ok":true,"stdout":"/tmp"}}',
        },
        {
            "type": "message",
            "role": "assistant",
            "id": "msg_3",
            "status": "completed",
            "content": [{"type": "output_text", "text": "done"}],
        },
    ]


def test_responses_previous_response_id_replays_tool_output_without_item_id() -> None:
    request = ModelCall(
        instructions="dev",
        messages=[
            Message.user("hello"),
            Message(
                role="assistant",
                parts=(
                    ToolCallPart(
                        tool_call_id="fc_1",
                        call_id="call_1",
                        tool_name="shell__execute",
                        tool_family="shell__execute",
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
                        tool_name="shell__execute",
                        tool_family="shell__execute",
                        output={"ok": True, "stdout": "/tmp"},
                    ),
                ),
            ),
        ],
    )
    request = replace(
        request,
        continuation=responses_models.response_continuation(
            SimpleNamespace(id="resp_1"),
            request=replace(request, messages=request.messages[:1]),
            emitted_message=request.messages[1],
            stateful=True,
        ),
    )
    payload = response_payload(
        _model("gpt-5", provider="openai", name="gpt-5").with_route(
            _route(provider="openai", adapter="responses", api=None, options={})
        ),
        request,
        stateful=True,
    )

    assert payload["previous_response_id"] == "resp_1"
    assert payload["input"] == [
        {
            "type": "function_call_output",
            "call_id": "call_1",
            "output": '{"ok":true,"name":"shell__execute","output":{"ok":true,"stdout":"/tmp"}}',
        }
    ]


def _prepared_agic(
    provider: _FakeModels,
    route: ModelRoute,
    model: Model,
) -> _AgicFrame:
    tool = _FakeTool()
    state = SimpleNamespace(
        program=Program(span=Span(1)),
        fingerprint="live-1",
        revision="live-1",
        workspaces={},
    )
    from toolang.execution.runnables import AgicRoutes

    return _AgicFrame(
        run=BoundRun(
            run_id="run_1",
            root_run_id="run_1",
            thread="thread-1",
            bindings=RunBindings(runnable="agic:main"),
            input=RunnableInput(),
            control_input=CallInput({}),
            state=cast(Any, state),
            state_ref=ControlRef.for_run("run_1", 0),
            setup=AgentSetup(
                revision="test-setup",
                layout=AgentLayout.resident(Path("/"), "alice"),
                providers={},
                adapters={},
                models=ModelCollection(),
                tools=ToolCollection.from_tools({tool.name: tool}),
                envs={},
            ),
            resources=AgentResources(
                tools=(AgentToolResource(tool.name, "shell", "shell", "execute"),)
            ),
            created_at="2026-04-10T00:00:00Z",
        ),
        agic=AgicDecl(
            name="main",
            input=Parameter(name="_", span=Span(1)),
            messages=(
                AstMessage(
                    role="user", content="Reply directly.", explicit=False, span=Span(1)
                ),
            ),
            span=Span(1),
        ),
        model=model.with_route(route),
        adapter=provider,
        environ={},
        instructions="",
        inputs=cast(
            Any,
            SimpleNamespace(
                rendered_input=("", (Message.user("hello"),), ()), runnables=()
            ),
        ),
        tools={tool.name: tool},
        routes=AgicRoutes(),
        services=(),
    )


def _run_agic(prepared: _AgicFrame) -> Message | None:
    return asyncio.run(
        _execute(
            _AgicState(
                prepared=prepared,
                layout=AgentLayout.resident(Path("/tmp"), "home"),
                emit=_ignore_event,
                pending_inputs=tuple,
                steer_before_next_step=lambda: False,
                immediate_steer=lambda: False,
                before_call=lambda: None,
                messages=MessageBuffer(prepared.inputs.rendered_input[1]),
            )
        )
    )


@pytest.mark.parametrize("options", [None, ()])
@pytest.mark.parametrize(
    "control",
    [None, Reasoning("high"), Reasoning("none"), Reasoning(budget_tokens=8192)],
)
def test_missing_reasoning_controls_allow_explicit_attempts(options, control):
    model = Model(
        id="m",
        name="M",
        _toolang=ModelToolang(provider="third_party"),
        reasoning=True,
        reasoning_options=options,
    )
    assert resolve_model_reasoning(model, control) == control


def test_explicit_reasoning_budget_respects_known_bounds():
    model = _reasoning_model([{"type": "budget_tokens", "min": 1024, "max": 4096}])
    assert resolve_model_reasoning(model, Reasoning(budget_tokens=2048)) == Reasoning(
        budget_tokens=2048
    )
    for value in (512, 8192):
        with pytest.raises(ToolangError, match="reasoning budget"):
            resolve_model_reasoning(model, Reasoning(budget_tokens=value))


@pytest.mark.parametrize("protocol", ["chat", "responses"])
@pytest.mark.parametrize(
    ("name", "arguments", "kind"),
    [
        ("lookup", "", None),
        ("lookup", " ", None),
        ("lookup", '{"path":', "invalid_json"),
        ("lookup", "[]", "non_object_arguments"),
        ("lookup", "null", "non_object_arguments"),
        ("", "{}", "missing_name"),
    ],
)
def test_tool_argument_response_classification(protocol, name, arguments, kind) -> None:
    if protocol == "chat":
        response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content="partial",
                        tool_calls=[
                            SimpleNamespace(
                                id="call",
                                function=SimpleNamespace(
                                    name=name, arguments=arguments
                                ),
                            )
                        ],
                    )
                )
            ],
            usage=SimpleNamespace(prompt_tokens=3, completion_tokens=7),
        )

        def parse():
            return chat_completions_models.parse_chat_completion(
                response, model=_model()
            )
    else:
        response = SimpleNamespace(
            output=[
                SimpleNamespace(
                    type="function_call",
                    id="call",
                    call_id="call",
                    name=name,
                    arguments=arguments,
                )
            ],
            usage=SimpleNamespace(input_tokens=3, output_tokens=7),
        )

        def parse():
            return responses_models.parse_response(
                response,
                model=_model(),
                request=ModelCall(instructions="", messages=[]),
                stateful=False,
            )

    if kind is None:
        assert parse().tool_calls[0].input == {}
    else:
        with pytest.raises(ModelResponseError) as caught:
            parse()
        assert caught.value.kind == kind
        assert caught.value.usage == ModelUsage(3, 7)


@pytest.mark.parametrize("protocol", ["chat", "responses"])
@pytest.mark.parametrize(
    "ending", ["truncated", "eof", "network", "rejected", "malformed"]
)
def test_stream_failure_retains_usage_and_does_not_emit_tool_end(
    monkeypatch, protocol, ending
) -> None:
    import httpx
    from toolang.base.types.run import ModelPartEnd

    usage = SimpleNamespace(
        prompt_tokens=5, completion_tokens=8, input_tokens=5, output_tokens=8
    )
    call = SimpleNamespace(
        type="function_call",
        id="call",
        call_id="call",
        name="lookup",
        arguments='{"x":',
    )
    response = SimpleNamespace(
        id="resp",
        status="incomplete"
        if ending == "truncated"
        else "failed"
        if ending == "rejected"
        else "completed",
        incomplete_details=SimpleNamespace(reason="max_output_tokens"),
        output=[call],
        usage=usage,
    )
    events = []

    async def record(event):
        events.append(event)

    class Stream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def close(self):
            pass

        async def __aiter__(self):
            if protocol == "chat":
                yield SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            delta=SimpleNamespace(
                                content="partial",
                                tool_calls=[
                                    SimpleNamespace(
                                        index=0,
                                        id="call",
                                        function=SimpleNamespace(
                                            name="lookup", arguments='{"x":'
                                        ),
                                    )
                                ],
                            )
                        )
                    ],
                    usage=usage,
                )
                if ending not in {"eof", "network"}:
                    yield SimpleNamespace(
                        choices=[
                            SimpleNamespace(
                                finish_reason="length"
                                if ending == "truncated"
                                else "content_filter"
                                if ending == "rejected"
                                else "tool_calls"
                            )
                        ]
                    )
            else:
                yield SimpleNamespace(
                    type="response.in_progress", response=SimpleNamespace(usage=usage)
                )
                yield SimpleNamespace(
                    type="response.output_text.delta", delta="partial"
                )
                if ending not in {"eof", "network"}:
                    yield SimpleNamespace(
                        type=f"response.{response.status}", response=response
                    )
            if ending == "network":
                raise httpx.ReadError("disconnected")

    async def create(**kwargs):
        return Stream()

    module = chat_completions_models if protocol == "chat" else responses_models
    client = (
        SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        )
        if protocol == "chat"
        else SimpleNamespace(
            responses=SimpleNamespace(stream=lambda **kwargs: Stream())
        )
    )
    monkeypatch.setattr(module, "create_client", lambda *args, **kwargs: client)
    adapter = module.create_model_adapter({})
    with pytest.raises(ModelResponseError) as caught:
        asyncio.run(
            adapter.stream(
                _model("test", provider="openai", name="test").with_route(
                    _route(
                        provider="openai", adapter=adapter.name, api=None, options={}
                    )
                ),
                ModelCall(instructions="", messages=[]),
                environ={},
                on_event=record,
            )
        )
    assert (
        caught.value.kind
        == {
            "truncated": "output_limit",
            "eof": "incomplete_stream",
            "network": "transport_error",
            "rejected": "provider_rejection",
            "malformed": "invalid_json",
        }[ending]
    )
    assert caught.value.usage == ModelUsage(5, 8)
    assert not any(
        isinstance(event, ModelPartEnd) and isinstance(event.data, ToolCallPart)
        for event in events
    )
