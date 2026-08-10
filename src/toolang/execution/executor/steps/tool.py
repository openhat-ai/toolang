"""Tool-call steps and result part events."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
import time
from typing import TYPE_CHECKING

from toolang.base.protocols.tool import AgentTool
from toolang.base.types.message import Message, ToolResultPart
from toolang.base.types.run import ToolCall, ToolCallResult
from toolang.base.types.tool import ToolContext, ToolService
from toolang.common.errors import ToolangError
from toolang.common.layout import AgentLayout
from toolang.common.time import elapsed_ms, utc_now

from ...events import PartBegin, PartEnd, StepBegin, StepEnd
from ...records import RunInputRef, StepInput, StepOutputRef
from ...types import StepPath
from ..diagnostics import log_tool_call_input, log_tool_call_output

if TYPE_CHECKING:
    from ..runs.agic import _AgicState

_LOGGER = logging.getLogger(__name__)


async def execute(state: _AgicState, call: ToolCall) -> ToolCallResult:
    """Perform one tool call and emit its complete step event stream."""

    prepared = state.prepared
    run = prepared.run
    state.before_tool_call()
    step_index = state.next_step
    state.next_step += 1
    step_started = time.perf_counter()
    started_at = utc_now()
    plugin_name = _plugin_name(prepared.tools.get(call.name))
    source = state.tool_call_sources.get(call.tool_call_id)
    step_input: tuple[StepInput, ...]
    if source is not None:
        step_input = (
            StepOutputRef(
                step=StepPath(run.run_id, (source[0],)),
                part=source[1],
            ),
        )
    elif state.last_step is not None:
        step_input = (StepOutputRef(step=StepPath(run.run_id, (state.last_step,))),)
    else:
        step_input = (RunInputRef(),)
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
    await state.emit(
        StepBegin(
            step=StepPath(run.run_id, (step_index,)),
            kind="tool",
            input=step_input,
            given={
                "tool": call.name,
                "plugin": plugin_name,
                "tool_call_id": call.tool_call_id,
                "call_id": call.call_id,
            },
            started_at=started_at,
        )
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
        raise
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
    status = "failed" if record.error else "finished"
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
            output=(part,),
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
        error = str(exc)
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
        raise ToolangError(f"unknown tool plugin for tool: {tool_name}")
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
