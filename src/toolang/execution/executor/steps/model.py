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
from typing import TYPE_CHECKING

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
from toolang.base.errors import ToolangError
from toolang.common.time import elapsed_ms, utc_now
from toolang.state.state import AgentState, StatePublication

from ...events import PartBegin, PartDelta, PartEnd, StepBegin, StepEnd
from ...assembly import assemble_messages
from ...records import ControlRecord
from ...types import (
    Local,
    ModelStepGiven,
    ModelStepNoted,
    ModelTokenCount,
    ModelTokenPrice,
    ControlRef,
    ErrorMessage,
    FieldRef,
    StepRef,
    RunRef,
)
from .._messages import _MessageBuffer
from ..budget import message_tokens
from ..common import _StepFailed, control_local_pointer
from ..diagnostics import log_model_request, log_model_result, log_model_target
from ..limits import _ModelAccounting
from . import tool as tool_step

if TYPE_CHECKING:
    from ..prepare import _AgicFrame
    from ..runs.agic import _AgicState

_LOGGER = logging.getLogger(__name__)


class _NeedsCompact(Exception):
    def __init__(self, end: RunRef) -> None:
        self.end = end


def _candidate(
    state: _AgicState,
    agent_state: AgentState | StatePublication,
    state_ref: ControlRef,
) -> tuple[_AgicFrame, _MessageBuffer, tuple[ControlRecord, ...], ModelCall]:
    prepared = state.frame_for_step(agent_state, state_ref)
    state.claimed_inputs = (*state.claimed_inputs, *state.pending_inputs())
    recalled = (
        state.execution.runtime_controls(prepared.run.run_id, refresh=False)
        if state.execution is not None
        else ()
    )
    preceding = tuple(
        sorted((*state.claimed_inputs, *recalled), key=lambda control: control.index)
    )
    messages = state.messages.copy()
    if not messages.started:
        messages.initialize(prepared.messages)
    elif prepared.prompt_context:
        messages.append(Message.user(prepared.prompt_context))
    if (
        not messages.started
        and state.execution is not None
        and "near" in prepared.recall
    ):
        messages.prepend(*state.execution.message_history().tail(prepared.run.horizon))
    for control in preceding:
        messages.append_control(control)
    request = ModelCall(
        instructions=_model_instructions(state, prepared),
        messages=assemble_messages(
            prepared.far, prepared.near, messages.messages, prepared.recall
        ),
        tools=_model_tools(prepared)
        if prepared.model.tools and not state.repairing_output
        else (),
        output_schema=deepcopy(state.output_binding.output_schema),
        continuation=state.continuation,
        max_output_tokens=prepared.output_budget,
    )
    return prepared, messages, preceding, request


def _estimate_binding(prepared: _AgicFrame) -> object:
    return (
        prepared.model,
        prepared.run.state.revision,
        prepared.run.horizon,
        prepared.recall,
    )


def _boundary(
    state: _AgicState, prepared: _AgicFrame, request: ModelCall
) -> RunRef | None:
    budget = prepared.input_budget
    if (
        budget is None
        or state.estimate.count(request, _estimate_binding(prepared)) <= budget
    ):
        return None
    execution = state.execution
    if (
        execution is None
        or prepared.run.thread.startswith("compact_")
        or "near" not in prepared.recall
    ):
        raise ToolangError(
            "model input exceeds its budget; no compactable near history"
        )
    roots = execution.message_history().near_roots(prepared.run.horizon)
    if len(roots) < 2:
        raise ToolangError(
            "model input exceeds its budget; fixed content, now, or required near cannot be compacted"
        )
    retained = 0
    end = roots[-1][0]
    # Reserve at most half of the input budget for near; always retain its last
    # historical root. Advance at least one root when compaction is necessary.
    for index in range(len(roots) - 1, 0, -1):
        root, messages = roots[index]
        size = sum(message_tokens(message) for message in messages)
        if index != len(roots) - 1 and retained + size > budget // 2:
            break
        retained += size
        end = root
    return end


