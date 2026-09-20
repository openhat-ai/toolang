from __future__ import annotations

import asyncio
from dataclasses import replace
from decimal import Decimal
import json
from types import SimpleNamespace
from typing import Any, cast

import pytest

from toolang.base.errors import ToolangError
from toolang.base.types.message import Message, TextPart, ToolCallPart, ToolResultPart
from toolang.base.types.model import Model, ModelRoute, ModelToolang, Reasoning
from toolang.base.types.run import ModelCall, ModelUsage, ModelUsageMeter, ToolCall
from toolang.base.types.tool import ToolDefinition
from toolang.plugin.adapters import chat_completions, responses
from toolang.plugin.adapters import generate_content as generate_content_adapter
from toolang.plugin.adapters import messages as messages_adapter
from toolang.plugin.adapters.generate_content import (
    generate_content_payload,
    generate_content_usage,
    parse_generate_content,
)
from toolang.plugin.adapters.messages import (
    messages_usage,
    messages_payload,
    parse_message_response,
)


def _route(
    provider: str = "test",
    adapter: str = "chat_completions",
    *,
    api: str | None = None,
    options: dict[str, object] | None = None,
    env: tuple[str, ...] = (),
) -> ModelRoute:
    return ModelRoute(
        provider=provider,
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
    structured_output: bool | None = None,
    reasoning: bool | None = None,
) -> Model:
    return Model(
        id=model_id,
        name=name or model_id,
        _toolang=ModelToolang(provider=provider, ready=True),
        structured_output=structured_output,
        reasoning=reasoning,
    )


@pytest.mark.parametrize(
    "adapter", ["responses", "chat_completions", "messages", "generate_content"]
)
@pytest.mark.parametrize("output", [{}, {"partial": "kept"}])
def test_tool_error_text_reaches_each_provider_without_an_empty_output(adapter, output):
    error = "Workspace rules were just loaded. This operation was not executed; please retry if it complies with them."
    route = _route(provider="test", adapter=adapter, api=None, options={})
    model = _model("model", provider="test", name="model")
    request = ModelCall(
        "",
        [
            Message(
                "assistant",
                (ToolCallPart("tool-1", "fs__write", "fs", {}, call_id="call-1"),),
            ),
            Message(
                "tool",
                (
                    ToolResultPart(
                        "tool-1",
                        "fs__write",
                        "fs",
                        output=output,
                        error=error,
                        call_id="call-1",
                    ),
                ),
            ),
        ],
        max_output_tokens=1024,
    )
    if adapter == "responses":
        payload = responses.response_payload(route, model, request, stateful=False)
        item = next(
            item for item in payload["input"] if item["type"] == "function_call_output"
        )
        assert item["call_id"] == "call-1"
        result = json.loads(item["output"])
    elif adapter == "chat_completions":
        payload = chat_completions.chat_completion_payload(
            route, model, request, stream=False
        )
        item = next(item for item in payload["messages"] if item["role"] == "tool")
        assert item["tool_call_id"] == "call-1"
        result = json.loads(item["content"])
    elif adapter == "messages":
        payload = cast(
            dict[str, Any], messages_payload(route, model, request, stream=False)
        )
        item = next(
            part
            for message in payload["messages"]
            for part in message["content"]
            if part["type"] == "tool_result"
        )
        assert item["tool_use_id"] == "call-1" and item["is_error"] is True
        result = json.loads(item["content"])
    else:
        payload = cast(dict[str, Any], generate_content_payload(route, model, request))
        item = next(
            part["functionResponse"]
            for message in payload["contents"]
            for part in message["parts"]
            if "functionResponse" in part
        )
        assert item["id"] == "call-1"
        result = item["response"]
    assert result["error"] == error
    assert result.get("output") == (output or None)
    assert set(result) <= {"ok", "name", "output", "error"}


@pytest.mark.parametrize("change", ["none", "compaction", "instructions"])
def test_responses_continuation_requires_unchanged_context(change: str) -> None:
    from openai.types.responses import ResponseReasoningItem

    route = _route(provider="openai", adapter="responses", api=None, options={})
    model = _model("model", provider="openai", name="model")
    call = Message(
        role="assistant",
        parts=(
            ToolCallPart(
                tool_call_id="fc_1",
                call_id="call_1",
                tool_name="shell__execute",
                tool_family="shell",
                input={"command": "pwd"},
            ),
        ),
    )
    result = Message(
        role="tool",
        parts=(
            ToolResultPart(
                tool_call_id="fc_1",
                call_id="call_1",
                tool_name="shell__execute",
                tool_family="shell",
                output={"stdout": "/tmp"},
            ),
        ),
    )
    previous = ModelCall("original instructions", [Message.user("old history")])
    reasoning = ResponseReasoningItem(id="rs_1", summary=[], type="reasoning")
    continuation = responses.response_continuation(
        SimpleNamespace(
            id="resp_1",
            output=[reasoning, SimpleNamespace(type="function_call", id="fc_1")],
        ),
        request=previous,
        emitted_message=call,
        stateful=True,
    )
    request = replace(
        previous, messages=[*previous.messages, call, result], continuation=continuation
    )
    if change == "compaction":
        request = replace(
            request, messages=[Message.user("<far>summary</far>"), call, result]
        )
    elif change == "instructions":
        request = replace(request, instructions="updated instructions")

    payload = responses.response_payload(route, model, request, stateful=True)

    if change == "none":
        assert payload["previous_response_id"] == "resp_1"
        assert [item["type"] for item in payload["input"]] == ["function_call_output"]
    else:
        assert "previous_response_id" not in payload
        assert [item["type"] for item in payload["input"]] == [
            "message",
            "message",
            "reasoning",
            "function_call",
            "function_call_output",
        ]
        assert payload["input"][2] == reasoning.model_dump(
            mode="json", exclude_none=True
        )
        assert payload["input"][0]["content"][0]["text"] == request.instructions
        assert isinstance(request.messages[0].parts[0], TextPart)
        assert (
            payload["input"][1]["content"][0]["text"]
            == request.messages[0].parts[0].text
        )


