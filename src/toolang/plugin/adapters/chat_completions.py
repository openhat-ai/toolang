"""Chat Completions-compatible model adapter."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Mapping, Sequence
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
from toolang.base.types.model import Model, Reasoning
from toolang.common.immutable import mutable_data
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
from ._credentials import credential_value

from ._structured_output import (
    append_structured_output_directive,
    is_object_schema,
    openai_strict_object_schema,
)
from ._usage import billing_value, optional_int, reported_cost

_ADAPTER_LOGGER = logging.getLogger(__name__)
_LOG_PREVIEW_LIMIT = 4_000


@dataclass(frozen=True, slots=True)
class ChatCompletionsModelAdapter(ModelAdapter):
    """OpenAI Chat Completions API compatible adapter."""

    name: str = "chat_completions"
    description: str | None = "Use the OpenAI Chat Completions-compatible API shape."
    default_api: str | None = None

    async def invoke(
        self,
        model: Model,
        request: ModelCall,
        *,
        environ: Mapping[str, str],
    ) -> ModelCallResult:
        """Execute one non-streaming Chat Completions API call."""

        return await invoke_chat_completion(model, request, environ=environ)

    async def stream(
        self,
        model: Model,
        request: ModelCall,
        *,
        environ: Mapping[str, str],
        on_event: ModelStreamHandler,
    ) -> ModelCallResult:
        """Execute one streaming Chat Completions API call."""

        return await stream_chat_completion(
            model, request, environ=environ, on_event=on_event
        )


def create_model_adapter(config: Mapping[str, object]) -> ModelAdapter:
    """Create the built-in Chat Completions model adapter."""

    del config
    return ChatCompletionsModelAdapter()


def create_client(model: Model, *, environ: Mapping[str, str]) -> Any:
    """Create one OpenAI-compatible client for one resolved route."""

    if model._toolang.route.api is None:
        raise ToolangError("Chat Completions adapter requires a resolved API")
    try:
        from openai import AsyncOpenAI
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise ToolangError(
            "The 'openai' package is not installed. Reinstall toolang with its runtime dependencies to enable runtime execution."
        ) from exc
    kwargs: dict[str, Any] = {
        "base_url": model._toolang.route.api,
        "api_key": credential_value(model._toolang.route.env, environ=environ)
        or "toolang",
    }
    if model._toolang.route.headers:
        kwargs["default_headers"] = dict(model._toolang.route.headers)
    return AsyncOpenAI(**kwargs)


async def invoke_chat_completion(
    model: Model,
    request: ModelCall,
    *,
    environ: Mapping[str, str],
) -> ModelCallResult:
    """Execute one non-streaming Chat Completions API call."""

    client = create_client(model, environ=environ)
    payload = chat_completion_payload(model, request, stream=False)
    _log_api_request(model, payload, stream=False)
    response = await client.chat.completions.create(
        **_openai_sdk_payload(model, payload)
    )
    _log_api_response(model, response, stream=False)
    return parse_chat_completion(
        response,
        audio_format=_output_audio_format(model),
    )


async def stream_chat_completion(
    model: Model,
    request: ModelCall,
    *,
    environ: Mapping[str, str],
    on_event: ModelStreamHandler,
) -> ModelCallResult:
    """Execute one streaming Chat Completions API call."""

    client = create_client(model, environ=environ)
    payload = chat_completion_payload(model, request, stream=True)
    _log_api_request(model, payload, stream=True)
    reasoning_parts: list[str] = []
    text_parts: list[str] = []
    audio_data_parts: list[str] = []
    audio_transcript_parts: list[str] = []
    tool_buffers: dict[int, _ToolCallBuffer] = {}
    final_usage: ModelUsage | None = None
    text_started = False
    defer_text = _audio_output_requested(model)
    stream = await client.chat.completions.create(**_openai_sdk_payload(model, payload))
    try:
        async for chunk in stream:
            chunk_usage = chat_usage(chunk)
            if chunk_usage is not None:
                final_usage = chunk_usage
            choice = _first_choice(chunk)
            if choice is None:
                continue
            delta = getattr(choice, "delta", None)
            if delta is None:
                continue
            reasoning_content = getattr(delta, "reasoning_content", None)
            if isinstance(reasoning_content, str) and reasoning_content:
                reasoning_parts.append(reasoning_content)
            audio = getattr(delta, "audio", None)
            audio_data = _value_text(audio, "data")
            audio_transcript = _value_text(audio, "transcript")
            if audio_data:
                audio_data_parts.append(audio_data)
            if audio_transcript:
                audio_transcript_parts.append(audio_transcript)
            content = getattr(delta, "content", None)
            if isinstance(content, str) and content:
                text_parts.append(content)
                if not defer_text:
                    if not text_started:
                        text_started = True
                        await on_event(ModelPartStart(kind="text"))
                    await on_event(ModelPartDelta(delta=TextDelta(text=content)))
            for call_delta in getattr(delta, "tool_calls", None) or ():
                index = getattr(call_delta, "index", None)
                if not isinstance(index, int):
                    index = len(tool_buffers)
                buffer = tool_buffers.setdefault(index, _ToolCallBuffer())
                if not buffer.started:
                    buffer.started = True
                    await on_event(ModelPartStart(kind="tool_call"))
                buffer.append(call_delta)
                arguments_delta = _tool_call_delta_arguments(call_delta)
                if arguments_delta:
                    await on_event(
                        ModelPartDelta(
                            delta=ToolCallDelta(
                                text=arguments_delta,
                                tool_call_id=buffer.tool_call_id
                                or f"tool-call-{index}",
                            )
                        )
                    )
    finally:
        close = getattr(stream, "close", None)
        if callable(close):
            await close()
    text = "".join(text_parts)
    tool_calls = tuple(
        buffer.to_tool_call(index) for index, buffer in sorted(tool_buffers.items())
    )
    audio = _audio_part(
        data="".join(audio_data_parts),
        transcript="".join(audio_transcript_parts),
        format=_output_audio_format(model),
    )
    message = _assistant_message(
        text=text,
        tool_calls=tool_calls,
        reasoning_content="".join(reasoning_parts),
        audio=audio,
    )
    keep_text = bool(text and (audio is None or text != audio.transcript))
    if keep_text and defer_text:
        await on_event(ModelPartStart(kind="text"))
        for delta in text_parts:
            await on_event(ModelPartDelta(delta=TextDelta(text=delta)))
    if keep_text:
        await on_event(ModelPartEnd(data=TextPart(text=text)))
    if audio is not None:
        await on_event(ModelPartStart(kind="audio"))
        await on_event(ModelPartEnd(data=audio))
    for call in tool_calls:
        await on_event(
            ModelPartEnd(
                data=ToolCallPart(
                    tool_call_id=call.tool_call_id,
                    call_id=call.call_id,
                    tool_name=call.name,
                    tool_family=call.name,
                    input=dict(call.input),
                )
            )
        )
    result = ModelCallResult(message=message, tool_calls=tool_calls)
    if final_usage is not None:
        result = ModelCallResult(
            message=message, tool_calls=tool_calls, usage=final_usage
        )
    _log_api_response(model, result, stream=True)
    return result


def chat_completion_payload(
    model: Model,
    request: ModelCall,
    *,
    stream: bool,
) -> dict[str, Any]:
    """Build one Chat Completions API payload."""

    native_schema = (
        openai_strict_object_schema(request.output_schema)
        if request.output_schema is not None
        and model.structured_output is True
        and model._toolang.provider.lower() != "deepseek"
        else None
    )
    instructions = (
        append_structured_output_directive(
            request.instructions,
            request.output_schema,
        )
        if request.output_schema is not None and native_schema is None
        else request.instructions
    )
    payload: dict[str, Any] = {
        "model": model.id,
        "messages": chat_messages(
            model=model,
            instructions=instructions,
            messages=request.messages,
        ),
    }
    if request.tools:
        payload["tools"] = [tool_payload(item) for item in request.tools]
    options = mutable_data(model._toolang.route.options)
    if options:
        payload.update(options)
    _apply_structured_output(
        payload,
        request.output_schema,
        native_schema=native_schema,
        json_object=(
            model._toolang.provider.lower() == "deepseek"
            and model.structured_output is True
            and request.output_schema is not None
            and is_object_schema(request.output_schema)
        ),
    )
    _apply_reasoning(payload, request.reasoning, model._toolang.provider)
    if request.max_output_tokens is not None:
        field = (
            "max_completion_tokens"
            if model._toolang.provider == "openai"
            or "max_completion_tokens" in model._toolang.route.options
            else "max_tokens"
        )
        payload.pop("max_completion_tokens", None)
        payload.pop("max_tokens", None)
        payload[field] = request.max_output_tokens
    payload["stream"] = stream
    if stream and "stream_options" not in payload:
        payload["stream_options"] = {"include_usage": True}
    return payload


def _apply_structured_output(
    payload: dict[str, Any],
    schema: dict[str, object] | None,
    *,
    native_schema: dict[str, object] | None,
    json_object: bool,
) -> None:
    if schema is None:
        return
    if "response_format" in payload:
        raise ToolangError(
            "Chat Completions response_format conflicts with normalized structured output"
        )
    if native_schema is None:
        if json_object:
            payload["response_format"] = {"type": "json_object"}
        return
    payload["response_format"] = {
        "type": "json_schema",
        "json_schema": {
            "name": "output",
            "strict": True,
            "schema": native_schema,
        },
    }


def _apply_reasoning(
    payload: dict[str, Any],
    reasoning: Reasoning | None,
    provider: str,
) -> None:
    if reasoning is None:
        return
    effort = reasoning.effort
    budget = reasoning.budget_tokens
    if effort is None and budget is None:
        return
    disabled = effort == "none"
    if disabled and budget is not None:
        raise ToolangError(
            "disabled Chat Completions reasoning conflicts with a budget"
        )
    provider = provider.lower()
    if provider == "openrouter":
        payload.pop("reasoning", None)
        payload.pop("reasoning_effort", None)
        if budget is not None and effort is not None:
            raise ToolangError(
                "OpenRouter accepts either reasoning effort or budget_tokens"
            )
        wire: dict[str, object] = {}
        if disabled:
            wire["enabled"] = False
        if isinstance(effort, str) and not disabled:
            wire["effort"] = effort
        if isinstance(budget, int) and not isinstance(budget, bool):
            wire["max_tokens"] = budget
        if wire:
            payload["reasoning"] = wire
        return
    if provider == "deepseek":
        payload.pop("thinking", None)
        payload.pop("reasoning_effort", None)
        if budget is not None:
            raise ToolangError(
                "DeepSeek Chat Completions does not support token budgets"
            )
        if disabled:
            payload["thinking"] = {"type": "disabled"}
        elif isinstance(effort, str):
            payload["thinking"] = {"type": "enabled"}
        if isinstance(effort, str) and not disabled:
            payload["reasoning_effort"] = effort
        return
    if budget is not None:
        raise ToolangError(
            f"{provider} Chat Completions does not support token budgets"
        )
    payload.pop("reasoning_effort", None)
    if provider == "xai" and disabled:
        raise ToolangError("xAI Chat Completions reasoning cannot be disabled")
    if disabled:
        payload["reasoning_effort"] = "none"
    elif isinstance(effort, str):
        payload["reasoning_effort"] = effort


def _openai_sdk_payload(
    model: Model,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Move known compatible-provider extensions through the SDK escape hatch."""

    result = dict(payload)
    extension_fields = {
        "openrouter": ("reasoning",),
        "deepseek": ("thinking",),
    }.get(model._toolang.provider.lower(), ())
    extensions = {
        field: result.pop(field) for field in extension_fields if field in result
    }
    if not extensions:
        return result
    raw_extra_body = result.get("extra_body")
    if raw_extra_body is not None and not isinstance(raw_extra_body, Mapping):
        raise ToolangError("Chat Completions extra_body must be an object")
    extra_body = dict(cast(Mapping[str, Any], raw_extra_body or {}))
    extra_body.update(extensions)
    result["extra_body"] = extra_body
    return result


