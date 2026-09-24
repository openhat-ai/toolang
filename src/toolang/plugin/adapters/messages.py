"""Anthropic Messages model adapter."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json
import asyncio
from typing import cast

import httpx

from ._errors import (
    model_events,
    model_transport,
    model_transport_errors,
    provider_error,
    raise_for_model_status,
)
from ._tool_calls import parse_tool_arguments
from toolang.base.errors import ModelResponseError, ToolangError
from toolang.base.protocols.model import ModelAdapter
from toolang.base.types.message import (
    DocumentPart,
    ImagePart,
    Message,
    Part,
    ReasoningPart,
    ReasoningDelta,
    ToolCallDelta,
    TextDelta,
    TextPart,
    ToolCallPart,
    ToolResultPart,
)
from toolang.base.types.model import Model, Reasoning
from ._payload import output_allowance, request_options
from ._credentials import credential_value
from toolang.base.types.run import (
    ModelCall,
    ModelCallResult,
    ModelStreamHandler,
    ModelUsage,
    ModelUsageMeter,
    ToolCall,
)

from ._structured_output import append_structured_output_directive
from ._usage import billing_value, reported_cost
from ._parts import PartStream, compatible, native_metadata


@dataclass(frozen=True, slots=True)
class MessagesModelAdapter(ModelAdapter):
    """Anthropic Messages API adapter."""

    name: str = "messages"
    description: str | None = "Use the Anthropic Messages API shape."
    default_api: str | None = "https://api.anthropic.com/v1"

    def output_allowance(self, options: Mapping[str, object]) -> int | None:
        """Normalize this protocol's explicitly authored output allowance."""

        return output_allowance(options, "max_tokens")

    @model_transport
    async def invoke(
        self,
        model: Model,
        request: ModelCall,
        *,
        environ: Mapping[str, str],
    ) -> ModelCallResult:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                _messages_url(model),
                headers=_headers(model, environ=environ),
                json=messages_payload(model, request, stream=False),
            )
            await raise_for_model_status(response)
            return parse_message_response(_json_object(response.json()), model=model)

    @model_transport
    async def stream(
        self,
        model: Model,
        request: ModelCall,
        *,
        environ: Mapping[str, str],
        on_event: ModelStreamHandler,
    ) -> ModelCallResult:
        parts = PartStream(model_events(on_event))
        blocks: dict[int, dict[str, object]] = {}
        usage: dict[str, object] = {}
        stopped: set[int] = set()
        ended = False
        try:
            with model_transport_errors(
                usage=lambda: messages_usage(usage),
                partial_text=lambda: "".join(
                    _text(block.get("text"))
                    for block in blocks.values()
                    if block.get("type") == "text"
                ),
            ):
                async with httpx.AsyncClient() as client:
                    async with client.stream(
                        "POST",
                        _messages_url(model),
                        headers=_headers(model, environ=environ),
                        json=messages_payload(model, request, stream=True),
                    ) as response:
                        await raise_for_model_status(response)
                        async for line in response.aiter_lines():
                            if not line.startswith("data:"):
                                continue
                            raw = line.removeprefix("data:").strip()
                            if not raw or raw == "[DONE]":
                                continue
                            event = _json_object(json.loads(raw))
                            kind = event.get("type")
                            index = _int(event.get("index"))
                            if kind == "message_start":
                                usage.update(
                                    _json_object(
                                        _json_object(event.get("message")).get("usage")
                                    )
                                )
                            elif kind == "message_delta":
                                usage.update(_json_object(event.get("usage")))
                                _check_stop_reason(
                                    _json_object(event.get("delta")).get("stop_reason")
                                )
                            elif kind == "message_stop":
                                ended = True
                                break
                            elif kind == "error":
                                raise provider_error(event.get("error"))
                            elif kind == "content_block_start" and index is not None:
                                block = _json_object(event.get("content_block"))
                                blocks[index] = block
                                part = _message_part(block, index=index, model=model)
                                if part is not None:
                                    if isinstance(part, TextPart | ReasoningPart):
                                        await parts.text(
                                            index,
                                            part.text,
                                            reasoning=isinstance(part, ReasoningPart),
                                        )
                                    else:
                                        await parts.start(index, part)
                            elif kind == "content_block_delta" and index is not None:
                                block = blocks.get(index)
                                if block is None:
                                    raise ToolangError(
                                        "Messages delta refers to an unknown content block"
                                    )
                                delta = _json_object(event.get("delta"))
                                delta_type = _text(delta.get("type"))
                                if delta_type in {
                                    "text_delta",
                                    "thinking_delta",
                                    "signature_delta",
                                }:
                                    key = {
                                        "text_delta": "text",
                                        "thinking_delta": "thinking",
                                        "signature_delta": "signature",
                                    }[delta_type]
                                    value = _text(delta.get(key))
                                    block[key] = _text(block.get(key)) + value
                                    if delta_type != "signature_delta":
                                        await parts.delta(
                                            index,
                                            ReasoningDelta(value)
                                            if delta_type == "thinking_delta"
                                            else TextDelta(value),
                                        )
                                elif delta_type == "input_json_delta":
                                    value = _text(delta.get("partial_json"))
                                    block["partial_json"] = (
                                        _text(block.get("partial_json")) + value
                                    )
                                    block.pop("input", None)
                                    call_id = (
                                        _text(block.get("id")) or f"tool-call-{index}"
                                    )
                                    await parts.delta(
                                        index, ToolCallDelta(value, call_id)
                                    )
                            elif kind == "content_block_stop" and index is not None:
                                block = blocks[index]
                                part = _message_part(block, index=index, model=model)
                                if part is not None:
                                    await parts.finish(index, part)
                                stopped.add(index)
                if not ended:
                    raise ModelResponseError(
                        "model stream ended before message_stop",
                        kind="incomplete_stream",
                    )
                # Text/tool values are still usable on compatible APIs without block-stop
                # events. Opaque reasoning state requires an explicit completed block.
                for index, block in blocks.items():
                    if index not in stopped:
                        part = _message_part(block, index=index, model=model)
                        if isinstance(part, ReasoningPart):
                            part = ReasoningPart(part.text)
                        if part is not None:
                            await parts.finish(index, part)
        except (Exception, asyncio.CancelledError):
            await parts.interrupt()
            raise
        message = parts.message()
        return ModelCallResult(
            message=message,
            tool_calls=tuple(
                _tool_call(block, fallback=f"tool-call-{index}")
                for index, block in blocks.items()
                if block.get("type") == "tool_use"
            ),
            usage=messages_usage(usage),
        )