def test_responses_reasoning_tracks_retained_tool_exchanges() -> None:
    from openai.types.responses import ResponseReasoningItem

    request = ModelCall("instructions", [Message.user("input")])
    for index in range(3):
        call = Message(
            "assistant",
            (ToolCallPart(f"fc_{index}", "tool", "tool", {}, call_id=f"call_{index}"),),
        )
        reasoning = ResponseReasoningItem(
            id=f"rs_{index}",
            type="reasoning",
            summary=[],
        )
        continuation = responses.response_continuation(
            SimpleNamespace(
                id=f"resp_{index}",
                output=[
                    reasoning,
                    SimpleNamespace(type="function_call", id=f"fc_{index}"),
                ],
            ),
            request=request,
            emitted_message=call,
            stateful=True,
        )
        request = replace(
            request, messages=[*request.messages, call], continuation=continuation
        )
    assert request.continuation is not None
    assert set(request.continuation["reasoning"]) == {"fc_0", "fc_1", "fc_2"}
    compacted = replace(
        request, messages=[Message.user("summary"), *request.messages[2:]]
    )
    payload = responses.response_payload(
        _route(provider="openai", adapter="responses", api=None, options={}),
        _model("model", provider="openai", name="model"),
        compacted,
        stateful=True,
    )
    assert [(item["type"], item.get("id")) for item in payload["input"][2:]] == [
        ("reasoning", "rs_1"),
        ("function_call", "fc_1"),
        ("reasoning", "rs_2"),
        ("function_call", "fc_2"),
    ]
    continuation = responses.response_continuation(
        SimpleNamespace(id="resp_3"),
        request=compacted,
        emitted_message=Message.assistant("done"),
        stateful=True,
    )
    assert continuation is not None
    assert set(continuation["reasoning"]) == {"fc_1", "fc_2"}
    json.dumps(continuation)


@pytest.mark.parametrize(
    "adapter, provider, options, field",
    [
        ("responses", "openai", {"max_output_tokens": 9000}, "max_output_tokens"),
        ("chat_completions", "openai", {"max_tokens": 9000}, "max_completion_tokens"),
        ("chat_completions", "deepseek", {"max_tokens": 9000}, "max_tokens"),
        ("messages", "anthropic", {"max_tokens": 9000}, "max_tokens"),
        (
            "generate_content",
            "google",
            {"generationConfig": {"maxOutputTokens": 9000}},
            "maxOutputTokens",
        ),
    ],
)
def test_adapter_sends_the_recorded_output_reservation(
    adapter, provider, options, field
) -> None:
    route = _route(provider=provider, adapter=adapter, api=None, options=options)
    model = _model("model", provider=provider, name="model")
    request = ModelCall("instructions", [Message.user("input")], max_output_tokens=1234)
    if adapter == "responses":
        payload = responses.response_payload(route, model, request, stateful=False)
    elif adapter == "chat_completions":
        payload = chat_completions.chat_completion_payload(
            route, model, request, stream=False
        )
    elif adapter == "messages":
        payload = messages_payload(route, model, request, stream=False)
    else:
        payload = cast(
            dict[str, Any],
            generate_content_payload(route, model, request)["generationConfig"],
        )
    assert payload[field] == 1234


@pytest.mark.parametrize(
    "api",
    (
        "https://api.anthropic.com/v1",
        "https://api.minimax.io/anthropic/v1",
    ),
)
def test_messages_adapter_appends_resource_to_resolved_api(api: str) -> None:
    route = _route(provider="provider", adapter="messages", api=api, options={})

    assert messages_adapter._messages_url(route) == f"{api}/messages"