def chat_messages(
    *,
    model: Model,
    instructions: str,
    messages: list[Message],
) -> list[dict[str, Any]]:
    """Build one replayable Chat Completions messages list."""

    payload: list[dict[str, Any]] = []
    if instructions.strip():
        payload.append({"role": "system", "content": instructions.strip()})
    for message in messages:
        encoded = encode_message(model, message)
        if isinstance(encoded, list):
            payload.extend(encoded)
        elif encoded is not None:
            payload.append(encoded)
    return payload


def encode_message(
    model: Model, message: Message
) -> dict[str, Any] | list[dict[str, Any]] | None:
    """Encode one run-loop message into Chat Completions message objects."""

    role = message.role.strip()
    if role == "user":
        return {"role": "user", "content": _encode_user_content(message)}
    if role == "assistant":
        text = _text_content(message)
        tool_calls = [
            _encode_tool_call_part(part)
            for part in message.parts
            if isinstance(part, ToolCallPart)
        ]
        payload: dict[str, Any] = {"role": "assistant", "content": text}
        reasoning_content = _reasoning_content_for_payload(
            model, message, tool_calls=tool_calls
        )
        if reasoning_content is not None:
            payload["reasoning_content"] = reasoning_content
        if tool_calls:
            payload["tool_calls"] = tool_calls
        return payload
    if role == "tool":
        results = [
            _encode_tool_result_part(part)
            for part in message.parts
            if isinstance(part, ToolResultPart)
        ]
        if len(results) != len(message.parts):
            raise ToolangError("tool messages can only contain tool result parts")
        if not results:
            return None
        return results[0] if len(results) == 1 else results
    return None


