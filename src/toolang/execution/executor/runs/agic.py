"""Agic run execution."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from toolang.base.types.message import Message
from toolang.common.errors import ToolangError
from toolang.lang.ast import AgicDecl

from ..common import (
    BoundRun,
    EventEmitter,
    Local,
    decode_agic_output,
    program_structs,
    value_text,
)
from ..prepare import PreparedAgic, prepare_agic
from ..steps import model as model_step
from ..steps import tool as tool_step
from ...records import RunControlRecord

if TYPE_CHECKING:
    from ..executor import _Execution

_MAX_TOOL_ROUNDS = 8


@dataclass(slots=True)
class _AgicState:
    """Mutable state shared by one agic's model and tool steps."""

    prepared: PreparedAgic
    home: Path
    emit: EventEmitter
    pending_inputs: Callable[[], tuple[RunControlRecord, ...]]
    before_call: Callable[[], None]
    messages: list[Message]
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
    bound = replace(
        binding,
        input=Message.user(
            value_text(primary.value) if primary.shape != "none" else ""
        ),
        args=invoke,
    )
    prepared = prepare_agic(execution, bound, agic)
    state = _AgicState(
        prepared,
        home=execution.setup.home,
        emit=execution.emit,
        pending_inputs=lambda: execution.pending_controls(binding.run_id, "steer"),
        before_call=lambda: execution.raise_if_stopping(binding.run_id, call=True),
        messages=list(prepared.messages),
    )
    message = await _execute(state)
    return Local(
        decode_agic_output(
            message,
            agic.output,
            structs=program_structs(binding),
        ),
        "item",
    )


async def _execute(state: _AgicState) -> Message | None:
    for _ in range(_MAX_TOOL_ROUNDS):
        result = await model_step.execute(state)
        if result.tool_calls:
            for call in result.tool_calls:
                await tool_step.execute(state, call)
            continue
        if state.pending_inputs():
            continue
        return result.message
    raise ToolangError("Model tool loop exceeded the maximum number of rounds.")