def test_messages_payload_maps_reasoning_and_parse_normalizes_cache_usage() -> None:
    route = _route(
        provider="anthropic",
        adapter="messages",
        api="https://api.anthropic.com/v1",
        options={},
    )
    model = _model("claude-sonnet", provider="anthropic", name="claude-sonnet")
    request = ModelCall(
        instructions="Be concise.",
        messages=[Message.user("Inspect the workspace.")],
        tools=(_tool(),),
        max_output_tokens=1024,
        reasoning=Reasoning("high"),
    )

    payload = messages_payload(route, model, request, stream=False)
    result = parse_message_response(
        {
            "content": [
                {"type": "text", "text": "I will inspect it."},
                {
                    "type": "tool_use",
                    "id": "call-1",
                    "name": "shell__execute",
                    "input": {"command": "pwd"},
                },
            ],
            "usage": {
                "input_tokens": 40,
                "cache_read_input_tokens": 60,
                "cache_creation_input_tokens": 10,
                "cache_creation": {
                    "ephemeral_5m_input_tokens": 8,
                    "ephemeral_1h_input_tokens": 2,
                },
                "output_tokens": 20,
                "output_tokens_details": {"thinking_tokens": 12},
                "server_tool_use": {
                    "web_search_requests": 1,
                    "web_fetch_requests": 2,
                },
                "service_tier": "priority",
                "inference_geo": "us",
            },
        }
    )

    assert payload["thinking"] == {"type": "adaptive"}
    assert payload["output_config"] == {"effort": "high"}
    assert payload["tools"] == [
        {
            "name": "shell__execute",
            "description": "Run a shell command.",
            "input_schema": {"type": "object"},
        }
    ]
    assert result.tool_calls == (
        ToolCall("call-1", "call-1", "shell__execute", {"command": "pwd"}),
    )
    assert result.usage == ModelUsage(
        input_tokens=110,
        output_tokens=20,
        input_uncached_tokens=40,
        input_cache_read_tokens=60,
        input_cache_write_tokens=10,
        output_visible_tokens=8,
        output_reasoning_tokens=12,
        meters=(
            ModelUsageMeter(
                name="anthropic.cache_write.5m",
                quantity=Decimal(8),
                unit="token",
            ),
            ModelUsageMeter(
                name="anthropic.cache_write.1h",
                quantity=Decimal(2),
                unit="token",
            ),
            ModelUsageMeter(
                name="anthropic.server_tool.web_search",
                quantity=Decimal(1),
                unit="request",
            ),
            ModelUsageMeter(
                name="anthropic.server_tool.web_fetch",
                quantity=Decimal(2),
                unit="request",
            ),
        ),
        billing={"service_tier": "priority", "inference_geo": "us"},
    )


def test_generate_content_preserves_thought_signatures_and_thinking_usage() -> None:
    call = ToolCallPart(
        tool_call_id="call-1",
        call_id="call-1",
        tool_name="shell__execute",
        tool_family="shell__execute",
        input={"command": "pwd"},
    )
    result_part = ToolResultPart(
        tool_call_id="call-1",
        call_id="call-1",
        tool_name="shell__execute",
        tool_family="shell__execute",
        output={"stdout": "/tmp"},
    )
    route = _route(
        provider="google",
        adapter="generate_content",
        api="https://generativelanguage.googleapis.com/v1beta",
        options={},
    )
    model = _model("gemini", provider="google", name="gemini")
    request = ModelCall(
        instructions="Be concise.",
        reasoning=Reasoning("high"),
        messages=[
            Message.user("Inspect the workspace."),
            Message(role="assistant", parts=(call,)),
            Message(role="tool", parts=(result_part,)),
        ],
        continuation={"thought_signatures": {"call-1": "opaque-signature"}},
    )

    payload = generate_content_payload(route, model, request)
    result = parse_generate_content(
        {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {"text": "internal", "thought": True},
                            {"text": "Done."},
                            {
                                "functionCall": {
                                    "id": "call-2",
                                    "name": "shell__execute",
                                    "args": {"command": "ls"},
                                },
                                "thoughtSignature": "next-signature",
                            },
                        ]
                    }
                }
            ],
            "usageMetadata": {
                "promptTokenCount": 100,
                "cachedContentTokenCount": 60,
                "candidatesTokenCount": 10,
                "thoughtsTokenCount": 30,
                "toolUsePromptTokenCount": 20,
                "totalTokenCount": 160,
                "promptTokensDetails": [
                    {"modality": "TEXT", "tokenCount": 80},
                    {"modality": "AUDIO", "tokenCount": 20},
                ],
                "candidatesTokensDetails": [{"modality": "TEXT", "tokenCount": 10}],
                "toolUsePromptTokensDetails": [{"modality": "TEXT", "tokenCount": 20}],
                "serviceTier": "PRIORITY",
                "trafficType": "ON_DEMAND",
            },
        }
    )

    contents = cast(list[dict[str, Any]], payload["contents"])
    assistant_part = contents[1]["parts"][0]
    tool_part = contents[2]["parts"][0]
    assert assistant_part == {
        "functionCall": {
            "id": "call-1",
            "name": "shell__execute",
            "args": {"command": "pwd"},
        },
        "thoughtSignature": "opaque-signature",
    }
    assert tool_part["functionResponse"]["id"] == "call-1"
    assert payload["generationConfig"] == {"thinkingConfig": {"thinkingLevel": "HIGH"}}
    assert result.message is not None
    assert result.message.parts[0] == TextPart("Done.")
    assert result.continuation == {"thought_signatures": {"call-2": "next-signature"}}
    assert result.usage == ModelUsage(
        input_tokens=120,
        output_tokens=40,
        input_uncached_tokens=60,
        input_cache_read_tokens=60,
        input_audio_tokens=20,
        output_visible_tokens=10,
        output_reasoning_tokens=30,
        output_audio_tokens=0,
        meters=(
            ModelUsageMeter(
                name="google.tool_use_prompt",
                quantity=Decimal(20),
                unit="token",
            ),
        ),
        billing={"service_tier": "priority", "traffic_type": "on_demand"},
    )