def tool_payload(definition: ToolDefinition) -> dict[str, Any]:
    """Return one Chat Completions-compatible tool definition payload."""

    return {
        "type": "function",
        "function": {
            "name": definition.name,
            "description": definition.description,
            "parameters": dict(definition.parameters),
        },
    }


def parse_chat_completion(
    response: Any,
    *,
    audio_format: AudioFormat = "wav",
) -> ModelCallResult:
    """Normalize one Chat Completions response object."""

    choice = _first_choice(response)
    if choice is None:
        return ModelCallResult(usage=chat_usage(response))
    raw_message = getattr(choice, "message", None)
    if raw_message is None:
        return ModelCallResult(usage=chat_usage(response))
    text = getattr(raw_message, "content", None)
    reasoning_content = getattr(raw_message, "reasoning_content", None)
    tool_calls = tuple(parse_tool_calls(getattr(raw_message, "tool_calls", None)))
    audio = _audio_part_from_value(
        getattr(raw_message, "audio", None),
        format=audio_format,
    )
    return ModelCallResult(
        message=_assistant_message(
            text=text if isinstance(text, str) else "",
            tool_calls=tool_calls,
            reasoning_content=reasoning_content
            if isinstance(reasoning_content, str)
            else "",
            audio=audio,
        ),
        tool_calls=tool_calls,
        usage=chat_usage(response),
    )


