"""Model-call steps and streaming part events."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
import logging
import time
from typing import TYPE_CHECKING

from toolang.base.types.message import (
    Message,
    MessagePart,
    MessagePartType,
    TextDelta,
    TextPart,
    ToolCallDelta,
    ToolCallPart,
    message_text,
)
from toolang.base.types.model import ModelTarget
from toolang.base.types.run import (
    ModelCall,
    ModelCallResult,
    ModelPartDelta,
    ModelPartEnd,
    ModelPartStart,
    ToolCall,
)
from toolang.common.time import elapsed_ms, utc_now

from ...events import PartBegin, PartDelta, PartEnd, StepBegin, StepEnd
from ...records import (
    RunControlRecord,
    RunInputRef,
    StepInput,
    StepOutputRef,
    model_call_to_data,
)
from ...types import StepPath
from ..diagnostics import log_model_request, log_model_result, log_model_target
from ..limits import _ModelAccounting

if TYPE_CHECKING:
    from ..runs.agic import _AgicState

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class _ModelStream:
    step: int
    part_count: int = 0
    text_part: int | None = None
    tool_parts: dict[str, int] = field(default_factory=dict)
    started_parts: set[int] = field(default_factory=set)


async def execute(state: _AgicState) -> ModelCallResult:
    """Perform one model call and emit its complete step event stream."""

    prepared = state.prepared
    run = prepared.run
    state.before_model_call()
    step_index = state.next_step
    state.next_step += 1
    step_started = time.perf_counter()
    started_at = utc_now()
    consumed_inputs = _consume_pending_inputs(state)
    step_input = (
        *_step_input(state),
        *(RunInputRef(index=item.index) for item in consumed_inputs),
    )
    stream = _ModelStream(step=step_index)
    _LOGGER.info(
        "Step started thread=%s run=%s step=%s kind=model",
        run.thread,
        run.run_id,
        step_index,
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
    await state.emit(
        StepBegin(
            step=StepPath(run.run_id, (step_index,)),
            kind="model",
            input=step_input,
            started_at=started_at,
            given={
                "model": _model_target_data(prepared.model),
                "call": model_call_to_data(request),
            },
        )
    )
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
    except asyncio.CancelledError:
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
        await state.emit(
            StepEnd(
                step=StepPath(run.run_id, (step_index,)),
                kind="model",
                status="failed",
                error=str(exc) or type(exc).__name__,
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
        raise
    return await _apply_response(
        state,
        stream,
        current,
        step_index=step_index,
        duration_ms=elapsed_ms(step_started),
    )


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
    if current.message is not None:
        state.messages.append(current.message)
    state.model_state = current.state
    accounting = state.account_usage(current.usage)
    await state.emit(
        StepEnd(
            step=StepPath(run.run_id, (step_index,)),
            kind="model",
            status="finished",
            output=output,
            noted={
                **_accounting_data(accounting),
                "reasoning_content": _message_reasoning_content(current.message),
                "state": dict(current.state) if current.state is not None else None,
            },
            finished_at=utc_now(),
        )
    )
    state.last_step = step_index
    state.record_accounting(accounting)
    usage = current.usage
    _LOGGER.info(
        "Step finished thread=%s run=%s step=%s kind=model status=finished input=%s output=%s tool_calls=%s duration_ms=%s",
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
        return


def _output_parts(
    stream: _ModelStream,
    *,
    current: ModelCallResult,
    tool_calls: Sequence[ToolCall],
) -> list[tuple[int, MessagePart]]:
    items: list[tuple[int, MessagePart]] = []
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


def _step_input(state: _AgicState) -> tuple[StepInput, ...]:
    if state.last_step is None:
        return (RunInputRef(),)
    return (
        StepOutputRef(
            step=StepPath(state.prepared.run.run_id, (state.last_step,)),
        ),
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


def _next_part_index(stream: _ModelStream) -> int:
    part_index = stream.part_count
    stream.part_count += 1
    return part_index


async def _emit_part_begin(
    state: _AgicState,
    stream: _ModelStream,
    *,
    part_index: int,
    kind: MessagePartType,
) -> None:
    if part_index in stream.started_parts:
        return
    stream.started_parts.add(part_index)
    await state.emit(
        PartBegin(
            step=StepPath(state.prepared.run.run_id, (stream.step,)),
            part=part_index,
            part_type=kind,
        )
    )


def _message_reasoning_content(message: Message | None) -> str | None:
    if message is None:
        return None
    return next(
        (
            part.reasoning
            for part in message.parts
            if isinstance(part, ToolCallPart) and part.reasoning
        ),
        None,
    )


def _accounting_data(accounting: _ModelAccounting) -> dict[str, object]:
    usage = accounting.usage
    price = accounting.price
    return {
        "tokens": (
            {"input": usage.input_tokens, "output": usage.output_tokens}
            if usage is not None
            else None
        ),
        "price": (
            {
                "input": _decimal_text(price.input),
                "output": _decimal_text(price.output),
            }
            if price is not None
            else None
        ),
        "cost": _decimal_text(accounting.cost),
    }


def _decimal_text(value: Decimal | None) -> str | None:
    return format(value, "f") if value is not None else None


def _model_target_data(target: ModelTarget) -> dict[str, object]:
    return {
        "ref": target.ref,
        "provider": target.provider,
        "name": target.name,
        "model": target.model,
        "adapter": target.adapter,
        "base_url": target.base_url,
        "scope": target.scope,
        "tags": list(target.tags),
        "options": dict(target.options),
        "tools": target.tools,
        "streaming": target.streaming,
    }
