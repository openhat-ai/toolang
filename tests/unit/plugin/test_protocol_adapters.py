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
from toolang.base.types.model import ModelTarget
from toolang.base.types.run import ModelCall, ModelUsage, ModelUsageMeter, ToolCall
from toolang.base.types.tool import ToolDefinition
from toolang.plugin.models.adapters import chat_completions, responses
from toolang.plugin.models.adapters import generate_content as generate_content_adapter
from toolang.plugin.models.adapters import messages as messages_adapter
from toolang.plugin.models.adapters.generate_content import (
    generate_content_payload,
    generate_content_usage,
    parse_generate_content,
)
from toolang.plugin.models.adapters.messages import (
    messages_usage,
    messages_payload,
    parse_message_response,
)


@pytest.mark.parametrize(
    "api",
    (
        "https://api.anthropic.com/v1",
        "https://api.minimax.io/anthropic/v1",
    ),
)
def test_messages_adapter_appends_resource_to_resolved_api(api: str) -> None:
    target = ModelTarget(
        ref="provider/model",
        provider="provider",
        name="model",
        model="model",
        adapter="messages",
        base_url=api,
    )

    assert messages_adapter._messages_url(target) == f"{api}/messages"


def test_messages_payload_maps_reasoning_and_parse_normalizes_cache_usage() -> None:
    target = ModelTarget(
        ref="anthropic/claude-sonnet",
        provider="anthropic",
        name="claude-sonnet",
        model="claude-sonnet",
        adapter="messages",
        base_url="https://api.anthropic.com/v1",
        api_key="secret",
        reasoning={"effort": "high"},
    )
    request = ModelCall(
        instructions="Be concise.",
        messages=[Message.user("Inspect the workspace.")],
        tools=(_tool(),),
    )

    payload = messages_payload(target, request, stream=False)
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
    target = ModelTarget(
        ref="google/gemini",
        provider="google",
        name="gemini",
        model="gemini",
        adapter="generate_content",
        base_url="https://generativelanguage.googleapis.com/v1beta",
        api_key="secret",
        reasoning={"effort": "high"},
    )
    request = ModelCall(
        instructions="Be concise.",
        messages=[
            Message.user("Inspect the workspace."),
            Message(role="assistant", parts=(call,)),
            Message(role="tool", parts=(result_part,)),
        ],
        continuation={"thought_signatures": {"call-1": "opaque-signature"}},
    )

    payload = generate_content_payload(target, request)
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
            ModelTarget(
                ref="anthropic/test",
                provider="anthropic",
                name="test",
                model="test",
                adapter="messages",
                base_url="https://api.anthropic.com/v1",
                api_key="secret",
            ),
            ModelCall(instructions="", messages=[Message.user("hello")]),
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
            ModelTarget(
                ref="google/test",
                provider="google",
                name="test",
                model="test",
                adapter="generate_content",
                base_url="https://generativelanguage.googleapis.com/v1beta",
                api_key="secret",
            ),
            ModelCall(instructions="", messages=[Message.user("hello")]),
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


def test_messages_payload_supports_effort_with_a_token_budget() -> None:
    target = ModelTarget(
        ref="anthropic/claude",
        provider="anthropic",
        name="claude",
        model="claude",
        adapter="messages",
        options={"max_tokens": 4096},
        reasoning={"enabled": True, "effort": "high", "budget_tokens": 2048},
    )

    payload = messages_payload(
        target,
        ModelCall(instructions="", messages=[Message.user("hello")]),
        stream=False,
    )

    assert payload["thinking"] == {"type": "enabled", "budget_tokens": 2048}
    assert payload["output_config"] == {"effort": "high"}


def test_generate_content_auth_uses_header_instead_of_url_query() -> None:
    target = ModelTarget(
        ref="google/gemini",
        provider="google",
        name="gemini",
        model="gemini/preview",
        adapter="generate_content",
        base_url="https://generativelanguage.googleapis.com/v1beta",
        api_key="secret-key",
    )

    url = generate_content_adapter._generate_url(target, stream=True)
    headers = generate_content_adapter._generate_headers(target)

    assert url.endswith("/models/gemini%2Fpreview:streamGenerateContent?alt=sse")
    assert "secret-key" not in url
    assert "key=" not in url
    assert headers["x-goog-api-key"] == "secret-key"