def parse_tool_calls(raw_tool_calls: object) -> list[ToolCall]:
    """Extract normalized tool calls from one Chat Completions message."""

    if not isinstance(raw_tool_calls, Iterable) or isinstance(
        raw_tool_calls, (str, bytes, dict)
    ):
        return []
    results: list[ToolCall] = []
    for item in raw_tool_calls:
        function = getattr(item, "function", None)
        tool_call_id = _optional_attr_text(item, "id")
        name = _optional_attr_text(function, "name")
        if not tool_call_id and not name:
            arguments = _optional_attr_text(function, "arguments")
            if arguments:
                raise ToolangError("model emitted a tool call without a function name")
            continue
        if not name:
            raise ToolangError("model emitted a tool call without a function name")
        results.append(
            ToolCall(
                tool_call_id=tool_call_id or name,
                call_id=tool_call_id or name,
                name=name,
                input=parse_tool_arguments(getattr(function, "arguments", "{}")),
            )
        )
    return results


def chat_usage(response: Any) -> ModelUsage | None:
    """Extract one normalized model usage summary."""

    usage = getattr(response, "usage", None)
    input_tokens = getattr(usage, "prompt_tokens", None)
    output_tokens = getattr(usage, "completion_tokens", None)
    if not isinstance(input_tokens, int) or not isinstance(output_tokens, int):
        return None
    input_details = getattr(usage, "prompt_tokens_details", None)
    output_details = getattr(usage, "completion_tokens_details", None)
    cached = optional_int(usage, "prompt_cache_hit_tokens")
    if cached is None:
        cached = optional_int(input_details, "cached_tokens")
    cache_write = optional_int(input_details, "cache_write_tokens")
    if cache_write is None:
        cache_write = optional_int(input_details, "cache_creation_input_tokens")
    cache_miss = optional_int(usage, "prompt_cache_miss_tokens")
    uncached = cache_miss
    if uncached is None and (cached is not None or cache_write is not None):
        uncached = input_tokens - (cached or 0) - (cache_write or 0)
    reasoning = optional_int(output_details, "reasoning_tokens")
    cost, currency = reported_cost(usage)
    service_tier = billing_value(response, "service_tier") or billing_value(
        usage, "service_tier"
    )
    return ModelUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        input_uncached_tokens=uncached,
        input_cache_read_tokens=cached,
        input_cache_write_tokens=cache_write,
        input_audio_tokens=optional_int(input_details, "audio_tokens"),
        output_visible_tokens=(
            output_tokens - reasoning if reasoning is not None else None
        ),
        output_reasoning_tokens=reasoning,
        output_audio_tokens=optional_int(output_details, "audio_tokens"),
        reported_cost=cost,
        reported_currency=currency,
        billing={"service_tier": service_tier} if service_tier is not None else {},
    )


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


