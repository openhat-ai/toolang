"""Responses-compatible model adapter."""

from __future__ import annotations

import json
import asyncio
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, cast
from types import SimpleNamespace

from toolang.base.errors import ModelResponseError, ToolangError
from toolang.base.protocols.model import ModelAdapter
from toolang.base.types.message import (
    AudioFormat,
    AudioPart,
    DocumentPart,
    ImagePart,
    Message,
    Part,
    ReasoningPart,
    TextPart,
    ToolCallDelta,
    ToolCallPart,
    ToolResultPart,
    message_summary,
)
from toolang.base.types.model import Model, Reasoning
from ._payload import clear_options, output_allowance, request_options
from toolang.base.types.run import (
    ModelCall,
    ModelCallResult,
    ModelStreamHandler,
    ModelUsage,
    ToolCall,
)
from toolang.base.types.tool import ToolDefinition
from ._credentials import credential_value
from ._errors import (
    model_events,
    model_transport,
    model_transport_errors,
    provider_error,
)
from ._tool_calls import parse_tool_arguments

from ._structured_output import (
    append_structured_output_directive,
    openai_strict_object_schema,
)
from ._usage import billing_value, optional_int, reported_cost
from ._parts import PartStream, compatible, native_metadata

_ADAPTER_LOGGER = logging.getLogger(__name__)
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
    default_api: str | None = "https://api.openai.com/v1"

    def output_allowance(self, options: Mapping[str, object]) -> int | None:
        """Normalize this protocol's explicitly authored output allowance."""

        return output_allowance(options, "max_output_tokens", sdk_extensions=True)

    @model_transport
    async def invoke(
        self,
        model: Model,
        request: ModelCall,
        *,
        environ: Mapping[str, str],
    ) -> ModelCallResult:
        """Execute one non-streaming Responses API call."""

        _require_supported_inputs(model, request)
        return await invoke_response(
            model,
            request,
            stateful=_stateful_route(model),
            environ=environ,
        )

    @model_transport
    async def stream(
        self,
        model: Model,
        request: ModelCall,
        *,
        environ: Mapping[str, str],
        on_event: ModelStreamHandler,
    ) -> ModelCallResult:
        """Execute one streaming Responses API call."""

        _require_supported_inputs(model, request)
        return await stream_response(
            model,
            request,
            stateful=_stateful_route(model),
            environ=environ,
            on_event=model_events(on_event),
        )


def create_model_adapter(config: Mapping[str, object]) -> ModelAdapter:
    """Create the built-in Responses model adapter."""

    del config
    return ResponsesModelAdapter()


def _stateful_route(model: Model) -> bool:
    return model._toolang.provider in _STATEFUL_PROVIDERS


def _require_supported_inputs(
    model: Model,
    request: ModelCall,
) -> None:
    if model._toolang.provider != "openai":
        return
    if _supports_openai_audio_input(model):
        return
    if not _request_has_audio_input(request):
        return
    raise ToolangError(
        f"audio input is not supported for OpenAI model '{model.id}' via the Responses adapter; "
        "transcribe audio first, send it as a generic file to a route that supports audio files, or use an audio-capable model route"
    )


