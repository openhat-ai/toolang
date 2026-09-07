"""Agic run execution."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import TYPE_CHECKING

from toolang.base.types.message import Message
from toolang.base.types.policy import RunLimits
from toolang.base.types.run import ModelContinuation, ModelUsage
from toolang.common.errors import ToolangError
from toolang.common.layout import AgentLayout
from toolang.common.time import utc_now
from toolang.lang.ast import AgicDecl, StructDecl
from toolang.lang.errors import ToolangOutputError
from toolang.lang.input import coerce_output, output_json_schema
from toolang.state.state import AgentState, StatePublication
from toolang.state.state import state_program

from ...events import StepBegin, StepEnd
from ...records import ControlRecord
from ...types import (
    ControlRef,
    FieldRef,
    StepNoted,
    StepRef,
)
from ..common import (
    BoundRun,
    EventEmitter,
    Local,
    program_structs,
)

from ..limits import _ModelAccounting
from .._messages import _MessageBuffer
from ..prepare import _AgicFrame, prepare_agic
from ..steps import model as model_step
from ..steps import tool as tool_step
from ...runnables import (
    resolve_runnable,
)

ExecutionState = AgentState | StatePublication

if TYPE_CHECKING:
    from ..executor import _Execution


@dataclass(frozen=True, slots=True)
class _OutputBinding:
    """One immutable output contract for a complete Agic invocation."""

    type_name: str | None = None
    structs: Mapping[str, StructDecl] = field(default_factory=dict)
    output_schema: dict[str, object] | None = None


@dataclass(slots=True)
class _AgicState:
    """Mutable state shared by one agic's model and tool steps."""

    prepared: _AgicFrame
    layout: AgentLayout
    emit: EventEmitter
    pending_inputs: Callable[[], tuple[ControlRecord, ...]]
    steer_before_next_step: Callable[[], bool]
    immediate_steer: Callable[[], bool]
    before_call: Callable[[], None]
    messages: _MessageBuffer
    execution: _Execution | None = None
    account_usage: Callable[[ModelUsage | None], _ModelAccounting] = lambda usage: (
        _ModelAccounting(usage=usage)
    )
    record_accounting: Callable[[_ModelAccounting], None] = lambda _accounting: None
    limits: RunLimits = RunLimits()
    record_output: Callable[[FieldRef], None] = lambda _ref: None
    output: FieldRef | None = None
    continuation: ModelContinuation | None = None
    output_binding: _OutputBinding = field(default_factory=_OutputBinding)
    next_step: int = 0
    last_step: int | None = None
    next_model_inputs: tuple[FieldRef, ...] | None = None
    model_calls: int = 0
    tool_calls: int = 0
    tool_call_sources: dict[str, tuple[int, int]] = field(default_factory=dict)
    initial_inputs: tuple[FieldRef, ...] = ()
    claimed_inputs: tuple[ControlRecord, ...] = ()
    repairing_output: bool = False
    begin_step: (
        Callable[
            [Callable[[ExecutionState, ControlRef], StepBegin]],
            Awaitable[tuple[ExecutionState, ControlRef]],
        ]
        | None
    ) = None
    refresh_frame: Callable[[ExecutionState, ControlRef], _AgicFrame] | None = None

    def check_model_call_limit(self) -> None:
        """Check the next call without counting an uncommitted preparation."""

        limit = self.limits.agic_model_calls
        if limit is not None and self.model_calls >= limit:
            raise ToolangError(f"Agic model call limit exceeded: {limit}")

    def before_tool_call(self) -> None:
        """Apply one tool-call checkpoint and reserve its agic-local count."""

        limit = self.limits.agic_tool_calls
        if limit is not None and self.tool_calls >= limit:
            raise ToolangError(f"Agic tool call limit exceeded: {limit}")
        self.tool_calls += 1

    async def start_step(
        self,
        build: Callable[[ExecutionState, ControlRef], StepBegin],
    ) -> tuple[ExecutionState, ControlRef]:
        """Commit one physical-step boundary and return its State snapshot."""

        if self.begin_step is not None:
            return await self.begin_step(build)
        run = self.prepared.run
        event = build(run.state, run.state_ref)
        await self.emit(event)
        return run.state, run.state_ref

    async def end_step(
        self, event: StepEnd, *, canceled_noted: StepNoted | None = None
    ) -> None:
        """Commit the terminal fact before propagating a delivery interruption."""

        interruption: asyncio.CancelledError | None = None
        while True:
            try:
                await self.emit(event)
            except asyncio.CancelledError as exc:
                interruption = exc
                if self.execution is None:
                    raise
                record = self.execution.store.get_step(ref=event.step)
                # Delivery can be interrupted after persistence. Never end it twice.
                if record is None or record.status != "running":
                    raise
                if event.status != "canceled":
                    event = replace(
                        event,
                        status="canceled",
                        noted=canceled_noted or event.noted,
                        error=None,
                        finished_at=utc_now(),
                    )
            else:
                if interruption is not None:
                    raise interruption
                return

    def frame_for_step(self, state: ExecutionState, ref: ControlRef) -> _AgicFrame:
        """Prepare one step from the State captured at its boundary."""

        if self.refresh_frame is None:
            return self.prepared
        return self.refresh_frame(state, ref)