def create_model_adapter(config: Mapping[str, object]) -> ModelAdapter:
    """Create the built-in Messages adapter."""

    del config
    return MessagesModelAdapter()


def messages_payload(
    model: Model,
    request: ModelCall,
    *,
    stream: bool,
) -> dict[str, object]:
    """Encode one canonical request for Anthropic Messages."""

    native_schema = request.output_schema if model.structured_output is True else None
    instructions = (
        append_structured_output_directive(
            request.instructions,
            request.output_schema,
        )
        if request.output_schema is not None and native_schema is None
        else request.instructions
    )
    options = request_options(model._toolang.route.options)
    configured_max_tokens = options.pop("max_tokens", None)
    max_tokens = (
        request.max_output_tokens
        if request.max_output_tokens is not None
        else configured_max_tokens
    )
    if max_tokens is None:
        raise ToolangError(
            "Messages requires an output allowance; set max_output or a model "
            "output limit"
        )
    if (
        isinstance(max_tokens, bool)
        or not isinstance(max_tokens, int)
        or max_tokens <= 0
    ):
        raise ToolangError("Messages max_tokens must be a positive integer")
    budget = request.reasoning.budget_tokens if request.reasoning else None
    if isinstance(budget, int) and not isinstance(budget, bool):
        if budget <= 0:
            raise ToolangError("Messages thinking budget_tokens must be positive")
        if budget >= max_tokens:
            raise ToolangError(
                "Messages thinking budget_tokens must be lower than max_tokens"
            )
    payload: dict[str, object] = {
        "model": model.id,
        "max_tokens": max_tokens,
        "messages": [
            _encode_message(message, model=model) for message in request.messages
        ],
        "stream": stream,
    }
    if instructions:
        payload["system"] = instructions
    if request.tools:
        payload["tools"] = [
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": dict(tool.parameters),
            }
            for tool in request.tools
        ]
    payload.update(options)
    _apply_reasoning(payload, request.reasoning)
    _apply_structured_output(
        payload,
        request.output_schema,
        native_schema=native_schema,
    )
    return payload