def test_chat_usage_normalizes_cache_aliases_writes_and_reported_cost() -> None:
    deepseek = chat_completions.chat_usage(
        SimpleNamespace(
            usage=SimpleNamespace(
                prompt_tokens=100,
                completion_tokens=10,
                prompt_tokens_details=SimpleNamespace(cached_tokens=0),
                prompt_cache_hit_tokens=60,
                prompt_cache_miss_tokens=40,
            )
        )
    )
    openrouter = chat_completions.chat_usage(
        SimpleNamespace(
            service_tier="priority",
            usage=SimpleNamespace(
                prompt_tokens=100,
                completion_tokens=40,
                prompt_tokens_details=SimpleNamespace(
                    cached_tokens=60,
                    cache_write_tokens=10,
                    audio_tokens=5,
                ),
                completion_tokens_details=SimpleNamespace(
                    reasoning_tokens=30,
                    audio_tokens=2,
                ),
                cost="0.03",
                currency="USD",
            ),
        )
    )

    assert deepseek == ModelUsage(
        input_tokens=100,
        output_tokens=10,
        input_uncached_tokens=40,
        input_cache_read_tokens=60,
    )
    assert openrouter == ModelUsage(
        input_tokens=100,
        output_tokens=40,
        input_uncached_tokens=30,
        input_cache_read_tokens=60,
        input_cache_write_tokens=10,
        input_audio_tokens=5,
        output_visible_tokens=10,
        output_reasoning_tokens=30,
        output_audio_tokens=2,
        reported_cost=Decimal("0.03"),
        reported_currency="USD",
        billing={"service_tier": "priority"},
    )


def test_responses_usage_normalizes_optional_components_and_cost() -> None:
    usage = responses.response_usage(
        SimpleNamespace(
            service_tier="flex",
            usage=SimpleNamespace(
                input_tokens=100,
                output_tokens=40,
                input_tokens_details=SimpleNamespace(
                    cached_tokens=60,
                    cache_write_tokens=10,
                    audio_tokens=5,
                ),
                output_tokens_details=SimpleNamespace(
                    reasoning_tokens=30,
                    audio_tokens=2,
                ),
                cost="0.02",
                currency="USD",
            ),
        )
    )

    assert usage == ModelUsage(
        input_tokens=100,
        output_tokens=40,
        input_uncached_tokens=30,
        input_cache_read_tokens=60,
        input_cache_write_tokens=10,
        input_audio_tokens=5,
        output_visible_tokens=10,
        output_reasoning_tokens=30,
        output_audio_tokens=2,
        reported_cost=Decimal("0.02"),
        reported_currency="USD",
        billing={"service_tier": "flex"},
    )


def test_protocol_usage_keeps_missing_components_unknown() -> None:
    anthropic = messages_usage({"input_tokens": 40, "output_tokens": 7})
    gemini = generate_content_usage(
        {"promptTokenCount": 100, "candidatesTokenCount": 10}
    )

    assert anthropic == ModelUsage(
        input_tokens=40,
        output_tokens=7,
        input_uncached_tokens=40,
    )
    assert gemini == ModelUsage(
        input_tokens=100,
        output_tokens=10,
        output_visible_tokens=10,
    )


def test_messages_stream_returns_normalized_final_usage(monkeypatch) -> None:
    lines: tuple[dict[str, object], ...] = (
        {
            "type": "message_start",
            "message": {
                "usage": {
                    "input_tokens": 40,
                    "cache_read_input_tokens": 60,
                    "cache_creation_input_tokens": 0,
                    "output_tokens": 0,
                }
            },
        },
        {
            "type": "message_delta",
            "usage": {
                "output_tokens": 7,
                "output_tokens_details": {"thinking_tokens": 2},
            },
        },
    )
    monkeypatch.setattr(
        messages_adapter.httpx,
        "AsyncClient",
        lambda: _FakeAsyncClient(lines),
    )
    adapter = messages_adapter.MessagesModelAdapter()

    result = asyncio.run(
        adapter.stream(
            _route(
                provider="anthropic",
                adapter="messages",
                api="https://api.anthropic.com/v1",
                options={},
                env=("ANTHROPIC_API_KEY",),
            ),
            _model("test", provider="anthropic", name="test"),
            ModelCall(
                instructions="",
                messages=[Message.user("hello")],
                max_output_tokens=1024,
            ),
            environ={"ANTHROPIC_API_KEY": "secret"},
            on_event=_ignore_event,
        )
    )

    assert result.usage == ModelUsage(
        input_tokens=100,
        output_tokens=7,
        input_uncached_tokens=40,
        input_cache_read_tokens=60,
        input_cache_write_tokens=0,
        output_visible_tokens=5,
        output_reasoning_tokens=2,
    )


