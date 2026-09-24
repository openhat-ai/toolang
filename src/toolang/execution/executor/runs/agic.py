"""Agic run execution."""

from __future__ import annotations

import asyncio
from asyncio import sleep
from time import monotonic
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import TYPE_CHECKING

from toolang.base.errors import ModelResponseError
from toolang.base.types.message import Message
from toolang.base.types.policy import RunLimits
from toolang.base.types.run import ModelCallResult, ModelContinuation, ModelUsage
from toolang.common.errors import ToolangError
from toolang.common.layout import AgentLayout
from toolang.common.time import utc_now
from toolang.lang.ast import AgicDecl, FlowDecl, StructDecl
from toolang.lang.errors import ToolangOutputError
from toolang.lang.input import coerce_output
from toolang.state.state import AgentState

from ...errors import EmptyModelOutput
from ...events import StepBegin, StepEnd
from ...assembly import prompting
from ...assembly.run_results import run_completion
from ...records import ControlRecord
from ...types import (
    ModelAccounting,
    MessageTemplate,
    ControlRef,
    FieldRef,
    RunRef,
    RecallTarget,
    StepNoted,
    StepRef,
)
from ..iteration import iteration_values
from ..common import (
    _StepFailed,
    BoundRun,
    EventEmitter,
    Local,
    program_structs,
)