def _check_stop_reason(reason: object) -> None:
    if reason == "max_tokens":
        raise ModelResponseError(
            "model response truncated by output limit", kind="output_limit"
        )
    if reason in {"refusal", "model_context_window_exceeded"}:
        raise ModelResponseError(
            f"provider stopped response: {reason}", kind="provider_rejection"
        )


def parse_message_response(
    payload: Mapping[str, object], *, model: Model
) -> ModelCallResult:
    """Parse one Anthropic Messages response with Part-owned native state."""

    parts: list[Part] = []
    calls: list[ToolCall] = []
    content = payload.get("content")
    with model_transport_errors(
        usage=lambda: messages_usage(_json_object(payload.get("usage"))),
        partial_text=lambda: "".join(
            _text(_json_object(block).get("text"))
            for block in (content if isinstance(content, list) else ())
            if _json_object(block).get("type") == "text"
        ),
    ):
        if "error" in payload:
            raise provider_error(payload["error"])
        _check_stop_reason(payload.get("stop_reason"))
        if isinstance(content, list):
            for index, raw in enumerate(content):
                block = _json_object(raw)
                part = _message_part(block, index=index, model=model)
                if part is not None:
                    parts.append(part)
                if block.get("type") == "tool_use":
                    calls.append(_tool_call(block, fallback=f"tool-call-{index}"))
    return ModelCallResult(
        message=Message(role="assistant", parts=tuple(parts)),
        tool_calls=tuple(calls),
        usage=messages_usage(_json_object(payload.get("usage"))),
    )


