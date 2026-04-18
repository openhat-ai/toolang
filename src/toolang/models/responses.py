"""Helpers shared by Responses-compatible model providers and adapters."""

from __future__ import annotations

import json
from typing import Any

from toolang.base.error import ToolangError
from toolang.base.types.message import Message, TextDelta, TextPart, ToolCallDelta, ToolCallPart, ToolResultPart
from toolang.base.types.model import ModelTarget
from toolang.base.types.run import (
    ModelCall,
    ModelCallResult,
    ModelEventHandler,
    ModelPartDeltaEvent,
    ModelPartEndEvent,
    ModelPartStartEvent,
    ModelUsage,
    ToolCall,
)
from toolang.base.types.tool import ToolDefinition


def create_client(target: ModelTarget) -> Any:
    """Create one OpenAI-compatible client for a resolved model target."""

    try:
        from openai import OpenAI
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise ToolangError(
            "The 'openai' package is not installed. Reinstall toolang with its runtime dependencies to enable runtime execution."
        ) from exc
    kwargs: dict[str, Any] = {}
    if target.base_url:
        kwargs["base_url"] = target.base_url
    if target.api_key is not None:
        kwargs["api_key"] = target.api_key
    if target.headers:
        kwargs["default_headers"] = dict(target.headers)
    return OpenAI(**kwargs)


def invoke_response(
    target: ModelTarget,
    request: ModelCall,
    *,
    stateful: bool,
) -> ModelCallResult:
    """Execute one non-streaming Responses API call."""

    client = create_client(target)
    response = client.responses.create(
        **response_payload(
            target,
            request,
            stateful=stateful,
        )
    )
    return parse_response(
        response,
        request=request,
        stateful=stateful,
    )


def stream_response(
    target: ModelTarget,
    request: ModelCall,
    *,
    stateful: bool,
    on_event: ModelEventHandler,
) -> ModelCallResult:
    """Execute one streaming Responses API call."""

    client = create_client(target)
    with client.responses.stream(
        **response_payload(
            target,
            request,
            stateful=stateful,
        )
    ) as stream:
        seen_tool_inputs: set[str] = set()
        text_started = False
        for event in stream:
            event_type = getattr(event, "type", None)
            if event_type == "response.output_text.delta":
                delta = str(getattr(event, "delta", ""))
                if delta:
                    if not text_started:
                        text_started = True
                        on_event(ModelPartStartEvent(kind="text"))
                    on_event(ModelPartDeltaEvent(delta=TextDelta(text=delta)))
                continue
            if event_type != "response.function_call_arguments.delta":
                continue
            current_tool_call_id = tool_call_id(
                getattr(event, "item_id", ""),
                getattr(event, "call_id", ""),
                fallback=f"tool-call-{getattr(event, 'output_index', None) or 'unknown'}",
            )
            if current_tool_call_id not in seen_tool_inputs:
                seen_tool_inputs.add(current_tool_call_id)
                on_event(
                    ModelPartStartEvent(
                        kind="tool_call",
                    )
                )
            delta = str(getattr(event, "delta", ""))
            if delta:
                on_event(
                    ModelPartDeltaEvent(
                        delta=ToolCallDelta(
                            text=delta,
                            tool_call_id=current_tool_call_id,
                        ),
                    )
                )
        response = stream.get_final_response()
    result = parse_response(
        response,
        request=request,
        stateful=stateful,
    )
    if result.message is not None:
        for part in result.message.parts:
            if isinstance(part, (TextPart, ToolCallPart)):
                on_event(ModelPartEndEvent(data=part))
    return result


def response_payload(
    target: ModelTarget,
    request: ModelCall,
    *,
    stateful: bool,
) -> dict[str, Any]:
    """Build one Responses API payload."""

    state = dict(request.state or {})
    previous_response_id = state.get("previous_response_id") if stateful else None
    baseline_count = state.get("baseline_count") if stateful else None
    message_offset = baseline_count if isinstance(baseline_count, int) and baseline_count >= 0 else 0
    messages = request.messages[message_offset:] if previous_response_id else request.messages
    payload: dict[str, Any] = {
        "model": target.model,
        "input": response_input(
            instructions=request.instructions,
            messages=messages,
            include_instructions=not bool(previous_response_id),
            replay_tool_items=not stateful or bool(previous_response_id),
        ),
    }
    if request.tools:
        payload["tools"] = [tool_payload(item) for item in request.tools]
    if isinstance(previous_response_id, str) and previous_response_id.strip():
        payload["previous_response_id"] = previous_response_id
    options = dict(target.options)
    if options:
        payload.update(options)
    return payload