def test_generate_content_stream_returns_normalized_final_usage(monkeypatch) -> None:
    lines: tuple[dict[str, object], ...] = (
        {
            "candidates": [],
            "usageMetadata": {
                "promptTokenCount": 100,
                "cachedContentTokenCount": 60,
                "candidatesTokenCount": 7,
                "thoughtsTokenCount": 2,
                "totalTokenCount": 109,
            },
        },
    )
    monkeypatch.setattr(
        generate_content_adapter.httpx,
        "AsyncClient",
        lambda: _FakeAsyncClient(lines),
    )
    adapter = generate_content_adapter.GenerateContentModelAdapter()

    result = asyncio.run(
        adapter.stream(
            _route(
                provider="google",
                adapter="generate_content",
                api="https://generativelanguage.googleapis.com/v1beta",
                options={},
                env=("GOOGLE_API_KEY",),
            ),
            _model("test", provider="google", name="test"),
            ModelCall(instructions="", messages=[Message.user("hello")]),
            environ={"GOOGLE_API_KEY": "secret"},
            on_event=_ignore_event,
        )
    )

    assert result.usage == ModelUsage(
        input_tokens=100,
        output_tokens=9,
        input_uncached_tokens=40,
        input_cache_read_tokens=60,
        output_visible_tokens=7,
        output_reasoning_tokens=2,
    )


def test_messages_payload_supports_a_token_budget() -> None:
    route = _route(
        provider="anthropic", adapter="messages", api=None, options={"max_tokens": 4096}
    )
    model = _model("claude", provider="anthropic", name="claude")

    payload = messages_payload(
        route,
        model,
        ModelCall(
            instructions="",
            messages=[Message.user("hello")],
            reasoning=Reasoning(budget_tokens=2048),
        ),
        stream=False,
    )

    assert payload["thinking"] == {"type": "enabled", "budget_tokens": 2048}
    assert "output_config" not in payload


def test_messages_canonical_reasoning_replaces_raw_reasoning_options() -> None:
    payload = messages_payload(
        _route(
            provider="anthropic",
            adapter="messages",
            api=None,
            options={
                "thinking": {"type": "disabled"},
                "output_config": {"effort": "low", "verbosity": "low"},
            },
        ),
        _model("claude", provider="anthropic", name="claude"),
        ModelCall(
            instructions="",
            messages=[Message.user("hello")],
            max_output_tokens=1024,
            reasoning=Reasoning("high"),
        ),
        stream=False,
    )

    assert payload["thinking"] == {"type": "adaptive"}
    assert payload["output_config"] == {"effort": "high", "verbosity": "low"}


def test_generate_content_auth_uses_header_instead_of_url_query() -> None:
    route = _route(
        provider="google",
        adapter="generate_content",
        api="https://generativelanguage.googleapis.com/v1beta",
        options={},
        env=("GOOGLE_API_KEY",),
    )
    model = _model("gemini/preview", provider="google", name="gemini")

    url = generate_content_adapter._generate_url(route, model=model, stream=True)
    headers = generate_content_adapter._generate_headers(
        route, environ={"GOOGLE_API_KEY": "secret-key"}
    )

    assert url.endswith("/models/gemini%2Fpreview:streamGenerateContent?alt=sse")
    assert "secret-key" not in url
    assert "key=" not in url
    assert headers["x-goog-api-key"] == "secret-key"


def test_reasoning_rejects_overlapping_effort_and_budget() -> None:
    with pytest.raises(ValueError, match="either effort or budget_tokens"):
        Reasoning("high", 2048)


def test_generate_content_canonical_reasoning_replaces_raw_reasoning_control() -> None:
    payload = generate_content_payload(
        _route(
            provider="google",
            adapter="generate_content",
            api=None,
            options={
                "generationConfig": {
                    "thinkingConfig": {
                        "includeThoughts": True,
                        "thinkingBudget": 2048,
                    }
                }
            },
        ),
        _model("gemini", provider="google", name="gemini"),
        ModelCall(
            instructions="",
            messages=[Message.user("hello")],
            reasoning=Reasoning("high"),
        ),
    )

    assert payload["generationConfig"] == {
        "thinkingConfig": {
            "includeThoughts": True,
            "thinkingLevel": "HIGH",
        }
    }


@pytest.mark.parametrize(
    ("provider", "reasoning", "expected"),
    (
        (
            "openrouter",
            Reasoning(budget_tokens=2048),
            {"reasoning": {"max_tokens": 2048}},
        ),
        (
            "deepseek",
            Reasoning("max"),
            {
                "thinking": {"type": "enabled"},
                "reasoning_effort": "max",
            },
        ),
        ("xai", Reasoning("high"), {"reasoning_effort": "high"}),
        ("groq", Reasoning("none"), {"reasoning_effort": "none"}),
        ("custom", Reasoning("high"), {"reasoning_effort": "high"}),
    ),
)
def test_chat_completions_maps_known_reasoning_dialects(
    provider: str,
    reasoning: Reasoning,
    expected: dict[str, object],
) -> None:
    route = _route(provider=provider, adapter="chat_completions", api=None, options={})
    model = _model("model", provider=provider, name="model")

    payload = chat_completions.chat_completion_payload(
        route,
        model,
        ModelCall(
            instructions="",
            messages=[Message.user("hello")],
            reasoning=reasoning,
        ),
        stream=False,
    )

    for key, value in expected.items():
        assert payload[key] == value
    assert "budget_tokens" not in json.dumps(payload)


