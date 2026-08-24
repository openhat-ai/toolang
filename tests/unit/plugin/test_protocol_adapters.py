from __future__ import annotations

from typing import Any, cast

from toolang.base.types.message import Message, TextPart, ToolCallPart, ToolResultPart
from toolang.base.types.model import ModelTarget
from toolang.base.types.run import ModelCall, ModelUsage, ToolCall
from toolang.base.types.tool import ToolDefinition
from toolang.plugin.models.adapters.generate_content import (
    generate_content_payload,
    parse_generate_content,
)
from toolang.plugin.models.adapters.messages import (
    messages_payload,
    parse_message_response,
)


def test_messages_payload_maps_reasoning_and_parse_normalizes_cache_usage() -> None:
    target = ModelTarget(
        ref="anthropic/claude-sonnet",
        provider="anthropic",
        name="claude-sonnet",
        model="claude-sonnet",
        adapter="messages",
        base_url="https://api.anthropic.com",
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
                "output_tokens": 7,
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
        output_tokens=7,
        input_uncached_tokens=40,
        input_cache_read_tokens=60,
        input_cache_write_tokens=10,
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
        state={"thought_signatures": {"call-1": "opaque-signature"}},
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
    assert result.state == {"thought_signatures": {"call-2": "next-signature"}}
    assert result.usage == ModelUsage(
        input_tokens=100,
        output_tokens=40,
        input_uncached_tokens=40,
        input_cache_read_tokens=60,
        output_visible_tokens=10,
        output_reasoning_tokens=30,
    )


def _tool() -> ToolDefinition:
    return ToolDefinition(
        name="shell__execute",
        description="Run a shell command.",
        parameters={"type": "object"},
    )