def parse_response(
    response: Any,
    *,
    request: ModelCall,
    stateful: bool,
) -> ModelCallResult:
    """Normalize one Responses API response object."""

    tool_calls = tuple(parse_tool_calls(response))
    message = assistant_message(response, tool_calls=tool_calls)
    return ModelCallResult(
        message=message,
        tool_calls=tool_calls,
        usage=response_usage(response),
        state=response_state(
            response,
            request=request,
            emitted_message=message,
            stateful=stateful,
        ),
    )


def response_input(
    *,
    instructions: str,
    messages: list[Message],
    include_instructions: bool,
    replay_tool_items: bool,
) -> list[dict[str, Any]]:
    """Build one replayable typed Responses API input list."""

    results: list[dict[str, Any]] = []
    if include_instructions and instructions.strip():
        results.append(
            _message_item(
                role="developer",
                parts=[{"type": "input_text", "text": instructions.strip()}],
            )
        )
    for message_index, message in enumerate(messages):
        encoded = encode_message(
            message,
            replay_tool_items=replay_tool_items,
            message_index=message_index,
        )
        if encoded is None:
            continue
        if isinstance(encoded, list):
            results.extend(encoded)
        else:
            results.append(encoded)
    return results


def encode_message(
    message: Message,
    *,
    replay_tool_items: bool = True,
    message_index: int | None = None,
) -> dict[str, Any] | list[dict[str, Any]] | None:
    """Encode one run-strategy message into typed Responses API input items."""

    role = message.role.strip()
    if role in {"user", "assistant"}:
        return _encode_actor_message(
            message,
            replay_tool_items=replay_tool_items,
            message_index=message_index,
        )
    if role == "tool":
        if not replay_tool_items:
            return None
        items = [
            _encode_tool_result_part(
                part,
                message=message,
                message_index=message_index,
                part_index=part_index,
            )
            for part_index, part in enumerate(message.parts)
        ]
        if not items:
            return None
        return items[0] if len(items) == 1 else items
    return None


def tool_payload(definition: ToolDefinition) -> dict[str, Any]:
    """Return one Responses-compatible tool definition payload."""

    return {
        "type": "function",
        "name": definition.name,
        "description": definition.description,
        "parameters": dict(definition.parameters),
    }


def response_text(response: Any) -> str:
    """Extract one response text value, allowing empty tool-only turns."""

    text = getattr(response, "output_text", None)
    if isinstance(text, str):
        return text
    collected: list[str] = []
    for item in getattr(response, "output", []):
        if getattr(item, "type", None) != "message":
            continue
        for content in getattr(item, "content", []):
            content_type = getattr(content, "type", None)
            if content_type in {"output_text", "text"} and getattr(content, "text", None):
                collected.append(str(content.text))
    return "".join(collected)


def parse_tool_calls(response: Any) -> list[ToolCall]:
    """Extract one normalized tool-call list from a response."""

    results: list[ToolCall] = []
    for item in getattr(response, "output", []):
        if getattr(item, "type", None) != "function_call":
            continue
        results.append(
            ToolCall(
                tool_call_id=tool_call_id(
                    getattr(item, "id", ""),
                    getattr(item, "call_id", ""),
                    fallback=str(getattr(item, "call_id", "")),
                ),
                call_id=str(getattr(item, "call_id", "")),
                name=str(getattr(item, "name", "")).strip(),
                input=parse_tool_arguments(getattr(item, "arguments", "{}")),
            )
        )
    return results


def assistant_message(
    response: Any,
    *,
    tool_calls: tuple[ToolCall, ...],
) -> Message | None:
    """Return one canonical assistant message from one response."""

    parts: list[TextPart | ToolCallPart] = []
    text = response_text(response)
    if text:
        parts.append(TextPart(text=text))
    for call in tool_calls:
        parts.append(
            ToolCallPart(
                tool_call_id=call.tool_call_id,
                call_id=call.call_id,
                tool_name=call.name,
                tool_family=call.name,
                input=dict(call.input),
            )
        )
    if not parts:
        return None
    return Message(role="assistant", parts=tuple(parts))


