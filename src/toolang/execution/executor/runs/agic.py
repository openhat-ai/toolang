"""Agic run execution."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from toolang.base.types.message import Message, TextPart
from toolang.base.types.run import ModelUsage, RunLimits
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
    before_call: Callable[[], None]
    messages: list[Message]
    account_usage: Callable[[ModelUsage | None], _ModelAccounting] = (
        lambda usage: _ModelAccounting(usage=usage)
    )
    record_accounting: Callable[[_ModelAccounting], None] = (
        lambda _accounting: None
    )
    limits: RunLimits = RunLimits()
    record_output: Callable[[StepOutputRef], None] = lambda _ref: None
    output: StepOutputRef | None = None
    model_state: dict[str, Any] | None = None
    next_step: int = 0
    last_step: int | None = None
    model_calls: int = 0
    tool_calls: int = 0
    tool_call_sources: dict[str, tuple[int, int]] = field(default_factory=dict)

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
    execution.require_model_pricing(prepared.model)
    state = _AgicState(
        prepared,
        layout=execution.layout,
        emit=execution.emit,
        pending_inputs=lambda: execution.steer_controls_for_call(binding.run_id),
        before_call=lambda: execution.raise_if_stopping(binding.run_id, call=True),
        account_usage=lambda usage: execution.model_accounting(
            prepared.model, usage
        ),
        record_accounting=lambda accounting: execution.record_model_accounting(
            prepared.model, accounting
        ),
        limits=binding.limits,
        record_output=lambda ref: execution.record_output(binding.run_id, ref),
        messages=list(prepared.messages),
        next_step=execution.next_step(binding.run_id),
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
