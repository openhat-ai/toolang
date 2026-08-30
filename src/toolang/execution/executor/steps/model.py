"""Model-call steps and streaming part events."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
import json
import logging
import time
from typing import TYPE_CHECKING, cast

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
from toolang.base.types.run import (
    ModelCall,
    ModelContinuation,
    ModelCallResult,
    ModelPartDelta,
    ModelPartEnd,
    ModelPartStart,
    ToolCall,
)
from toolang.base.types.tool import ToolDefinition
from toolang.common.time import elapsed_ms, utc_now
from toolang.lang.types import Array
from toolang.state.state import AgentState

from ...events import PartBegin, PartDelta, PartEnd, StepBegin, StepEnd
from ...records import (
    ControlRecord,
    SteerControlPayload,
)
from ...types import (
    Local,
    ModelStepGiven,
    ModelStepNoted,
    ModelTokenCount,
    ModelTokenPrice,
    ControlRef,
    Pointer,
    StepPath,
)
from ..common import _StepFailed, control_local_pointer
from ..diagnostics import log_model_request, log_model_result, log_model_target
from ..limits import _ModelAccounting

if TYPE_CHECKING:
    from ..prepare import _AgicFrame
    from ..runs.agic import _AgicState

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class _ModelStream:
    step: int
    part_count: int = 0
    text_part: int | None = None
    tool_parts: dict[str, int] = field(default_factory=dict)
    started_parts: set[int] = field(default_factory=set)
    part_types: dict[int, PartType] = field(default_factory=dict)
    text_chunks: list[str] = field(default_factory=list)
    tool_chunks: dict[str, list[str]] = field(default_factory=dict)
    completed_parts: dict[int, Part] = field(default_factory=dict)


async def execute(state: _AgicState) -> ModelCallResult:
    """Perform one model call and emit its complete step event stream."""

    run = state.prepared.run
    state.before_model_call()
    step_index = state.next_step
    state.next_step += 1
    step_started = time.perf_counter()
    started_at = utc_now()
    consumed_inputs = state.claimed_inputs or state.pending_inputs()
    step_input = (
        *_step_input(state),
        *(control_local_pointer(item, "_") for item in consumed_inputs),
    )
    stream = _ModelStream(step=step_index)
    _LOGGER.info(
        "Step started thread=%s run=%s step=%s kind=model",
        run.thread,
        run.run_id,
        step_index,
    )
    prepared = state.prepared
    request: ModelCall | None = None
    next_messages: list[Message] | None = None

    def begin_step(agent_state: AgentState, state_ref: ControlRef) -> StepBegin:
        nonlocal prepared, request, next_messages
        prepared = state.frame_for_step(agent_state, state_ref)
        next_messages = _messages_with_inputs(
            prepared.messages if state.last_step is None else state.messages,
            consumed_inputs,
        )
        request = ModelCall(
            instructions=_model_instructions(state, prepared),
            messages=list(next_messages),
            tools=(
                _model_tools(prepared)
                if prepared.model.tools and not state.repairing_output
                else ()
            ),
            output_schema=deepcopy(state.output_binding.output_schema),
            continuation=state.continuation,
        )
        return StepBegin(
            step=StepPath(run.run_id, (step_index,)),
            kind="model",
            state=state_ref,
            input=step_input,
            started_at=started_at,
            given=ModelStepGiven(model=prepared.model.ref, call=request),
        )

    await state.start_step(begin_step)
    if (
        request is None or next_messages is None
    ):  # pragma: no cover - boundary builder invariant
        raise RuntimeError("model step boundary did not build its request")
    state.prepared = prepared
    state.claimed_inputs = ()
    state.messages = next_messages
    log_model_target(
        prepared.model,
        thread_id=run.thread,
        run_id=run.run_id,
        step_index=step_index,
    )
    log_model_request(
        request,
        thread_id=run.thread,
        run_id=run.run_id,
        step_index=step_index,
    )
    try:
        if prepared.model.streaming:
            current = await prepared.adapter.stream(
                prepared.model,
                request,
                on_event=lambda event: _handle_event(state, stream, event),
            )
        else:
            current = await prepared.adapter.invoke(prepared.model, request)
        _validate_stream_result(stream, current)
    except asyncio.CancelledError:
        await _close_open_parts(state, stream)
        await state.emit(
            StepEnd(
                step=StepPath(run.run_id, (step_index,)),
                kind="model",
                status="canceled",
                finished_at=utc_now(),
            )
        )
        raise
    except Exception as exc:
        message = str(exc) or type(exc).__name__
        await _close_open_parts(state, stream)
        await state.emit(
            StepEnd(
                step=StepPath(run.run_id, (step_index,)),
                kind="model",
                status="failed",
                error=message,
                finished_at=utc_now(),
            )
        )
        _LOGGER.error(
            "Step failed thread=%s run=%s step=%s kind=model error=%r duration_ms=%s",
            run.thread,
            run.run_id,
            step_index,
            str(exc),
            elapsed_ms(step_started),
        )
        raise _StepFailed(StepPath(run.run_id, (step_index,)), exc) from exc
    return await _apply_response(
        state,
        stream,
        current,
        step_index=step_index,
        duration_ms=elapsed_ms(step_started),
    )


def _model_instructions(state: _AgicState, prepared: _AgicFrame) -> str:
    """Combine authored and runtime protocol only for an effective tool call."""

    runtime = (
        prepared.runtime_instructions
        if prepared.model.tools and not state.repairing_output
        else ""
    )
    if prepared.instructions and runtime:
        return f"{prepared.instructions}\n\n{runtime}"
    return prepared.instructions or runtime


def _model_tools(prepared: _AgicFrame) -> tuple[ToolDefinition, ...]:
    """Combine public and inner runtime tools only at the adapter boundary."""

    definitions = {name: tool.definition() for name, tool in prepared.tools.items()}
    definitions.update(
        {name: tool.definition for name, tool in prepared.runtime_tools.items()}
    )
    return tuple(definitions[name] for name in sorted(definitions))


async def _apply_response(
    state: _AgicState,
    stream: _ModelStream,
    current: ModelCallResult,
    *,
    step_index: int,
    duration_ms: int,
) -> ModelCallResult:
    prepared = state.prepared
    run = prepared.run
    parsed_calls = tuple(current.tool_calls)
    log_model_result(
        current,
        thread_id=run.thread,
        run_id=run.run_id,
        step_index=step_index,
    )
    output_parts = _output_parts(
        stream,
        current=current,
        tool_calls=parsed_calls,
    )
    for part_index, part in output_parts:
        await _emit_part_begin(
            state,
            stream,
            part_index=part_index,
            kind=part.type,
        )
        await state.emit(
            PartEnd(
                step=StepPath(run.run_id, (step_index,)),
                part=part_index,
                data=part,
            )
        )
        if isinstance(part, ToolCallPart):
            state.tool_call_sources[part.tool_call_id] = (step_index, part_index)
    output = tuple(part for _, part in sorted(output_parts, key=lambda item: item[0]))
    if output:
        state.messages.append(Message(role="assistant", parts=output))
    state.continuation = current.continuation
    accounting = state.account_usage(current.usage)
    await state.emit(
        StepEnd(
            step=StepPath(run.run_id, (step_index,)),
            kind="model",
            status="succeeded",
            output=Local.typed("Part[]", output, "_", 0),
            noted=_model_step_noted(
                accounting,
                continuation=current.continuation,
            ),
            finished_at=utc_now(),
        )
    )
    state.last_step = step_index
    state.record_accounting(accounting)
    usage = current.usage
    _LOGGER.info(
        "Step finished thread=%s run=%s step=%s kind=model status=succeeded input=%s output=%s tool_calls=%s duration_ms=%s",
        run.thread,
        run.run_id,
        step_index,
        usage.input_tokens if usage is not None else 0,
        usage.output_tokens if usage is not None else 0,
        len(parsed_calls),
        duration_ms,
    )
    return current


async def _handle_event(
    state: _AgicState,
    stream: _ModelStream,
    event: object,
) -> None:
    if isinstance(event, ModelPartStart):
        if event.kind == "text":
            await _emit_part_begin(
                state,
                stream,
                part_index=_ensure_text_part_index(stream),
                kind="text",
            )
        return
    if isinstance(event, ModelPartDelta):
        if isinstance(event.delta, TextDelta):
            stream.text_chunks.append(event.delta.text)
            part_index = _ensure_text_part_index(stream)
            await _emit_part_begin(
                state,
                stream,
                part_index=part_index,
                kind="text",
            )
            if event.delta.text:
                await state.emit(
                    PartDelta(
                        step=StepPath(state.prepared.run.run_id, (stream.step,)),
                        part=part_index,
                        delta=event.delta,
                    )
                )
            return
        if isinstance(event.delta, ToolCallDelta):
            stream.tool_chunks.setdefault(event.delta.tool_call_id, []).append(
                event.delta.text
            )
            part_index = _ensure_tool_part_index(stream, event.delta.tool_call_id)
            await _emit_part_begin(
                state,
                stream,
                part_index=part_index,
                kind="tool_call",
            )
            if event.delta.text:
                await state.emit(
                    PartDelta(
                        step=StepPath(state.prepared.run.run_id, (stream.step,)),
                        part=part_index,
                        delta=event.delta,
                    )
                )
            return
    if isinstance(event, ModelPartEnd):
        if isinstance(event.data, TextPart):
            _validate_text_prefix(
                stream,
                event.data.text,
                source="ModelPartEnd",
            )
            part_index = _ensure_text_part_index(stream)
        elif isinstance(event.data, ToolCallPart):
            part_index = _ensure_tool_part_index(stream, event.data.tool_call_id)
        else:
            return
        await _emit_part_begin(
            state,
            stream,
            part_index=part_index,
            kind=event.data.type,
        )
        stream.completed_parts[part_index] = event.data
        return


def _validate_stream_result(
    stream: _ModelStream,
    current: ModelCallResult,
) -> None:
    message = current.message
    final_text = (
        message_text(message.parts)
        if message is not None and message.role == "assistant"
        else ""
    )
    _validate_text_prefix(stream, final_text, source="ModelCallResult")
    if stream.text_part is None:
        return
    completed = stream.completed_parts.get(stream.text_part)
    if isinstance(completed, TextPart) and completed.text != final_text:
        raise ValueError(
            "ModelCallResult text does not match authoritative ModelPartEnd"
        )


def _validate_text_prefix(
    stream: _ModelStream,
    final_text: str,
    *,
    source: str,
) -> None:
    streamed = "".join(stream.text_chunks)
    if not final_text.startswith(streamed):
        raise ValueError(f"{source} text does not extend streamed TextDelta content")


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
                continue
            items.append((_next_part_index(stream), part))
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


def _step_input(state: _AgicState) -> tuple[Pointer, ...]:
    if state.next_model_inputs is not None:
        inputs = state.next_model_inputs
        state.next_model_inputs = None
        return inputs
    if state.last_step is None:
        return state.initial_inputs
    return (
        Pointer.step(
            StepPath(state.prepared.run.run_id, (state.last_step,)),
            "output",
            "value",
        ),
    )


def _messages_with_inputs(
    current: Sequence[Message],
    inputs: Sequence[ControlRecord],
) -> list[Message]:
    messages = list(current)
    for input in inputs:
        if isinstance(input.payload, SteerControlPayload):
            primary = next(
                (item for item in input.payload.locals if item.name == "_"), None
            )
            if (
                primary is not None
                and isinstance(primary.value, Array)
                and all(isinstance(item, Part) for item in primary.value)
            ):
                messages.append(
                    Message(
                        role="user",
                        parts=cast(tuple[Part, ...], tuple(primary.value)),
                    )
                )
    return messages


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


def _next_part_index(stream: _ModelStream) -> int:
    part_index = stream.part_count
    stream.part_count += 1
    return part_index


async def _emit_part_begin(
    state: _AgicState,
    stream: _ModelStream,
    *,
    part_index: int,
    kind: PartType,
) -> None:
    if part_index in stream.started_parts:
        return
    await state.emit(
        PartBegin(
            step=StepPath(state.prepared.run.run_id, (stream.step,)),
            part=part_index,
            part_type=kind,
        )
    )
    stream.started_parts.add(part_index)
    stream.part_types[part_index] = kind


async def _close_open_parts(state: _AgicState, stream: _ModelStream) -> None:
    """Close every streamed Part before emitting a terminal Model Step."""

    for part_index in sorted(stream.started_parts):
        await state.emit(
            PartEnd(
                step=StepPath(state.prepared.run.run_id, (stream.step,)),
                part=part_index,
                data=stream.completed_parts.get(part_index)
                or _partial_part(stream, part_index),
            )
        )


def _partial_part(stream: _ModelStream, part_index: int) -> Part:
    part_type = stream.part_types[part_index]
    if part_type == "text":
        return TextPart(text="".join(stream.text_chunks))
    if part_type == "tool_call":
        tool_call_id = next(
            call_id
            for call_id, index in stream.tool_parts.items()
            if index == part_index
        )
        raw_input = "".join(stream.tool_chunks.get(tool_call_id, ()))
        try:
            decoded = json.loads(raw_input) if raw_input else {}
        except json.JSONDecodeError:
            decoded = {}
        return ToolCallPart(
            tool_call_id=tool_call_id,
            tool_name="",
            tool_family="",
            input=dict(decoded) if isinstance(decoded, Mapping) else {},
        )
    raise RuntimeError(f"unsupported partial model Part type: {part_type}")


def _model_step_noted(
    accounting: _ModelAccounting,
    *,
    continuation: ModelContinuation | None,
) -> ModelStepNoted:
    usage = accounting.usage
    price = accounting.price
    return ModelStepNoted(
        tokens=(
            ModelTokenCount(input=usage.input_tokens, output=usage.output_tokens)
            if usage is not None
            else None
        ),
        price=(
            ModelTokenPrice(
                input=_decimal_text(price.input),
                output=_decimal_text(price.output),
            )
            if price is not None
            else None
        ),
        cost=_decimal_text(accounting.cost),
        accounting=accounting.accounting,
        continuation=(dict(continuation) if continuation is not None else None),
    )


def _decimal_text(value: Decimal | None) -> str | None:
    return format(value, "f") if value is not None else None