def test_deepseek_canonical_reasoning_replaces_raw_reasoning_controls() -> None:
    payload = chat_completions.chat_completion_payload(
        _route(
            provider="deepseek",
            adapter="chat_completions",
            api=None,
            options={
                "thinking": {"type": "disabled"},
                "reasoning_effort": "low",
            },
        ),
        _model("model", provider="deepseek", name="model"),
        ModelCall(
            instructions="",
            messages=[Message.user("hello")],
            reasoning=Reasoning("high"),
        ),
        stream=False,
    )

    assert payload["thinking"] == {"type": "enabled"}
    assert payload["reasoning_effort"] == "high"


def test_chat_completions_rejects_unsupported_reasoning() -> None:
    request = ModelCall(
        instructions="",
        messages=[Message.user("hello")],
        reasoning=Reasoning(budget_tokens=2048),
    )
    with pytest.raises(ToolangError, match="xai.*does not support token budgets"):
        chat_completions.chat_completion_payload(
            _route(provider="xai", adapter="chat_completions", api=None, options={}),
            _model("model", provider="xai", name="model"),
            request,
            stream=False,
        )


def test_responses_maps_supported_reasoning_and_rejects_token_budgets() -> None:
    route = _route(provider="openai", adapter="responses", api=None, options={})
    model = _model("model", provider="openai", name="model")

    assert responses.response_payload(
        route,
        model,
        ModelCall(
            instructions="",
            messages=[Message.user("hello")],
            reasoning=Reasoning("none"),
        ),
        stateful=False,
    )["reasoning"] == {"effort": "none"}
    with pytest.raises(ToolangError, match="does not support reasoning token budgets"):
        responses.response_payload(
            route,
            model,
            ModelCall(
                instructions="",
                messages=[Message.user("hello")],
                reasoning=Reasoning(budget_tokens=2048),
            ),
            stateful=False,
        )


def test_protocol_adapters_map_normalized_structured_output() -> None:
    schema: dict[str, object] = {
        "additionalProperties": False,
        "properties": {"answer": {"type": "boolean"}},
        "required": ["answer"],
        "type": "object",
    }
    request = ModelCall(
        instructions="Keep this logical instruction unchanged.",
        messages=[Message.user("Decide.")],
        output_schema=schema,
    )

    chat_payload = chat_completions.chat_completion_payload(
        _route(provider="openai", adapter="chat_completions", api=None, options={}),
        _model("model", provider="openai", name="model", structured_output=True),
        request,
        stream=False,
    )
    response_payload = responses.response_payload(
        _route(
            provider="openai",
            adapter="responses",
            api=None,
            options={"text": {"verbosity": "low"}},
        ),
        _model("model", provider="openai", name="model", structured_output=True),
        request,
        stateful=False,
    )
    message_payload = messages_payload(
        _route(
            provider="anthropic",
            adapter="messages",
            api=None,
            options={"max_tokens": 1024},
        ),
        _model("model", provider="anthropic", name="model", structured_output=True),
        ModelCall(
            instructions="Keep this logical instruction unchanged.",
            messages=[Message.user("Decide.")],
            output_schema=schema,
            reasoning=Reasoning("high"),
        ),
        stream=False,
    )
    generate_payload = generate_content_payload(
        _route(
            provider="google",
            adapter="generate_content",
            api=None,
            options={"generationConfig": {"temperature": 0}},
        ),
        _model("model", provider="google", name="model", structured_output=True),
        request,
    )

    assert chat_payload["response_format"] == {
        "type": "json_schema",
        "json_schema": {
            "name": "output",
            "strict": True,
            "schema": schema,
        },
    }
    assert response_payload["text"] == {
        "verbosity": "low",
        "format": {
            "type": "json_schema",
            "name": "output",
            "strict": True,
            "schema": schema,
        },
    }
    assert message_payload["output_config"] == {
        "effort": "high",
        "format": {"type": "json_schema", "schema": schema},
    }
    assert generate_payload["generationConfig"] == {
        "temperature": 0,
        "responseMimeType": "application/json",
        "responseJsonSchema": schema,
    }
    assert request.instructions == "Keep this logical instruction unchanged."
    assert request.messages == [Message.user("Decide.")]