def _message_part(
    block: Mapping[str, object], *, index: int, model: Model
) -> Part | None:
    kind = block.get("type")
    if kind == "text":
        return TextPart(_text(block.get("text")))
    if kind in {"thinking", "redacted_thinking"}:
        return ReasoningPart(
            text=_text(block.get("thinking")) if kind == "thinking" else "",
            signature=_text(block.get("signature" if kind == "thinking" else "data"))
            or None,
            provider=model._toolang.provider,
            provider_metadata=native_metadata(
                model, "messages", type=kind, index=index
            ),
        )
    if kind == "tool_use":
        return _tool_part(_tool_call(block, fallback=f"tool-call-{index}"))
    return None


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
                ModelUsageMeter(name=name, quantity=float(quantity), unit="token")
            )
    server_tools = _json_object(value.get("server_tool_use"))
    for field, name in (
        ("web_search_requests", "anthropic.server_tool.web_search"),
        ("web_fetch_requests", "anthropic.server_tool.web_fetch"),
    ):
        quantity = _int(server_tools.get(field))
        if quantity is not None and quantity > 0:
            meters.append(
                ModelUsageMeter(name=name, quantity=float(quantity), unit="request")
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


def _apply_structured_output(
    payload: dict[str, object],
    schema: dict[str, object] | None,
    *,
    native_schema: dict[str, object] | None,
) -> None:
    if schema is None:
        return
    raw_current = payload.get("output_config")
    if raw_current is not None and not isinstance(raw_current, Mapping):
        raise ToolangError(
            "Messages output_config format conflicts with normalized structured output"
        )
    current = (
        dict(cast(Mapping[str, object], raw_current)) if raw_current is not None else {}
    )
    if "format" in current:
        raise ToolangError(
            "Messages output_config format conflicts with normalized structured output"
        )
    if native_schema is not None:
        current["format"] = {
            "type": "json_schema",
            "schema": dict(native_schema),
        }
    if current:
        payload["output_config"] = current


def _encode_message(
    message: Message,
    *,
    model: Model,
) -> dict[str, object]:
    role = "assistant" if message.role == "assistant" else "user"
    content: list[dict[str, object]] = []
    for part in message.parts:
        if isinstance(part, ReasoningPart):
            if message.role != "assistant" or not compatible(part, model, "messages"):
                continue
            kind = part.provider_metadata.get("type")
            if kind not in {"thinking", "redacted_thinking"} or not part.signature:
                raise ToolangError(
                    "Messages reasoning requires a native block type and signature"
                )
            content.append(
                {"type": "thinking", "thinking": part.text, "signature": part.signature}
                if kind == "thinking"
                else {"type": "redacted_thinking", "data": part.signature}
            )
        elif isinstance(part, TextPart):
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
            content.append(
                {
                    "type": "tool_use",
                    "id": call_id,
                    "name": part.tool_name,
                    "input": dict(part.input),
                }
            )
        elif isinstance(part, ToolResultPart):
            output = dict(part.output)
            if part.error is not None:
                output = {"error": part.error, **({"output": output} if output else {})}
            content.append(
                {
                    "type": "tool_result",
                    "tool_use_id": part.call_id or part.tool_call_id,
                    "content": json.dumps(output, ensure_ascii=False),
                    "is_error": part.error is not None,
                }
            )
    return {"role": role, "content": content}


def _messages_url(model: Model) -> str:
    if model._toolang.route.api is None:
        raise ToolangError("Messages adapter requires a resolved API")
    return f"{model._toolang.route.api.rstrip('/')}/messages"


def _headers(
    model: Model,
    *,
    environ: Mapping[str, str],
) -> dict[str, str]:
    api_key = credential_value(model._toolang.route.env, environ=environ)
    if not api_key and model._toolang.route.env != ():
        raise ToolangError("Messages adapter requires a resolved API key")
    return {
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
        **({"x-api-key": api_key} if api_key else {}),
        **model._toolang.route.headers,
    }


def _tool_call(block: Mapping[str, object], *, fallback: str) -> ToolCall:
    call_id = _text(block.get("id")) or fallback
    name = _text(block.get("name")).strip()
    if not name:
        raise ModelResponseError(
            "model emitted a tool call without a function name", kind="missing_name"
        )
    # A streaming start block has input={}; the following deltas are authoritative.
    raw_input = block.get("partial_json", block.get("input"))
    return ToolCall(call_id, call_id, name, parse_tool_arguments(raw_input))


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
    reasoning: Reasoning | None,
) -> None:
    if reasoning is None:
        return
    effort = reasoning.effort
    budget = reasoning.budget_tokens
    if effort is None and budget is None:
        return
    disabled = effort == "none"
    if disabled and budget is not None:
        raise ToolangError("disabled Messages reasoning conflicts with a token budget")
    thinking = _json_object(payload.get("thinking"))
    for key in ("type", "budget_tokens"):
        thinking.pop(key, None)
    raw_output_config = payload.get("output_config")
    if raw_output_config is not None and not isinstance(raw_output_config, Mapping):
        raise ToolangError("Messages output_config must be an object")
    output_config = (
        dict(cast(Mapping[str, object], raw_output_config))
        if raw_output_config is not None
        else {}
    )
    output_config.pop("effort", None)
    if disabled:
        payload["thinking"] = {"type": "disabled"}
    elif isinstance(budget, int) and not isinstance(budget, bool):
        payload["thinking"] = {**thinking, "type": "enabled", "budget_tokens": budget}
    elif isinstance(effort, str):
        payload["thinking"] = {**thinking, "type": "adaptive"}
    if isinstance(effort, str) and not disabled:
        output_config["effort"] = effort
    if output_config:
        payload["output_config"] = output_config
    else:
        payload.pop("output_config", None)