def _text_content(message: Message) -> str:
    text: list[str] = []
    for part in message.parts:
        if isinstance(part, TextPart):
            text.append(part.text)
            continue
        if isinstance(part, ToolCallPart) and message.role == "assistant":
            continue
        if isinstance(part, ToolResultPart) and message.role == "tool":
            continue
        if isinstance(part, AudioPart):
            text.append(part.transcript or message_summary((part,)))
            continue
        if isinstance(part, (ImagePart, DocumentPart)):
            text.append(message_summary((part,)))
            continue
        raise ToolangError(f"unsupported chat message part: {part.type}")
    return "\n".join(item for item in text if item)


def _encode_user_content(message: Message) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = []
    for part in message.parts:
        if isinstance(part, TextPart):
            content.append({"type": "text", "text": part.text})
            continue
        if isinstance(part, ImagePart):
            if part.image_url is None:
                raise ToolangError("Chat Completions image input requires image_url")
            content.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": part.image_url,
                        "detail": part.detail,
                    },
                }
            )
            continue
        if isinstance(part, AudioPart):
            content.append(
                {
                    "type": "input_audio",
                    "input_audio": {
                        "data": part.data,
                        "format": part.format,
                    },
                }
            )
            continue
        if isinstance(part, DocumentPart):
            if part.url is not None:
                raise ToolangError(
                    "Chat Completions document input does not accept a URL; "
                    "provide document data or a provider file_id"
                )
            file: dict[str, Any] = {}
            if part.data is not None:
                file["file_data"] = part.data
            elif part.file_id is not None:
                file["file_id"] = part.file_id
            else:  # pragma: no cover - guarded by DocumentPart validation
                raise ToolangError("document part is missing data or file_id")
            if part.filename is not None:
                file["filename"] = part.filename
            content.append({"type": "file", "file": file})
            continue
        raise ToolangError(f"unsupported user message part: {part.type}")
    return content


def _encode_tool_call_part(part: ToolCallPart) -> dict[str, Any]:
    return {
        "id": part.call_id or part.tool_call_id,
        "type": "function",
        "function": {
            "name": part.tool_name,
            "arguments": json.dumps(
                part.input, ensure_ascii=False, separators=(",", ":")
            ),
        },
    }


def _encode_tool_result_part(part: ToolResultPart) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ok": part.error is None,
        "name": part.tool_name,
    }
    if part.output or part.error is None:
        payload["output"] = dict(part.output)
    if part.error is not None:
        payload["error"] = part.error
    call_id = part.call_id or part.tool_call_id
    if not call_id:
        raise ToolangError("tool follow-up message is missing call_id")
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
    }


def _assistant_message(
    *,
    text: str,
    tool_calls: tuple[ToolCall, ...],
    reasoning_content: str = "",
    audio: AudioPart | None = None,
) -> Message | None:
    parts: list[TextPart | AudioPart | ToolCallPart] = []
    if text and (audio is None or text != audio.transcript):
        parts.append(TextPart(text=text))
    if audio is not None:
        parts.append(audio)
    for index, call in enumerate(tool_calls):
        parts.append(
            ToolCallPart(
                tool_call_id=call.tool_call_id,
                call_id=call.call_id,
                tool_name=call.name,
                tool_family=call.name,
                input=dict(call.input),
                reasoning=(reasoning_content or None) if index == 0 else None,
            )
        )
    if not parts:
        return None
    return Message(role="assistant", parts=tuple(parts))