@pytest.mark.parametrize(
    "schema",
    (
        {"type": "boolean"},
        {"items": {"type": "string"}, "type": "array"},
        {},
        {
            "additionalProperties": False,
            "properties": {"answer": {"type": "boolean"}},
            "type": "object",
        },
    ),
)
def test_openai_adapters_fall_back_for_non_strict_object_schemas(
    schema: dict[str, object],
) -> None:
    request = ModelCall(
        instructions="Keep this logical instruction unchanged.",
        messages=[Message.user("Decide.")],
        output_schema=schema,
    )
    route = _route(provider="openai", adapter="chat_completions", api=None, options={})
    model = _model("model", provider="openai", name="model", structured_output=True)

    chat_payload = chat_completions.chat_completion_payload(
        route,
        model,
        request,
        stream=False,
    )
    response_payload = responses.response_payload(
        replace(route, adapter="responses"),
        model,
        request,
        stateful=False,
    )

    assert "response_format" not in chat_payload
    assert "text" not in response_payload
    schema_text = json.dumps(
        schema,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    assert schema_text in chat_payload["messages"][0]["content"]
    assert schema_text in response_payload["input"][0]["content"][0]["text"]
    assert request.instructions == "Keep this logical instruction unchanged."
    assert request.messages == [Message.user("Decide.")]


def test_openai_adapters_inline_strict_root_struct_schema() -> None:
    schema: dict[str, object] = {
        "$defs": {
            "Answer": {
                "additionalProperties": False,
                "properties": {"answer": {"type": "boolean"}},
                "required": ["answer"],
                "type": "object",
            }
        },
        "$ref": "#/$defs/Answer",
    }
    request = ModelCall(
        instructions="",
        messages=[Message.user("Decide.")],
        output_schema=schema,
    )
    route = _route(provider="openai", adapter="chat_completions", api=None, options={})
    model = _model("model", provider="openai", name="model", structured_output=True)

    chat_payload = chat_completions.chat_completion_payload(
        route,
        model,
        request,
        stream=False,
    )
    response_payload = responses.response_payload(
        replace(route, adapter="responses"),
        model,
        request,
        stateful=False,
    )

    expected = {
        "$defs": schema["$defs"],
        "additionalProperties": False,
        "properties": {"answer": {"type": "boolean"}},
        "required": ["answer"],
        "type": "object",
    }
    assert chat_payload["response_format"]["json_schema"]["schema"] == expected
    assert response_payload["text"]["format"]["schema"] == expected


def test_generate_content_falls_back_for_tools_before_gemini_3() -> None:
    schema: dict[str, object] = {"type": "boolean"}
    request = ModelCall(
        instructions="Keep this logical instruction unchanged.",
        messages=[Message.user("Decide.")],
        tools=(_tool(),),
        output_schema=schema,
    )
    route = _route(provider="google", adapter="generate_content", api=None, options={})
    model = _model(
        "gemini-2.5-flash",
        provider="google",
        name="gemini-2.5-flash",
        structured_output=True,
    )

    payload = generate_content_payload(route, model, request)

    assert "tools" in payload
    assert "generationConfig" not in payload
    schema_text = json.dumps(schema, separators=(",", ":"), sort_keys=True)
    system_instruction = cast(dict[str, Any], payload["systemInstruction"])
    assert schema_text in system_instruction["parts"][0]["text"]
    assert request.instructions == "Keep this logical instruction unchanged."


def test_generate_content_uses_native_schema_with_tools_for_gemini_3() -> None:
    schema: dict[str, object] = {"type": "boolean"}
    request = ModelCall(
        instructions="",
        messages=[Message.user("Decide.")],
        tools=(_tool(),),
        output_schema=schema,
    )
    route = _route(provider="google", adapter="generate_content", api=None, options={})
    model = _model(
        "models/gemini-3.1-pro-preview",
        provider="google",
        name="gemini-3.1-pro-preview",
        structured_output=True,
    )

    payload = generate_content_payload(route, model, request)

    assert "tools" in payload
    assert payload["generationConfig"] == {
        "responseMimeType": "application/json",
        "responseJsonSchema": schema,
    }
    assert "systemInstruction" not in payload


@pytest.mark.parametrize("with_tools", [False, True])
@pytest.mark.parametrize(
    ("schema", "json_object"),
    [
        ({"type": "object", "properties": {"answer": {"type": "boolean"}}}, True),
        (
            {"$ref": "#/$defs/Progress", "$defs": {"Progress": {"type": "object"}}},
            True,
        ),
        ({"type": "boolean"}, False),
        ({"type": "array", "items": {"type": "string"}}, False),
        ({}, False),
    ],
)
def test_deepseek_uses_json_mode_only_for_object_outputs(
    schema, json_object, with_tools
) -> None:
    request = ModelCall(
        instructions="Keep this logical instruction unchanged.",
        messages=[Message.user("Decide.")],
        tools=(_tool(),) if with_tools else (),
        output_schema=schema,
    )
    route = _route(
        provider="deepseek", adapter="chat_completions", api=None, options={}
    )
    model = _model(
        "deepseek-v4-pro",
        provider="deepseek",
        name="DeepSeek V4 Pro",
        structured_output=True,
    )

    payload = chat_completions.chat_completion_payload(
        route,
        model,
        request,
        stream=False,
    )

    assert payload.get("response_format") == (
        {"type": "json_object"} if json_object else None
    )
    assert ("tools" in payload) is with_tools
    schema_text = json.dumps(schema, separators=(",", ":"), sort_keys=True)
    assert schema_text in payload["messages"][0]["content"]
    assert request.instructions == "Keep this logical instruction unchanged."


@pytest.mark.parametrize("capability", (False, None))
def test_messages_falls_back_without_native_structured_output_capability(
    capability: bool | None,
) -> None:
    schema: dict[str, object] = {"type": "boolean"}
    request = ModelCall(
        instructions="Keep this logical instruction unchanged.",
        messages=[Message.user("Decide.")],
        output_schema=schema,
    )
    route = _route(
        provider="anthropic",
        adapter="messages",
        api=None,
        options={"output_config": {"effort": "high"}, "max_tokens": 1024},
    )
    model = _model(
        "model", provider="anthropic", name="model", structured_output=capability
    )

    payload = messages_payload(route, model, request, stream=False)

    assert payload["output_config"] == {"effort": "high"}
    schema_text = json.dumps(schema, separators=(",", ":"), sort_keys=True)
    assert schema_text in cast(str, payload["system"])
    assert request.instructions == "Keep this logical instruction unchanged."


def test_protocol_adapters_fall_back_without_advertised_model_capability() -> None:
    schema: dict[str, object] = {
        "additionalProperties": False,
        "properties": {"answer": {"type": "boolean"}},
        "required": ["answer"],
        "type": "object",
    }
    request = ModelCall(
        instructions="Keep this logical instruction unchanged.",
        messages=[Message.user("Decide.")],
        output_schema=schema,
    )
    route = _route(
        provider="provider", adapter="chat_completions", api=None, options={}
    )
    model = _model("model", provider="provider", name="model", structured_output=False)

    chat_payload = chat_completions.chat_completion_payload(
        route,
        model,
        request,
        stream=False,
    )
    response_payload = responses.response_payload(
        replace(route, adapter="responses"),
        model,
        request,
        stateful=False,
    )
    message_payload = messages_payload(
        replace(route, adapter="messages", options={"max_tokens": 1024}),
        model,
        request,
        stream=False,
    )
    generate_payload = generate_content_payload(
        replace(route, adapter="generate_content"),
        model,
        request,
    )

    assert "response_format" not in chat_payload
    assert "text" not in response_payload
    assert "output_config" not in message_payload
    assert "generationConfig" not in generate_payload
    schema_text = json.dumps(schema, separators=(",", ":"), sort_keys=True)
    assert schema_text in chat_payload["messages"][0]["content"]
    assert schema_text in response_payload["input"][0]["content"][0]["text"]
    assert schema_text in cast(str, message_payload["system"])
    system_instruction = cast(dict[str, Any], generate_payload["systemInstruction"])
    assert schema_text in system_instruction["parts"][0]["text"]
    assert request.instructions == "Keep this logical instruction unchanged."


@pytest.mark.parametrize(
    ("target", "build"),
    (
        (
            (
                _route(
                    provider="openai",
                    adapter="chat_completions",
                    api=None,
                    options={"response_format": {"type": "json_object"}},
                ),
                _model("model", provider="openai", name="model"),
            ),
            lambda route, model, request: chat_completions.chat_completion_payload(
                route, model, request, stream=False
            ),
        ),
        (
            (
                _route(
                    provider="openai",
                    adapter="responses",
                    api=None,
                    options={"text": {"format": {"type": "json_object"}}},
                ),
                _model("model", provider="openai", name="model"),
            ),
            lambda route, model, request: responses.response_payload(
                route, model, request, stateful=False
            ),
        ),
        (
            (
                _route(
                    provider="anthropic",
                    adapter="messages",
                    api=None,
                    options={
                        "output_config": {"format": {"type": "json_schema"}},
                        "max_tokens": 1024,
                    },
                ),
                _model("model", provider="anthropic", name="model"),
            ),
            lambda route, model, request: messages_payload(
                route, model, request, stream=False
            ),
        ),
        (
            (
                _route(
                    provider="google",
                    adapter="generate_content",
                    api=None,
                    options={
                        "generationConfig": {"responseSchema": {"type": "string"}}
                    },
                ),
                _model("model", provider="google", name="model"),
            ),
            generate_content_payload,
        ),
    ),
)
def test_protocol_adapters_reject_conflicting_structured_output_options(
    target: tuple[ModelRoute, Model],
    build,
) -> None:
    request = ModelCall(
        instructions="",
        messages=[Message.user("hello")],
        output_schema={"type": "boolean"},
    )

    with pytest.raises(ToolangError, match="conflicts with normalized structured"):
        build(*target, request)


class _FakeStreamResponse:
    def __init__(self, lines: tuple[dict[str, object], ...]) -> None:
        self._lines = lines

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        del exc_type, exc, traceback

    def raise_for_status(self) -> None:
        return None

    async def aiter_lines(self):
        for line in self._lines:
            yield f"data: {json.dumps(line)}"


class _FakeAsyncClient:
    def __init__(self, lines: tuple[dict[str, object], ...]) -> None:
        self._lines = lines

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        del exc_type, exc, traceback

    def stream(self, *args, **kwargs):
        del args, kwargs
        return _FakeStreamResponse(self._lines)


async def _ignore_event(event: object) -> None:
    del event


def _tool() -> ToolDefinition:
    return ToolDefinition(
        name="shell__execute",
        description="Run a shell command.",
        parameters={"type": "object"},
    )


@pytest.mark.parametrize(
    ("adapter", "build"),
    (
        (
            "chat_completions",
            lambda route, model, request: chat_completions.chat_completion_payload(
                route, model, request, stream=False
            ),
        ),
        (
            "responses",
            lambda route, model, request: responses.response_payload(
                route, model, request, stateful=False
            ),
        ),
        (
            "generate_content",
            generate_content_payload,
        ),
    ),
)
def test_adapters_omit_an_absent_output_allowance(adapter, build) -> None:
    route = _route(provider="test", adapter=adapter, api=None, options={})
    model = _model("model", provider="test", name="model")
    request = ModelCall(instructions="", messages=[Message.user("hello")])

    payload = build(route, model, request)

    assert "max_tokens" not in payload
    assert "max_completion_tokens" not in payload
    assert "max_output_tokens" not in payload
    assert "generationConfig" not in payload
