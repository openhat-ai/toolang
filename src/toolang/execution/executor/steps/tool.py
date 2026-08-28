"""Tool-call steps and result part events."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import logging
from collections.abc import Mapping
import re
import time
from typing import TYPE_CHECKING

from toolang.base.protocols.tool import AgentTool
from toolang.base.types.message import Message, ToolResultPart
from toolang.base.types.run import ToolCall, ToolCallResult
from toolang.base.types.tool import ToolContext, ToolService
from toolang.common.errors import ToolangError
from toolang.common.layout import AgentLayout
from toolang.common.template import render_text_template
from toolang.common.time import elapsed_ms, utc_now
from toolang.state.state import AgentState

from ...events import PartBegin, PartEnd, StepBegin, StepEnd
from ...types import (
    ControlRef,
    Local,
    Pointer,
    StepPath,
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
    step_input: tuple[Pointer, ...]
    if source is not None:
        step_input = (Pointer.step(StepPath(run.run_id, (source[0],)), source[1]),)
    elif state.last_step is not None:
        step_input = (Pointer.step(StepPath(run.run_id, (state.last_step,))),)
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

    def begin_step(agent_state: AgentState, state_ref: ControlRef) -> StepBegin:
        nonlocal prepared, plugin_name, summary_context
        prepared = state.frame_for_step(agent_state, state_ref)
        tool = prepared.tools.get(call.name)
        plugin_name = _plugin_name(tool)
        summary_context = _tool_summary_context(call, tool)
        return StepBegin(
            step=StepPath(run.run_id, (step_index,)),
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

    await state.start_step(begin_step)
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
        await state.emit(
            StepEnd(
                step=StepPath(run.run_id, (step_index,)),
                kind="tool",
                status="canceled",
                noted=ToolStepNoted(summary=_tool_summary(summary_context, "canceled")),
                finished_at=utc_now(),
            )
        )
        raise
    except Exception as exc:
        error = str(exc) or type(exc).__name__
        await state.emit(
            StepEnd(
                step=StepPath(run.run_id, (step_index,)),
                kind="tool",
                status="failed",
                noted=ToolStepNoted(summary=_tool_summary(summary_context, "failed")),
                error=error,
                finished_at=utc_now(),
            )
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
        raise _StepFailed(StepPath(run.run_id, (step_index,)), exc) from exc
    part = ToolResultPart(
        tool_call_id=record.tool_call_id,
        call_id=record.call_id,
        tool_name=record.name,
        tool_family=record.name,
        output=dict(record.output),
        error=record.error,
    )
    await state.emit(
        PartBegin(
            step=StepPath(run.run_id, (step_index,)),
            part=0,
            part_type=part.type,
        )
    )
    await state.emit(
        PartEnd(
            step=StepPath(run.run_id, (step_index,)),
            part=0,
            data=part,
        )
    )
    status = "failed" if record.error is not None else "succeeded"
    log_tool_call_output(
        record,
        thread_id=run.thread,
        run_id=run.run_id,
        step_index=step_index,
        plugin_name=plugin_name,
    )
    await state.emit(
        StepEnd(
            step=StepPath(run.run_id, (step_index,)),
            kind="tool",
            status=status,
            output=Local.typed("ToolResultPart", part, None, 0),
            noted=ToolStepNoted(summary=_tool_summary(summary_context, status)),
            finished_at=utc_now(),
            error=record.error,
        )
    )
    state.messages.append(_followup_message(record))
    state.last_step = step_index
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
                f"executor runtime action cannot be invoked as a tool: {name}"
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
        output = {}
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


def _followup_message(tool_call: ToolCallResult) -> Message:
    return Message(
        role="tool",
        parts=(
            ToolResultPart(
                tool_call_id=tool_call.tool_call_id,
                call_id=tool_call.call_id,
                tool_name=tool_call.name,
                tool_family=tool_call.name,
                output=dict(tool_call.output),
                error=tool_call.error,
            ),
        ),
    )
