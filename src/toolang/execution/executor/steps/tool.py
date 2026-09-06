"""Tool-call steps and result part events."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import logging
from collections.abc import Callable, Mapping
import re
import time
from typing import TYPE_CHECKING

from toolang.base.protocols.tool import AgentTool
from toolang.base.types.message import ToolResultPart
from toolang.base.types.run import ToolCall, ToolCallResult
from toolang.base.types.tool import ToolContext, ToolService
from toolang.base.errors import ToolFailure
from toolang.common.errors import ToolangError
from toolang.common.layout import AgentLayout
from toolang.common.template import render_text_template
from toolang.common.time import elapsed_ms, utc_now
from toolang.state.state import AgentState, StatePublication

from ...events import PartBegin, PartEnd, StepBegin, StepEnd
from ...types import (
    ControlRef,
    ErrorMessage,
    ErrorRef,
    FieldRef,
    Local,
    StepRef,
    ToolStepGiven,
    ToolStepNoted,
)
from ..common import _StepFailed
from ..diagnostics import log_tool_call_input, log_tool_call_output

if TYPE_CHECKING:
    from ..runs.agic import _AgicState

_LOGGER = logging.getLogger(__name__)
_MAX_ARGUMENT_PREVIEW_CHARS = 80
_DEFAULT_TOOL_SUMMARY_TEMPLATES = {
    "running": "Executing {{name}} {{args.0}} ...",
    "succeeded": "Executed {{name}} {{args.0}}",
    "failed": "Failed {{name}} {{args.0}}",
    "canceled": "Canceled {{name}} {{args.0}}",
}
_SENSITIVE_ARGUMENT_MARKERS = (
    "apikey",
    "authorization",
    "credential",
    "passwd",
    "password",
    "privatekey",
    "secret",
    "token",
)


async def begin(
    state: _AgicState,
    step: StepRef,
    call: ToolCall,
    build: Callable[[AgentState | StatePublication, ControlRef], StepBegin],
) -> None:
    """Establish the Tool Step boundary even when steer interrupts its lock wait."""

    interruption: asyncio.CancelledError | None = None
    while True:
        try:
            await state.start_step(build)
        except asyncio.CancelledError as exc:
            interruption = exc
            if state.execution is None:
                raise
            record = state.execution.store.get_step(ref=step)
            if record is None and state.immediate_steer():
                # Cancellation while waiting for the boundary lock has no Step
                # yet. Establish it before recording the skipped response.
                continue
            # A boundary cancel may already have ended it in the executor.
            if record is None or record.status != "running":
                raise
        else:
            if interruption is None:
                return
        await cancel(state, step, call)
        raise interruption


@dataclass(frozen=True, slots=True)
class _ToolSummaryContext:
    family: str
    name: str
    args: tuple[str, ...]


async def execute(state: _AgicState, call: ToolCall) -> ToolCallResult:
    """Perform one tool call and emit its complete step event stream."""

    run = state.prepared.run
    state.before_tool_call()
    step_index = state.next_step
    state.next_step += 1
    step_started = time.perf_counter()
    started_at = utc_now()
    source = state.tool_call_sources.get(call.tool_call_id)
    step_input: tuple[FieldRef, ...]
    if source is not None:
        step_input = (
            FieldRef.from_path(
                StepRef.from_local(run.run_id, (source[0],)),
                "output",
                "value",
                source[1],
            ),
        )
    elif state.last_step is not None:
        step_input = (
            FieldRef.from_path(
                StepRef.from_local(run.run_id, (state.last_step,)),
                "output",
                "value",
            ),
        )
    else:
        step_input = state.initial_inputs
    _LOGGER.info(
        "Step started thread=%s run=%s step=%s kind=tool tool=%s",
        run.thread,
        run.run_id,
        step_index,
        call.name,
    )
    prepared = state.prepared
    plugin_name = "-"
    summary_context = _tool_summary_context(call, None)

    def begin_step(
        agent_state: AgentState | StatePublication,
        state_ref: ControlRef,
    ) -> StepBegin:
        nonlocal prepared, plugin_name, summary_context
        prepared = state.frame_for_step(agent_state, state_ref)
        tool = prepared.tools.get(call.name)
        plugin_name = _plugin_name(tool)
        summary_context = _tool_summary_context(call, tool)
        return StepBegin(
            step=StepRef.from_local(run.run_id, (step_index,)),
            kind="tool",
            state=state_ref,
            input=step_input,
            given=ToolStepGiven(
                plugin=plugin_name,
                call=call,
                summary=_tool_summary(summary_context, "running"),
            ),
            started_at=started_at,
        )

    await begin(state, StepRef.from_local(run.run_id, (step_index,)), call, begin_step)
    state.prepared = prepared
    log_tool_call_input(
        call,
        thread_id=run.thread,
        run_id=run.run_id,
        step_index=step_index,
        plugin_name=plugin_name,
    )
    try:
        record = await invoke_tool_call(
            run_id=run.run_id,
            tools=prepared.tools,
            services=prepared.services,
            layout=state.layout,
            call=call,
        )
    except asyncio.CancelledError:
        await cancel(
            state,
            StepRef.from_local(run.run_id, (step_index,)),
            call,
            summary=_tool_summary(summary_context, "canceled"),
        )
        raise
    except Exception as exc:
        error = str(exc) or type(exc).__name__
        await state.end_step(
            StepEnd(
                step=StepRef.from_local(run.run_id, (step_index,)),
                kind="tool",
                status="failed",
                noted=ToolStepNoted(summary=_tool_summary(summary_context, "failed")),
                error=ErrorMessage(error),
                finished_at=utc_now(),
            ),
            canceled_noted=ToolStepNoted(summary="canceled"),
        )
        _LOGGER.error(
            "Step failed thread=%s run=%s step=%s kind=tool tool=%s error=%r duration_ms=%s",
            run.thread,
            run.run_id,
            step_index,
            call.name,
            error,
            elapsed_ms(step_started),
        )
        raise _StepFailed(StepRef.from_local(run.run_id, (step_index,)), exc) from exc
    part = ToolResultPart(
        tool_call_id=record.tool_call_id,
        call_id=record.call_id,
        tool_name=record.name,
        tool_family=record.name,
        output=dict(record.output),
        error=record.error,
    )
    status = "failed" if record.error is not None else "succeeded"
    log_tool_call_output(
        record,
        thread_id=run.thread,
        run_id=run.run_id,
        step_index=step_index,
        plugin_name=plugin_name,
    )
    await finish(
        state,
        StepRef.from_local(run.run_id, (step_index,)),
        part,
        summary=_tool_summary(summary_context, status),
        canceled_summary=_tool_summary(summary_context, "canceled"),
    )
    _LOGGER.info(
        "Step finished thread=%s run=%s step=%s kind=tool tool=%s status=%s duration_ms=%s",
        run.thread,
        run.run_id,
        step_index,
        call.name,
        status,
        elapsed_ms(step_started),
    )
    return record


async def finish(
    state: _AgicState,
    step: StepRef,
    part: ToolResultPart,
    *,
    summary: str | None = None,
    canceled_summary: str = "canceled",
    error: ErrorMessage | ErrorRef | None = None,
) -> None:
    """Persist a tool result even if its delivery is interrupted."""

    output = Local.typed("ToolResultPart", part, None, 0)
    # The result already exists, even if an interrupt prevents its delivery.
    state.messages.append_ref(
        "tool", FieldRef.from_path(step, "output", "value"), output
    )
    state.last_step = step.index
    end = PartEnd(step=step, part=0, data=part)
    ended = False
    try:
        await state.emit(PartBegin(step=step, part=0, part_type=part.type))
        ended = True
        await state.emit(end)
    except asyncio.CancelledError:
        if not ended:
            await state.emit(end)
        await state.end_step(
            StepEnd(
                step=step,
                kind="tool",
                status="canceled",
                output=output,
                noted=ToolStepNoted(summary=canceled_summary),
                finished_at=utc_now(),
            ),
        )
        raise
    await state.end_step(
        StepEnd(
            step=step,
            kind="tool",
            status="failed" if part.error is not None else "succeeded",
            output=output,
            noted=ToolStepNoted(
                summary=summary if summary is not None else part.tool_name
            ),
            error=error
            or (ErrorMessage(part.error) if part.error is not None else None),
            finished_at=utc_now(),
        ),
        canceled_noted=ToolStepNoted(summary=canceled_summary),
    )


async def cancel(
    state: _AgicState,
    step: StepRef,
    call: ToolCall,
    *,
    summary: str = "canceled",
    part: ToolResultPart | None = None,
    aborted_by: ControlRef | None = None,
) -> None:
    """End a started call; only steer recovery needs a cancellation result."""

    if part is None and state.immediate_steer():
        part = canceled_result(call)
    if part is not None:
        state.messages.append_ref(
            "tool",
            FieldRef.from_path(step, "output", "value"),
            Local.typed("ToolResultPart", part, None, 0),
        )
        state.last_step = step.index
    await state.end_step(
        StepEnd(
            step=step,
            kind="tool",
            status="canceled",
            output=Local.typed("ToolResultPart", part, None, 0)
            if part is not None
            else None,
            noted=ToolStepNoted(summary=summary),
            aborted_by=aborted_by,
            finished_at=utc_now(),
        ),
    )


def canceled_result(call: ToolCall) -> ToolResultPart:
    """Build the model-facing result for a call interrupted or skipped by steer."""

    return ToolResultPart(
        tool_call_id=call.tool_call_id,
        call_id=call.call_id,
        tool_name=call.name,
        tool_family=call.name,
        error="canceled by steer",
    )


async def skip(state: _AgicState, calls: tuple[ToolCall, ...]) -> None:
    """Record steer-skipped calls without invoking handlers or reserving budget."""

    message_start = len(state.messages.pending)
    inputs: list[FieldRef] = []
    control = (
        next(
            (
                item.ref
                for item in state.execution.pending_controls(
                    state.prepared.run.run_id, "steer"
                )
                if item.timing in {"immediate", "next_step"}
            ),
            None,
        )
        if state.execution is not None
        else None
    )
    for call in calls:
        step = StepRef.from_local(state.prepared.run.run_id, (state.next_step,))
        state.next_step += 1
        source = state.tool_call_sources[call.tool_call_id]
        try:
            await begin(
                state,
                step,
                call,
                lambda _state, ref: StepBegin(
                    step=step,
                    kind="tool",
                    state=ref,
                    input=(
                        FieldRef.from_path(
                            StepRef.from_local(step.run_id, (source[0],)),
                            "output",
                            "value",
                            source[1],
                        ),
                    ),
                    given=ToolStepGiven(
                        plugin="_too"
                        if call.name.startswith("_too__")
                        else _plugin_name(state.prepared.tools.get(call.name)),
                        call=call,
                    ),
                    started_at=utc_now(),
                ),
            )
            await cancel(
                state,
                step,
                call,
                part=canceled_result(call),
                aborted_by=control,
            )
        except asyncio.CancelledError:
            if not state.immediate_steer():
                raise
        inputs.append(FieldRef.from_path(step, "output", "value"))
        state.last_step = step.index
    if inputs:
        state.next_model_inputs = tuple(inputs)
        # Include begin-interruption results, preserving the batch's message shape.
        state.messages.group_tools(message_start)


def _plugin_name(tool: AgentTool | None) -> str:
    plugin_name = getattr(tool, "plugin_name", None)
    if isinstance(plugin_name, str) and plugin_name:
        return plugin_name
    return "-"


def _tool_summary_context(
    call: ToolCall,
    tool: AgentTool | None,
) -> _ToolSummaryContext:
    family, name = _tool_identity(call, tool)
    return _ToolSummaryContext(
        family=family,
        name=name,
        args=_argument_previews(call, tool),
    )


def _tool_summary(context: _ToolSummaryContext, status: str) -> str:
    template = _DEFAULT_TOOL_SUMMARY_TEMPLATES.get(status, "{{name}} {{args.0}}")
    rendered = render_text_template(
        template,
        {
            "family": context.family,
            "name": context.name,
            "args": context.args,
        },
    )
    return " ".join(rendered.split())


def _tool_identity(
    call: ToolCall,
    tool: AgentTool | None,
) -> tuple[str, str]:
    ref = getattr(tool, "ref", None)
    family = getattr(ref, "toolset", None)
    name = getattr(ref, "name", None)
    if isinstance(family, str) and family and isinstance(name, str) and name:
        return family, name

    family, separator, name = call.name.partition("__")
    if separator and family and name:
        return family, name

    fallback_family = getattr(tool, "toolset", None) or getattr(tool, "plugin_name", "")
    return (
        fallback_family if isinstance(fallback_family, str) else "",
        call.name or "tool",
    )


def _argument_previews(
    call: ToolCall,
    tool: AgentTool | None,
) -> tuple[str, ...]:
    if tool is None:
        return ()
    try:
        properties = tool.definition().parameters.get("properties")
    except Exception:
        return ()
    if not isinstance(properties, Mapping):
        return ()
    previews: list[str] = []
    for raw_name, raw_schema in properties.items():
        if not isinstance(raw_name, str) or raw_name not in call.input:
            continue
        schema = raw_schema if isinstance(raw_schema, Mapping) else {}
        if _is_sensitive_argument(raw_name, schema):
            previews.append("<redacted>")
        else:
            previews.append(_format_argument_preview(call.input[raw_name]))
    return tuple(previews)


def _is_sensitive_argument(name: str, schema: Mapping[object, object]) -> bool:
    compact_name = re.sub(r"[^a-z0-9]", "", name.lower())
    schema_format = schema.get("format")
    return (
        schema.get("writeOnly") is True
        or (
            isinstance(schema_format, str)
            and schema_format.lower() in {"password", "secret"}
        )
        or any(marker in compact_name for marker in _SENSITIVE_ARGUMENT_MARKERS)
    )


def _format_argument_preview(value: object) -> str:
    if isinstance(value, str):
        compact = " ".join(value.split())
        if not compact or any(char.isspace() for char in compact):
            return f"“{_truncate_argument(compact, _MAX_ARGUMENT_PREVIEW_CHARS - 2)}”"
        return _truncate_argument(compact, _MAX_ARGUMENT_PREVIEW_CHARS)
    try:
        compact = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError):
        compact = " ".join(str(value).split())
    return _truncate_argument(compact, _MAX_ARGUMENT_PREVIEW_CHARS)


def _truncate_argument(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return f"{value[: limit - 1].rstrip()}…"


async def invoke_tool_call(
    *,
    run_id: str,
    tools: Mapping[str, AgentTool],
    services: tuple[ToolService, ...],
    layout: AgentLayout,
    call: ToolCall,
) -> ToolCallResult:
    """Invoke one selected tool and normalize its result or error."""

    name = call.name
    arguments = dict(call.input)
    try:
        if name.startswith("_too__"):
            raise ToolangError(
                f"inner runtime tool cannot use generic tool dispatch: {name}"
            )
        tool = tools.get(name)
        if tool is None:
            raise ToolangError(f"unknown tool call: {name or '<empty>'}")
        output = await tool.invoke(
            arguments,
            _tool_context(
                run_id=run_id,
                layout=layout,
                tool_name=name,
                tools=tools,
                services=services,
            ),
        )
        error = None
    except Exception as exc:
        output = dict(exc.output) if isinstance(exc, ToolFailure) else {}
        error = str(exc) or type(exc).__name__
    return ToolCallResult(
        tool_call_id=call.tool_call_id,
        call_id=call.call_id,
        name=name,
        input=arguments,
        output=output,
        error=error,
    )


def _tool_context(
    *,
    run_id: str,
    layout: AgentLayout,
    tool_name: str,
    tools: Mapping[str, AgentTool],
    services: tuple[ToolService, ...],
) -> ToolContext:
    tool = tools.get(tool_name)
    plugin_name = getattr(tool, "plugin_name", None)
    if not isinstance(plugin_name, str) or not plugin_name:
        raise ToolangError(f"unknown toolset plugin for tool: {tool_name}")
    return ToolContext(
        run_id=run_id,
        home=layout.home,
        room=layout.tool_room(plugin_name),
        wd=layout.home,
        services=services,
        placement=layout.placement,
    )
