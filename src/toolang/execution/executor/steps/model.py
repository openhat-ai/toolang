"""Model-call steps and streaming part events."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
import logging
import time
from typing import TYPE_CHECKING, Any

from toolang.base.types.message import (
    Message,
    Part,
    PartType,
    TextDelta,
    TextPart,
    ToolCallDelta,
    ToolCallPart,
    message_text,
)
from toolang.base.events import (
    ModelPartDeltaEvent,
    ModelPartEndEvent,
    ModelPartStartEvent,
)
from toolang.base.types.run import (
    ModelCall,
    ModelCallResult,
    ToolCall,
)
from toolang.common.time import elapsed_ms, utc_now

from ...events import PartBegin, PartDelta, PartEnd, StepBegin, StepEnd
from ...records import (
    OutputRef,
    RunControlRecord,
    RunControlRef,
    StepInputItem,
    trace_child_path,
)
from ..diagnostics import log_model_request, log_model_result, log_model_target

if TYPE_CHECKING:
    from ..runs.agic import _AgicState

_LOGGER = logging.getLogger("toolang.run")


@dataclass(slots=True)
class _ModelStream:
    step: int
    part_count: int = 0
    text_part: int | None = None
    tool_parts: dict[str, int] = field(default_factory=dict)
    started_parts: set[int] = field(default_factory=set)


def execute(state: _AgicState) -> ModelCallResult:
    """Perform one model call and emit its complete step event stream."""

    prepared = state.prepared
    run = prepared.run
    state.before_call()
    step_index = state.next_step
    state.next_step += 1
    step_started = time.perf_counter()
    started_at = utc_now()
    consumed_inputs = _consume_pending_inputs(state)
    step_input = (
        *_step_input(state),
        *(RunControlRef(index=item.index) for item in consumed_inputs),
    )
    stream = _ModelStream(step=step_index)
    _LOGGER.info(
        "Step started thread=%s run=%s step=%s kind=model",
        run.thread_id,
        run.run_id,
        step_index,
    )
    state.emit(
        StepBegin(
            step=trace_child_path(run.run_id, step_index),
            kind="model",
            input=step_input,
            started_at=started_at,
            context={
                "prompt_context": prepared.prompt_context,
                "instruct": prepared.instructions,
            },
        )
    )
    request = ModelCall(
        instructions=prepared.instructions,
        messages=list(state.messages),
        tools=(
            tuple(
                tool.definition()
                for tool in sorted(prepared.tools.values(), key=lambda item: item.name)
            )
            if prepared.model.tools
            else ()
        ),
        state=state.model_state,
    )
    log_model_target(
        prepared.model,
        thread_id=run.thread_id,
        run_id=run.run_id,
        step_index=step_index,
    )
    log_model_request(
        request,
        thread_id=run.thread_id,
        run_id=run.run_id,
        step_index=step_index,
    )
    try:
        if prepared.model.streaming:
            current = prepared.adapter.stream(
                prepared.model,
                request,
                on_event=lambda event: _handle_event(state, stream, event),
            )
        else:
            current = prepared.adapter.invoke(prepared.model, request)
    except Exception as exc:
        state.emit(
            StepEnd(
                step=trace_child_path(run.run_id, step_index),
                kind="model",
                status="failed",
                error=str(exc) or type(exc).__name__,
                started_at=started_at,
                finished_at=utc_now(),
            )
        )
        _LOGGER.error(
            "Step failed thread=%s run=%s step=%s kind=model error=%r duration_ms=%s",
            run.thread_id,
            run.run_id,
            step_index,
            str(exc),
            elapsed_ms(step_started),
        )
        raise
    return _apply_response(
        state,
        stream,
        current,
        request=request,
        step_index=step_index,
        started_at=started_at,
        duration_ms=elapsed_ms(step_started),
    )


def _apply_response(
    state: _AgicState,
    stream: _ModelStream,
    current: ModelCallResult,
    *,
    request: ModelCall,
    step_index: int,
    started_at: str,
    duration_ms: int,
) -> ModelCallResult:
    prepared = state.prepared
    run = prepared.run
    parsed_calls = tuple(current.tool_calls)
    log_model_result(
        current,
        thread_id=run.thread_id,
        run_id=run.run_id,
        step_index=step_index,
    )
    output_parts = _output_parts(
        stream,
        current=current,
        tool_calls=parsed_calls,
    )
    for part_index, part in output_parts:
        _emit_part_begin(
            state,
            stream,
            part_index=part_index,
            kind=part.type,
        )
        state.emit(
            PartEnd(
                step=trace_child_path(run.run_id, step_index),
                part=part_index,
                data=part,
            )
        )
        if isinstance(part, ToolCallPart):
            state.tool_call_sources[part.tool_call_id] = (step_index, part_index)
    output = tuple(part for _, part in sorted(output_parts, key=lambda item: item[0]))
    if current.message is not None:
        state.messages.append(current.message)
    state.model_state = current.state
    state.emit(
        StepEnd(
            step=trace_child_path(run.run_id, step_index),
            kind="model",
            status="finished",
            output=output,
            detail={
                "model_ref": prepared.model.ref,
                "usage": {
                    "input_tokens": current.usage.input_tokens
                    if current.usage is not None
                    else 0,
                    "output_tokens": current.usage.output_tokens
                    if current.usage is not None
                    else 0,
                },
                "provider": prepared.model.provider,
                "model": prepared.model.model,
                "adapter": prepared.model.adapter,
                "base_url": prepared.model.base_url,
                "reasoning_content": _message_reasoning_content(current.message),
                "adapter_request": _request_data(request),
            },
            started_at=started_at,
            finished_at=utc_now(),
        )
    )
    state.last_step = step_index
    usage = current.usage
    _LOGGER.info(
        "Step finished thread=%s run=%s step=%s kind=model status=finished input=%s output=%s tool_calls=%s duration_ms=%s",
        run.thread_id,
        run.run_id,
        step_index,
        usage.input_tokens if usage is not None else 0,
        usage.output_tokens if usage is not None else 0,
        len(parsed_calls),
        duration_ms,
    )
    return current


def _handle_event(state: _AgicState, stream: _ModelStream, event: object) -> None:
    if isinstance(event, ModelPartStartEvent):
        if event.kind == "text":
            _emit_part_begin(
                state,
                stream,
                part_index=_ensure_text_part_index(stream),
                kind="text",
            )
        return
    if isinstance(event, ModelPartDeltaEvent):
        if isinstance(event.delta, TextDelta):
            part_index = _ensure_text_part_index(stream)
            _emit_part_begin(
                state,
                stream,
                part_index=part_index,
                kind="text",
            )
            if event.delta.text:
                state.emit(
                    PartDelta(
                        step=trace_child_path(state.prepared.run.run_id, stream.step),
                        part=part_index,
                        delta=event.delta,
                    )
                )
            return
        if isinstance(event.delta, ToolCallDelta):
            part_index = _ensure_tool_part_index(stream, event.delta.tool_call_id)
            _emit_part_begin(
                state,
                stream,
                part_index=part_index,
                kind="tool_call",
            )
            if event.delta.text:
                state.emit(
                    PartDelta(
                        step=trace_child_path(state.prepared.run.run_id, stream.step),
                        part=part_index,
                        delta=event.delta,
                    )
                )
            return
    if isinstance(event, ModelPartEndEvent):
        return


def _output_parts(
    stream: _ModelStream,
    *,
    current: ModelCallResult,
    tool_calls: Sequence[ToolCall],
) -> list[tuple[int, Part]]:
    items: list[tuple[int, Part]] = []
    seen_tool_calls: set[str] = set()
    saw_text = False
    message = current.message
    if message is not None and message.role == "assistant":
        for part in message.parts:
            if isinstance(part, TextPart):
                part_index = _ensure_text_part_index(stream)
                items.append((part_index, part))
                saw_text = True
                continue
            if isinstance(part, ToolCallPart):
                part_index = _ensure_tool_part_index(stream, part.tool_call_id)
                items.append((part_index, part))
                seen_tool_calls.add(part.tool_call_id)
    current_text = message_text(message.parts) if message is not None else ""
    if not saw_text and current_text:
        part_index = _ensure_text_part_index(stream)
        items.append((part_index, TextPart(text=current_text)))
    for call in tool_calls:
        if call.tool_call_id in seen_tool_calls:
            continue
        part_index = _ensure_tool_part_index(stream, call.tool_call_id)
        items.append(
            (
                part_index,
                ToolCallPart(
                    tool_call_id=call.tool_call_id,
                    call_id=call.call_id,
                    tool_name=call.name,
                    tool_family=call.name,
                    input=dict(call.input),
                ),
            )
        )
    return items


def _step_input(state: _AgicState) -> tuple[StepInputItem, ...]:
    if state.last_step is None:
        return (RunControlRef(),)
    return (
        OutputRef(step=trace_child_path(state.prepared.run.run_id, state.last_step)),
    )


def _consume_pending_inputs(state: _AgicState) -> tuple[RunControlRecord, ...]:
    inputs = state.pending_inputs()
    for input in inputs:
        if input.kind == "steer" and input.input is not None:
            state.messages.append(input.input)
    return inputs


def _ensure_text_part_index(stream: _ModelStream) -> int:
    if stream.text_part is None:
        stream.text_part = stream.part_count
        stream.part_count += 1
    return stream.text_part


def _ensure_tool_part_index(stream: _ModelStream, tool_call_id: str) -> int:
    part_index = stream.tool_parts.get(tool_call_id)
    if part_index is None:
        part_index = stream.part_count
        stream.part_count += 1
        stream.tool_parts[tool_call_id] = part_index
    return part_index


def _emit_part_begin(
    state: _AgicState,
    stream: _ModelStream,
    *,
    part_index: int,
    kind: PartType,
) -> None:
    if part_index in stream.started_parts:
        return
    stream.started_parts.add(part_index)
    state.emit(
        PartBegin(
            step=trace_child_path(state.prepared.run.run_id, stream.step),
            part=part_index,
            type_=kind,
        )
    )


def _request_data(request: ModelCall) -> dict[str, Any]:
    return {
        "instructions": request.instructions,
        "messages": [message.to_data() for message in request.messages],
        "tools": [
            {
                "name": tool.name,
                "description": tool.description,
                "parameters": dict(tool.parameters),
            }
            for tool in request.tools
        ],
        "state": request.state,
    }


def _message_reasoning_content(message: Message | None) -> str | None:
    if message is None:
        return None
    value = message.meta.get("reasoning_content")
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None
