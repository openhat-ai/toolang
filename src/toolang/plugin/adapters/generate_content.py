"""Google Gemini Generate Content model adapter."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
import json
import asyncio
from typing import cast
from urllib.parse import quote

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
    AudioPart,
    DocumentPart,
    ImagePart,
    Message,
    Part,
    ReasoningPart,
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
class GenerateContentModelAdapter(ModelAdapter):
    """Google Gemini Generate Content API adapter."""

    name: str = "generate_content"
    description: str | None = "Use the Google Gemini Generate Content API shape."
    default_api: str | None = "https://generativelanguage.googleapis.com/v1beta"

    def output_allowance(self, options: Mapping[str, object]) -> int | None:
        """Normalize this protocol's explicitly authored output allowance."""

        generation = options.get("generationConfig")
        return (
            output_allowance(cast(Mapping[str, object], generation), "maxOutputTokens")
            if isinstance(generation, Mapping)
            else None
        )

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
                _generate_url(model, stream=False),
                headers=_generate_headers(model, environ=environ),
                json=generate_content_payload(model, request),
            )
            await raise_for_model_status(response)
            return parse_generate_content(_json_object(response.json()), model=model)

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
        usage: dict[str, object] = {}
        active: int | None = None
        calls: list[ToolCall] = []
        finish_reason = None
        text: list[str] = []

        async def close_active() -> None:
            nonlocal active
            if active is not None:
                part = parts.parts[parts.indices[active]]
                if isinstance(part, ReasoningPart):
                    part = replace(
                        part,
                        provider=model._toolang.provider,
                        provider_metadata=native_metadata(model, "generate_content"),
                    )
                await parts.finish(active, part)
                active = None

        try:
            try:
                with model_transport_errors(
                    usage=lambda: generate_content_usage(usage),
                    partial_text=lambda: "".join(text),
                ):
                    async with httpx.AsyncClient() as client:
                        async with client.stream(
                            "POST",
                            _generate_url(model, stream=True),
                            headers=_generate_headers(model, environ=environ),
                            json=generate_content_payload(model, request),
                        ) as response:
                            await raise_for_model_status(response)
                            async for line in response.aiter_lines():
                                if not line.startswith("data:"):
                                    continue
                                raw = line.removeprefix("data:").strip()
                                if not raw or raw == "[DONE]":
                                    continue
                                chunk = _json_object(json.loads(raw))
                                usage.update(_json_object(chunk.get("usageMetadata")))
                                text.append(_response_text(chunk))
                                _check_response(chunk)
                                finish_reason = (
                                    _candidate(chunk).get("finishReason")
                                    or finish_reason
                                )
                                for native in _candidate_parts(chunk):
                                    part = _generate_part(
                                        native, model=model, fallback=len(calls)
                                    )
                                    if part is None:
                                        continue
                                    if isinstance(part, ToolCallPart):
                                        await close_active()
                                        calls.append(
                                            ToolCall(
                                                part.tool_call_id,
                                                part.call_id or part.tool_call_id,
                                                part.tool_name,
                                                part.input,
                                            )
                                        )
                                        await parts.finish(len(parts.parts), part)
                                    elif isinstance(part, TextPart | ReasoningPart):
                                        if part.signature is not None:
                                            # A signed chunk (including empty text) owns its signature.
                                            await close_active()
                                            await parts.finish(len(parts.parts), part)
                                        else:
                                            if (
                                                active is not None
                                                and parts.parts[
                                                    parts.indices[active]
                                                ].type
                                                != part.type
                                            ):
                                                await close_active()
                                            if active is None:
                                                active = len(parts.parts)
                                            await parts.text(
                                                active,
                                                part.text,
                                                reasoning=isinstance(
                                                    part, ReasoningPart
                                                ),
                                            )
                    if finish_reason is None:
                        raise ModelResponseError(
                            "model stream ended before a terminal finish reason",
                            kind="incomplete_stream",
                        )
            except ModelResponseError as exc:
                if finish_reason is None or exc.kind not in {
                    "transport_error",
                    "incomplete_stream",
                }:
                    raise
            await close_active()
        except (Exception, asyncio.CancelledError):
            await parts.interrupt()
            raise
        return ModelCallResult(
            message=parts.message(),
            tool_calls=tuple(calls),
            usage=generate_content_usage(usage),
        )


def create_model_adapter(config: Mapping[str, object]) -> ModelAdapter:
    """Create the built-in Generate Content adapter."""

    del config
    return GenerateContentModelAdapter()