def test_generate_content_rejects_overlapping_effort_and_budget() -> None:
    target = ModelTarget(
        ref="google/gemini",
        provider="google",
        name="gemini",
        model="gemini",
        adapter="generate_content",
        reasoning={"effort": "high", "budget_tokens": 2048},
    )

    with pytest.raises(ToolangError, match="either reasoning effort or budget_tokens"):
        generate_content_payload(
            target,
            ModelCall(instructions="", messages=[Message.user("hello")]),
        )


@pytest.mark.parametrize(
    ("provider", "reasoning", "expected"),
    (
        (
            "openrouter",
            {"enabled": True, "budget_tokens": 2048},
            {"reasoning": {"enabled": True, "max_tokens": 2048}},
        ),
        (
            "deepseek",
            {"enabled": True, "effort": "max"},
            {
                "thinking": {"type": "enabled"},
                "reasoning_effort": "max",
            },
        ),
        ("xai", {"effort": "high"}, {"reasoning_effort": "high"}),
        ("groq", {"enabled": False}, {"reasoning_effort": "none"}),
        ("custom", {"effort": "high"}, {"reasoning_effort": "high"}),
    ),
)
def test_chat_completions_maps_known_reasoning_dialects(
    provider: str,
    reasoning: dict[str, object],
    expected: dict[str, object],
) -> None:
    target = ModelTarget(
        ref=f"{provider}/model",
        provider=provider,
        name="model",
        model="model",
        adapter="chat_completions",
        reasoning=reasoning,
    )

    payload = chat_completions.chat_completion_payload(
        target,
        ModelCall(instructions="", messages=[Message.user("hello")]),
        stream=False,
    )

    for key, value in expected.items():
        assert payload[key] == value
    assert "budget_tokens" not in json.dumps(payload)


def test_chat_completions_rejects_unsupported_or_overlapping_reasoning() -> None:
    request = ModelCall(instructions="", messages=[Message.user("hello")])
    with pytest.raises(ToolangError, match="either reasoning effort or budget_tokens"):
        chat_completions.chat_completion_payload(
            ModelTarget(
                ref="openrouter/model",
                provider="openrouter",
                name="model",
                model="model",
                adapter="chat_completions",
                reasoning={"effort": "high", "budget_tokens": 2048},
            ),
            request,
            stream=False,
        )
    with pytest.raises(ToolangError, match="xai.*does not support token budgets"):
        chat_completions.chat_completion_payload(
            ModelTarget(
                ref="xai/model",
                provider="xai",
                name="model",
                model="model",
                adapter="chat_completions",
                reasoning={"budget_tokens": 2048},
            ),
            request,
            stream=False,
        )


