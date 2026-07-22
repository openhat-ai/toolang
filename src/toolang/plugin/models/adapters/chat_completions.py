"""Chat Completions-compatible model adapter."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from toolang.base.errors import ToolangError
from toolang.base.protocols.model import ModelAdapter
from toolang.base.types.message import (
    AudioPart,
    FilePart,
    ImagePart,
    Message,
    TextDelta,
    TextPart,
    ToolCallDelta,
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
from toolang.base.types.tool import ToolDefinition

_ADAPTER_LOGGER = logging.getLogger("toolang.model.adapter")
_LOG_PREVIEW_LIMIT = 4_000


@dataclass(frozen=True, slots=True)
class ChatCompletionsModelAdapter(ModelAdapter):
    """OpenAI Chat Completions API compatible adapter."""

    name: str = "chat_completions"
    description: str | None = "Use the OpenAI Chat Completions-compatible API shape."

    def invoke(
        self,
        target: ModelTarget,
        request: ModelCall,
    ) -> ModelCallResult:
        """Execute one non-streaming Chat Completions API call."""

        return invoke_chat_completion(target, request)

    def stream(
        self,
        target: ModelTarget,
        request: ModelCall,
        *,
        on_event: ModelStreamHandler,
    ) -> ModelCallResult:
        """Execute one streaming Chat Completions API call."""

        return stream_chat_completion(target, request, on_event=on_event)


def create_model_adapter(config: Mapping[str, object]) -> ModelAdapter:
    """Create the built-in Chat Completions model adapter."""

    del config
    return ChatCompletionsModelAdapter()


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


def invoke_chat_completion(
    target: ModelTarget,
    request: ModelCall,
) -> ModelCallResult:
    """Execute one non-streaming Chat Completions API call."""

    client = create_client(target)
    payload = chat_completion_payload(target, request, stream=False)
    _log_api_request(target, payload, stream=False)
    response = client.chat.completions.create(**payload)
    _log_api_response(target, response, stream=False)
    return parse_chat_completion(response)


def stream_chat_completion(
    target: ModelTarget,
    request: ModelCall,
    *,
    on_event: ModelStreamHandler,
) -> ModelCallResult:
    """Execute one streaming Chat Completions API call."""

    client = create_client(target)
    payload = chat_completion_payload(target, request, stream=True)
    _log_api_request(target, payload, stream=True)
    reasoning_parts: list[str] = []
    text_parts: list[str] = []
    tool_buffers: dict[int, _ToolCallBuffer] = {}
    final_usage: ModelUsage | None = None
    text_started = False
    stream = client.chat.completions.create(**payload)
    try:
        for chunk in stream:
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
            content = getattr(delta, "content", None)
            if isinstance(content, str) and content:
                if not text_started:
                    text_started = True
                    on_event(ModelPartStart(kind="text"))
                text_parts.append(content)
                on_event(ModelPartDelta(delta=TextDelta(text=content)))
            for call_delta in getattr(delta, "tool_calls", None) or ():
                index = getattr(call_delta, "index", None)
                if not isinstance(index, int):
                    index = len(tool_buffers)
                buffer = tool_buffers.setdefault(index, _ToolCallBuffer())
                if not buffer.started:
                    buffer.started = True
                    on_event(ModelPartStart(kind="tool_call"))
                buffer.append(call_delta)
                arguments_delta = _tool_call_delta_arguments(call_delta)
                if arguments_delta:
                    on_event(
                        ModelPartDelta(
                            delta=ToolCallDelta(
                                text=arguments_delta,
                                tool_call_id=buffer.tool_call_id or f"tool-call-{index}",
                            )
                        )
                    )
    finally:
        close = getattr(stream, "close", None)
        if callable(close):
            close()
    text = "".join(text_parts)
    tool_calls = tuple(buffer.to_tool_call(index) for index, buffer in sorted(tool_buffers.items()))
    message = _assistant_message(
        text=text,
        tool_calls=tool_calls,
        reasoning_content="".join(reasoning_parts),
    )
    if text:
        on_event(ModelPartEnd(data=TextPart(text=text)))
    for call in tool_calls:
        on_event(
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
        result = ModelCallResult(message=message, tool_calls=tool_calls, usage=final_usage)
    _log_api_response(target, result, stream=True)
    return result


def chat_completion_payload(
    target: ModelTarget,
    request: ModelCall,
    *,
    stream: bool,
) -> dict[str, Any]:
    """Build one Chat Completions API payload."""

    payload: dict[str, Any] = {
        "model": target.model,
        "messages": chat_messages(
            target=target,
            instructions=request.instructions,
            messages=request.messages,
        ),
    }
    if request.tools:
        payload["tools"] = [tool_payload(item) for item in request.tools]
    options = dict(target.options)
    if options:
        payload.update(options)
    payload["stream"] = stream
    if stream and target.provider == "deepseek" and "stream_options" not in payload:
        payload["stream_options"] = {"include_usage": True}
    return payload


def chat_messages(
    *,
    target: ModelTarget,
    instructions: str,
    messages: list[Message],
) -> list[dict[str, Any]]:
    """Build one replayable Chat Completions messages list."""

    payload: list[dict[str, Any]] = []
    if instructions.strip():
        payload.append({"role": "system", "content": instructions.strip()})
    for message in messages:
        encoded = encode_message(target, message)
        if isinstance(encoded, list):
            payload.extend(encoded)
        elif encoded is not None:
            payload.append(encoded)
    return payload


def encode_message(target: ModelTarget, message: Message) -> dict[str, Any] | list[dict[str, Any]] | None:
    """Encode one run-loop message into Chat Completions message objects."""

    role = message.role.strip()
    if role == "user":
        return {"role": "user", "content": _text_content(message)}
    if role == "assistant":
        text = _text_content(message)
        tool_calls = [_encode_tool_call_part(part) for part in message.parts if isinstance(part, ToolCallPart)]
        payload: dict[str, Any] = {"role": "assistant", "content": text}
        reasoning_content = _reasoning_content_for_payload(target, message, tool_calls=tool_calls)
        if reasoning_content is not None:
            payload["reasoning_content"] = reasoning_content
        if tool_calls:
            payload["tool_calls"] = tool_calls
        return payload
    if role == "tool":
        results = [
            _encode_tool_result_part(part, message=message)
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


def parse_chat_completion(response: Any) -> ModelCallResult:
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
    return ModelCallResult(
        message=_assistant_message(
            text=text if isinstance(text, str) else "",
            tool_calls=tool_calls,
            reasoning_content=reasoning_content if isinstance(reasoning_content, str) else "",
        ),
        tool_calls=tool_calls,
        usage=chat_usage(response),
    )


def parse_tool_calls(raw_tool_calls: object) -> list[ToolCall]:
    """Extract normalized tool calls from one Chat Completions message."""

    if not isinstance(raw_tool_calls, Iterable) or isinstance(raw_tool_calls, (str, bytes, dict)):
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
        if isinstance(part, (ImagePart, AudioPart, FilePart)):
            raise ToolangError("chat completions adapter only supports text and tool message parts")
        raise ToolangError(f"unsupported chat message part: {part.type}")
    return "\n".join(item for item in text if item)


def _encode_tool_call_part(part: ToolCallPart) -> dict[str, Any]:
    return {
        "id": part.call_id or part.tool_call_id,
        "type": "function",
        "function": {
            "name": part.tool_name,
            "arguments": json.dumps(part.input, ensure_ascii=False, separators=(",", ":")),
        },
    }


def _encode_tool_result_part(part: ToolResultPart, *, message: Message) -> dict[str, Any]:
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
) -> Message | None:
    parts: list[TextPart | ToolCallPart] = []
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
    meta = {"reasoning_content": reasoning_content} if reasoning_content and tool_calls else {}
    return Message(role="assistant", parts=tuple(parts), meta=meta)


def _reasoning_content_for_payload(
    target: ModelTarget,
    message: Message,
    *,
    tool_calls: list[dict[str, Any]],
) -> str | None:
    if target.provider != "deepseek" or not tool_calls:
        return None
    reasoning_content = message.meta.get("reasoning_content")
    if not isinstance(reasoning_content, str):
        return None
    text = reasoning_content.strip()
    return text or None


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
    target: ModelTarget,
    payload: dict[str, Any],
    *,
    stream: bool,
) -> None:
    if not _ADAPTER_LOGGER.isEnabledFor(logging.DEBUG):
        return
    _ADAPTER_LOGGER.debug(
        "adapter.request provider=%s ref=%s model=%s adapter=%s stream=%s payload=%s",
        target.provider,
        target.ref,
        target.model,
        target.adapter,
        stream,
        _preview_data(payload),
    )


def _log_api_response(
    target: ModelTarget,
    response: Any,
    *,
    stream: bool,
) -> None:
    if not _ADAPTER_LOGGER.isEnabledFor(logging.DEBUG):
        return
    _ADAPTER_LOGGER.debug(
        "adapter.result provider=%s ref=%s model=%s adapter=%s stream=%s payload=%s",
        target.provider,
        target.ref,
        target.model,
        target.adapter,
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