def generate_content_payload(
    model: Model,
    request: ModelCall,
) -> dict[str, object]:
    """Encode one canonical request for Gemini Generate Content."""

    native_schema = request.output_schema if model.structured_output is True else None
    if (
        native_schema is not None
        and request.tools
        and not _supports_structured_output_with_tools(model.id)
    ):
        native_schema = None
    instructions = (
        append_structured_output_directive(
            request.instructions,
            request.output_schema,
        )
        if request.output_schema is not None and native_schema is None
        else request.instructions
    )
    options = request_options(model._toolang.route.options)
    payload: dict[str, object] = {
        "contents": [
            _encode_message(message, model=model) for message in request.messages
        ],
    }
    if instructions:
        payload["systemInstruction"] = {"parts": [{"text": instructions}]}
    if request.tools:
        payload["tools"] = [
            {
                "functionDeclarations": [
                    {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": dict(tool.parameters),
                    }
                    for tool in request.tools
                ]
            }
        ]
    if options:
        generation = options.pop("generationConfig", {})
        if isinstance(generation, Mapping):
            payload["generationConfig"] = dict(generation)
        payload.update(options)
    _apply_reasoning(payload, request.reasoning)
    _apply_structured_output(
        payload,
        request.output_schema,
        native_schema=native_schema,
    )
    if request.max_output_tokens is not None:
        generation = dict(
            cast(Mapping[str, object], payload.get("generationConfig", {}))
        )
        generation["maxOutputTokens"] = request.max_output_tokens
        payload["generationConfig"] = generation
    return payload


def _check_response(payload: Mapping[str, object]) -> None:
    if "error" in payload:
        raise provider_error(payload["error"])
    if _json_object(payload.get("promptFeedback")).get("blockReason"):
        raise ModelResponseError(
            "provider blocked the prompt", kind="provider_rejection"
        )
    reason = _candidate(payload).get("finishReason")
    if reason == "MAX_TOKENS":
        raise ModelResponseError(
            "model response truncated by output limit", kind="output_limit"
        )
    if reason == "MALFORMED_FUNCTION_CALL":
        raise ModelResponseError(
            "provider reported malformed function call", kind="invalid_json"
        )
    if reason and reason != "STOP":
        raise ModelResponseError(
            f"provider stopped response: {reason}", kind="provider_rejection"
        )


def parse_generate_content(
    payload: Mapping[str, object], *, model: Model
) -> ModelCallResult:
    """Parse Gemini Parts without moving signatures across their boundaries."""

    parts: list[Part] = []
    calls: list[ToolCall] = []
    with model_transport_errors(
        usage=lambda: generate_content_usage(
            _json_object(payload.get("usageMetadata"))
        ),
        partial_text=lambda: _response_text(payload),
    ):
        _check_response(payload)
        for native in _candidate_parts(payload):
            part = _generate_part(native, model=model, fallback=len(calls))
            if part is not None:
                parts.append(part)
            if isinstance(part, ToolCallPart):
                calls.append(
                    ToolCall(
                        part.tool_call_id,
                        part.call_id or part.tool_call_id,
                        part.tool_name,
                        part.input,
                    )
                )
    return ModelCallResult(
        message=Message(role="assistant", parts=tuple(parts)),
        tool_calls=tuple(calls),
        usage=generate_content_usage(_json_object(payload.get("usageMetadata"))),
    )


def _generate_part(
    native: Mapping[str, object], *, model: Model, fallback: int
) -> Part | None:
    signature = _text(native.get("thoughtSignature")) or None
    function = _json_object(native.get("functionCall"))
    if function:
        part = _tool_part(_function_call(function, fallback=fallback))
        if signature is not None:
            part = replace(
                part,
                signature=signature,
                provider=model._toolang.provider,
                provider_metadata=native_metadata(model, "generate_content"),
            )
        return part
    text = _text(native.get("text"))
    if text or signature is not None:
        if native.get("thought") is True:
            return ReasoningPart(
                text,
                signature=signature,
                provider=model._toolang.provider,
                provider_metadata=native_metadata(model, "generate_content"),
            )
        return TextPart(
            text,
            signature=signature,
            provider=model._toolang.provider if signature is not None else None,
            provider_metadata=native_metadata(model, "generate_content")
            if signature is not None
            else {},
        )
    return None


