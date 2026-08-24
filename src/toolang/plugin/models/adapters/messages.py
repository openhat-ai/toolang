"""Anthropic Messages model adapter."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json
from typing import Any, cast

import httpx

from toolang.base.errors import ToolangError
from toolang.base.protocols.model import ModelAdapter
from toolang.base.types.message import (
    DocumentPart,
    ImagePart,
    Message,
    TextDelta,
    TextPart,
    ToolCallPart,
    ToolResultPart,
)
from toolang.base.types.model import ModelTarget
from toolang.base.types.run import (
    ModelCall,
    ModelCallResult,
    ModelPartDelta,
    ModelPartEnd,
    ModelPartStart,
    ModelStreamHandler,
    ModelUsage,
    ToolCall,
)


@dataclass(frozen=True, slots=True)
class MessagesModelAdapter(ModelAdapter):
    """Anthropic Messages API adapter."""

    name: str = "messages"
    description: str | None = "Use the Anthropic Messages API shape."
    default_endpoint: str | None = "https://api.anthropic.com"

    async def invoke(
        self,
        target: ModelTarget,
        request: ModelCall,
    ) -> ModelCallResult:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                _messages_url(target),
                headers=_headers(target),
                json=messages_payload(target, request, stream=False),
            )
            response.raise_for_status()
            return parse_message_response(_json_object(response.json()))

    async def stream(
        self,
        target: ModelTarget,
        request: ModelCall,
        *,
        on_event: ModelStreamHandler,
    ) -> ModelCallResult:
        payload = messages_payload(target, request, stream=True)
        text: list[str] = []
        tool_blocks: dict[int, dict[str, object]] = {}
        usage: dict[str, object] = {}
        async with httpx.AsyncClient() as client:
            async with client.stream(
                "POST",
                _messages_url(target),
                headers=_headers(target),
                json=payload,
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    raw = line.removeprefix("data:").strip()
                    if not raw or raw == "[DONE]":
                        continue
                    event = _json_object(json.loads(raw))
                    event_type = event.get("type")
                    if event_type == "message_start":
                        message = _json_object(event.get("message"))
                        usage.update(_json_object(message.get("usage")))
                    elif event_type == "message_delta":
                        usage.update(_json_object(event.get("usage")))
                    elif event_type == "content_block_start":
                        index = _int(event.get("index"))
                        block = _json_object(event.get("content_block"))
                        if index is not None and block.get("type") == "tool_use":
                            tool_blocks[index] = dict(block)
                            await on_event(ModelPartStart(kind="tool_call"))
                    elif event_type == "content_block_delta":
                        delta = _json_object(event.get("delta"))
                        if delta.get("type") == "text_delta":
                            value = _text(delta.get("text"))
                            if value:
                                if not text:
                                    await on_event(ModelPartStart(kind="text"))
                                text.append(value)
                                await on_event(ModelPartDelta(delta=TextDelta(value)))
                        elif delta.get("type") == "input_json_delta":
                            index = _int(event.get("index"))
                            value = _text(delta.get("partial_json"))
                            if index is not None and value:
                                block = tool_blocks.setdefault(index, {})
                                block["partial_json"] = (
                                    _text(block.get("partial_json")) + value
                                )
        calls = tuple(
            _tool_call(block, fallback=f"tool-call-{index}")
            for index, block in sorted(tool_blocks.items())
        )
        parts: list[TextPart | ToolCallPart] = []
        output = "".join(text)
        if output:
            part = TextPart(output)
            parts.append(part)
            await on_event(ModelPartEnd(data=part))
        for call in calls:
            part = _tool_part(call)
            parts.append(part)
            await on_event(ModelPartEnd(data=part))
        message = Message(role="assistant", parts=tuple(parts))
        return ModelCallResult(
            message=message,
            tool_calls=calls,
            usage=messages_usage(usage),
        )


def create_model_adapter(config: Mapping[str, object]) -> ModelAdapter:
    """Create the built-in Messages adapter."""

    del config
    return MessagesModelAdapter()


def messages_payload(
    target: ModelTarget,
    request: ModelCall,
    *,
    stream: bool,
) -> dict[str, object]:
    """Encode one canonical request for Anthropic Messages."""

    options = dict(target.options)
    max_tokens = options.pop("max_tokens", 4096)
    payload: dict[str, object] = {
        "model": target.model,
        "max_tokens": max_tokens,
        "messages": [_encode_message(message) for message in request.messages],
        "stream": stream,
    }
    if request.instructions:
        payload["system"] = request.instructions
    if request.tools:
        payload["tools"] = [
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": dict(tool.parameters),
            }
            for tool in request.tools
        ]
    _apply_reasoning(payload, target.reasoning)
    payload.update(options)
    return payload


def parse_message_response(payload: Mapping[str, object]) -> ModelCallResult:
    """Parse one Anthropic Messages response."""

    parts: list[TextPart | ToolCallPart] = []
    calls: list[ToolCall] = []
    content = payload.get("content")
    if isinstance(content, list):
        for raw in content:
            block = _json_object(raw)
            if block.get("type") == "text":
                value = _text(block.get("text"))
                if value:
                    parts.append(TextPart(value))
            elif block.get("type") == "tool_use":
                call = _tool_call(block, fallback=f"tool-call-{len(calls)}")
                calls.append(call)
                parts.append(_tool_part(call))
    return ModelCallResult(
        message=Message(role="assistant", parts=tuple(parts)),
        tool_calls=tuple(calls),
        usage=messages_usage(_json_object(payload.get("usage"))),
    )


def messages_usage(value: Mapping[str, object]) -> ModelUsage | None:
    """Normalize Anthropic cache-aware token usage."""

    uncached = _int(value.get("input_tokens"))
    output = _int(value.get("output_tokens"))
    if uncached is None or output is None:
        return None
    cache_read = _int(value.get("cache_read_input_tokens")) or 0
    cache_write = _int(value.get("cache_creation_input_tokens")) or 0
    return ModelUsage(
        input_tokens=uncached + cache_read + cache_write,
        output_tokens=output,
        input_uncached_tokens=uncached,
        input_cache_read_tokens=cache_read,
        input_cache_write_tokens=cache_write,
    )


def _encode_message(message: Message) -> dict[str, object]:
    role = "assistant" if message.role == "assistant" else "user"
    content: list[dict[str, object]] = []
    for part in message.parts:
        if isinstance(part, TextPart):
            content.append({"type": "text", "text": part.text})
        elif isinstance(part, ImagePart) and part.image_url is not None:
            content.append(
                {
                    "type": "image",
                    "source": {"type": "url", "url": part.image_url},
                }
            )
        elif isinstance(part, DocumentPart) and part.data is not None:
            content.append(
                {
                    "type": "document",
                    "source": {
                        "type": "base64",
                        "media_type": part.media_type or "application/pdf",
                        "data": part.data,
                    },
                }
            )
        elif isinstance(part, ToolCallPart):
            content.append(
                {
                    "type": "tool_use",
                    "id": part.call_id or part.tool_call_id,
                    "name": part.tool_name,
                    "input": dict(part.input),
                }
            )
        elif isinstance(part, ToolResultPart):
            content.append(
                {
                    "type": "tool_result",
                    "tool_use_id": part.call_id or part.tool_call_id,
                    "content": json.dumps(part.output, ensure_ascii=False),
                    "is_error": part.error is not None,
                }
            )
    return {"role": role, "content": content}


def _messages_url(target: ModelTarget) -> str:
    if target.base_url is None:
        raise ToolangError("Messages adapter requires a resolved endpoint")
    base = target.base_url.rstrip("/")
    return f"{base}/messages" if base.endswith("/v1") else f"{base}/v1/messages"


def _headers(target: ModelTarget) -> dict[str, str]:
    if not target.api_key:
        raise ToolangError("Messages adapter requires a resolved API key")
    return {
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
        "x-api-key": target.api_key,
        **target.headers,
    }


def _tool_call(block: Mapping[str, object], *, fallback: str) -> ToolCall:
    call_id = _text(block.get("id")) or fallback
    name = _text(block.get("name"))
    raw_input = block.get("input")
    if not isinstance(raw_input, Mapping):
        partial = _text(block.get("partial_json")) or "{}"
        try:
            raw_input = _json_object(json.loads(partial))
        except json.JSONDecodeError:
            raw_input = {}
    return ToolCall(call_id, call_id, name, dict(cast(Mapping[str, Any], raw_input)))


def _tool_part(call: ToolCall) -> ToolCallPart:
    return ToolCallPart(
        tool_call_id=call.tool_call_id,
        call_id=call.call_id,
        tool_name=call.name,
        tool_family=call.name,
        input=dict(call.input),
    )


def _json_object(value: object) -> dict[str, object]:
    return dict(cast(Mapping[str, object], value)) if isinstance(value, Mapping) else {}


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _apply_reasoning(
    payload: dict[str, object],
    reasoning: Mapping[str, object],
) -> None:
    if not reasoning:
        return
    enabled = reasoning.get("enabled")
    effort = reasoning.get("effort")
    budget = reasoning.get("budget_tokens")
    if enabled is False or effort == "none":
        payload["thinking"] = {"type": "disabled"}
        return
    if isinstance(budget, int) and not isinstance(budget, bool):
        payload["thinking"] = {"type": "enabled", "budget_tokens": budget}
    elif enabled is True or isinstance(effort, str):
        payload["thinking"] = {"type": "adaptive"}
    if isinstance(effort, str):
        payload["output_config"] = {"effort": effort}
