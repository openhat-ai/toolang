"""Model-call steps and streaming part events."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

from toolang.base.errors import ModelResponseError, ToolangError
from toolang.base.types.message import (
    Part,
    PartType,
    ReasoningPart,
    ReasoningDelta,
    TextDelta,
    TextPart,
    ToolCallDelta,
    ToolCallPart,
)
from toolang.base.types.run import (
    ModelCall,
    ModelCallResult,
    ModelContinuation,
    ModelPartDelta,
    ModelPartEnd,
    ModelPartStart,
    ToolCall,
)
from toolang.common.time import elapsed_ms, utc_now
from toolang.state.state import AgentState

from ...assembly import prompting
from ...assembly.message_buffer import MessageBuffer
from ...events import PartBegin, PartDelta, PartEnd, StepBegin, StepEnd
from ...recall import required_declarations, history_variables
from ...records import ControlRecord, RecallControlPayload
from ...types import (
    ModelAccounting,
    ControlRef,
    ErrorMessage,
    FieldRef,
    Local,
    ModelMessages,
    MessageTemplate,
    ModelStepGiven,
    ModelStepNoted,
    Output,
    RunRef,
    StepRef,
    TypedRef,
)
from ..budget import InputEstimate, message_tokens
from ..common import _StepFailed, control_input_pointer
from ..diagnostics import log_model_request, log_model_result, log_model_target
from . import tool as tool_step

# Streaming is an execution decision, not model data.
_MODEL_STREAMING = True


if TYPE_CHECKING:
    from ..frame import _AgicFrame
    from ..runs.agic import _AgicState

_LOGGER = logging.getLogger(__name__)


class _NeedsCompact(Exception):
    def __init__(self, end: RunRef) -> None:
        self.end = end


def _candidate(
    state: _AgicState,
    agent_state: AgentState,
    state_ref: ControlRef,
) -> tuple[
    _AgicFrame, MessageBuffer, tuple[ControlRecord, ...], ModelCall, ModelMessages
]:
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
    history = (
        state.execution.message_history().select(prepared.run.horizon)
        if state.execution is not None and prepared.run.parent is None
        else None
    )
    if state.execution is not None:
        resident = (
            state.model_frame.declarations
            if state.messages.started
            else prepared.declarations
        )
        visible = {item.target: item.revision for item in resident}
        if history is not None and "near" in prepared.recall:
            visible.update(history.recalls)
        if messages.started:
            visible.update(messages.recalls)
        visible.update(
            (control.payload.target, control.payload.revision)
            for control in preceding
            if isinstance(control.payload, RecallControlPayload)
        )
        additions = required_declarations(
            (*prepared.declarations, *prepared.workspaces),
            visible,
        )
        for payload in additions:
            state.execution.recall(RunRef(prepared.run.run_id), payload, visible)
        preceding_refs = {item.ref for item in preceding}
        new_controls = tuple(
            control
            for control in state.execution.runtime_controls(
                prepared.run.run_id, refresh=False
            )
            if control.ref not in preceding_refs
        )
        preceding = (*preceding, *new_controls)
    step = StepRef.from_local(prepared.run.run_id, (state.next_step,))
    inputs = prepared.inputs
    if state.repairing_output and inputs.runnables:
        inputs = replace(inputs, runnables=())
    assembled, recorded = prompting.messages(
        inputs,
        messages,
        step=step,
        controls=preceding,
        history=history,
        recall=prepared.recall,
        reset=(
            prepared.run.horizon != state.model_frame.run.horizon
            or prepared.recall != state.model_frame.recall
        ),
    )
    request = ModelCall(
        instructions=prepared.instructions,
        messages=assembled,
        tools=(
            prompting.tools(prepared.tools)
            if prepared.model.tool_call and not state.repairing_output
            else ()
        ),
        output_schema=deepcopy(state.output_binding.output_schema),
        continuation=(
            state.continuation
            # Stateful adapters validate the actual prefix before reusing a cursor.
            if prepared.model == state.model_frame.model
            and prepared.reasoning == state.model_frame.reasoning
            else None
        ),
        max_output_tokens=prepared.output_budget,
        reasoning=prepared.reasoning,
    )
    return (
        prepared,
        messages,
        preceding,
        request,
        recorded,
    )


def _estimate_binding(prepared: _AgicFrame) -> object:
    return (
        prepared.model,
        prepared.reasoning,
        prepared.run.state.revision,
        prepared.run.horizon,
        prepared.recall,
    )


def _boundary(
    state: _AgicState,
    prepared: _AgicFrame,
    request: ModelCall,
    controls: Sequence[ControlRecord],
) -> RunRef | None:
    budget = prepared.input_budget
    if (
        budget is None
        or state.estimate.count(
            request, _estimate_binding(prepared), prepared.input_overhead
        )
        <= budget
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
    history = execution.message_history().select(prepared.run.horizon)
    roots = history.roots
    if len(roots) < 2:
        raise ToolangError(
            "model input exceeds its budget; fixed content, now, or required near cannot be compacted"
        )
    # Re-render the smallest retained history view. Child requests have no
    # automatic history prefix to slice away; templates may embed history in
    # messages, context, or instructions instead.
    inputs = replace(
        prepared.inputs,
        facts={
            **prepared.inputs.facts,
            **history_variables("", roots[-1][1], prepared.recall),
        },
        runnables=() if state.repairing_output else prepared.inputs.runnables,
    )
    instructions, _declarations = prompting.instructions(inputs)
    messages, _recorded = prompting.messages(
        inputs,
        state.messages.copy(),
        step=StepRef.from_local(prepared.run.run_id, (state.next_step,)),
        controls=controls,
    )
    required = replace(
        request,
        instructions=instructions,
        messages=[*roots[-1][1], *messages]
        if prepared.run.parent is None
        else messages,
    )
    # This lower bound excludes the summary.
    if InputEstimate().count(required, None, prepared.input_overhead) > budget:
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
    prepared, _messages, controls, request, _recorded = _candidate(
        state, *state.execution.state_snapshot()
    )
    return _boundary(state, prepared, request, controls)


@dataclass(slots=True)
class _ModelStream:
    step: int
    started_parts: set[int] = field(default_factory=set)
    ended_parts: set[int] = field(default_factory=set)
    part_types: dict[int, PartType] = field(default_factory=dict)
    chunks: dict[int, list[str]] = field(default_factory=dict)
    tool_ids: dict[int, str] = field(default_factory=dict)
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
        agent_state: AgentState,
        state_ref: ControlRef,
    ) -> StepBegin:
        nonlocal prepared, request, next_messages
        prepared, next_messages, preceding, request, recorded = _candidate(
            state, agent_state, state_ref
        )
        canceling = state.execution is not None and bool(
            state.execution.pending_controls(run.run_id, "cancel")
        )
        if interruption is None and not canceling:
            boundary = _boundary(state, prepared, request, preceding)
            if boundary is not None:
                raise _NeedsCompact(boundary)
        return StepBegin(
            step=StepRef.from_local(run.run_id, (step_index,)),
            kind="model",
            state=state_ref,
            input=(
                *_step_input(state),
                *(control_input_pointer(item, "_") for item in state.claimed_inputs),
            ),
            preceded_by=tuple(item.ref for item in preceding),
            started_at=utc_now(),
            given=ModelStepGiven(
                model=prepared.model.ref,
                setup=prepared.run.setup.revision,
                call=request,
                messages=recorded,
            ),
        )

    def adopt_begin() -> None:
        state.prepared = prepared
        state.model_frame = prepared
        state.continuation = request.continuation if request is not None else None
        state.messages = next_messages
        state.visible_recalls = {
            item.target: item.revision for item in prepared.declarations
        }
        if (
            state.execution is not None
            and prepared.run.parent is None
            and "near" in prepared.recall
        ):
            state.visible_recalls.update(
                state.execution.message_history().select(prepared.run.horizon).recalls
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
        if _MODEL_STREAMING:
            current = await prepared.adapter.stream(
                prepared.model,
                request,
                environ=prepared.environ,
                on_event=lambda event: _handle_event(state, stream, event),
            )
        else:
            current = await prepared.adapter.invoke(
                prepared.model,
                request,
                environ=prepared.environ,
            )
        _validate_stream_result(stream, current)
        output = await _emit_response_parts(state, stream, current)
        if prepared.input_budget is not None:
            state.estimate.observe(
                request,
                _estimate_binding(prepared),
                current.usage.input_tokens if current.usage else None,
                prepared.input_overhead,
            )
    except asyncio.CancelledError:
        await _end_incomplete(state, stream)
        raise
    except ModelResponseError as exc:
        # The adapter may have received more text than it emitted as deltas.
        text_indices = [
            index for index, kind in stream.part_types.items() if kind == "text"
        ]
        previous = "".join(
            part.text
            if isinstance(part := stream.completed_parts.get(index), TextPart)
            else "".join(stream.chunks.get(index, ()))
            for index in text_indices
        )
        if exc.partial_text and exc.partial_text.startswith(previous):
            suffix = exc.partial_text[len(previous) :]
            if suffix:
                if (
                    len(text_indices) == 1
                    and text_indices[0] not in stream.completed_parts
                ):
                    stream.completed_parts[text_indices[0]] = TextPart(exc.partial_text)
                else:
                    index = max(stream.part_types, default=-1) + 1
                    stream.completed_parts[index] = TextPart(suffix)
        await _end_incomplete(
            state, stream, error=ErrorMessage(str(exc)), response_error=exc
        )
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
    local = Local.typed("Part[]", output)
    if output:
        state.messages.append_ref(
            "assistant",
            FieldRef.from_path(
                StepRef.from_local(run.run_id, (step_index,)),
                "output",
                "local",
                "value",
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
                output=Output(local, "_") if local is not None else None,
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
    if not isinstance(event, ModelPartStart | ModelPartDelta | ModelPartEnd):
        return
    kind = (
        event.kind
        if isinstance(event, ModelPartStart)
        else event.delta.kind
        if isinstance(event, ModelPartDelta)
        else event.data.type
    )
    await _emit_part_begin(state, stream, part_index=event.part, kind=kind)
    if isinstance(event, ModelPartStart):
        return
    if event.part in stream.completed_parts:
        raise ValueError("model update targets a completed Part")
    if isinstance(event, ModelPartDelta):
        if isinstance(event.delta, ToolCallDelta):
            previous = stream.tool_ids.setdefault(event.part, event.delta.tool_call_id)
            if previous != event.delta.tool_call_id:
                raise ValueError("model delta changed a Part's tool call ID")
        stream.chunks.setdefault(event.part, []).append(event.delta.text)
        if event.delta.text:
            await state.emit(
                PartDelta(
                    step=StepRef.from_local(state.prepared.run.run_id, (stream.step,)),
                    part=event.part,
                    delta=event.delta,
                )
            )
    else:
        _validate_part(stream, event.part, event.data, source="ModelPartEnd")
        stream.completed_parts[event.part] = event.data


def _validate_stream_result(stream: _ModelStream, current: ModelCallResult) -> None:
    parts = dict(_output_parts(stream, current=current, tool_calls=current.tool_calls))
    for index in stream.started_parts:
        if index not in parts:
            raise ValueError("ModelCallResult omitted an observed Part")
        _validate_part(stream, index, parts[index], source="ModelCallResult")
        if (
            index in stream.completed_parts
            and stream.completed_parts[index] != parts[index]
        ):
            raise ValueError(
                f"ModelCallResult {parts[index].type} does not match authoritative ModelPartEnd"
            )


def _validate_part(
    stream: _ModelStream, index: int, part: Part, *, source: str
) -> None:
    if index in stream.part_types and stream.part_types[index] != part.type:
        raise ValueError(f"{source} changed a Part's type")
    if isinstance(part, TextPart | ReasoningPart):
        if not part.text.startswith("".join(stream.chunks.get(index, ()))):
            delta = "ReasoningDelta" if isinstance(part, ReasoningPart) else "TextDelta"
            raise ValueError(
                f"{source} {part.type} does not extend streamed {delta} content"
            )
    if isinstance(part, ToolCallPart) and index in stream.tool_ids:
        if stream.tool_ids[index] != part.tool_call_id:
            raise ValueError(f"{source} changed a Part's tool call ID")


def _output_parts(
    stream: _ModelStream,
    *,
    current: ModelCallResult,
    tool_calls: Sequence[ToolCall],
) -> list[tuple[int, Part]]:
    del stream
    message = current.message
    parts = (
        list(message.parts)
        if message is not None and message.role == "assistant"
        else []
    )
    seen = {part.tool_call_id for part in parts if isinstance(part, ToolCallPart)}
    parts.extend(
        ToolCallPart(
            tool_call_id=call.tool_call_id,
            call_id=call.call_id,
            tool_name=call.name,
            tool_family=call.name,
            input=dict(call.input),
        )
        for call in tool_calls
        if call.tool_call_id not in seen
    )
    return list(enumerate(parts))


def _step_input(state: _AgicState) -> tuple[FieldRef, ...]:
    if state.next_model_inputs is not None:
        return state.next_model_inputs
    if state.last_step is None:
        return state.initial_inputs
    return (
        FieldRef.from_path(
            StepRef.from_local(state.prepared.run.run_id, (state.last_step,)),
            "output",
            "local",
            "value",
        ),
    )


async def _emit_part_begin(
    state: _AgicState,
    stream: _ModelStream,
    *,
    part_index: int,
    kind: PartType,
) -> None:
    if type(part_index) is not int or part_index < 0:
        raise ValueError("model Part ordinal must be a non-negative integer")
    if part_index in stream.started_parts:
        if stream.part_types[part_index] != kind:
            raise ValueError("model update changed a Part's type")
        return
    if part_index != len(stream.started_parts):
        raise ValueError("model Part ordinals must follow first observation order")
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
    if part_index in stream.ended_parts:
        return
    if part_index in stream.chunks and isinstance(part, TextPart | ReasoningPart):
        prefix = "".join(stream.chunks[part_index])
        suffix = part.text[len(prefix) :]
        if suffix:
            stream.chunks[part_index].append(suffix)
            await state.emit(
                PartDelta(
                    step=StepRef.from_local(state.prepared.run.run_id, (stream.step,)),
                    part=part_index,
                    delta=ReasoningDelta(suffix)
                    if isinstance(part, ReasoningPart)
                    else TextDelta(suffix),
                )
            )
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
    state: _AgicState,
    stream: _ModelStream,
    *,
    error: ErrorMessage | None = None,
    response_error: ModelResponseError | None = None,
) -> None:
    """Retain execution facts even when interrupted while closing display events."""

    parts = {
        index: stream.completed_parts.get(index) or _partial_part(stream, index)
        for index in sorted(stream.started_parts | stream.completed_parts.keys())
    }
    # Every observed ordinal stays in durable output, including incomplete tools.
    # Only completed tools and readable prefixes may enter subsequent context.
    output = tuple(parts.values())
    retained = {
        index: part
        for index, part in parts.items()
        if (
            index in stream.completed_parts
            or isinstance(part, TextPart | ReasoningPart)
        )
        and (response_error is None or isinstance(part, TextPart | ReasoningPart))
    }
    step = StepRef.from_local(state.prepared.run.run_id, (stream.step,))
    local = Local.typed("Part[]", output) if output else None
    calls = tuple(
        ToolCall(
            part.tool_call_id,
            part.call_id or part.tool_call_id,
            part.tool_name,
            part.input,
        )
        for part in retained.values()
        if isinstance(part, ToolCallPart)
    )
    accounting = state.account_usage(response_error.usage) if response_error else None
    if retained and response_error is None:
        refs = tuple(
            TypedRef(
                FieldRef.from_path(step, "output", "local", "value", index), "Part"
            )
            for index in retained
        )
        values: dict[object, Part] = dict(zip(refs, retained.values()))
        state.messages.append_template(
            MessageTemplate("assistant", refs),
            lambda ref: values[ref],
        )
        state.last_step = stream.step
        for index, part in retained.items():
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
                    output=Output(local, "_") if local is not None else None,
                    error=error,
                    noted=_model_step_noted(accounting, continuation=None)
                    if response_error is not None
                    else None,
                    finished_at=utc_now(),
                )
            )
        finally:
            if response_error is not None:
                state.record_accounting(accounting)
            if error is None:
                await tool_step.skip(state, calls, canceled=not state.immediate_steer())


def _partial_part(stream: _ModelStream, part_index: int) -> Part:
    part_type = stream.part_types[part_index]
    if part_type in {"text", "reasoning"}:
        cls = ReasoningPart if part_type == "reasoning" else TextPart
        return cls(text="".join(stream.chunks.get(part_index, ())))
    if part_type == "tool_call":
        tool_call_id = stream.tool_ids.get(part_index, f"tool-call-{part_index}")
        raw_input = "".join(stream.chunks.get(part_index, ()))
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
    accounting: ModelAccounting | None,
    *,
    continuation: ModelContinuation | None,
) -> ModelStepNoted:
    return ModelStepNoted(
        accounting=accounting,
        continuation=(dict(continuation) if continuation is not None else None),
    )
