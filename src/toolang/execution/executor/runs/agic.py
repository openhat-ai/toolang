"""Agic run execution."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from toolang.base.types.message import Message, ToolResultPart
from toolang.base.types.policy import RunLimits
from toolang.base.types.run import ModelUsage, ToolCall
from toolang.common.errors import ToolangError
from toolang.common.layout import AgentLayout
from toolang.lang.ast import AgicDecl
from toolang.lang.input import coerce_output

from ...records import RunControlRecord
from ...types import StepPath, Pointer
from ..common import (
    BoundRun,
    EventEmitter,
    Local,
    program_structs,
)
from ..limits import _ModelAccounting
from ..prepare import _AgicFrame, prepare_agic
from ..steps import model as model_step
from ..steps import tool as tool_step

if TYPE_CHECKING:
    from ..executor import _Execution


@dataclass(slots=True)
class _AgicState:
    """Mutable state shared by one agic's model and tool steps."""

    prepared: _AgicFrame
    layout: AgentLayout
    emit: EventEmitter
    pending_inputs: Callable[[], tuple[RunControlRecord, ...]]
    steer_before_next_step: Callable[[], bool]
    immediate_steer: Callable[[], bool]
    before_call: Callable[[], None]
    messages: list[Message]
    account_usage: Callable[[ModelUsage | None], _ModelAccounting] = lambda usage: (
        _ModelAccounting(usage=usage)
    )
    record_accounting: Callable[[_ModelAccounting], None] = lambda _accounting: None
    limits: RunLimits = RunLimits()
    record_output: Callable[[Pointer], None] = lambda _ref: None
    output: Pointer | None = None
    model_state: dict[str, Any] | None = None
    next_step: int = 0
    last_step: int | None = None
    next_model_inputs: tuple[Pointer, ...] | None = None
    model_calls: int = 0
    tool_calls: int = 0
    tool_call_sources: dict[str, tuple[int, int]] = field(default_factory=dict)
    initial_inputs: tuple[Pointer, ...] = ()
    claimed_inputs: tuple[RunControlRecord, ...] = ()

    def before_model_call(self) -> None:
        """Apply one model-call checkpoint and reserve its agic-local count."""

        self.before_call()
        limit = self.limits.agic_model_calls
        if limit is not None and self.model_calls >= limit:
            raise ToolangError(f"Agic model call limit exceeded: {limit}")
        self.model_calls += 1

    def before_tool_call(self) -> None:
        """Apply one tool-call checkpoint and reserve its agic-local count."""

        self.before_call()
        limit = self.limits.agic_tool_calls
        if limit is not None and self.tool_calls >= limit:
            raise ToolangError(f"Agic tool call limit exceeded: {limit}")
        self.tool_calls += 1


async def execute(
    execution: _Execution,
    binding: BoundRun,
    agic: AgicDecl,
    locals: Mapping[str, Local],
) -> Local:
    """Execute one complete agic model-tool cycle."""

    prepared = prepare_agic(
        execution,
        binding,
        agic,
        variables={
            name: local.value for name, local in locals.items() if local.shape != "none"
        },
    )
    execution.require_model_pricing(prepared.model)
    state = _AgicState(
        prepared,
        layout=execution.layout,
        emit=execution.emit,
        pending_inputs=lambda: execution.steer_controls_for_call(binding.run_id),
        steer_before_next_step=lambda: execution.steer_before_next_step(binding.run_id),
        immediate_steer=lambda: execution.immediate_steer(binding.run_id),
        before_call=lambda: execution.raise_if_stopping(binding.run_id, call=True),
        account_usage=lambda usage: execution.model_accounting(prepared.model, usage),
        record_accounting=lambda accounting: execution.record_model_accounting(
            prepared.model, accounting
        ),
        limits=binding.limits,
        record_output=lambda ref: execution.record_output(binding.run_id, ref),
        messages=list(prepared.messages),
        next_step=execution.next_step(binding.run_id),
        initial_inputs=tuple(
            local.ref
            for _name, local in sorted(locals.items())
            if local.shape != "none" and local.ref is not None
        ),
    )
    message = await _execute(state)
    if state.output is None:
        raise RuntimeError("agic completed without a model output")
    return Local(
        coerce_output(
            message or Message(role="assistant"),
            agic.output,
            structs=program_structs(binding),
        ),
        "item",
        state.output,
        type_name=agic.output or "Part[]",
    )


async def _execute(state: _AgicState) -> Message | None:
    while True:
        try:
            result = await model_step.execute(state)
        except asyncio.CancelledError:
            if state.immediate_steer():
                continue
            raise
        if state.last_step is None:
            raise RuntimeError("model step did not record its index")
        ref = Pointer.step(StepPath(state.prepared.run.run_id, (state.last_step,)))
        state.output = ref
        state.record_output(ref)
        if result.tool_calls:
            if state.steer_before_next_step():
                _append_canceled_tool_results(state, result.tool_calls)
                continue
            for index, call in enumerate(result.tool_calls):
                try:
                    await tool_step.execute(state, call)
                except asyncio.CancelledError:
                    if not state.immediate_steer():
                        raise
                    _append_canceled_tool_results(state, result.tool_calls[index:])
                    break
            continue
        if inputs := state.pending_inputs():
            state.claimed_inputs = inputs
            continue
        return result.message


def _append_canceled_tool_results(
    state: _AgicState,
    calls: tuple[ToolCall, ...],
) -> None:
    """Complete skipped tool calls in model history without executing them."""

    state.next_model_inputs = tuple(
        Pointer.step(
            StepPath(state.prepared.run.run_id, (source[0],)),
            source[1],
        )
        for call in calls
        if (source := state.tool_call_sources.get(call.tool_call_id)) is not None
    )
    state.messages.append(
        Message(
            role="tool",
            parts=tuple(
                ToolResultPart(
                    tool_call_id=call.tool_call_id,
                    call_id=call.call_id,
                    tool_name=call.name,
                    tool_family=call.name,
                    error="canceled by steer",
                )
                for call in calls
            ),
        )
    )