def _reasoning_content_for_payload(
    model: Model,
    message: Message,
    *,
    tool_calls: list[dict[str, Any]],
) -> str | None:
    if model._toolang.provider != "deepseek" or not tool_calls:
        return None
    return next(
        (
            part.reasoning
            for part in message.parts
            if isinstance(part, ToolCallPart) and part.reasoning
        ),
        None,
    )


def _audio_part_from_value(
    value: object,
    *,
    format: AudioFormat,
) -> AudioPart | None:
    return _audio_part(
        data=_value_text(value, "data"),
        transcript=_value_text(value, "transcript"),
        format=format,
    )


def _audio_part(
    *,
    data: str,
    transcript: str,
    format: AudioFormat,
) -> AudioPart | None:
    if not data:
        return None
    return AudioPart(
        data=data,
        format=format,
        transcript=transcript or None,
    )


def _value_text(value: object, name: str) -> str:
    raw = (
        cast(Mapping[str, object], value).get(name)
        if isinstance(value, Mapping)
        else getattr(value, name, None)
    )
    return raw if isinstance(raw, str) else ""


def _output_audio_format(model: Model) -> AudioFormat:
    audio = model._toolang.route.options.get("audio")
    raw = (
        cast(Mapping[str, object], audio).get("format")
        if isinstance(audio, Mapping)
        else None
    )
    if raw is None:
        return "wav"
    value = str(raw).strip().lower()
    if value not in {"mp3", "wav"}:
        raise ToolangError(
            f"unsupported canonical audio output format: {value or '<empty>'}"
        )
    return cast(AudioFormat, value)


def _audio_output_requested(model: Model) -> bool:
    if isinstance(model._toolang.route.options.get("audio"), Mapping):
        return True
    modalities = model._toolang.route.options.get("modalities")
    return (
        isinstance(modalities, Sequence)
        and not isinstance(modalities, (str, bytes, bytearray))
        and "audio" in modalities
    )


def _first_choice(response: Any) -> Any | None:
    choices = getattr(response, "choices", None)
    if not choices:
        return None
    try:
        return choices[0]
    except (IndexError, TypeError):
        return None


def _tool_call_delta_arguments(delta: Any) -> str:
    function = getattr(delta, "function", None)
    value = getattr(function, "arguments", None)
    return value if isinstance(value, str) else ""


def _optional_attr_text(value: object, name: str) -> str:
    raw = getattr(value, name, None)
    if not isinstance(raw, str):
        return ""
    return raw.strip()


@dataclass(slots=True)
class _ToolCallBuffer:
    tool_call_id: str = ""
    name: str = ""
    arguments: list[str] | None = None
    started: bool = False

    def append(self, delta: Any) -> None:
        tool_call_id = _optional_attr_text(delta, "id")
        if tool_call_id:
            self.tool_call_id = tool_call_id
        function = getattr(delta, "function", None)
        name = _optional_attr_text(function, "name")
        if name:
            self.name = name
        arguments = _tool_call_delta_arguments(delta)
        if arguments:
            if self.arguments is None:
                self.arguments = []
            self.arguments.append(arguments)

    def to_tool_call(self, index: int) -> ToolCall:
        if not self.name:
            raise ToolangError("model emitted a tool call without a function name")
        tool_call_id = self.tool_call_id or f"tool-call-{index}"
        return ToolCall(
            tool_call_id=tool_call_id,
            call_id=tool_call_id,
            name=self.name,
            input=parse_tool_arguments("".join(self.arguments or []) or "{}"),
        )


def _log_api_request(
    model: Model,
    payload: dict[str, Any],
    *,
    stream: bool,
) -> None:
    if not _ADAPTER_LOGGER.isEnabledFor(logging.DEBUG):
        return
    _ADAPTER_LOGGER.debug(
        "adapter.request provider=%s ref=%s model=%s adapter=%s stream=%s payload=%s",
        model._toolang.provider,
        model.ref,
        model.id,
        model._toolang.route.adapter,
        stream,
        _preview_data(payload),
    )


def _log_api_response(
    model: Model,
    response: Any,
    *,
    stream: bool,
) -> None:
    if not _ADAPTER_LOGGER.isEnabledFor(logging.DEBUG):
        return
    _ADAPTER_LOGGER.debug(
        "adapter.result provider=%s ref=%s model=%s adapter=%s stream=%s payload=%s",
        model._toolang.provider,
        model.ref,
        model.id,
        model._toolang.route.adapter,
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
