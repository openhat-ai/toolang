"""Model execution helpers for prepared prompt builds."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Any

from toolang.concepts.tools import ToolCallResult, ToolDefinition
from toolang.errors import ToolangError
from toolang.tools import ToolRuntime

from .build import PromptBuild

MAX_TOOL_ROUNDS = 8


@dataclass(frozen=True, slots=True)
class _PendingToolCall:
    call_id: str
    result: ToolCallResult


@dataclass(frozen=True, slots=True)
class ModelExecutionResult:
    """Completed model execution result with local tool-call records."""

    output_text: str
    tool_calls: list[ToolCallResult] = field(default_factory=list)


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
        pending_calls = _tool_calls_from_response(current, tool_runtime)
        if not pending_calls:
            return ModelExecutionResult(
                output_text=_coerce_response_text(current),
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


def _create_openai_client() -> Any:
    try:
        from openai import OpenAI
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise ToolangError(
            "The 'openai' package is not installed. Run 'uv add openai' to enable toolang invoke."
        ) from exc
    return OpenAI()


def _coerce_response_text(response: Any) -> str:
    text = getattr(response, "output_text", None)
    if isinstance(text, str) and text.strip():
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
    raise ToolangError("Model response did not contain text output.")


def _tool_calls_from_response(
    response: Any,
    tool_runtime: ToolRuntime,
) -> list[_PendingToolCall]:
    results: list[_PendingToolCall] = []
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
        try:
            output = provider.invoke(arguments, tool_runtime.context)
            error = None
        except Exception as exc:
            output = {}
            error = str(exc)
        results.append(
            _PendingToolCall(
                call_id=call_id,
                result=ToolCallResult(
                    family=provider.family,
                    name=name,
                    arguments=arguments,
                    output=output,
                    error=error,
                ),
            )
        )
    return results


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