from ...assembly.message_buffer import MessageBuffer
from ..budget import InputEstimate
from ..frame import _AgicFrame, build_agic_frame
from ..steps import model as model_step
from ..steps import tool as tool_step
from ...runnables import (
    resolve_bound_runnable,
    resolve_module_runnable,
)


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
    model_frame: _AgicFrame = field(init=False)
    layout: AgentLayout
    emit: EventEmitter
    pending_inputs: Callable[[], tuple[ControlRecord, ...]]
    steer_before_next_step: Callable[[], bool]
    immediate_steer: Callable[[], bool]
    before_call: Callable[[], None]
    messages: MessageBuffer
    execution: _Execution | None = None
    account_usage: Callable[[ModelUsage | None], ModelAccounting | None] = (
        lambda usage: (
            ModelAccounting(usage.input_tokens, usage.output_tokens)
            if usage is not None
            else None
        )
    )
    record_accounting: Callable[[ModelAccounting | None], None] = lambda _accounting: (
        None
    )
    limits: RunLimits = RunLimits()
    record_output: Callable[[FieldRef], None] = lambda _ref: None
    output: FieldRef | None = None
    scheduled_run: tuple[BoundRun, AgicDecl | FlowDecl] | None = None
    continuation: ModelContinuation | None = None
    output_binding: _OutputBinding = field(default_factory=_OutputBinding)
    next_step: int = 0
    last_step: int | None = None
    next_model_inputs: tuple[FieldRef, ...] | None = None
    model_calls: int = 0
    model_recoveries: int = 0
    retry_not_before: float = 0
    tool_calls: int = 0
    tool_call_sources: dict[str, tuple[int, int]] = field(default_factory=dict)
    visible_recalls: dict[RecallTarget, str] = field(default_factory=dict)
    initial_inputs: tuple[FieldRef, ...] = ()
    claimed_inputs: tuple[ControlRecord, ...] = ()
    repairing_output: bool = False
    estimate: InputEstimate = field(default_factory=InputEstimate)
    begin_step: (
        Callable[
            [Callable[[AgentState, ControlRef], StepBegin]],
            Awaitable[tuple[AgentState, ControlRef]],
        ]
        | None
    ) = None
    refresh_frame: Callable[[AgentState, ControlRef], _AgicFrame] | None = None

    def __post_init__(self) -> None:
        # Tools may refresh prepared without changing the last dispatched call.
        self.model_frame = self.prepared

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
        build: Callable[[AgentState, ControlRef], StepBegin],
    ) -> tuple[AgentState, ControlRef]:
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

    def frame_for_step(self, state: AgentState, ref: ControlRef) -> _AgicFrame:
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
    frames: dict[tuple[str, RunRef | None], _AgicFrame] = {}

    def refresh_frame(state: AgentState, ref: ControlRef) -> _AgicFrame:
        horizon = execution.horizon_for(binding.run_id, pending=True)
        selected = execution.message_history().select(horizon)
        key = (state.revision, horizon)
        cached = frames.get(key)
        if cached is not None:
            return replace(
                cached,
                run=replace(cached.run, state=state, state_ref=ref),
            )
        if agic.name is not None:
            ref_name, candidate = resolve_module_runnable(
                state, binding.module, agic.name, kind="agic"
            )
        else:
            ref_name = binding.bindings.runnable
            if ref_name is None:  # pragma: no cover - accepted run invariant
                raise ValueError("active run is missing its runnable binding")
            candidate = resolve_bound_runnable(state, binding.module, ref_name)
        if not isinstance(candidate, AgicDecl):  # pragma: no cover - kind invariant
            raise TypeError(f"active agic changed kind: {ref_name}")
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
        prepared = build_agic_frame(
            execution,
            replace(current_binding, horizon=horizon),
            candidate,
            variables={**variables, **iteration_values()},
            far=selected.far,
            near=selected.near,
        )
        execution.require_model_pricing(prepared.model)
        frames[key] = prepared
        return prepared

    prepared = refresh_frame(binding.state, binding.state_ref)
    output_structs = program_structs(prepared.run)
    output_binding = _OutputBinding(
        type_name=prepared.agic.output,
        structs=MappingProxyType(dict(output_structs)),
        output_schema=prompting.output_schema(
            prepared.run.state,
            prepared.agic,
            module=prepared.run.module,
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
            state.prepared.model,
            usage,
        ),
        record_accounting=lambda accounting: execution.record_model_accounting(
            state.prepared.model, accounting
        ),
        limits=binding.limits,
        record_output=lambda ref: execution.record_output(binding.run_id, ref),
        messages=MessageBuffer(),
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
    """Request one corrected response without changing its output contract."""

    if type_name is None:  # pragma: no cover - guarded by _can_repair_output
        raise ValueError("output repair requires a declared type")
    return Message.user(
        f"Your previous response did not satisfy the required {type_name} output "
        f"contract. Return only a corrected {type_name} value. Do not explain the "
        "value, add a preface, or wrap it in Markdown code fences."
    )


def _recover_model_response(state: _AgicState, error: ModelResponseError) -> None:
    """Retry before tool execution, sharing a bounded allowance across the run."""

    step = StepRef.from_local(state.prepared.run.run_id, (state.next_step - 1,))
    limit = state.limits.agic_model_calls
    if (
        not error.recoverable
        or state.model_recoveries >= 2
        or (limit is not None and state.model_calls >= limit)
    ):
        raise _StepFailed(step, error) from error
    state.model_recoveries += 1
    state.next_model_inputs = (FieldRef.from_path(step, "error"),)
    if error.kind == "transport_error":
        state.retry_not_before = monotonic() + max(
            float(state.model_recoveries), error.retry_after or 0
        )
    else:
        state.messages.append(
            Message.user(
                "Your previous model response could not be used "
                f"({error.kind}). None of its tool calls were executed. "
                "Return a complete response; use a function name and a JSON object "
                "for every tool call. Keep the response concise."
            )
        )


async def _execute(state: _AgicState) -> Message | None:
    while True:
        try:
            if delay := max(0, state.retry_not_before - monotonic()):
                await sleep(delay)
            state.retry_not_before = 0
            try:
                result = await model_step.execute(state)
            except ModelResponseError as exc:
                _recover_model_response(state, exc)
                continue
        except asyncio.CancelledError:
            if state.immediate_steer():
                continue
            raise
        if state.last_step is None:
            raise RuntimeError("model step did not record its index")
        ref = FieldRef.from_path(
            StepRef.from_local(state.prepared.run.run_id, (state.last_step,)),
            "output",
            "local",
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
            completions: list[MessageTemplate] = []
            for index, call in enumerate(result.tool_calls):
                interrupted = False
                try:
                    try:
                        await tool_step.execute(
                            state,
                            call,
                            tool_call_count=len(result.tool_calls),
                            routes=routes,
                        )
                    except asyncio.CancelledError:
                        if state.immediate_steer():
                            interrupted = True
                        elif not (
                            state.scheduled_run is not None
                            and state.execution is not None
                            and state.execution.canceled_within(
                                state.scheduled_run[0].run_id
                            )
                        ):
                            raise
                        # An accepted request survives delivery interruption. A
                        # target-local cancel terminates only that scheduled Run.
                    await _dispatch_run(state, completions)
                except asyncio.CancelledError:
                    if not state.immediate_steer():
                        if (
                            state.scheduled_run is not None
                            and state.execution is not None
                        ):
                            binding, _ = state.scheduled_run
                            state.scheduled_run = None
                            await state.execution.executor._ensure_terminal(
                                binding.run_id, emit=state.emit, status="canceled"
                            )
                        await tool_step.skip(
                            state, result.tool_calls[index + 1 :], canceled=True
                        )
                        raise
                    interrupted = True
                if interrupted:
                    await tool_step.skip(state, result.tool_calls[index + 1 :])
                    break
            if state.execution is not None:
                for completion in completions:
                    state.messages.append_template(
                        completion, state.execution.store.resolve_value
                    )
            continue
        if inputs := state.pending_inputs():
            state.claimed_inputs = inputs
            continue
        if not _declares_repairable_output(state):
            _require_visible_output(result)
        return result.message


async def _dispatch_run(state: _AgicState, completions: list[MessageTemplate]) -> None:
    """Dispatch after the receipt Step ends; defer context until the batch ends."""

    scheduled = state.scheduled_run
    if scheduled is None:
        return
    execution = state.execution
    if execution is None:
        raise RuntimeError("Agic runtime execution is unavailable")
    state.scheduled_run = None
    binding, runnable = scheduled
    try:
        if execution.canceled_within(binding.run_id):
            await execution.executor._ensure_terminal(
                binding.run_id, emit=state.emit, status="canceled"
            )
        else:
            await execution.execute(binding, runnable, output_binding=None)
    except asyncio.CancelledError:
        await execution.executor._ensure_terminal(
            binding.run_id, emit=state.emit, status="canceled"
        )
        if not execution.canceled_within(binding.run_id):
            completion = execution.store.run_completion(binding.run_id)
            if completion is not None:
                completions.append(completion)
            raise
    except Exception:
        child = execution.store.get_run(run_id=binding.run_id)
        if child is None or child.status in {"pending", "running"}:
            raise
    child = execution.store.get_run(run_id=binding.run_id)
    if child is None:
        raise RuntimeError(f"scheduled Run disappeared: {binding.run_id}")
    if child.status == "succeeded" and child.output is not None:
        state.output = FieldRef.from_path(RunRef(child.id), "output", "local", "value")
        state.record_output(state.output)
    completions.append(
        run_completion(
            child, execution.store.resolve_value, execution.store.resolve_error
        )
    )


def _declares_repairable_output(state: _AgicState) -> bool:
    """Return whether an unusable response still gets an output repair.

    A typed, non-text contract already fails loudly when its value cannot be
    coerced, so the empty-output guard applies only to the contracts that would
    otherwise accept an empty message as success.
    """

    return state.output_binding.type_name not in {None, "Part", "Part[]", "Text"}


def _require_visible_output(result: ModelCallResult) -> None:
    """Reject a terminal model step that returned nothing a caller can see."""

    message = result.message
    if message is not None and message.parts:
        return
    usage = result.usage
    if (
        usage is not None
        and usage.output_tokens > 0
        and usage.output_reasoning_tokens is not None
        and usage.output_reasoning_tokens >= usage.output_tokens
    ):
        raise EmptyModelOutput(
            "model produced no visible output: reasoning consumed the output "
            f"budget ({usage.output_reasoning_tokens} of {usage.output_tokens} "
            "output tokens); raise the output limit or lower the reasoning effort"
        )
    raise EmptyModelOutput("model produced no visible output")