async def execute(
    execution: _Execution,
    binding: BoundRun,
    agic: AgicDecl,
    locals: Mapping[str, Local],
) -> Local:
    """Execute one complete agic model-tool cycle."""

    variables = {
        name: local.value for name, local in locals.items() if local.shape != "none"
    }
    frames: dict[tuple[str, FieldRef | None], _AgicFrame] = {}

    def refresh_frame(state: ExecutionState, ref: ControlRef) -> _AgicFrame:
        horizon = execution.horizon_for(binding.run_id, pending=True)
        far, near = execution.message_history().select(horizon)
        key = (state.revision, horizon)
        cached = frames.get(key)
        if cached is not None:
            return replace(
                cached,
                run=replace(cached.run, state=state, state_ref=ref),
            )
        candidate = resolve_runnable(
            state_program(state, binding.module),
            agic.name,
            kind="agic",
        )
        if not isinstance(candidate, AgicDecl):  # pragma: no cover - kind invariant
            raise TypeError(f"active agic changed kind: {agic.name}")
        current_binding = (
            binding
            if state.revision == binding.state.revision and ref == binding.state_ref
            else execution.refresh_run_binding(
                binding,
                state,
                ref,
                candidate,
                module=binding.module,
            )
        )
        prepared = prepare_agic(
            execution,
            replace(current_binding, horizon=horizon),
            candidate,
            variables=variables,
            far=far,
            near=near,
        )
        execution.require_model_pricing(prepared.model)
        frames[key] = prepared
        return prepared

    prepared = refresh_frame(binding.state, binding.state_ref)
    output_structs = program_structs(prepared.run)
    output_binding = _OutputBinding(
        type_name=prepared.agic.output,
        structs=MappingProxyType(dict(output_structs)),
        output_schema=output_json_schema(
            prepared.agic.output,
            structs=output_structs,
        ),
    )
    state = _AgicState(
        prepared,
        layout=execution.layout,
        emit=execution.emit,
        pending_inputs=lambda: execution.steer_controls_for_call(binding.run_id),
        steer_before_next_step=lambda: execution.steer_before_next_step(binding.run_id),
        immediate_steer=lambda: execution.immediate_steer(binding.run_id),
        before_call=lambda: execution.raise_if_canceling(binding.run_id, call=True),
        account_usage=lambda usage: execution.model_accounting(
            state.prepared.model, usage
        ),
        record_accounting=lambda accounting: execution.record_model_accounting(
            state.prepared.model, accounting
        ),
        limits=binding.limits,
        record_output=lambda ref: execution.record_output(binding.run_id, ref),
        messages=_MessageBuffer(),
        output_binding=output_binding,
        execution=execution,
        next_step=execution.next_step(binding.run_id),
        initial_inputs=tuple(
            local.ref
            for _name, local in sorted(locals.items())
            if local.shape != "none" and local.ref is not None
        ),
        begin_step=execution.begin_step,
        refresh_frame=refresh_frame,
    )
    message = await _execute(state)
    if state.output is None:
        raise RuntimeError("agic completed without a model output")
    output_type = state.output_binding.type_name
    try:
        output = coerce_output(
            message or Message(role="assistant"),
            output_type,
            structs=state.output_binding.structs,
        )
    except ToolangOutputError:
        if not _can_repair_output(state, output_type):
            raise
        state.messages.append(_output_repair_message(output_type))
        state.repairing_output = True
        try:
            message = await _execute(state)
        finally:
            state.repairing_output = False
        output = coerce_output(
            message or Message(role="assistant"),
            output_type,
            structs=state.output_binding.structs,
        )
    return Local(
        output,
        "item",
        state.output,
        type_name=output_type or "Part[]",
    )


def _can_repair_output(state: _AgicState, type_name: str | None) -> bool:
    if type_name is None or type_name in {"Part", "Part[]", "Text"}:
        return False
    limit = state.limits.agic_model_calls
    return limit is None or state.model_calls < limit


def _output_repair_message(type_name: str | None) -> Message:
    if type_name is None:  # pragma: no cover - guarded by _can_repair_output
        raise ValueError("output repair requires a declared type")
    return Message.user(
        f"Your previous response did not satisfy the required {type_name} output "
        f"contract. Return only a corrected {type_name} value. Do not explain the "
        "value, add a preface, or wrap it in Markdown code fences."
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
        ref = FieldRef.from_path(
            StepRef.from_local(state.prepared.run.run_id, (state.last_step,)),
            "output",
            "value",
        )
        state.output = ref
        state.record_output(ref)
        if result.tool_calls:
            # A reload inside this batch changes State, not its routing authority.
            routes = state.prepared.routes
            if state.steer_before_next_step():
                await tool_step.skip(state, result.tool_calls)
                continue
            for index, call in enumerate(result.tool_calls):
                try:
                    await tool_step.execute(
                        state,
                        call,
                        tool_call_count=len(result.tool_calls),
                        routes=routes,
                    )
                except asyncio.CancelledError:
                    if not state.immediate_steer():
                        await tool_step.skip(
                            state, result.tool_calls[index + 1 :], canceled=True
                        )
                        raise
                    await tool_step.skip(state, result.tool_calls[index + 1 :])
                    break
            continue
        if inputs := state.pending_inputs():
            state.claimed_inputs = inputs
            continue
        return result.message
