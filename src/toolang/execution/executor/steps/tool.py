"""Tool-call steps and result part events."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
import json
import logging
from collections.abc import Callable, Mapping
from pathlib import Path
import re
import time
from typing import TYPE_CHECKING, Literal

from toolang.base.protocols.tool import Tool, ToolHistory, ToolRuntime
from toolang.base.types.message import Message, ToolResultPart
from toolang.base.types.run import ToolCall, ToolCallResult
from toolang.base.types.tool import (
    ToolContext,
    ToolResult,
    ToolService,
    RuntimeToolContext,
    HistoryToolContext,
    ServiceToolContext,
)
from toolang.common.errors import ToolangError
from toolang.common.layout import AgentLayout
from toolang.common.template import render_text_template
from toolang.common.time import elapsed_ms, utc_now
from toolang.state.state import AgentState

from ...events import PartBegin, PartEnd, StepBegin, StepEnd
from ...records import RecallControlPayload
from ...tool_results import workspace_reply, workspace_reply_from_step
from ...runnables import AgicRoutes
from ...tools.agent_state.types import AgentStateToolContext
from ...types import (
    ControlRef,
    ErrorMessage,
    ErrorRef,
    FieldRef,
    Local,
    Output,
    StepRef,
    ToolStepGiven,
    ToolStepNoted,
    StepStatus,
)
from ..common import _StepFailed
from ..tool_runtime import _ToolRuntime
from ..tool_history import _ToolHistory
from ..diagnostics import log_tool_call_input, log_tool_call_output
from ..rules import _HonorRequired, check_rules

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


async def _begin(
    state: _AgicState,
    step: StepRef,
    call: ToolCall,
    build: Callable[[AgentState, ControlRef], StepBegin],
    *,
    trigger: Literal["model", "runtime"] = "model",
    canceled_summary: Callable[[], str] | None = None,
) -> None:
    """Establish the Tool Step boundary even when interrupted during its lock wait."""

    interruption: asyncio.CancelledError | None = None
    while True:
        try:
            await state.start_step(build)
        except asyncio.CancelledError as exc:
            interruption = exc
            if state.execution is None:
                raise
            record = state.execution.store.get_step(ref=step)
            if record is None:
                # Cancellation while waiting for the boundary lock has no Step
                # yet. Establish it before recording the skipped response.
                continue
            # A boundary cancel may already have ended it in the executor.
            if record.status != "running":
                raise
        else:
            if interruption is None:
                return
        await _cancel(
            state,
            step,
            call,
            trigger=trigger,
            summary=canceled_summary() if canceled_summary else "canceled",
        )
        raise interruption


@dataclass(frozen=True, slots=True)
class _ToolSummaryContext:
    family: str
    name: str
    args: tuple[str, ...]
    arguments: Mapping[str, object]
    tool: Tool | None


async def execute(
    state: _AgicState,
    call: ToolCall,
    *,
    trigger: Literal["model", "runtime"] = "model",
    tool_call_count: int = 1,
    routes: AgicRoutes | None = None,
) -> ToolCallResult:
    """Perform one tool call and emit its complete step event stream."""

    if trigger == "model":
        state.before_tool_call()
    try:
        return await _execute(
            state, call, trigger=trigger, tool_call_count=tool_call_count, routes=routes
        )
    except _HonorRequired as required:
        paths = dict.fromkeys(required.paths)
        identity = f"honor_{state.prepared.run.run_id}_{state.next_step}"
        honor = ToolCall(
            tool_call_id=identity,
            call_id=identity,
            name="_toolang__honor",
            input={
                "paths": [{"workspace": name, "path": path} for name, path in paths]
            },
        )
        step = StepRef.from_local(state.prepared.run.run_id, (state.next_step,))
        try:
            result = await _execute(
                state,
                honor,
                trigger="runtime",
                input_ref=_call_source(state, call),
            )
        except asyncio.CancelledError:
            # Delivery can be interrupted after a successful StepEnd. As with
            # end_step recovery, consult the committed outcome only on interruption.
            assert state.execution is not None
            record = state.execution.store.get_step(ref=step)
            assert record is not None
            reply = workspace_reply_from_step(
                record, state.execution.store.resolve_value
            )
            assert reply is not None
            state.messages.append(Message("tool", (reply,)))
            state.last_step = step.index
            raise
        part = workspace_reply(call, error=result.error)
        state.messages.append(Message("tool", (part,)))
        state.last_step = step.index
        return ToolCallResult(
            tool_call_id=call.tool_call_id,
            call_id=call.call_id,
            name=call.name,
            input=dict(call.input),
            output={},
            error=part.error,
        )


def _call_source(state: _AgicState, call: ToolCall) -> FieldRef | None:
    source = state.tool_call_sources.get(call.tool_call_id)
    return (
        FieldRef.from_path(
            StepRef.from_local(state.prepared.run.run_id, (source[0],)),
            "output",
            "local",
            "value",
            source[1],
        )
        if source is not None
        else None
    )


async def _execute(
    state: _AgicState,
    call: ToolCall,
    *,
    trigger: Literal["model", "runtime"] = "model",
    tool_call_count: int = 1,
    routes: AgicRoutes | None = None,
    input_ref: FieldRef | None = None,
) -> ToolCallResult:
    run = state.prepared.run
    step_index = state.next_step
    state.next_step += 1
    step_started = time.perf_counter()
    started_at = utc_now()
    source_ref = input_ref or (
        _call_source(state, call) if trigger == "model" else None
    )
    step_input: tuple[FieldRef, ...]
    if source_ref is not None:
        step_input = (source_ref,)
    elif trigger == "runtime":
        step_input = ()
    elif state.last_step is not None:
        step_input = (
            FieldRef.from_path(
                StepRef.from_local(run.run_id, (state.last_step,)),
                "output",
                "local",
                "value",
            ),
        )
    else:
        step_input = state.initial_inputs
    prepared = state.prepared
    plugin_name = "-"
    summary_context = _tool_summary_context(call, None)
    operation: Tool | Exception | None = None
    context: ToolContext | None = None
    step = StepRef.from_local(run.run_id, (step_index,))
    runtime: _ToolRuntime | None = None

    def begin_step(
        agent_state: AgentState,
        state_ref: ControlRef,
    ) -> StepBegin:
        nonlocal prepared, plugin_name, summary_context
        nonlocal operation, context, runtime
        runtime_tools = prepared.run.setup.tools.runtime if trigger == "runtime" else {}
        # Bind the operation to the Step's State even if reload removed its Agic.
        if (
            _plugin_name(runtime_tools.get(call.name) or prepared.tools.get(call.name))
            != "_toolang"
        ):
            prepared = state.frame_for_step(agent_state, state_ref)
        tools = {**prepared.tools, **runtime_tools} if runtime_tools else prepared.tools
        tool = tools.get(call.name)
        plugin_name = _plugin_name(tool)
        summary_context = _tool_summary_context(call, tool)
        runtime = (
            _ToolRuntime(
                state, step, source_ref, tool_call_count, routes or prepared.routes
            )
            if plugin_name == "_toolang"
            else None
        )
        try:
            if tool is None:
                raise ToolangError(f"unknown tool call: {call.name}")
            context = _tool_context(
                layout=state.layout,
                tool=tool,
                services=prepared.services,
                runtime=runtime,
                history=_ToolHistory(state.execution.store.db_path, run.thread)
                if plugin_name == "history" and state.execution is not None
                else None,
                workspaces={
                    name: Path(path) for name, path in agent_state.workspaces.items()
                },
            )
            tool_paths = tool.paths(call.input, context)
            paths = tuple(
                (workspace, path)
                for workspace, values in (tool_paths or {}).items()
                for path in values
            )
            operation = tool
        except Exception as exc:
            operation = exc
        else:
            if trigger == "model" and state.execution is not None and paths:
                pending = {
                    control.payload.target
                    for control in state.execution.runtime_controls(run.run_id)
                    if isinstance(control.payload, RecallControlPayload)
                }
                check_rules(context, paths, state.visible_recalls, pending)
        return StepBegin(
            step=StepRef.from_local(run.run_id, (step_index,)),
            kind="tool",
            state=state_ref,
            input=step_input,
            given=ToolStepGiven(
                plugin=plugin_name,
                call=call,
                summary=_tool_summary(summary_context, "running"),
                trigger=trigger,
            ),
            started_at=started_at,
        )

    try:
        await _begin(
            state,
            step,
            call,
            begin_step,
            trigger=trigger,
            canceled_summary=lambda: _tool_summary(summary_context, "canceled"),
        )
    except _HonorRequired:
        # Preflight runs before begin is persisted; honor takes this next slot.
        state.next_step = step_index
        raise
    state.prepared = prepared
    _LOGGER.info(
        "Step started thread=%s run=%s step=%s kind=tool tool=%s",
        run.thread,
        run.run_id,
        step_index,
        call.name,
    )
    log_tool_call_input(
        call,
        thread_id=run.thread,
        run_id=run.run_id,
        step_index=step_index,
        plugin_name=plugin_name,
    )
    try:
        assert operation is not None
        record = await invoke_tool_call(call=call, tool=operation, context=context)
    except asyncio.CancelledError:
        await _cancel(
            state,
            StepRef.from_local(run.run_id, (step_index,)),
            call,
            summary=_tool_summary(summary_context, "canceled"),
            trigger=trigger,
        )
        raise
    except Exception as exc:
        error = str(exc) or type(exc).__name__
        await state.end_step(
            StepEnd(
                step=StepRef.from_local(run.run_id, (step_index,)),
                kind="tool",
                status="failed",
                noted=ToolStepNoted(
                    summary=_tool_summary(
                        summary_context, "failed", ToolResult(error=error)
                    )
                ),
                error=ErrorMessage(error),
                finished_at=utc_now(),
            ),
            canceled_noted=ToolStepNoted(
                summary=_tool_summary(summary_context, "canceled")
            ),
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
    try:
        await _finish(
            state,
            step,
            part,
            summary=_tool_summary(
                summary_context, status, ToolResult(record.output, record.error)
            ),
            canceled_summary=_tool_summary(summary_context, "canceled"),
            error=runtime.error if runtime is not None else None,
            trigger=trigger,
        )
    except asyncio.CancelledError:
        if runtime is None or runtime.transfer is None or not state.immediate_steer():
            raise
        # Delivery cannot undo an already committed execute.
    _LOGGER.info(
        "Step finished thread=%s run=%s step=%s kind=tool tool=%s status=%s duration_ms=%s",
        run.thread,
        run.run_id,
        step_index,
        call.name,
        status,
        elapsed_ms(step_started),
    )
    if runtime is not None and runtime.transfer is not None:
        raise runtime.transfer
    if runtime is not None and runtime.failure is not None:
        raise _StepFailed(step, runtime.failure) from runtime.failure
    return record


async def _finish(
    state: _AgicState,
    step: StepRef,
    part: ToolResultPart,
    *,
    summary: str | None = None,
    canceled_summary: str = "canceled",
    error: ErrorMessage | ErrorRef | None = None,
    trigger: Literal["model", "runtime"] = "model",
) -> None:
    """Persist a tool result even if its delivery is interrupted."""

    output = Local.typed("ToolResultPart", part)
    # The result already exists, even if an interrupt prevents its delivery.
    if trigger == "model":
        state.messages.append_ref(
            "tool", FieldRef.from_path(step, "output", "local", "value"), output
        )
        state.last_step = step.index
    end = PartEnd(step=step, part=0, data=part)
    ended = False
    status: StepStatus = "failed" if part.error is not None else "succeeded"
    noted = ToolStepNoted(summary=summary if summary is not None else part.tool_name)
    error = error or (ErrorMessage(part.error) if part.error is not None else None)
    try:
        await state.emit(PartBegin(step=step, part=0, part_type=part.type))
        ended = True
        await state.emit(end)
    except asyncio.CancelledError:
        status = "canceled"
        noted = ToolStepNoted(summary=canceled_summary)
        error = None
        if not ended:
            await state.emit(end)
        raise
    finally:
        await state.end_step(
            StepEnd(
                step=step,
                kind="tool",
                status=status,
                output=Output(output),
                noted=noted,
                error=error,
                finished_at=utc_now(),
            ),
            canceled_noted=ToolStepNoted(summary=canceled_summary),
        )


async def _cancel(
    state: _AgicState,
    step: StepRef,
    call: ToolCall,
    *,
    summary: str = "canceled",
    part: ToolResultPart | None = None,
    aborted_by: ControlRef | None = None,
    trigger: Literal["model", "runtime"] = "model",
) -> None:
    """End a started call with a durable result, including ordinary cancellation."""

    if part is None:
        part = canceled_result(
            call, reason="canceled by steer" if state.immediate_steer() else "canceled"
        )
    if trigger == "model":
        state.messages.append_ref(
            "tool",
            FieldRef.from_path(step, "output", "local", "value"),
            Local.typed("ToolResultPart", part),
        )
        state.last_step = step.index
    await state.end_step(
        StepEnd(
            step=step,
            kind="tool",
            status="canceled",
            output=Output(Local.typed("ToolResultPart", part)),
            noted=ToolStepNoted(summary=summary),
            aborted_by=aborted_by,
            finished_at=utc_now(),
        ),
    )


def canceled_result(
    call: ToolCall, *, reason: str = "canceled by steer"
) -> ToolResultPart:
    """Build the model-facing result for an interrupted or unexecuted call."""

    return ToolResultPart(
        tool_call_id=call.tool_call_id,
        call_id=call.call_id,
        tool_name=call.name,
        tool_family=call.name,
        error=reason,
    )


async def skip(
    state: _AgicState, calls: tuple[ToolCall, ...], *, canceled: bool = False
) -> None:
    """Close an interrupted batch without invoking handlers or reserving budget."""

    message_start = len(state.messages.pending)
    inputs: list[FieldRef] = []
    interruption: asyncio.CancelledError | None = None
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
        if state.execution is not None and not canceled
        else None
    )
    for call in calls:
        step = StepRef.from_local(state.prepared.run.run_id, (state.next_step,))
        state.next_step += 1
        source = state.tool_call_sources[call.tool_call_id]
        tool = state.prepared.tools.get(call.name)
        summary = _tool_summary_context(call, tool)
        try:
            await _begin(
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
                            "local",
                            "value",
                            source[1],
                        ),
                    ),
                    given=ToolStepGiven(
                        plugin=_plugin_name(tool),
                        call=call,
                        summary=_tool_summary(summary, "running"),
                    ),
                    started_at=utc_now(),
                ),
                canceled_summary=lambda: _tool_summary(summary, "canceled"),
            )
            await _cancel(
                state,
                step,
                call,
                summary=_tool_summary(summary, "canceled"),
                part=canceled_result(
                    call,
                    reason="canceled; operation not executed"
                    if canceled
                    else "canceled by steer",
                ),
                aborted_by=None if canceled else control,
            )
        except asyncio.CancelledError as exc:
            if not canceled and not state.immediate_steer():
                # A cancel can replace a pending steer while its skipped batch
                # is being recorded. Close the remaining calls before exiting.
                interruption = exc
                canceled = True
        inputs.append(FieldRef.from_path(step, "output", "local", "value"))
        state.last_step = step.index
    if inputs:
        state.next_model_inputs = tuple(inputs)
        # Include begin-interruption results, preserving the batch's message shape.
        state.messages.group_tools(message_start)
    if interruption is not None:
        raise interruption


def _plugin_name(tool: Tool | None) -> str:
    plugin_name = getattr(tool, "plugin_name", None)
    if isinstance(plugin_name, str) and plugin_name:
        return plugin_name
    return "-"


def _tool_summary_context(
    call: ToolCall,
    tool: Tool | None,
) -> _ToolSummaryContext:
    family, name = _tool_identity(call, tool)
    try:
        properties = tool.definition().parameters.get("properties", {}) if tool else {}
    except Exception:
        properties = {}
    if not isinstance(properties, Mapping):
        properties = {}
    arguments = dict(call.input)
    for key in arguments:
        schema = properties.get(key)
        if _is_sensitive_argument(key, schema if isinstance(schema, Mapping) else {}):
            arguments[key] = "<redacted>"
    return _ToolSummaryContext(
        family=family,
        name=name,
        args=tuple(
            _format_argument_preview(arguments[key])
            for key in properties
            if key in arguments
        ),
        arguments=arguments,
        tool=tool,
    )


def _tool_summary(
    context: _ToolSummaryContext,
    status: Literal["running", "succeeded", "failed", "canceled"],
    result: ToolResult | None = None,
) -> str:
    if context.tool is not None:
        try:
            summary = context.tool.summary(
                deepcopy(context.arguments),
                None if status == "canceled" else deepcopy(result),
            )
            if isinstance(summary, str) and summary.strip():
                summary = " ".join(summary.split())
                return (
                    f"Canceled: {summary.removesuffix('...').rstrip()}"
                    if status == "canceled"
                    else summary
                )
        except Exception:
            # Presentation must not change execution or log potentially secret data.
            _LOGGER.warning("Tool description failed for %s", context.name)
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
    tool: Tool | None,
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
    call: ToolCall,
    tool: Tool | Exception,
    context: ToolContext | None,
) -> ToolCallResult:
    """Translate the plugin result into an execution result with call identity."""

    try:
        if isinstance(tool, Exception):
            raise tool
        assert context is not None
        result = await tool.invoke(call.input, context)
        output, error = result.output, result.error
    except Exception as exc:
        output = {}
        error = str(exc) or type(exc).__name__
    return ToolCallResult(
        tool_call_id=call.tool_call_id,
        call_id=call.call_id,
        name=call.name,
        input=dict(call.input),
        output=output,
        error=error,
    )


def _tool_context(
    *,
    layout: AgentLayout,
    tool: Tool,
    services: tuple[ToolService, ...],
    runtime: ToolRuntime | None = None,
    history: ToolHistory | None = None,
    workspaces: Mapping[str, Path] | None = None,
) -> ToolContext:
    plugin_name = getattr(tool, "plugin_name", None)
    if not isinstance(plugin_name, str) or not plugin_name:
        raise ToolangError(f"unknown toolset plugin for tool: {tool.name}")
    args = (layout.home, layout.tool_room(plugin_name), workspaces or {})
    if plugin_name == "_toolang" and runtime is not None:
        return RuntimeToolContext(*args, runtime=runtime)
    if plugin_name == "history" and history is not None:
        return HistoryToolContext(*args, history=history)
    if plugin_name == "service":
        return ServiceToolContext(*args, services=services)
    if plugin_name == "me":
        return AgentStateToolContext(*args, layout=layout)
    return ToolContext(*args)