def generate_content_usage(value: Mapping[str, object]) -> ModelUsage | None:
    """Normalize Gemini cached input and thought token usage."""

    prompt = _int(value.get("promptTokenCount"))
    visible = _int(value.get("candidatesTokenCount"))
    if prompt is None or visible is None:
        return None
    cached = _int(value.get("cachedContentTokenCount"))
    thoughts = _int(value.get("thoughtsTokenCount"))
    tool_prompt = _int(value.get("toolUsePromptTokenCount"))
    input_tokens = prompt + (tool_prompt or 0)
    output_tokens = visible + (thoughts or 0)
    total = _int(value.get("totalTokenCount"))
    if total is not None and total >= input_tokens + visible:
        output_tokens = total - input_tokens
    input_audio = _modality_tokens(
        value,
        fields=("promptTokensDetails", "toolUsePromptTokensDetails"),
        modality="audio",
    )
    output_audio = _modality_tokens(
        value,
        fields=("candidatesTokensDetails",),
        modality="audio",
    )
    meters: list[ModelUsageMeter] = []
    if tool_prompt is not None and tool_prompt > 0:
        meters.append(
            ModelUsageMeter(
                name="google.tool_use_prompt",
                quantity=float(tool_prompt),
                unit="token",
            )
        )
    meters.extend(_modality_meters(value))
    billing = {
        name: item
        for name, item in (
            ("service_tier", billing_value(value, "serviceTier")),
            ("traffic_type", billing_value(value, "trafficType")),
        )
        if item is not None
    }
    cost, currency = reported_cost(value)
    return ModelUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        input_uncached_tokens=(input_tokens - cached if cached is not None else None),
        input_cache_read_tokens=cached,
        input_audio_tokens=input_audio,
        output_visible_tokens=visible,
        output_reasoning_tokens=thoughts,
        output_audio_tokens=output_audio,
        meters=tuple(meters),
        reported_cost=cost,
        reported_currency=currency,
        billing=billing,
    )


def _modality_tokens(
    value: Mapping[str, object],
    *,
    fields: tuple[str, ...],
    modality: str,
) -> int | None:
    seen = False
    total = 0
    for field in fields:
        details = value.get(field)
        if not isinstance(details, list):
            continue
        seen = True
        for raw in details:
            item = _json_object(raw)
            if _text(item.get("modality")).lower() != modality:
                continue
            count = _int(item.get("tokenCount"))
            if count is not None:
                total += count
    return total if seen else None


def _modality_meters(
    value: Mapping[str, object],
) -> tuple[ModelUsageMeter, ...]:
    quantities: dict[str, int] = {}
    for field, direction in (
        ("promptTokensDetails", "input"),
        ("toolUsePromptTokensDetails", "input"),
        ("candidatesTokensDetails", "output"),
    ):
        details = value.get(field)
        if not isinstance(details, list):
            continue
        for raw in details:
            item = _json_object(raw)
            modality = _text(item.get("modality")).lower()
            count = _int(item.get("tokenCount"))
            if not modality or modality in {"text", "audio"} or count is None:
                continue
            name = f"google.{direction}.{modality}"
            quantities[name] = quantities.get(name, 0) + count
    return tuple(
        ModelUsageMeter(name=name, quantity=float(quantity), unit="token")
        for name, quantity in sorted(quantities.items())
        if quantity > 0
    )


def _encode_message(
    message: Message,
    *,
    model: Model,
) -> dict[str, object]:
    role = "model" if message.role == "assistant" else "user"
    parts: list[dict[str, object]] = []
    for part in message.parts:
        if isinstance(part, ReasoningPart):
            if message.role != "assistant" or not compatible(
                part, model, "generate_content"
            ):
                continue
            thought: dict[str, object] = {"text": part.text, "thought": True}
            if part.signature is not None:
                thought["thoughtSignature"] = part.signature
            parts.append(thought)
        elif isinstance(part, TextPart):
            text: dict[str, object] = {"text": part.text}
            if (
                message.role == "assistant"
                and compatible(part, model, "generate_content")
                and part.signature is not None
            ):
                text["thoughtSignature"] = part.signature
            parts.append(text)
        elif isinstance(part, ImagePart):
            if part.image_url is not None:
                parts.append(
                    {
                        "fileData": {
                            "fileUri": part.image_url,
                            "mimeType": part.media_type or "image/*",
                        }
                    }
                )
        elif isinstance(part, AudioPart):
            parts.append(
                {
                    "inlineData": {
                        "data": part.data,
                        "mimeType": part.media_type or f"audio/{part.format}",
                    }
                }
            )
        elif isinstance(part, DocumentPart):
            if part.url is not None:
                parts.append(
                    {
                        "fileData": {
                            "fileUri": part.url,
                            "mimeType": part.media_type or "application/pdf",
                        }
                    }
                )
            elif part.data is not None:
                parts.append(
                    {
                        "inlineData": {
                            "data": part.data,
                            "mimeType": part.media_type or "application/pdf",
                        }
                    }
                )
        elif isinstance(part, ToolCallPart):
            call_id = part.call_id or part.tool_call_id
            function_part: dict[str, object] = {
                "functionCall": {
                    "id": call_id,
                    "name": part.tool_name,
                    "args": dict(part.input),
                }
            }
            if (
                message.role == "assistant"
                and compatible(part, model, "generate_content")
                and part.signature is not None
            ):
                function_part["thoughtSignature"] = part.signature
            parts.append(function_part)
        elif isinstance(part, ToolResultPart):
            output = dict(part.output)
            if part.error is not None:
                output = {"error": part.error, **({"output": output} if output else {})}
            parts.append(
                {
                    "functionResponse": {
                        "id": part.call_id or part.tool_call_id,
                        "name": part.tool_name,
                        "response": output,
                    }
                }
            )
    return {"role": role, "parts": parts}