def compaction_boundary(state: _AgicState) -> RunRef | None:
    """Reprepare after admission without committing a Step or consuming deltas."""
    if state.execution is None:
        raise RuntimeError("Agic runtime execution is unavailable")
    prepared, _messages, _controls, request = _candidate(
        state, *state.execution.state_snapshot()
    )
    return _boundary(state, prepared, request)


@dataclass(slots=True)
class _ModelStream:
    step: int
    part_count: int = 0
    text_part: int | None = None
    tool_parts: dict[str, int] = field(default_factory=dict)
    started_parts: set[int] = field(default_factory=set)
    ended_parts: set[int] = field(default_factory=set)
    part_types: dict[int, PartType] = field(default_factory=dict)
    text_chunks: list[str] = field(default_factory=list)
    tool_chunks: dict[str, list[str]] = field(default_factory=dict)
    completed_parts: dict[int, Part] = field(default_factory=dict)


async def execute(state: _AgicState) -> ModelCallResult:
    """Perform one model call and emit its complete step event stream."""

    run = state.prepared.run
    state.check_model_call_limit()
    step_index = state.next_step
    stream = _ModelStream(step=step_index)
    prepared = state.prepared
    request: ModelCall | None = None
    next_messages = state.messages

    def begin_step(
        agent_state: AgentState | StatePublication,
        state_ref: ControlRef,
    ) -> StepBegin:
        nonlocal prepared, request, next_messages
        prepared, next_messages, preceding, request = _candidate(
            state, agent_state, state_ref
        )
        canceling = state.execution is not None and bool(
            state.execution.pending_controls(run.run_id, "cancel")
        )
        if interruption is None and not canceling:
            boundary = _boundary(state, prepared, request)
            if boundary is not None:
                raise _NeedsCompact(boundary)
        return StepBegin(
            step=StepRef.from_local(run.run_id, (step_index,)),
            kind="model",
            state=state_ref,
            input=(
                *_step_input(state),
                *(control_local_pointer(item, "_") for item in state.claimed_inputs),
            ),
            preceded_by=tuple(item.ref for item in preceding),
            started_at=utc_now(),
            given=ModelStepGiven(
                model=prepared.model.ref,
                call=request,
                delta=next_messages.take_delta(),
                recall=prepared.recall,
            ),
        )

    def adopt_begin() -> None:
        state.prepared = prepared
        state.messages = next_messages
        state.visible_recalls = (
            dict(state.execution.message_history().recalls(prepared.run.horizon))
            if state.execution is not None and "near" in prepared.recall
            else {}
        )
        state.visible_recalls.update(next_messages.recalls)
        state.claimed_inputs = ()
        state.next_model_inputs = None
        state.next_step = step_index + 1
        state.model_calls += 1
        _LOGGER.info(
            "Step started thread=%s run=%s step=%s kind=model",
            run.thread,
            run.run_id,
            step_index,
        )

    interruption: asyncio.CancelledError | None = None
    while True:
        try:
            await state.start_step(begin_step)
        except _NeedsCompact as needed:
            identity = f"compact_{run.run_id}_{state.next_step}"
            result = await tool_step.execute(
                state,
                ToolCall(
                    tool_call_id=identity,
                    call_id=identity,
                    name="_toolang__compact",
                    input={"thread": run.thread, "begin": None, "end": str(needed.end)},
                ),
                trigger="runtime",
            )
            if result.error:
                raise _StepFailed(
                    StepRef.from_local(run.run_id, (state.next_step - 1,)),
                    ToolangError(result.error),
                )
            step_index = state.next_step
            stream = _ModelStream(step=step_index)
            continue
        except asyncio.CancelledError as exc:
            interruption = exc
            if state.execution is None:
                raise
            record = state.execution.store.get_step(
                ref=StepRef.from_local(run.run_id, (step_index,))
            )
            if record is None:
                if state.immediate_steer():
                    raise
                # A real cancel still needs a begin/end boundary; no adapter runs.
                continue
            adopt_begin()
            if record.status != "running":
                raise
        else:
            if request is None:  # pragma: no cover - boundary builder invariant
                raise RuntimeError("model step boundary did not build its request")
            adopt_begin()
            if interruption is None:
                break
        await state.end_step(
            StepEnd(
                step=StepRef.from_local(run.run_id, (step_index,)),
                kind="model",
                status="canceled",
                finished_at=utc_now(),
            )
        )
        raise interruption
    step_started = time.perf_counter()
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
        state.before_call()
        if prepared.model.streaming:
            current = await prepared.adapter.stream(
                prepared.model,
                request,
                on_event=lambda event: _handle_event(state, stream, event),
            )
        else:
            current = await prepared.adapter.invoke(prepared.model, request)
        _validate_stream_result(stream, current)
        output = await _emit_response_parts(state, stream, current)
        if prepared.input_budget is not None:
            state.estimate.observe(
                request,
                _estimate_binding(prepared),
                current.usage.input_tokens if current.usage else None,
            )
    except asyncio.CancelledError:
        await _end_incomplete(state, stream)
        raise
    except Exception as exc:
        message = str(exc) or type(exc).__name__
        await _end_incomplete(state, stream, error=ErrorMessage(message))
        _LOGGER.error(
            "Step failed thread=%s run=%s step=%s kind=model error=%r duration_ms=%s",
            run.thread,
            run.run_id,
            step_index,
            str(exc),
            elapsed_ms(step_started),
        )
        raise _StepFailed(StepRef.from_local(run.run_id, (step_index,)), exc) from exc
    return await _apply_response(
        state,
        current,
        output,
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
    """Expose the selected registered tools at the adapter boundary."""

    definitions = {name: tool.definition() for name, tool in prepared.tools.items()}
    return tuple(definitions[name] for name in sorted(definitions))


async def _emit_response_parts(
    state: _AgicState,
    stream: _ModelStream,
    current: ModelCallResult,
) -> tuple[Part, ...]:
    """Publish completed Parts inside the model's interruption boundary."""

    run = state.prepared.run
    parsed_calls = tuple(current.tool_calls)
    log_model_result(
        current,
        thread_id=run.thread,
        run_id=run.run_id,
        step_index=stream.step,
    )
    output_parts = _output_parts(
        stream,
        current=current,
        tool_calls=parsed_calls,
    )
    stream.completed_parts.update(output_parts)
    for part_index, part in output_parts:
        await _emit_part_begin(
            state,
            stream,
            part_index=part_index,
            kind=part.type,
        )
        await _emit_part_end(state, stream, part_index, part)
        if isinstance(part, ToolCallPart):
            state.tool_call_sources[part.tool_call_id] = (stream.step, part_index)
    return tuple(part for _, part in sorted(output_parts, key=lambda item: item[0]))


async def _apply_response(
    state: _AgicState,
    current: ModelCallResult,
    output: tuple[Part, ...],
    *,
    step_index: int,
    duration_ms: int,
) -> ModelCallResult:
    run = state.prepared.run
    local = Local.typed("Part[]", output, "_", 0)
    if output:
        state.messages.append_ref(
            "assistant",
            FieldRef.from_path(
                StepRef.from_local(run.run_id, (step_index,)), "output", "value"
            ),
            local,
        )
    state.continuation = current.continuation
    accounting = state.account_usage(current.usage)
    state.last_step = step_index
    try:
        await state.end_step(
            StepEnd(
                step=StepRef.from_local(run.run_id, (step_index,)),
                kind="model",
                status="succeeded",
                output=local,
                noted=_model_step_noted(
                    accounting,
                    continuation=current.continuation,
                ),
                finished_at=utc_now(),
            )
        )
    except asyncio.CancelledError:
        if not state.immediate_steer():
            await tool_step.skip(state, tuple(current.tool_calls), canceled=True)
            raise
        # The complete response is durable and already in the message prefix.
        # Return its calls so steer recovery records their skipped results.
    state.record_accounting(accounting)
    usage = current.usage
    _LOGGER.info(
        "Step finished thread=%s run=%s step=%s kind=model input=%s output=%s tool_calls=%s duration_ms=%s",
        run.thread,
        run.run_id,
        step_index,
        usage.input_tokens if usage is not None else 0,
        usage.output_tokens if usage is not None else 0,
        len(current.tool_calls),
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
                        step=StepRef.from_local(
                            state.prepared.run.run_id, (stream.step,)
                        ),
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
                        step=StepRef.from_local(
                            state.prepared.run.run_id, (stream.step,)
                        ),
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
            part_index = _next_part_index(stream)
        stream.completed_parts[part_index] = event.data
        await _emit_part_begin(
            state,
            stream,
            part_index=part_index,
            kind=event.data.type,
        )
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
            used = {index for index, _ in items}
            part_index = next(
                (
                    index
                    for index, completed in stream.completed_parts.items()
                    if index not in used and completed == part
                ),
                None,
            )
            items.append(
                (_next_part_index(stream) if part_index is None else part_index, part)
            )
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


def _step_input(state: _AgicState) -> tuple[FieldRef, ...]:
    if state.next_model_inputs is not None:
        return state.next_model_inputs
    if state.last_step is None:
        return state.initial_inputs
    return (
        FieldRef.from_path(
            StepRef.from_local(state.prepared.run.run_id, (state.last_step,)),
            "output",
            "value",
        ),
    )


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
    stream.started_parts.add(part_index)
    stream.part_types[part_index] = kind
    await state.emit(
        PartBegin(
            step=StepRef.from_local(state.prepared.run.run_id, (stream.step,)),
            part=part_index,
            part_type=kind,
        )
    )


async def _emit_part_end(
    state: _AgicState, stream: _ModelStream, part_index: int, part: Part
) -> None:
    # Events are projected before awaiting observers; cancellation there must not
    # cause an already-delivered terminal event to be emitted again.
    stream.ended_parts.add(part_index)
    await state.emit(
        PartEnd(
            step=StepRef.from_local(state.prepared.run.run_id, (stream.step,)),
            part=part_index,
            data=part,
        )
    )


async def _end_incomplete(
    state: _AgicState, stream: _ModelStream, *, error: ErrorMessage | None = None
) -> None:
    """Retain execution facts even when interrupted while closing display events."""

    parts = {
        index: stream.completed_parts.get(index) or _partial_part(stream, index)
        for index in sorted(stream.started_parts | stream.completed_parts.keys())
    }
    # Unfinished ToolCall placeholders close display events only.
    output = tuple(
        part
        for index, part in parts.items()
        if index in stream.completed_parts or isinstance(part, TextPart)
    )
    step = StepRef.from_local(state.prepared.run.run_id, (stream.step,))
    local = Local.typed("Part[]", output, "_", 0) if output else None
    calls = tuple(
        ToolCall(
            part.tool_call_id,
            part.call_id or part.tool_call_id,
            part.tool_name,
            part.input,
        )
        for part in output
        if isinstance(part, ToolCallPart)
    )
    if local is not None:
        state.messages.append_ref(
            "assistant", FieldRef.from_path(step, "output", "value"), local
        )
        state.last_step = stream.step
        for index, part in enumerate(output):
            if isinstance(part, ToolCallPart):
                state.tool_call_sources[part.tool_call_id] = (stream.step, index)
    try:
        for index, part in parts.items():
            if index not in stream.ended_parts:
                await _emit_part_begin(state, stream, part_index=index, kind=part.type)
                await _emit_part_end(state, stream, index, part)
    except asyncio.CancelledError:
        error = None
        raise
    finally:
        try:
            await state.end_step(
                StepEnd(
                    step=step,
                    kind="model",
                    status="failed" if error is not None else "canceled",
                    output=local,
                    error=error,
                    finished_at=utc_now(),
                )
            )
        finally:
            if error is None:
                await tool_step.skip(state, calls, canceled=not state.immediate_steer())


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
