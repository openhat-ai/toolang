"""Model execution helpers for prepared prompt builds."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Any, Callable

from toolang.concepts.tools import ToolCallResult, ToolDefinition, ToolFamily
from toolang.errors import ToolangError
from toolang.tools import ToolRuntime

from .build import PromptBuild

MAX_TOOL_ROUNDS = 8


@dataclass(frozen=True, slots=True)
class _PendingToolCall:
    call_id: str
    tool_call_id: str
    result: ToolCallResult


@dataclass(frozen=True, slots=True)
class _ParsedToolCall:
    call_id: str
    tool_call_id: str
    family: ToolFamily
    name: str
    arguments: dict[str, Any]
    provider: Any


@dataclass(frozen=True, slots=True)
class ModelExecutionResult:
    """Completed model execution result with local tool-call records."""

    output_text: str
    tool_calls: list[ToolCallResult] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class TextDeltaEvent:
    """One streamed text delta from model execution."""

    delta: str


@dataclass(frozen=True, slots=True)
class ToolInputStartEvent:
    """One streamed tool-input start event."""

    tool_call_id: str


@dataclass(frozen=True, slots=True)
class ToolInputDeltaEvent:
    """One streamed tool-input delta event."""

    tool_call_id: str
    delta: str


@dataclass(frozen=True, slots=True)
class ToolInputAvailableEvent:
    """One complete tool input ready for local execution."""

    tool_call_id: str
    family: ToolFamily
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ToolOutputAvailableEvent:
    """One completed local tool output event."""

    tool_call_id: str
    result: ToolCallResult


ModelExecutionStreamEvent = (
    TextDeltaEvent
    | ToolInputStartEvent
    | ToolInputDeltaEvent
    | ToolInputAvailableEvent
    | ToolOutputAvailableEvent
)
ModelExecutionEventHandler = Callable[[ModelExecutionStreamEvent], None]


def execute_prompt_build(build: PromptBuild) -> ModelExecutionResult:
    """Execute one prepared prompt build and return model output and tool calls."""

    openai_client = _create_openai_client()
    tool_runtime = build.tool_runtime
    tool_definitions = (
        tool_runtime.definitions()
        if tool_runtime is not None and tool_runtime.providers
        else []
    )
    response = _create_response(
        openai_client,
        model=build.model,
        messages=build.messages,
        tools=tool_definitions,
    )
    if not tool_definitions:
        return ModelExecutionResult(output_text=_coerce_response_text(response))
    return _continue_with_tools(
        openai_client,
        response=response,
        model=build.model,
        tool_runtime=tool_runtime,
        tool_definitions=tool_definitions,
    )


def execute_prompt_build_stream(
    build: PromptBuild,
    *,
    on_event: ModelExecutionEventHandler,
) -> ModelExecutionResult:
    """Execute one prepared prompt build and emit streamed text and tool events."""

    openai_client = _create_openai_client()
    tool_runtime = build.tool_runtime
    tool_definitions = (
        tool_runtime.definitions()
        if tool_runtime is not None and tool_runtime.providers
        else []
    )
    return _continue_with_stream(
        openai_client,
        model=build.model,
        messages=build.messages,
        tool_runtime=tool_runtime,
        tool_definitions=tool_definitions,
        on_event=on_event,
    )


def _continue_with_tools(
    openai_client: Any,
    *,
    response: Any,
    model: str,
    tool_runtime: ToolRuntime | None,
    tool_definitions: list[ToolDefinition],
) -> ModelExecutionResult:
    if tool_runtime is None:
        return ModelExecutionResult(output_text=_coerce_response_text(response))

    executed_calls: list[ToolCallResult] = []
    current = response
    for _ in range(MAX_TOOL_ROUNDS):
        pending_calls = _execute_tool_calls(
            _parse_tool_calls_from_response(current, tool_runtime),
            tool_runtime,
        )
        if not pending_calls:
            return ModelExecutionResult(
                output_text=_coerce_response_text(
                    current, allow_empty=bool(executed_calls)
                ),
                tool_calls=executed_calls,
            )
        followup_input = []
        for call in pending_calls:
            executed_calls.append(call.result)
            payload = {
                "ok": call.result.error is None,
                "family": call.result.family,
                "name": call.result.name,
                "output": call.result.output,
            }
            if call.result.error is not None:
                payload["error"] = call.result.error
            followup_input.append(
                {
                    "type": "function_call_output",
                    "call_id": call.call_id,
                    "output": json.dumps(payload, ensure_ascii=False),
                }
            )
        current = _create_response(
            openai_client,
            model=model,
            messages=followup_input,
            tools=tool_definitions,
            previous_response_id=getattr(current, "id", None),
        )
    raise ToolangError("Model tool loop exceeded the maximum number of rounds.")


def _continue_with_stream(
    openai_client: Any,
    *,
    model: str,
    messages: list[dict[str, Any]],
    tool_runtime: ToolRuntime | None,
    tool_definitions: list[ToolDefinition],
    on_event: ModelExecutionEventHandler,
) -> ModelExecutionResult:
    executed_calls: list[ToolCallResult] = []
    current_messages = messages
    previous_response_id: str | None = None
    for _ in range(MAX_TOOL_ROUNDS):
        current, emitted_text, started_tool_inputs = _create_streamed_response(
            openai_client,
            model=model,
            messages=current_messages,
            tools=tool_definitions,
            previous_response_id=previous_response_id,
            on_event=on_event,
        )
        if not emitted_text and current.output_text:
            on_event(TextDeltaEvent(delta=current.output_text))
        if tool_runtime is None:
            return ModelExecutionResult(output_text=_coerce_response_text(current))
        parsed_calls = _parse_tool_calls_from_response(current, tool_runtime)
        if not parsed_calls:
            return ModelExecutionResult(
                output_text=_coerce_response_text(
                    current, allow_empty=bool(executed_calls)
                ),
                tool_calls=executed_calls,
            )
        followup_input = []
        for parsed in parsed_calls:
            if parsed.tool_call_id not in started_tool_inputs:
                on_event(ToolInputStartEvent(tool_call_id=parsed.tool_call_id))
            on_event(
                ToolInputAvailableEvent(
                    tool_call_id=parsed.tool_call_id,
                    family=parsed.family,
                    name=parsed.name,
                    arguments=parsed.arguments,
                )
            )
            call = _invoke_tool_call(parsed, tool_runtime)
            executed_calls.append(call.result)
            on_event(
                ToolOutputAvailableEvent(
                    tool_call_id=call.tool_call_id,
                    result=call.result,
                )
            )
            payload = {
                "ok": call.result.error is None,
                "family": call.result.family,
                "name": call.result.name,
                "output": call.result.output,
            }
            if call.result.error is not None:
                payload["error"] = call.result.error
            followup_input.append(
                {
                    "type": "function_call_output",
                    "call_id": call.call_id,
                    "output": json.dumps(payload, ensure_ascii=False),
                }
            )
        current_messages = followup_input
        previous_response_id = getattr(current, "id", None)
    raise ToolangError("Model tool loop exceeded the maximum number of rounds.")


def _create_openai_client() -> Any:
    try:
        from openai import OpenAI
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise ToolangError(
            "The 'openai' package is not installed. Run 'uv add openai' to enable toolang invoke."
        ) from exc
    return OpenAI()


def _coerce_response_text(response: Any, *, allow_empty: bool = False) -> str:
    text = getattr(response, "output_text", None)
    if isinstance(text, str) and text.strip():
        return text
    if allow_empty and isinstance(text, str):
        return text

    collected: list[str] = []
    for item in getattr(response, "output", []):
        if getattr(item, "type", None) != "message":
            continue
        for content in getattr(item, "content", []):
            content_type = getattr(content, "type", None)
            if content_type in {"output_text", "text"} and getattr(content, "text", None):
                collected.append(content.text)

    if collected:
        return "".join(collected)
    if allow_empty:
        return ""
    raise ToolangError("Model response did not contain text output.")


def _parse_tool_calls_from_response(
    response: Any,
    tool_runtime: ToolRuntime,
) -> list[_ParsedToolCall]:
    results: list[_ParsedToolCall] = []
    providers_by_name = {
        provider.definition().name: provider
        for provider in tool_runtime.providers.values()
    }
    for item in getattr(response, "output", []):
        if getattr(item, "type", None) != "function_call":
            continue
        name = str(getattr(item, "name", "")).strip()
        provider = providers_by_name.get(name)
        call_id = str(getattr(item, "call_id", "")).strip()
        arguments = _parse_tool_arguments(getattr(item, "arguments", "{}"))
        if provider is None:
            raise ToolangError(f"unknown tool call: {name or '<empty>'}")
        results.append(
            _ParsedToolCall(
                call_id=call_id,
                tool_call_id=_tool_call_id_from_item(item, fallback=call_id),
                family=provider.family,
                name=name,
                arguments=arguments,
                provider=provider,
            )
        )
    return results


def _execute_tool_calls(
    calls: list[_ParsedToolCall], tool_runtime: ToolRuntime
) -> list[_PendingToolCall]:
    return [_invoke_tool_call(call, tool_runtime) for call in calls]


def _invoke_tool_call(
    call: _ParsedToolCall,
    tool_runtime: ToolRuntime,
) -> _PendingToolCall:
    try:
        output = call.provider.invoke(call.arguments, tool_runtime.context)
        error = None
    except Exception as exc:
        output = {}
        error = str(exc)
    return _PendingToolCall(
        call_id=call.call_id,
        tool_call_id=call.tool_call_id,
        result=ToolCallResult(
            family=call.family,
            name=call.name,
            arguments=call.arguments,
            output=output,
            error=error,
        ),
    )


def _parse_tool_arguments(raw: object) -> dict[str, Any]:
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


def _create_response(
    openai_client: Any,
    *,
    model: str,
    messages: list[dict[str, Any]],
    tools: list[ToolDefinition] | None = None,
    previous_response_id: str | None = None,
) -> Any:
    payload: dict[str, Any] = {
        "model": model,
        "input": messages,
    }
    if tools:
        payload["tools"] = [
            {
                "type": "function",
                "name": item.name,
                "description": item.description,
                "parameters": item.parameters,
            }
            for item in tools
        ]
    if previous_response_id:
        payload["previous_response_id"] = previous_response_id
    return openai_client.responses.create(**payload)


def _create_response_stream(
    openai_client: Any,
    *,
    model: str,
    messages: list[dict[str, Any]],
    tools: list[ToolDefinition] | None,
    previous_response_id: str | None = None,
) -> Any:
    payload: dict[str, Any] = {
        "model": model,
        "input": messages,
    }
    if tools:
        payload["tools"] = [
            {
                "type": "function",
                "name": item.name,
                "description": item.description,
                "parameters": item.parameters,
            }
            for item in tools
        ]
    if previous_response_id:
        payload["previous_response_id"] = previous_response_id
    return openai_client.responses.stream(**payload)


def _create_streamed_response(
    openai_client: Any,
    *,
    model: str,
    messages: list[dict[str, Any]],
    tools: list[ToolDefinition] | None,
    previous_response_id: str | None,
    on_event: ModelExecutionEventHandler,
) -> tuple[Any, bool, set[str]]:
    emitted_text = False
    started_tool_inputs: set[str] = set()
    with _create_response_stream(
        openai_client,
        model=model,
        messages=messages,
        tools=tools,
        previous_response_id=previous_response_id,
    ) as stream:
        for event in stream:
            if getattr(event, "type", None) == "response.output_text.delta":
                delta = str(getattr(event, "delta", ""))
                if delta:
                    emitted_text = True
                    on_event(TextDeltaEvent(delta=delta))
            elif getattr(event, "type", None) == "response.function_call_arguments.delta":
                delta = str(getattr(event, "delta", ""))
                tool_call_id = _tool_call_id_from_event(event)
                if tool_call_id not in started_tool_inputs:
                    started_tool_inputs.add(tool_call_id)
                    on_event(ToolInputStartEvent(tool_call_id=tool_call_id))
                if delta:
                    on_event(
                        ToolInputDeltaEvent(
                            tool_call_id=tool_call_id,
                            delta=delta,
                        )
                    )
        return stream.get_final_response(), emitted_text, started_tool_inputs


def _tool_call_id_from_event(event: Any) -> str:
    item_id = str(getattr(event, "item_id", "")).strip()
    if item_id:
        return item_id
    call_id = str(getattr(event, "call_id", "")).strip()
    if call_id:
        return call_id
    output_index = getattr(event, "output_index", None)
    return f"tool-call-{output_index if output_index is not None else 'unknown'}"


def _tool_call_id_from_item(item: Any, *, fallback: str) -> str:
    item_id = str(getattr(item, "id", "")).strip()
    if item_id:
        return item_id
    call_id = str(getattr(item, "call_id", "")).strip()
    if call_id:
        return call_id
    return fallback
