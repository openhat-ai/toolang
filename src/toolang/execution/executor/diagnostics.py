"""Bounded model and tool diagnostics."""

from __future__ import annotations

import json
import logging

from toolang.base.types.model import ModelTarget
from toolang.base.types.run import ModelCall, ModelCallResult, ToolCall, ToolCallResult

_LOGGER = logging.getLogger(__name__)
_PREVIEW_LIMIT = 2_000


def log_model_target(
    model: ModelTarget, *, thread_id: str, run_id: str, step_index: int
) -> None:
    if not _LOGGER.isEnabledFor(logging.DEBUG):
        return
    _LOGGER.debug(
        "model.target thread=%s run=%s step=%s ref=%s provider=%s model=%s adapter=%s%s",
        thread_id,
        run_id,
        step_index,
        model.ref,
        model.provider,
        model.model,
        model.adapter,
        f" base_url={model.base_url}" if model.base_url else "",
    )


def log_model_request(
    request: ModelCall, *, thread_id: str, run_id: str, step_index: int
) -> None:
    if not _LOGGER.isEnabledFor(logging.DEBUG):
        return
    _LOGGER.debug(
        "model.request thread=%s run=%s step=%s instructions=%s messages=%s tools=%s output_schema=%s continuation=%s",
        thread_id,
        run_id,
        step_index,
        _preview(" ".join(request.instructions.split())),
        _data([message.to_data() for message in request.messages]),
        _data([tool.name for tool in request.tools]),
        _data(request.output_schema),
        _data(request.continuation),
    )


def log_model_result(
    result: ModelCallResult, *, thread_id: str, run_id: str, step_index: int
) -> None:
    if not _LOGGER.isEnabledFor(logging.DEBUG):
        return
    usage = (
        {
            "input_tokens": result.usage.input_tokens,
            "output_tokens": result.usage.output_tokens,
        }
        if result.usage is not None
        else None
    )
    _LOGGER.debug(
        "model.result thread=%s run=%s step=%s message=%s tool_calls=%s usage=%s continuation=%s",
        thread_id,
        run_id,
        step_index,
        _data(result.message.to_data() if result.message is not None else None),
        _data(
            [
                {
                    "tool_call_id": call.tool_call_id,
                    "call_id": call.call_id,
                    "name": call.name,
                    "input": call.input,
                }
                for call in result.tool_calls
            ]
        ),
        _data(usage),
        _data(result.continuation),
    )


def log_tool_call_input(
    call: ToolCall,
    *,
    thread_id: str,
    run_id: str,
    step_index: int,
    plugin_name: str,
) -> None:
    if not _LOGGER.isEnabledFor(logging.DEBUG):
        return
    _LOGGER.debug(
        "tool.request thread=%s run=%s step=%s plugin=%s tool=%s call=%s arguments=%s",
        thread_id,
        run_id,
        step_index,
        plugin_name,
        call.name,
        call.call_id or call.tool_call_id,
        _data(call.input),
    )


def log_tool_call_output(
    result: ToolCallResult,
    *,
    thread_id: str,
    run_id: str,
    step_index: int,
    plugin_name: str,
) -> None:
    if not _LOGGER.isEnabledFor(logging.DEBUG):
        return
    _LOGGER.debug(
        "tool.result thread=%s run=%s step=%s plugin=%s tool=%s call=%s output=%s error=%s",
        thread_id,
        run_id,
        step_index,
        plugin_name,
        result.name,
        result.call_id or result.tool_call_id,
        _data(result.output),
        result.error or "-",
    )


def _data(value: object) -> str:
    if value is None:
        return "-"
    try:
        return _preview(
            json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        )
    except TypeError:
        return _preview(repr(value))


def _preview(value: str) -> str:
    if len(value) <= _PREVIEW_LIMIT:
        return value
    return value[: _PREVIEW_LIMIT - 3] + "..."