def _supports_openai_audio_input(model: Model) -> bool:
    candidates = (
        model.id.strip().lower(),
        model.ref.strip().lower(),
        model.name.strip().lower(),
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


def create_client(model: Model, *, environ: Mapping[str, str]) -> Any:
    """Create one OpenAI-compatible client for one resolved route."""

    if model._toolang.route.api is None:
        raise ToolangError("Responses adapter requires a resolved API")
    try:
        from openai import AsyncOpenAI
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise ToolangError(
            "The 'openai' package is not installed. Reinstall toolang with its runtime dependencies to enable runtime execution."
        ) from exc
    kwargs: dict[str, Any] = {
        "base_url": model._toolang.route.api,
        "max_retries": 0,
        "api_key": credential_value(model._toolang.route.env, environ=environ)
        or "toolang",
    }
    if model._toolang.route.headers:
        kwargs["default_headers"] = dict(model._toolang.route.headers)
    return AsyncOpenAI(**kwargs)


async def invoke_response(
    model: Model,
    request: ModelCall,
    *,
    stateful: bool,
    environ: Mapping[str, str],
) -> ModelCallResult:
    """Execute one non-streaming Responses API call."""

    client = create_client(model, environ=environ)
    payload = response_payload(
        model,
        request,
        stateful=stateful,
    )
    _log_api_request(
        model,
        payload,
        stateful=stateful,
        stream=False,
    )
    response = await client.responses.create(**payload)
    _log_api_response(
        model,
        response,
        stateful=stateful,
        stream=False,
    )
    return parse_response(
        response,
        model=model,
        request=request,
        stateful=stateful,
    )


async def stream_response(
    model: Model,
    request: ModelCall,
    *,
    stateful: bool,
    environ: Mapping[str, str],
    on_event: ModelStreamHandler,
) -> ModelCallResult:
    """Execute one streaming Responses API call."""

    client = create_client(model, environ=environ)
    payload = response_payload(
        model,
        request,
        stateful=stateful,
    )
    _log_api_request(
        model,
        payload,
        stateful=stateful,
        stream=True,
    )
    parts = PartStream(on_event)
    item_ids: dict[int, str] = {}
    defer_text = _supports_openai_audio_input(model)
    deferred: dict[tuple[object, ...], str] = {}
    text_deltas: list[str] = []
    latest_response = None
    terminal_response = None
    try:
        with model_transport_errors():
            async with client.responses.stream(**payload) as stream:
                async for event in stream:
                    latest_response = (
                        getattr(event, "response", None) or latest_response
                    )
                    event_type = getattr(event, "type", "")
                    if event_type in {
                        "response.completed",
                        "response.incomplete",
                        "response.failed",
                    }:
                        terminal_response = getattr(event, "response", None)
                        break
                    if event_type == "error":
                        raise provider_error({"code": getattr(event, "code", None)})
                    if event_type == "response.output_text.delta":
                        text_deltas.append(str(getattr(event, "delta", "")))
                    output_index = getattr(event, "output_index", 0)
                    item = getattr(event, "item", None)
                    if event_type in {
                        "response.output_item.added",
                        "response.output_item.done",
                    }:
                        item_ids[output_index] = getattr(item, "id", "") or str(
                            output_index
                        )
                    identity = getattr(event, "item_id", "") or item_ids.get(
                        output_index, str(output_index)
                    )
                    field = (
                        "summary" if "reasoning_summary" in event_type else "content"
                    )
                    content_index = getattr(
                        event,
                        "summary_index" if field == "summary" else "content_index",
                        0,
                    )
                    if event_type in {
                        "response.reasoning_summary_text.delta",
                        "response.reasoning_text.delta",
                        "response.output_text.delta",
                    }:
                        reasoning = event_type != "response.output_text.delta"
                        key = (
                            "reasoning" if reasoning else "text",
                            identity,
                            field,
                            content_index,
                        )
                        value = getattr(event, "delta", "")
                        if not reasoning and defer_text:
                            deferred[key] = deferred.get(key, "") + value
                        else:
                            await parts.text(key, value, reasoning=reasoning)
                    elif event_type in {
                        "response.reasoning_summary_text.done",
                        "response.reasoning_text.done",
                        "response.output_text.done",
                    }:
                        reasoning = event_type != "response.output_text.done"
                        key = (
                            "reasoning" if reasoning else "text",
                            identity,
                            field,
                            content_index,
                        )
                        value = getattr(event, "text", "")
                        if not reasoning and defer_text:
                            previous = deferred.get(key, "")
                            if not value.startswith(previous):
                                raise ToolangError(
                                    "Responses text snapshot contradicts its deltas"
                                )
                            deferred[key] = value
                        else:
                            observed = (
                                parts.parts[parts.indices[key]]
                                if key in parts.indices
                                else None
                            )
                            previous = (
                                observed.text
                                if isinstance(observed, TextPart | ReasoningPart)
                                else ""
                            )
                            if not value.startswith(previous):
                                raise ToolangError(
                                    "Responses text snapshot contradicts its deltas"
                                )
                            await parts.text(
                                key, value[len(previous) :], reasoning=reasoning
                            )
                    elif (
                        event_type == "response.output_item.added"
                        and getattr(item, "type", None) == "function_call"
                    ):
                        call_id = tool_call_id(
                            getattr(item, "id", ""),
                            getattr(item, "call_id", ""),
                            fallback=identity,
                        )
                        await parts.start(
                            ("tool", identity),
                            ToolCallPart(
                                call_id,
                                getattr(item, "name", ""),
                                getattr(item, "name", ""),
                                call_id=getattr(item, "call_id", "") or None,
                            ),
                        )
                    elif event_type == "response.function_call_arguments.delta":
                        key = ("tool", identity)
                        call_id = tool_call_id(
                            identity, getattr(event, "call_id", ""), fallback=identity
                        )
                        await parts.start(key, ToolCallPart(call_id, "", ""))
                        await parts.delta(
                            key, ToolCallDelta(getattr(event, "delta", ""), call_id)
                        )
                    elif event_type == "response.output_item.done":
                        for key, part in _response_item_parts(
                            item, model=model, output_index=output_index
                        ):
                            if key in deferred:
                                if not isinstance(
                                    part, TextPart
                                ) or not part.text.startswith(deferred[key]):
                                    raise ToolangError(
                                        "Responses text snapshot contradicts its deltas"
                                    )
                            await parts.finish(key, part)
                if terminal_response is None:
                    raise ModelResponseError(
                        "model stream ended before a terminal Responses event",
                        kind="incomplete_stream",
                    )
                response = terminal_response
                parse_response(
                    response, model=model, request=request, stateful=stateful
                )
        final_parts = _response_parts(response, model=model)
        final_keys = {key for key, _ in final_parts}
        if parts.indices.keys() - final_keys:
            raise ToolangError("Responses final snapshot omitted an observed Part")
        for key, part in final_parts:
            if (
                key in deferred
                and isinstance(part, TextPart)
                and not part.text.startswith(deferred[key])
            ):
                raise ToolangError("Responses text snapshot contradicts its deltas")
            await parts.finish(key, part)
    except (Exception, asyncio.CancelledError) as exc:
        if isinstance(exc, ModelResponseError):
            if exc.usage is None:
                exc.usage = response_usage(latest_response)
            if not exc.partial_text:
                exc.partial_text = "".join(text_deltas)
        await parts.interrupt()
        raise
    _log_api_response(model, response, stateful=stateful, stream=True)
    message = parts.message()
    return ModelCallResult(
        message=message,
        tool_calls=tuple(parse_tool_calls(response)),
        usage=response_usage(response),
        continuation=response_continuation(
            response, request=request, emitted_message=message, stateful=stateful
        ),
    )


def response_payload(
    model: Model,
    request: ModelCall,
    *,
    stateful: bool,
) -> dict[str, Any]:
    """Build one Responses API payload."""

    native_schema = (
        openai_strict_object_schema(request.output_schema)
        if request.output_schema is not None and model.structured_output is True
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
    continuation = dict(request.continuation or {})
    previous_response_id = (
        continuation.get("previous_response_id") if stateful else None
    )
    baseline_count = continuation.get("baseline_count") if stateful else None
    message_offset = 0
    if (
        isinstance(baseline_count, int)
        and 0 <= baseline_count <= len(request.messages)
        and continuation.get("prefix")
        == _context_prefix(request, request.messages[:baseline_count])
    ):
        message_offset = baseline_count
    else:
        # Compaction or a changed binding must not inherit the provider's old
        # context, even when the number of selected messages stays unchanged.
        previous_response_id = None
    messages = (
        request.messages[message_offset:] if previous_response_id else request.messages
    )
    payload: dict[str, Any] = {
        "model": model.id,
        "input": response_input(
            model=model,
            instructions=instructions,
            messages=messages,
            include_instructions=not bool(previous_response_id),
            replay_tool_items=True,
        ),
    }
    if request.tools:
        payload["tools"] = [tool_payload(item) for item in request.tools]
    if isinstance(previous_response_id, str) and previous_response_id.strip():
        payload["previous_response_id"] = previous_response_id
    options = request_options(model._toolang.route.options)
    if options:
        payload.update(options)
    _apply_structured_output(
        payload,
        request.output_schema,
        native_schema=native_schema,
    )
    _apply_reasoning(payload, request.reasoning)
    if request.max_output_tokens is not None:
        clear_options(payload, "max_output_tokens")
        payload["max_output_tokens"] = request.max_output_tokens
    return payload


def _apply_structured_output(
    payload: dict[str, Any],
    schema: dict[str, object] | None,
    *,
    native_schema: dict[str, object] | None,
) -> None:
    if schema is None:
        return
    raw_text = payload.get("text")
    if raw_text is not None and not isinstance(raw_text, Mapping):
        raise ToolangError(
            "Responses text format conflicts with normalized structured output"
        )
    text = dict(cast(Mapping[str, object], raw_text)) if raw_text is not None else {}
    if "format" in text:
        raise ToolangError(
            "Responses text format conflicts with normalized structured output"
        )
    if native_schema is None:
        return
    text["format"] = {
        "type": "json_schema",
        "name": "output",
        "strict": True,
        "schema": native_schema,
    }
    payload["text"] = text


def _apply_reasoning(
    payload: dict[str, Any],
    reasoning: Reasoning | None,
) -> None:
    if reasoning is None:
        return
    effort = reasoning.effort
    budget = reasoning.budget_tokens
    if effort is None and budget is None:
        return
    if budget is not None:
        raise ToolangError("Responses does not support reasoning token budgets")
    extra = payload.get("extra_body")
    raw = payload.get(
        "reasoning", extra.get("reasoning") if isinstance(extra, Mapping) else None
    )
    wire = dict(raw) if isinstance(raw, Mapping) else {}
    if isinstance(effort, str):
        wire["effort"] = effort
    clear_options(payload, "reasoning")
    if wire:
        payload["reasoning"] = wire


def parse_response(
    response: Any,
    *,
    model: Model,
    request: ModelCall,
    stateful: bool,
) -> ModelCallResult:
    """Normalize one Responses API response object."""

    try:
        status = getattr(response, "status", None)
        if status == "incomplete":
            reason = getattr(
                getattr(response, "incomplete_details", None), "reason", None
            )
            if reason == "max_output_tokens":
                raise ModelResponseError(
                    "model response truncated by output limit; shorten the response or tool arguments",
                    kind="output_limit",
                )
            raise ModelResponseError(
                f"provider returned an incomplete response ({reason or 'unknown reason'})",
                kind="provider_rejection"
                if reason == "content_filter"
                else "incomplete_stream",
            )
        if status == "failed":
            raise provider_error(
                {"code": getattr(getattr(response, "error", None), "code", None)}
            )
        if status == "cancelled":
            raise ModelResponseError(
                "provider response cancelled", kind="provider_rejection"
            )
        if status not in {None, "completed"}:
            raise ModelResponseError(
                f"provider response is not complete: {status}", kind="incomplete_stream"
            )
        if any(
            getattr(part, "type", None) == "refusal"
            for item in getattr(response, "output", [])
            for part in (getattr(item, "content", None) or ())
        ):
            raise ModelResponseError(
                "provider refused the response", kind="provider_rejection"
            )
        tool_calls = tuple(parse_tool_calls(response))
    except ModelResponseError as exc:
        exc.usage = response_usage(response)
        exc.partial_text = response_text(response)
        raise
    message = assistant_message(response, model=model, tool_calls=tool_calls)
    return ModelCallResult(
        message=message,
        tool_calls=tool_calls,
        usage=response_usage(response),
        continuation=response_continuation(
            response,
            request=request,
            emitted_message=message,
            stateful=stateful,
        ),
    )


def response_input(
    *,
    model: Model | None = None,
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
            model=model,
            replay_tool_items=replay_tool_items,
            message_index=message_index,
        )
        if encoded is None:
            continue
        for item in encoded if isinstance(encoded, list) else (encoded,):
            results.append(item)
    return results


def encode_message(
    message: Message,
    *,
    model: Model | None = None,
    replay_tool_items: bool = True,
    message_index: int | None = None,
) -> dict[str, Any] | list[dict[str, Any]] | None:
    """Encode one run-loop message into typed Responses API input items."""

    role = message.role.strip()
    if role in {"user", "assistant"}:
        return _encode_actor_message(
            message,
            model=model,
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
            if content_type in {"output_text", "text"} and getattr(
                content, "text", None
            ):
                collected.append(str(content.text))
    return "".join(collected)


def parse_tool_calls(response: Any) -> list[ToolCall]:
    """Extract one normalized tool-call list from a response."""

    results: list[ToolCall] = []
    for item in getattr(response, "output", []):
        if getattr(item, "type", None) != "function_call":
            continue
        name = getattr(item, "name", None)
        if not isinstance(name, str) or not name.strip():
            raise ModelResponseError(
                "model emitted a tool call without a function name", kind="missing_name"
            )
        results.append(
            ToolCall(
                tool_call_id=tool_call_id(
                    getattr(item, "id", ""),
                    getattr(item, "call_id", ""),
                    fallback=str(getattr(item, "call_id", "")),
                ),
                call_id=str(getattr(item, "call_id", "")),
                name=name.strip(),
                input=parse_tool_arguments(getattr(item, "arguments", "{}")),
            )
        )
    return results


def assistant_message(
    response: Any, *, model: Model, tool_calls: tuple[ToolCall, ...]
) -> Message | None:
    """Return Parts in native item order, including standalone reasoning."""

    del tool_calls
    parts = tuple(part for _, part in _response_parts(response, model=model))
    return Message(role="assistant", parts=parts) if parts else None


def _response_parts(
    response: Any, *, model: Model
) -> list[tuple[tuple[object, ...], Part]]:
    parts: list[tuple[tuple[object, ...], Part]] = []
    for index, item in enumerate(getattr(response, "output", ())):
        parts.extend(_response_item_parts(item, model=model, output_index=index))
    # Some compatible APIs only provide the convenience output_text field.
    text = response_text(response)
    transcripts = {part.transcript for _, part in parts if isinstance(part, AudioPart)}
    if (
        text
        and text not in transcripts
        and not any(isinstance(part, TextPart) for _, part in parts)
    ):
        parts.append((("text", "0", "content", 0), TextPart(text)))
    return parts


def _response_item_parts(
    item: Any, *, model: Model, output_index: int
) -> list[tuple[tuple[object, ...], Part]]:
    identity = getattr(item, "id", "") or str(output_index)
    kind = getattr(item, "type", None)
    if kind == "reasoning":
        return _reasoning_item_parts(item, model=model, output_index=output_index)
    single = SimpleNamespace(output=[item])
    if kind == "function_call":
        call = parse_tool_calls(single)[0]
        return [
            (
                ("tool", identity),
                ToolCallPart(
                    call.tool_call_id,
                    call.name,
                    call.name,
                    dict(call.input),
                    call_id=call.call_id,
                ),
            )
        ]
    parts: list[tuple[tuple[object, ...], Part]] = []
    audio = _response_audio_parts(single)
    transcripts = {part.transcript for part in audio if part.transcript}
    if kind == "message":
        for index, content in enumerate(getattr(item, "content", ())):
            text = _value_text(content, "text")
            if (
                getattr(content, "type", None) in {"output_text", "text"}
                and text
                and text not in transcripts
            ):
                parts.append((("text", identity, "content", index), TextPart(text)))
    parts.extend(
        (("image", identity, index), part)
        for index, part in enumerate(_response_image_parts(single))
    )
    parts.extend((("audio", identity, index), part) for index, part in enumerate(audio))
    return parts


def _reasoning_item_parts(
    item: Any, *, model: Model, output_index: int
) -> list[tuple[tuple[object, ...], Part]]:
    identity = getattr(item, "id", "") or str(output_index)
    summary = list(getattr(item, "summary", ()) or ())
    content = list(getattr(item, "content", ()) or ())
    common = native_metadata(
        model, "responses", item_id=getattr(item, "id", None), output_index=output_index
    )
    shared: dict[str, object] = {
        "summary_count": len(summary),
        "content_count": len(content),
    }
    status = getattr(item, "status", None)
    if status is not None:
        shared["status"] = status
    signature = _value_text(item, "encrypted_content") or None
    parts: list[tuple[tuple[object, ...], Part]] = []
    for field, blocks, native_type in (
        ("summary", summary, "summary_text"),
        ("content", content, "reasoning_text"),
    ):
        for index, block in enumerate(blocks):
            text = _value_text(block, "text")
            block_type = getattr(block, "type", native_type)
            if block_type != native_type:
                raise ToolangError(
                    f"unsupported Responses reasoning content type: {block_type}"
                )
            metadata = {
                **common,
                "field": field,
                "index": index,
                "content_type": block_type,
            }
            if not parts:
                metadata.update(shared)
            part = ReasoningPart(
                text,
                signature=signature if not parts else None,
                provider=model._toolang.provider,
                provider_metadata=metadata,
            )
            if status in {"in_progress", "incomplete"}:
                part = ReasoningPart(text)
            parts.append((("reasoning", identity, field, index), part))
    if not parts and status not in {"in_progress", "incomplete"}:
        parts.append(
            (
                ("reasoning", identity, "opaque", 0),
                ReasoningPart(
                    "",
                    signature=signature,
                    provider=model._toolang.provider,
                    provider_metadata={**common, **shared},
                ),
            )
        )
    return parts


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


def response_continuation(
    response: Any,
    *,
    request: ModelCall,
    emitted_message: Message | None,
    stateful: bool,
) -> dict[str, Any] | None:
    """Return one opaque continuation payload for the next model turn."""

    if not stateful:
        return None
    response_id = getattr(response, "id", None)
    if not isinstance(response_id, str) or not response_id.strip():
        return None
    messages = [*request.messages]
    if emitted_message is not None:
        messages.append(emitted_message)
    continuation: dict[str, Any] = {
        "previous_response_id": response_id,
        "baseline_count": len(messages),
        "prefix": _context_prefix(request, messages),
    }
    return continuation


def _context_prefix(request: ModelCall, messages: Sequence[Message]) -> str:
    data = {
        "instructions": request.instructions,
        "messages": [message.to_data() for message in messages],
        "tools": [tool.to_data() for tool in request.tools],
        "output_schema": request.output_schema,
    }
    return sha256(
        json.dumps(
            data, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


def response_usage(response: Any) -> ModelUsage | None:
    """Extract one normalized model usage summary."""

    usage = getattr(response, "usage", None)
    input_tokens = getattr(usage, "input_tokens", None)
    output_tokens = getattr(usage, "output_tokens", None)
    if not isinstance(input_tokens, int) or not isinstance(output_tokens, int):
        return None
    input_details = getattr(usage, "input_tokens_details", None)
    output_details = getattr(usage, "output_tokens_details", None)
    cached = optional_int(input_details, "cached_tokens")
    cache_write = optional_int(input_details, "cache_write_tokens")
    uncached = None
    if cached is not None or cache_write is not None:
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
    model: Model | None,
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

    reasoning = _encode_reasoning_items(message, model=model)
    for index, part in enumerate(message.parts):
        if isinstance(part, ReasoningPart):
            _flush_text_buffer()
            if index in reasoning:
                items.append(reasoning[index])
            continue
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
            raise ToolangError(
                "assistant/user messages cannot contain tool result parts"
            )
    _flush_text_buffer()
    if not items:
        return None
    return items[0] if len(items) == 1 else items


def _encode_reasoning_items(
    message: Message, *, model: Model | None
) -> dict[int, dict[str, Any]]:
    groups: dict[tuple[object, object], list[tuple[int, ReasoningPart]]] = {}
    for index, part in enumerate(message.parts):
        if (
            not isinstance(part, ReasoningPart)
            or model is None
            or message.role != "assistant"
            or not compatible(part, model, "responses")
        ):
            continue
        meta = part.provider_metadata
        output_index, item_id = meta.get("output_index"), meta.get("item_id")
        if (
            type(output_index) is not int
            or output_index < 0
            or (item_id is not None and (not isinstance(item_id, str) or not item_id))
        ):
            raise ToolangError("Responses reasoning requires a native item identity")
        groups.setdefault((meta.get("output_index"), meta.get("item_id")), []).append(
            (index, part)
        )
    items: dict[int, dict[str, Any]] = {}
    for group in groups.values():
        # Canonical order follows observation; shared native state belongs to
        # the first native summary/content block, which may arrive later.
        owners = [
            part
            for _, part in group
            if "summary_count" in part.provider_metadata
            or "content_count" in part.provider_metadata
        ]
        if len(owners) != 1:
            raise ToolangError("Responses reasoning item is incomplete or malformed")
        owner = owners[0]
        meta = owner.provider_metadata
        opaque = len(group) == 1 and meta.get("field") is None and not owner.text
        if not opaque and any(
            part.provider_metadata.get("field") not in {"summary", "content"}
            or type(part.provider_metadata.get("index")) is not int
            for _, part in group
        ):
            raise ToolangError("Responses reasoning item has malformed content indices")
        item: dict[str, Any] = {"type": "reasoning", "summary": []}
        if meta.get("item_id") is not None:
            item["id"] = meta["item_id"]
        if "status" in meta:
            item["status"] = meta["status"]
        if owner.signature is not None:
            item["encrypted_content"] = owner.signature
        if not item.get("id") and not item.get("encrypted_content"):
            raise ToolangError(
                "Responses reasoning requires a native item ID or encrypted content"
            )
        for field, expected in (
            ("summary", "summary_text"),
            ("content", "reasoning_text"),
        ):
            values = [
                part
                for _, part in group
                if part.provider_metadata.get("field") == field
            ]
            values.sort(
                key=lambda part: cast(int, part.provider_metadata.get("index", -1))
            )
            count = meta.get(f"{field}_count")
            if (
                type(count) is not int
                or count != len(values)
                or any(
                    part.provider_metadata.get("index") != index
                    or part.provider_metadata.get("content_type") != expected
                    for index, part in enumerate(values)
                )
            ):
                raise ToolangError(
                    "Responses reasoning item is incomplete or malformed"
                )
            if values:
                item[field] = [{"type": expected, "text": part.text} for part in values]
        if any(part.signature is not None and part is not owner for _, part in group):
            raise ToolangError("Responses reasoning signature has multiple owners")
        items[group[0][0]] = item
    return items


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
    }
    if part.output or part.error is None:
        payload["output"] = dict(part.output)
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
    model: Model,
    payload: dict[str, Any],
    *,
    stateful: bool,
    stream: bool,
) -> None:
    if not _ADAPTER_LOGGER.isEnabledFor(logging.DEBUG):
        return
    _ADAPTER_LOGGER.debug(
        "adapter.request provider=%s ref=%s model=%s adapter=%s stateful=%s stream=%s payload=%s",
        model._toolang.provider,
        model.ref,
        model.id,
        model._toolang.route.adapter,
        stateful,
        stream,
        _preview_data(payload),
    )


def _log_api_response(
    model: Model,
    response: Any,
    *,
    stateful: bool,
    stream: bool,
) -> None:
    if not _ADAPTER_LOGGER.isEnabledFor(logging.DEBUG):
        return
    _ADAPTER_LOGGER.debug(
        "adapter.result provider=%s ref=%s model=%s adapter=%s stateful=%s stream=%s payload=%s",
        model._toolang.provider,
        model.ref,
        model.id,
        model._toolang.route.adapter,
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
