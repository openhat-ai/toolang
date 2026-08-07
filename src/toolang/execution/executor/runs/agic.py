"""Agic run execution."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from toolang.base.types.message import Message, TextPart
from toolang.common.errors import ToolangError
from toolang.common.layout import AgentLayout
from toolang.lang.ast import AgicDecl
from toolang.lang.input import coerce_output

from ...records import RunControlRecord, StepOutputRef
from ...types import StepPath
from ..common import (
    BoundRun,
    EventEmitter,
    Local,
    program_structs,
    value_percept,
    value_text,
)
from ..prepare import _AgicFrame, prepare_agic
from ..steps import model as model_step
from ..steps import tool as tool_step

if TYPE_CHECKING:
    from ..executor import _Execution

_MAX_TOOL_ROUNDS = 8


@dataclass(slots=True)
class _AgicState:
    """Mutable state shared by one agic's model and tool steps."""

    prepared: _AgicFrame
    layout: AgentLayout
    emit: EventEmitter
    pending_inputs: Callable[[], tuple[RunControlRecord, ...]]
    before_call: Callable[[], None]
    messages: list[Message]
    record_output: Callable[[StepOutputRef], None] = lambda _ref: None
    output: StepOutputRef | None = None
    model_state: dict[str, Any] | None = None
    next_step: int = 0
    last_step: int | None = None
    tool_call_sources: dict[str, tuple[int, int]] = field(default_factory=dict)


async def execute(
    execution: _Execution,
    binding: BoundRun,
    agic: AgicDecl,
    locals: Mapping[str, Local],
) -> Local:
    """Execute one complete agic model-tool cycle."""

    invoke = {name: local.value for name, local in locals.items() if name != "_"}
    primary = locals.get("_", Local())
    primary_parts = (
        value_percept(primary.value, type_name=primary.type_name)
        if primary.shape == "item"
        else None
    )
    if primary.shape == "none":
        primary_parts = ()
    bound = replace(
        binding,
        input=Message(
            role="user",
            parts=(
                primary_parts
                if primary_parts is not None
                else (TextPart(value_text(primary.value)),)
            ),
        ),
        args=invoke,
    )
    prepared = prepare_agic(
        execution,
        bound,
        agic,
        variables={
            name: local.value for name, local in locals.items() if local.shape != "none"
        },
    )
    state = _AgicState(
        prepared,
        layout=execution.layout,
        emit=execution.emit,
        pending_inputs=lambda: execution.steer_controls_for_call(binding.run_id),
        before_call=lambda: execution.raise_if_stopping(binding.run_id, call=True),
        record_output=lambda ref: execution.record_output(binding.run_id, ref),
        messages=list(prepared.messages),
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
    for _ in range(_MAX_TOOL_ROUNDS):
        result = await model_step.execute(state)
        if state.last_step is None:
            raise RuntimeError("model step did not record its index")
        ref = StepOutputRef(
            step=StepPath(state.prepared.run.run_id, (state.last_step,))
        )
        state.output = ref
        state.record_output(ref)
        if result.tool_calls:
            for call in result.tool_calls:
                await tool_step.execute(state, call)
            continue
        if state.pending_inputs():
            continue
        return result.message
    raise ToolangError("Model tool loop exceeded the maximum number of rounds.")