def test_responses_maps_supported_reasoning_and_rejects_token_budgets() -> None:
    request = ModelCall(instructions="", messages=[Message.user("hello")])
    disabled = ModelTarget(
        ref="openai/model",
        provider="openai",
        name="model",
        model="model",
        adapter="responses",
        reasoning={"enabled": False},
    )

    assert responses.response_payload(disabled, request, stateful=False)[
        "reasoning"
    ] == {"effort": "none"}
    with pytest.raises(ToolangError, match="does not support reasoning token budgets"):
        responses.response_payload(
            ModelTarget(
                ref="openai/model",
                provider="openai",
                name="model",
                model="model",
                adapter="responses",
                reasoning={"budget_tokens": 2048},
            ),
            request,
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
        ModelTarget(
            ref="openai/model",
            provider="openai",
            name="model",
            model="model",
            adapter="chat_completions",
            structured_output=True,
        ),
        request,
        stream=False,
    )
    response_payload = responses.response_payload(
        ModelTarget(
            ref="openai/model",
            provider="openai",
            name="model",
            model="model",
            adapter="responses",
            structured_output=True,
            options={"text": {"verbosity": "low"}},
        ),
        request,
        stateful=False,
    )
    message_payload = messages_payload(
        ModelTarget(
            ref="anthropic/model",
            provider="anthropic",
            name="model",
            model="model",
            adapter="messages",
            structured_output=True,
            reasoning={"effort": "high"},
        ),
        request,
        stream=False,
    )
    generate_payload = generate_content_payload(
        ModelTarget(
            ref="google/model",
            provider="google",
            name="model",
            model="model",
            adapter="generate_content",
            structured_output=True,
            options={"generationConfig": {"temperature": 0}},
        ),
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
    target = ModelTarget(
        ref="openai/model",
        provider="openai",
        name="model",
        model="model",
        adapter="chat_completions",
        structured_output=True,
    )

    chat_payload = chat_completions.chat_completion_payload(
        target,
        request,
        stream=False,
    )
    response_payload = responses.response_payload(
        replace(target, adapter="responses"),
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
    target = ModelTarget(
        ref="openai/model",
        provider="openai",
        name="model",
        model="model",
        adapter="chat_completions",
        structured_output=True,
    )

    chat_payload = chat_completions.chat_completion_payload(
        target,
        request,
        stream=False,
    )
    response_payload = responses.response_payload(
        replace(target, adapter="responses"),
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
    target = ModelTarget(
        ref="google/gemini-2.5-flash",
        provider="google",
        name="gemini-2.5-flash",
        model="gemini-2.5-flash",
        adapter="generate_content",
        structured_output=True,
    )

    payload = generate_content_payload(target, request)

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
    target = ModelTarget(
        ref="google/gemini-3.1-pro-preview",
        provider="google",
        name="gemini-3.1-pro-preview",
        model="models/gemini-3.1-pro-preview",
        adapter="generate_content",
        structured_output=True,
    )

    payload = generate_content_payload(target, request)

    assert "tools" in payload
    assert payload["generationConfig"] == {
        "responseMimeType": "application/json",
        "responseJsonSchema": schema,
    }
    assert "systemInstruction" not in payload


def test_chat_completions_falls_back_for_deepseek_json_output() -> None:
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
    target = ModelTarget(
        ref="deepseek/deepseek-v4-pro",
        provider="deepseek",
        name="DeepSeek V4 Pro",
        model="deepseek-v4-pro",
        adapter="chat_completions",
        structured_output=True,
    )

    payload = chat_completions.chat_completion_payload(
        target,
        request,
        stream=False,
    )

    assert "response_format" not in payload
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
    target = ModelTarget(
        ref="anthropic/model",
        provider="anthropic",
        name="model",
        model="model",
        adapter="messages",
        structured_output=capability,
        options={"output_config": {"effort": "high"}},
    )

    payload = messages_payload(target, request, stream=False)

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
    target = ModelTarget(
        ref="provider/model",
        provider="provider",
        name="model",
        model="model",
        adapter="chat_completions",
        structured_output=False,
    )

    chat_payload = chat_completions.chat_completion_payload(
        target,
        request,
        stream=False,
    )
    response_payload = responses.response_payload(
        replace(target, adapter="responses"),
        request,
        stateful=False,
    )
    message_payload = messages_payload(
        replace(target, adapter="messages"),
        request,
        stream=False,
    )
    generate_payload = generate_content_payload(
        replace(target, adapter="generate_content"),
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
            ModelTarget(
                ref="openai/model",
                provider="openai",
                name="model",
                model="model",
                adapter="chat_completions",
                options={"response_format": {"type": "json_object"}},
            ),
            lambda target, request: chat_completions.chat_completion_payload(
                target, request, stream=False
            ),
        ),
        (
            ModelTarget(
                ref="openai/model",
                provider="openai",
                name="model",
                model="model",
                adapter="responses",
                options={"text": {"format": {"type": "json_object"}}},
            ),
            lambda target, request: responses.response_payload(
                target, request, stateful=False
            ),
        ),
        (
            ModelTarget(
                ref="anthropic/model",
                provider="anthropic",
                name="model",
                model="model",
                adapter="messages",
                options={"output_config": {"format": {"type": "json_schema"}}},
            ),
            lambda target, request: messages_payload(target, request, stream=False),
        ),
        (
            ModelTarget(
                ref="google/model",
                provider="google",
                name="model",
                model="model",
                adapter="generate_content",
                options={"generationConfig": {"responseSchema": {"type": "string"}}},
            ),
            generate_content_payload,
        ),
    ),
)
def test_protocol_adapters_reject_conflicting_structured_output_options(
    target: ModelTarget,
    build,
) -> None:
    request = ModelCall(
        instructions="",
        messages=[Message.user("hello")],
        output_schema={"type": "boolean"},
    )

    with pytest.raises(ToolangError, match="conflicts with normalized structured"):
        build(target, request)


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