def _candidate(payload: Mapping[str, object]) -> dict[str, object]:
    candidates = payload.get("candidates")
    return (
        _json_object(candidates[0])
        if isinstance(candidates, list) and candidates
        else {}
    )


def _candidate_parts(payload: Mapping[str, object]) -> tuple[dict[str, object], ...]:
    content = _json_object(_candidate(payload).get("content"))
    parts = content.get("parts")
    if not isinstance(parts, list):
        return ()
    return tuple(_json_object(part) for part in parts)


def _response_text(payload: Mapping[str, object]) -> str:
    """Retain all received visible text even when response validation fails."""

    return "".join(
        _text(part.get("text"))
        for part in _candidate_parts(payload)
        if part.get("thought") is not True
    )


def _generate_url(
    model: Model,
    *,
    stream: bool,
) -> str:
    if model._toolang.route.api is None:
        raise ToolangError("Generate Content adapter requires a resolved API")
    action = "streamGenerateContent" if stream else "generateContent"
    suffix = "?alt=sse" if stream else ""
    return f"{model._toolang.route.api.rstrip('/')}/models/{quote(model.id, safe='')}:{action}{suffix}"


def _generate_headers(
    model: Model,
    *,
    environ: Mapping[str, str],
) -> dict[str, str]:
    api_key = credential_value(model._toolang.route.env, environ=environ)
    if not api_key and model._toolang.route.env != ():
        raise ToolangError("Generate Content adapter requires a resolved API key")
    return {
        "content-type": "application/json",
        **({"x-goog-api-key": api_key} if api_key else {}),
        **model._toolang.route.headers,
    }


def _function_call(value: Mapping[str, object], *, fallback: int) -> ToolCall:
    name = _text(value.get("name")).strip()
    if not name:
        raise ModelResponseError(
            "model emitted a tool call without a function name", kind="missing_name"
        )
    call_id = _text(value.get("id")) or f"tool-call-{fallback}"
    return ToolCall(call_id, call_id, name, parse_tool_arguments(value.get("args")))


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


def _apply_structured_output(
    payload: dict[str, object],
    schema: dict[str, object] | None,
    *,
    native_schema: dict[str, object] | None,
) -> None:
    if schema is None:
        return
    raw_generation = payload.get("generationConfig")
    generation = (
        dict(cast(Mapping[str, object], raw_generation))
        if isinstance(raw_generation, Mapping)
        else {}
    )
    output_fields = {
        "_responseJsonSchema",
        "responseFormat",
        "responseJsonSchema",
        "responseMimeType",
        "responseSchema",
    }
    if output_fields & generation.keys():
        raise ToolangError(
            "Generate Content response format conflicts with normalized structured output"
        )
    if native_schema is None:
        return
    generation["responseMimeType"] = "application/json"
    generation["responseJsonSchema"] = dict(native_schema)
    payload["generationConfig"] = generation


def _supports_structured_output_with_tools(model: str) -> bool:
    model_name = model.rsplit("/", 1)[-1].lower()
    return model_name.startswith("gemini-3")


def _apply_reasoning(
    payload: dict[str, object],
    reasoning: Reasoning | None,
) -> None:
    if reasoning is None:
        return
    raw_generation = payload.get("generationConfig")
    generation = (
        dict(cast(Mapping[str, object], raw_generation))
        if isinstance(raw_generation, Mapping)
        else {}
    )
    raw_thinking = generation.get("thinkingConfig")
    thinking = (
        dict(cast(Mapping[str, object], raw_thinking))
        if isinstance(raw_thinking, Mapping)
        else {}
    )
    effort = reasoning.effort
    budget = reasoning.budget_tokens
    if effort is None and budget is None:
        return
    if effort == "none" and budget is not None:
        raise ToolangError(
            "disabled Generate Content reasoning conflicts with a token budget"
        )
    if budget is not None and effort is not None:
        raise ToolangError(
            "Generate Content accepts either reasoning effort or budget_tokens"
        )
    thinking.pop("thinkingBudget", None)
    thinking.pop("thinkingLevel", None)
    if effort == "none":
        thinking["thinkingBudget"] = 0
    elif isinstance(budget, int) and not isinstance(budget, bool):
        thinking["thinkingBudget"] = budget
    elif isinstance(effort, str):
        thinking["thinkingLevel"] = effort.upper()
    generation["thinkingConfig"] = thinking
    payload["generationConfig"] = generation
