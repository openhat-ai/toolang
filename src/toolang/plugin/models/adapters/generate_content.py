"""Google Gemini Generate Content model adapter."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from decimal import Decimal
import json
from typing import Any, cast
from urllib.parse import quote

import httpx

from toolang.base.errors import ToolangError
from toolang.base.protocols.model import ModelAdapter
from toolang.base.types.message import (
    AudioPart,
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
class GenerateContentModelAdapter(ModelAdapter):
    """Google Gemini Generate Content API adapter."""

    name: str = "generate_content"
    description: str | None = "Use the Google Gemini Generate Content API shape."
    default_endpoint: str | None = "https://generativelanguage.googleapis.com/v1beta"

    async def invoke(
        self,
        target: ModelTarget,
        request: ModelCall,
    ) -> ModelCallResult:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                _generate_url(target, stream=False),
                headers={"content-type": "application/json", **target.headers},
                json=generate_content_payload(target, request),
            )
            response.raise_for_status()
            result = parse_generate_content(_json_object(response.json()))
            return replace(result, state=_merge_state(request.state, result.state))

    async def stream(
        self,
        target: ModelTarget,
        request: ModelCall,
        *,
        on_event: ModelStreamHandler,
    ) -> ModelCallResult:
        text: list[str] = []
        calls: list[ToolCall] = []
        signatures: dict[str, str] = {}
        usage: dict[str, object] = {}
        async with httpx.AsyncClient() as client:
            async with client.stream(
                "POST",
                _generate_url(target, stream=True),
                headers={"content-type": "application/json", **target.headers},
                json=generate_content_payload(target, request),
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    raw = line.removeprefix("data:").strip()
                    if not raw or raw == "[DONE]":
                        continue
                    chunk = _json_object(json.loads(raw))
                    usage.update(_json_object(chunk.get("usageMetadata")))
                    for part in _candidate_parts(chunk):
                        value = _text(part.get("text"))
                        if value and part.get("thought") is not True:
                            if not text:
                                await on_event(ModelPartStart(kind="text"))
                            text.append(value)
                            await on_event(ModelPartDelta(delta=TextDelta(value)))
                        function = _json_object(part.get("functionCall"))
                        if function:
                            call = _function_call(function, fallback=len(calls))
                            calls.append(call)
                            signature = _text(part.get("thoughtSignature"))
                            if signature:
                                signatures[call.call_id] = signature
                            await on_event(ModelPartStart(kind="tool_call"))
                            await on_event(ModelPartEnd(data=_tool_part(call)))
        parts: list[TextPart | ToolCallPart] = []
        output = "".join(text)
        if output:
            part = TextPart(output)
            parts.append(part)
            await on_event(ModelPartEnd(data=part))
        parts.extend(_tool_part(call) for call in calls)
        return ModelCallResult(
            message=Message(role="assistant", parts=tuple(parts)),
            tool_calls=tuple(calls),
            usage=generate_content_usage(usage),
            state=_merge_state(
                request.state,
                {_THOUGHT_SIGNATURES: signatures} if signatures else None,
            ),
        )


def create_model_adapter(config: Mapping[str, object]) -> ModelAdapter:
    """Create the built-in Generate Content adapter."""

    del config
    return GenerateContentModelAdapter()


def generate_content_payload(
    target: ModelTarget,
    request: ModelCall,
) -> dict[str, object]:
    """Encode one canonical request for Gemini Generate Content."""

    options = dict(target.options)
    payload: dict[str, object] = {
        "contents": [
            _encode_message(
                message,
                signatures=_state_signatures(request.state),
            )
            for message in request.messages
        ],
    }
    if request.instructions:
        payload["systemInstruction"] = {"parts": [{"text": request.instructions}]}
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
    _apply_reasoning(payload, target.reasoning)
    return payload


def parse_generate_content(payload: Mapping[str, object]) -> ModelCallResult:
    """Parse one Gemini Generate Content response."""

    parts: list[TextPart | ToolCallPart] = []
    calls: list[ToolCall] = []
    signatures: dict[str, str] = {}
    for part in _candidate_parts(payload):
        value = _text(part.get("text"))
        if value and part.get("thought") is not True:
            parts.append(TextPart(value))
        function = _json_object(part.get("functionCall"))
        if function:
            call = _function_call(function, fallback=len(calls))
            calls.append(call)
            parts.append(_tool_part(call))
            signature = _text(part.get("thoughtSignature"))
            if signature:
                signatures[call.call_id] = signature
    return ModelCallResult(
        message=Message(role="assistant", parts=tuple(parts)),
        tool_calls=tuple(calls),
        usage=generate_content_usage(_json_object(payload.get("usageMetadata"))),
        state={_THOUGHT_SIGNATURES: signatures} if signatures else None,
    )


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
                quantity=Decimal(tool_prompt),
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
        ModelUsageMeter(name=name, quantity=Decimal(quantity), unit="token")
        for name, quantity in sorted(quantities.items())
        if quantity > 0
    )


_THOUGHT_SIGNATURES = "thought_signatures"


def _encode_message(
    message: Message,
    *,
    signatures: Mapping[str, str],
) -> dict[str, object]:
    role = "model" if message.role == "assistant" else "user"
    parts: list[dict[str, object]] = []
    for part in message.parts:
        if isinstance(part, TextPart):
            parts.append({"text": part.text})
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
            signature = signatures.get(call_id)
            if signature:
                function_part["thoughtSignature"] = signature
            parts.append(function_part)
        elif isinstance(part, ToolResultPart):
            parts.append(
                {
                    "functionResponse": {
                        "id": part.call_id or part.tool_call_id,
                        "name": part.tool_name,
                        "response": dict(part.output),
                    }
                }
            )
    return {"role": role, "parts": parts}


def _candidate_parts(payload: Mapping[str, object]) -> tuple[dict[str, object], ...]:
    candidates = payload.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return ()
    candidate = _json_object(candidates[0])
    content = _json_object(candidate.get("content"))
    parts = content.get("parts")
    if not isinstance(parts, list):
        return ()
    return tuple(_json_object(part) for part in parts)


def _generate_url(target: ModelTarget, *, stream: bool) -> str:
    if target.base_url is None:
        raise ToolangError("Generate Content adapter requires a resolved endpoint")
    if not target.api_key:
        raise ToolangError("Generate Content adapter requires a resolved API key")
    action = "streamGenerateContent" if stream else "generateContent"
    suffix = "&alt=sse" if stream else ""
    model = quote(target.model, safe="")
    return (
        f"{target.base_url.rstrip('/')}/models/{model}:{action}"
        f"?key={quote(target.api_key, safe='')}{suffix}"
    )


def _function_call(value: Mapping[str, object], *, fallback: int) -> ToolCall:
    name = _text(value.get("name"))
    call_id = _text(value.get("id")) or f"tool-call-{fallback}"
    args = _json_object(value.get("args"))
    return ToolCall(call_id, call_id, name, dict(cast(Mapping[str, Any], args)))


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


def _state_signatures(state: Mapping[str, Any] | None) -> dict[str, str]:
    if not isinstance(state, Mapping):
        return {}
    value = state.get(_THOUGHT_SIGNATURES)
    if not isinstance(value, Mapping):
        return {}
    return {
        str(key): signature
        for key, signature in value.items()
        if isinstance(signature, str) and signature
    }


def _merge_state(
    previous: Mapping[str, Any] | None,
    current: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    merged = dict(previous or {})
    current_values = dict(current or {})
    signatures = _state_signatures(previous)
    signatures.update(_state_signatures(current))
    merged.update(current_values)
    if signatures:
        merged[_THOUGHT_SIGNATURES] = signatures
    return merged or None


def _apply_reasoning(
    payload: dict[str, object],
    reasoning: Mapping[str, object],
) -> None:
    if not reasoning:
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
    enabled = reasoning.get("enabled")
    effort = reasoning.get("effort")
    budget = reasoning.get("budget_tokens")
    if enabled is False or effort == "none":
        thinking["thinkingBudget"] = 0
    elif isinstance(budget, int) and not isinstance(budget, bool):
        thinking["thinkingBudget"] = budget
    elif enabled is True:
        thinking["thinkingBudget"] = -1
    if isinstance(effort, str) and effort != "none":
        thinking["thinkingLevel"] = effort.upper()
    generation["thinkingConfig"] = thinking
    payload["generationConfig"] = generation
