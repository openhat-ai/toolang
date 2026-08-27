"""Anthropic Messages model adapter."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from decimal import Decimal
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
    ModelUsageMeter,
    ToolCall,
)

from ._usage import billing_value, reported_cost


@dataclass(frozen=True, slots=True)
class MessagesModelAdapter(ModelAdapter):
    """Anthropic Messages API adapter."""

    name: str = "messages"
    description: str | None = "Use the Anthropic Messages API shape."
    default_api: str | None = "https://api.anthropic.com/v1"

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
            result = parse_message_response(_json_object(response.json()))
            return replace(result, cont=_merge_cont(request.cont, result.cont))

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
        thinking_blocks: dict[int, dict[str, object]] = {}
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
                        elif index is not None and block.get("type") in {
                            "thinking",
                            "redacted_thinking",
                        }:
                            thinking_blocks[index] = dict(block)
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
                        elif delta.get("type") in {
                            "thinking_delta",
                            "signature_delta",
                        }:
                            index = _int(event.get("index"))
                            if index is not None:
                                block = thinking_blocks.setdefault(
                                    index, {"type": "thinking"}
                                )
                                key = (
                                    "thinking"
                                    if delta.get("type") == "thinking_delta"
                                    else "signature"
                                )
                                value = _text(delta.get(key))
                                if value:
                                    block[key] = _text(block.get(key)) + value
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
            cont=_merge_cont(
                request.cont,
                _thinking_cont(
                    thinking_blocks=thinking_blocks,
                    tool_blocks=tool_blocks,
                ),
            ),
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
    explicit_max_tokens = "max_tokens" in options
    max_tokens = options.pop("max_tokens", 4096)
    if (
        isinstance(max_tokens, bool)
        or not isinstance(max_tokens, int)
        or max_tokens <= 0
    ):
        raise ToolangError("Messages max_tokens must be a positive integer")
    budget = target.reasoning.get("budget_tokens")
    if isinstance(budget, int) and not isinstance(budget, bool):
        if budget <= 0:
            raise ToolangError("Messages thinking budget_tokens must be positive")
        if budget >= max_tokens:
            if explicit_max_tokens:
                raise ToolangError(
                    "Messages thinking budget_tokens must be lower than max_tokens"
                )
            max_tokens = budget + 1
    payload: dict[str, object] = {
        "model": target.model,
        "max_tokens": max_tokens,
        "messages": [
            _encode_message(
                message,
                thinking_blocks=_cont_thinking_blocks(request.cont),
            )
            for message in request.messages
        ],
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
    thinking: list[dict[str, object]] = []
    call_thinking: dict[str, list[dict[str, object]]] = {}
    content = payload.get("content")
    if isinstance(content, list):
        for raw in content:
            block = _json_object(raw)
            if block.get("type") == "text":
                value = _text(block.get("text"))
                if value:
                    parts.append(TextPart(value))
            elif block.get("type") in {"thinking", "redacted_thinking"}:
                thinking.append(dict(block))
            elif block.get("type") == "tool_use":
                call = _tool_call(block, fallback=f"tool-call-{len(calls)}")
                calls.append(call)
                parts.append(_tool_part(call))
                if thinking:
                    call_thinking[call.call_id] = thinking
                    thinking = []
    return ModelCallResult(
        message=Message(role="assistant", parts=tuple(parts)),
        tool_calls=tuple(calls),
        usage=messages_usage(_json_object(payload.get("usage"))),
        cont=({_THINKING_BLOCKS: call_thinking} if call_thinking else None),
    )


def messages_usage(value: Mapping[str, object]) -> ModelUsage | None:
    """Normalize Anthropic cache-aware token usage."""

    uncached = _int(value.get("input_tokens"))
    output = _int(value.get("output_tokens"))
    if uncached is None or output is None:
        return None
    cache_read = _int(value.get("cache_read_input_tokens"))
    cache_write = _int(value.get("cache_creation_input_tokens"))
    output_details = _json_object(value.get("output_tokens_details"))
    thinking = _int(output_details.get("thinking_tokens"))
    meters: list[ModelUsageMeter] = []
    cache_creation = _json_object(value.get("cache_creation"))
    for field, name in (
        ("ephemeral_5m_input_tokens", "anthropic.cache_write.5m"),
        ("ephemeral_1h_input_tokens", "anthropic.cache_write.1h"),
    ):
        quantity = _int(cache_creation.get(field))
        if quantity is not None and quantity > 0:
            meters.append(
                ModelUsageMeter(name=name, quantity=Decimal(quantity), unit="token")
            )
    server_tools = _json_object(value.get("server_tool_use"))
    for field, name in (
        ("web_search_requests", "anthropic.server_tool.web_search"),
        ("web_fetch_requests", "anthropic.server_tool.web_fetch"),
    ):
        quantity = _int(server_tools.get(field))
        if quantity is not None and quantity > 0:
            meters.append(
                ModelUsageMeter(name=name, quantity=Decimal(quantity), unit="request")
            )
    billing = {
        name: item
        for name, item in (
            ("service_tier", billing_value(value, "service_tier")),
            ("inference_geo", billing_value(value, "inference_geo")),
        )
        if item is not None
    }
    cost, currency = reported_cost(value)
    return ModelUsage(
        input_tokens=uncached + (cache_read or 0) + (cache_write or 0),
        output_tokens=output,
        input_uncached_tokens=uncached,
        input_cache_read_tokens=cache_read,
        input_cache_write_tokens=cache_write,
        output_visible_tokens=(output - thinking if thinking is not None else None),
        output_reasoning_tokens=thinking,
        meters=tuple(meters),
        reported_cost=cost,
        reported_currency=currency,
        billing=billing,
    )


_THINKING_BLOCKS = "anthropic_thinking_blocks"


def _cont_thinking_blocks(
    cont: Mapping[str, Any] | None,
) -> dict[str, tuple[dict[str, object], ...]]:
    if not isinstance(cont, Mapping):
        return {}
    raw = cont.get(_THINKING_BLOCKS)
    if not isinstance(raw, Mapping):
        return {}
    values: dict[str, tuple[dict[str, object], ...]] = {}
    for call_id, raw_blocks in raw.items():
        if not isinstance(raw_blocks, list | tuple):
            continue
        blocks = tuple(
            dict(cast(Mapping[str, object], block))
            for block in raw_blocks
            if isinstance(block, Mapping)
            and block.get("type") in {"thinking", "redacted_thinking"}
        )
        if blocks:
            values[str(call_id)] = blocks
    return values


def _thinking_cont(
    *,
    thinking_blocks: Mapping[int, Mapping[str, object]],
    tool_blocks: Mapping[int, Mapping[str, object]],
) -> dict[str, Any] | None:
    by_call: dict[str, list[dict[str, object]]] = {}
    pending: list[dict[str, object]] = []
    for index in sorted(set(thinking_blocks) | set(tool_blocks)):
        if block := thinking_blocks.get(index):
            pending.append(dict(block))
        if tool := tool_blocks.get(index):
            call_id = _text(tool.get("id")) or f"tool-call-{index}"
            if pending:
                by_call[call_id] = pending
                pending = []
    return {_THINKING_BLOCKS: by_call} if by_call else None


def _merge_cont(
    previous: Mapping[str, Any] | None,
    current: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    merged = dict(previous or {})
    merged.update(dict(current or {}))
    blocks = _cont_thinking_blocks(previous)
    blocks.update(_cont_thinking_blocks(current))
    if blocks:
        merged[_THINKING_BLOCKS] = {
            call_id: [dict(block) for block in values]
            for call_id, values in blocks.items()
        }
    return merged or None


def _encode_message(
    message: Message,
    *,
    thinking_blocks: Mapping[str, tuple[dict[str, object], ...]],
) -> dict[str, object]:
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
            call_id = part.call_id or part.tool_call_id
            content.extend(dict(block) for block in thinking_blocks.get(call_id, ()))
            content.append(
                {
                    "type": "tool_use",
                    "id": call_id,
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
        raise ToolangError("Messages adapter requires a resolved API")
    return f"{target.base_url.rstrip('/')}/messages"


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
    unknown = set(reasoning) - {"enabled", "effort", "budget_tokens"}
    if unknown:
        joined = ", ".join(sorted(unknown))
        raise ToolangError(f"unknown Messages reasoning controls: {joined}")
    enabled = reasoning.get("enabled")
    effort = reasoning.get("effort")
    budget = reasoning.get("budget_tokens")
    if enabled is False and effort not in (None, "none"):
        raise ToolangError("disabled Messages reasoning conflicts with an effort")
    if enabled is True and effort == "none":
        raise ToolangError("enabled Messages reasoning conflicts with effort 'none'")
    if (enabled is False or effort == "none") and budget is not None:
        raise ToolangError("disabled Messages reasoning conflicts with a token budget")
    if enabled is False or effort == "none":
        payload["thinking"] = {"type": "disabled"}
        return
    if isinstance(budget, int) and not isinstance(budget, bool):
        payload["thinking"] = {"type": "enabled", "budget_tokens": budget}
    elif enabled is True or isinstance(effort, str):
        payload["thinking"] = {"type": "adaptive"}
    if isinstance(effort, str):
        payload["output_config"] = {"effort": effort}
