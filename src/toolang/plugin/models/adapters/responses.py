"""Responses-compatible model adapter."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast

from toolang.base.errors import ToolangError
from toolang.base.protocols.model import ModelAdapter
from toolang.base.types.message import (
    AudioFormat,
    AudioPart,
    DocumentPart,
    ImagePart,
    Message,
    TextDelta,
    TextPart,
    ToolCallDelta,
    ToolCallPart,
    ToolResultPart,
    message_summary,
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
from toolang.base.types.tool import ToolDefinition

_ADAPTER_LOGGER = logging.getLogger("toolang.model.adapter")
_LOG_PREVIEW_LIMIT = 4_000
_STATEFUL_PROVIDERS = frozenset({"openai"})
_AUDIO_MODEL_PREFIXES: tuple[str, ...] = (
    "gpt-audio",
    "gpt-4o-audio-preview",
    "gpt-4o-mini-audio-preview",
)


@dataclass(frozen=True, slots=True)
class ResponsesModelAdapter(ModelAdapter):
    """OpenAI Responses API compatible adapter."""

    name: str = "responses"
    description: str | None = "Use the OpenAI Responses-compatible API shape."

    async def invoke(
        self,
        target: ModelTarget,
        request: ModelCall,
    ) -> ModelCallResult:
        """Execute one non-streaming Responses API call."""

        _require_supported_inputs(target, request)
        return await invoke_response(
            target,
            request,
            stateful=_stateful_target(target),
        )

    async def stream(
        self,
        target: ModelTarget,
        request: ModelCall,
        *,
        on_event: ModelStreamHandler,
    ) -> ModelCallResult:
        """Execute one streaming Responses API call."""

        _require_supported_inputs(target, request)
        return await stream_response(
            target,
            request,
            stateful=_stateful_target(target),
            on_event=on_event,
        )


def create_model_adapter(config: Mapping[str, object]) -> ModelAdapter:
    """Create the built-in Responses model adapter."""

    del config
    return ResponsesModelAdapter()


def _stateful_target(target: ModelTarget) -> bool:
    return target.provider in _STATEFUL_PROVIDERS


def _require_supported_inputs(
    target: ModelTarget,
    request: ModelCall,
) -> None:
    if target.provider != "openai":
        return
    if _supports_openai_audio_input(target):
        return
    if not _request_has_audio_input(request):
        return
    raise ToolangError(
        f"audio input is not supported for OpenAI model '{target.model}' via the Responses adapter; "
        "transcribe audio first, send it as a generic file to a route that supports audio files, or use an audio-capable model route"
    )


def _supports_openai_audio_input(target: ModelTarget) -> bool:
    candidates = (
        target.model.strip().lower(),
        target.ref.strip().lower(),
        target.name.strip().lower(),
    )
    return any(
        candidate.startswith(prefix)
        for candidate in candidates
        for prefix in _AUDIO_MODEL_PREFIXES
    )


def _request_has_audio_input(request: ModelCall) -> bool:
    return any(
        isinstance(part, AudioPart)
        for message in request.messages
        if message.role == "user"
        for part in message.parts
    )


def create_client(target: ModelTarget) -> Any:
    """Create one OpenAI-compatible client for a resolved model target."""

    try:
        from openai import AsyncOpenAI
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
    return AsyncOpenAI(**kwargs)


async def invoke_response(
    target: ModelTarget,
    request: ModelCall,
    *,
    stateful: bool,
) -> ModelCallResult:
    """Execute one non-streaming Responses API call."""

    client = create_client(target)
    payload = response_payload(
        target,
        request,
        stateful=stateful,
    )
    _log_api_request(
        target,
        payload,
        stateful=stateful,
        stream=False,
    )
    response = await client.responses.create(
        **payload
    )
    _log_api_response(
        target,
        response,
        stateful=stateful,
        stream=False,
    )
    return parse_response(
        response,
        request=request,
        stateful=stateful,
    )


async def stream_response(
    target: ModelTarget,
    request: ModelCall,
    *,
    stateful: bool,
    on_event: ModelStreamHandler,
) -> ModelCallResult:
    """Execute one streaming Responses API call."""

    client = create_client(target)
    payload = response_payload(
        target,
        request,
        stateful=stateful,
    )
    _log_api_request(
        target,
        payload,
        stateful=stateful,
        stream=True,
    )
    async with client.responses.stream(
        **payload
    ) as stream:
        seen_tool_inputs: set[str] = set()
        text_started = False
        text_deltas: list[str] = []
        defer_text = _supports_openai_audio_input(target)
        async for event in stream:
            event_type = getattr(event, "type", None)
            if event_type == "response.output_text.delta":
                delta = str(getattr(event, "delta", ""))
                if delta:
                    text_deltas.append(delta)
                    if not defer_text:
                        if not text_started:
                            text_started = True
                            await on_event(ModelPartStart(kind="text"))
                        await on_event(ModelPartDelta(delta=TextDelta(text=delta)))
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
                await on_event(
                    ModelPartStart(
                        kind="tool_call",
                    )
                )
            delta = str(getattr(event, "delta", ""))
            if delta:
                await on_event(
                    ModelPartDelta(
                        delta=ToolCallDelta(
                            text=delta,
                            tool_call_id=current_tool_call_id,
                        ),
                    )
                )
        response = await stream.get_final_response()
    _log_api_response(
        target,
        response,
        stateful=stateful,
        stream=True,
    )
    result = parse_response(
        response,
        request=request,
        stateful=stateful,
    )
    if defer_text and _message_has_text(result.message):
        await on_event(ModelPartStart(kind="text"))
        for delta in text_deltas:
            await on_event(ModelPartDelta(delta=TextDelta(text=delta)))
    if result.message is not None:
        for part in result.message.parts:
            if isinstance(part, (ImagePart, AudioPart)):
                await on_event(ModelPartStart(kind=part.type))
                await on_event(ModelPartEnd(data=part))
            elif isinstance(part, (TextPart, ToolCallPart)):
                await on_event(ModelPartEnd(data=part))
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
    """Encode one run-loop message into typed Responses API input items."""

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

    audio_parts = _response_audio_parts(response)
    image_parts = _response_image_parts(response)
    parts: list[TextPart | ImagePart | AudioPart | ToolCallPart] = []
    text = response_text(response)
    transcripts = {part.transcript for part in audio_parts if part.transcript}
    if text and text not in transcripts:
        parts.append(TextPart(text=text))
    parts.extend(image_parts)
    parts.extend(audio_parts)
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


def _response_audio_parts(response: Any) -> list[AudioPart]:
    parts: list[AudioPart] = []
    for item in getattr(response, "output", ()):
        candidates = [item, *list(getattr(item, "content", ()))]
        for candidate in candidates:
            if getattr(candidate, "type", None) not in {
                "audio",
                "output_audio",
            }:
                continue
            data = _value_text(candidate, "data")
            if not data:
                continue
            raw_format = _value_text(candidate, "format").lower()
            format = raw_format if raw_format in {"mp3", "wav"} else "wav"
            parts.append(
                AudioPart(
                    data=data,
                    format=cast(AudioFormat, format),
                    transcript=_value_text(candidate, "transcript") or None,
                )
            )
    return parts


def _response_image_parts(response: Any) -> list[ImagePart]:
    parts: list[ImagePart] = []
    for item in getattr(response, "output", ()):
        candidates = [item, *list(getattr(item, "content", ()))]
        for candidate in candidates:
            candidate_type = getattr(candidate, "type", None)
            if candidate_type == "image_generation_call":
                data = _value_text(candidate, "result")
                if data:
                    parts.append(
                        ImagePart(
                            image_url=f"data:image/png;base64,{data}",
                            media_type="image/png",
                        )
                    )
                continue
            if candidate_type not in {"image", "output_image"}:
                continue
            file_id = _value_text(candidate, "file_id") or None
            image_url = (
                _value_text(candidate, "image_url")
                or _value_text(candidate, "url")
                or None
            )
            if file_id is not None:
                parts.append(ImagePart(file_id=file_id))
            elif image_url is not None:
                parts.append(ImagePart(image_url=image_url))
    return parts


def _value_text(value: object, name: str) -> str:
    raw = (
        cast(Mapping[str, object], value).get(name)
        if isinstance(value, Mapping)
        else getattr(value, name, None)
    )
    return raw if isinstance(raw, str) else ""


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
        if isinstance(part, ImagePart):
            if message.role == "assistant":
                text_buffer.append(
                    {"type": text_type, "text": message_summary((part,))}
                )
                continue
            text_buffer.append(_encode_image_part(part))
            continue
        if isinstance(part, AudioPart):
            if message.role == "assistant":
                text_buffer.append(
                    {
                        "type": text_type,
                        "text": part.transcript or message_summary((part,)),
                    }
                )
                continue
            text_buffer.append(_encode_audio_part(part))
            continue
        if isinstance(part, DocumentPart):
            if message.role == "assistant":
                text_buffer.append(
                    {"type": text_type, "text": message_summary((part,))}
                )
                continue
            text_buffer.append(_encode_document_part(part))
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


def _encode_image_part(part: ImagePart) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": "input_image",
        "detail": part.detail,
    }
    if part.file_id is not None:
        payload["file_id"] = part.file_id
    elif part.image_url is not None:
        payload["image_url"] = part.image_url
    else:  # pragma: no cover - guarded by ImagePart validation
        raise ToolangError("image part is missing image_url or file_id")
    return payload


def _encode_audio_part(part: AudioPart) -> dict[str, Any]:
    return {
        "type": "input_audio",
        "input_audio": {
            "data": part.data,
            "format": part.format,
        },
    }


def _encode_document_part(part: DocumentPart) -> dict[str, Any]:
    payload: dict[str, Any] = {"type": "input_file"}
    if part.data is not None:
        payload["file_data"] = part.data
    elif part.url is not None:
        payload["file_url"] = part.url
    elif part.file_id is not None:
        payload["file_id"] = part.file_id
    else:  # pragma: no cover - guarded by DocumentPart validation
        raise ToolangError("document part is missing data, url, or file_id")
    if part.filename is not None:
        payload["filename"] = part.filename
    return payload


def _message_has_text(message: Message | None) -> bool:
    return message is not None and any(
        isinstance(part, TextPart) for part in message.parts
    )


def _encode_tool_result_part(
    part: object,
    *,
    message_index: int | None,
    part_index: int,
) -> dict[str, Any]:
    if not isinstance(part, ToolResultPart):
        raise ToolangError("tool messages can only contain tool result parts")
    del message_index, part_index
    payload: dict[str, Any] = {
        "ok": part.error is None,
        "name": part.tool_name,
        "output": dict(part.output),
    }
    if part.error is not None:
        payload["error"] = part.error
    call_id = part.call_id or part.tool_call_id
    if not call_id:
        raise ToolangError("tool follow-up message is missing call_id")
    return {
        "type": "function_call_output",
        "call_id": call_id,
        "output": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
    }


def _log_api_request(
    target: ModelTarget,
    payload: dict[str, Any],
    *,
    stateful: bool,
    stream: bool,
) -> None:
    if not _ADAPTER_LOGGER.isEnabledFor(logging.DEBUG):
        return
    _ADAPTER_LOGGER.debug(
        "adapter.request provider=%s ref=%s model=%s adapter=%s stateful=%s stream=%s payload=%s",
        target.provider,
        target.ref,
        target.model,
        target.adapter,
        stateful,
        stream,
        _preview_data(payload),
    )


def _log_api_response(
    target: ModelTarget,
    response: Any,
    *,
    stateful: bool,
    stream: bool,
) -> None:
    if not _ADAPTER_LOGGER.isEnabledFor(logging.DEBUG):
        return
    _ADAPTER_LOGGER.debug(
        "adapter.result provider=%s ref=%s model=%s adapter=%s stateful=%s stream=%s payload=%s",
        target.provider,
        target.ref,
        target.model,
        target.adapter,
        stateful,
        stream,
        _preview_data(_response_data(response)),
    )


def _response_data(response: Any) -> Any:
    model_dump = getattr(response, "model_dump", None)
    if callable(model_dump):
        try:
            return model_dump(mode="json", exclude_none=True)
        except TypeError:
            return model_dump()
    to_dict = getattr(response, "to_dict", None)
    if callable(to_dict):
        return to_dict()
    return response


def _preview_data(value: object) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except TypeError:
        text = str(value)
    if len(text) <= _LOG_PREVIEW_LIMIT:
        return text
    return f"{text[:_LOG_PREVIEW_LIMIT]}...<truncated>"