def response_state(
    response: Any,
    *,
    request: ModelCall,
    emitted_message: Message | None,
    stateful: bool,
) -> dict[str, Any] | None:
    """Return one opaque state payload for the next model turn."""

    if not stateful:
        return None
    response_id = getattr(response, "id", None)
    if not isinstance(response_id, str) or not response_id.strip():
        return None
    return {
        "previous_response_id": response_id,
        "baseline_count": len(request.messages) + (1 if emitted_message is not None else 0),
    }


def response_usage(response: Any) -> ModelUsage | None:
    """Extract one normalized model usage summary."""

    usage = getattr(response, "usage", None)
    input_tokens = getattr(usage, "input_tokens", None)
    output_tokens = getattr(usage, "output_tokens", None)
    if not isinstance(input_tokens, int) or not isinstance(output_tokens, int):
        return None
    return ModelUsage(input_tokens=input_tokens, output_tokens=output_tokens)


def parse_tool_arguments(raw: object) -> dict[str, Any]:
    """Parse one function-call argument payload into a JSON object."""

    if isinstance(raw, dict):
        return {str(key): value for key, value in raw.items()}
    if raw is None:
        return {}
    try:
        parsed = json.loads(str(raw))
    except json.JSONDecodeError as exc:
        raise ToolangError(f"tool call arguments were not valid JSON: {raw}") from exc
    if not isinstance(parsed, dict):
        raise ToolangError("tool call arguments must decode to a JSON object")
    return dict(parsed)


def tool_call_id(*values: object, fallback: str) -> str:
    """Return one stable tool-call identifier."""

    for value in values:
        text = str(value).strip()
        if text:
            return text
    return fallback


def _encode_actor_message(
    message: Message,
    *,
    replay_tool_items: bool,
    message_index: int | None,
) -> dict[str, Any] | list[dict[str, Any]] | None:
    items: list[dict[str, Any]] = []
    text_buffer: list[dict[str, Any]] = []
    text_type = "output_text" if message.role == "assistant" else "input_text"
    text_item_index = 0

    def _flush_text_buffer() -> None:
        nonlocal text_buffer
        nonlocal text_item_index
        if not text_buffer:
            return
        items.append(
            _message_item(
                role=message.role,
                parts=text_buffer,
                message_index=message_index,
                item_index=text_item_index,
            )
        )
        text_buffer = []
        text_item_index += 1

    for part in message.parts:
        if isinstance(part, TextPart):
            text_buffer.append({"type": text_type, "text": part.text})
            continue
        _flush_text_buffer()
        if isinstance(part, ToolCallPart):
            if not replay_tool_items:
                continue
            items.append(_encode_tool_call_part(part))
            continue
        if isinstance(part, ToolResultPart):
            raise ToolangError("assistant/user messages cannot contain tool result parts")
    _flush_text_buffer()
    if not items:
        return None
    return items[0] if len(items) == 1 else items


def _message_item(
    *,
    role: str,
    parts: list[dict[str, Any]],
    message_index: int | None = None,
    item_index: int | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": "message",
        "role": role,
        "content": list(parts),
    }
    if role == "assistant":
        suffix = "current"
        if message_index is not None:
            suffix = str(message_index)
            if item_index not in {None, 0}:
                suffix = f"{suffix}_{item_index}"
        elif item_index not in {None, 0}:
            suffix = f"{suffix}_{item_index}"
        payload["id"] = f"msg_{suffix}"
        payload["status"] = "completed"
    return payload


def _encode_tool_call_part(part: ToolCallPart) -> dict[str, Any]:
    return {
        "type": "function_call",
        "id": part.tool_call_id,
        "call_id": part.call_id or part.tool_call_id,
        "name": part.tool_name,
        "arguments": json.dumps(part.input, ensure_ascii=False, separators=(",", ":")),
    }


def _encode_tool_result_part(
    part: object,
    *,
    message: Message,
    message_index: int | None,
    part_index: int,
) -> dict[str, Any]:
    if not isinstance(part, ToolResultPart):
        raise ToolangError("tool messages can only contain tool result parts")
    payload: dict[str, Any] = {
        "ok": "error" not in message.meta,
        "name": part.tool_name,
        "output": dict(part.output),
    }
    if "error" in message.meta:
        payload["error"] = str(message.meta["error"])
    call_id = part.call_id or part.tool_call_id
    if not call_id:
        raise ToolangError("tool follow-up message is missing call_id")
    suffix = "current" if message_index is None else str(message_index)
    return {
        "type": "function_call_output",
        "id": part.tool_call_id or f"fc_output_{suffix}_{part_index}",
        "call_id": call_id,
        "output": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
    }
