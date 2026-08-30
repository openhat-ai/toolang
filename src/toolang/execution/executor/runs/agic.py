"""Agic run execution."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import TYPE_CHECKING

from toolang.base.types.message import Message, ToolResultPart
from toolang.base.types.policy import RunLimits
from toolang.base.types.run import ModelContinuation, ModelUsage, ToolCall
from toolang.common.errors import ToolangError
from toolang.common.layout import AgentLayout
from toolang.lang.ast import AgicDecl, RunStmt, Span, StructDecl
from toolang.lang.errors import ToolangOutputError
from toolang.lang.input import coerce_output, output_json_schema
from toolang.state.state import AgentState
from toolang.state.state import state_program

from ...events import StepBegin
from ...records import RunControlPayload, ControlRecord
from ...types import (
    ControlRef,
    Local as RecordLocal,
    StepPath,
    Pointer,
    TypedPointer,
    local_to_protocol_data,
)
from ..common import (
    BoundRun,
    EventEmitter,
    Local,
    _ExecutionFailed,
    _ExecuteCommitted,
    _RunRejected,
    _StepFailed,
    program_structs,
)
from ..limits import _ModelAccounting
from ..prepare import _AgicFrame, prepare_agic
from ..steps import model as model_step
from ..steps import run as run_step
from ..steps import tool as tool_step
from ...runnables import (
    ResolvedRunnable,
    parse_runnable_ref,
    resolve_runnable,
    resolve_public_runnable,
)
from ...tools.runtime import EXECUTE_TOOL, RELOAD_TOOL, RUN_TOOL

if TYPE_CHECKING:
    from ..executor import _Execution


@dataclass(frozen=True, slots=True)
class _OutputBinding:
    """One immutable output contract for a complete Agic invocation."""

    type_name: str | None = None
    structs: Mapping[str, StructDecl] = field(default_factory=dict)
    structured_output: dict[str, object] | None = None


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
    messages: list[Message]
    execution: _Execution | None = None
    account_usage: Callable[[ModelUsage | None], _ModelAccounting] = lambda usage: (
        _ModelAccounting(usage=usage)
    )
    record_accounting: Callable[[_ModelAccounting], None] = lambda _accounting: None
    limits: RunLimits = RunLimits()
    record_output: Callable[[Pointer], None] = lambda _ref: None
    output: Pointer | None = None
    continuation: ModelContinuation | None = None
    output_binding: _OutputBinding = field(default_factory=_OutputBinding)
    next_step: int = 0
    last_step: int | None = None
    next_model_inputs: tuple[Pointer, ...] | None = None
    model_calls: int = 0
    tool_calls: int = 0
    tool_call_sources: dict[str, tuple[int, int]] = field(default_factory=dict)
    initial_inputs: tuple[Pointer, ...] = ()
    claimed_inputs: tuple[ControlRecord, ...] = ()
    repairing_output: bool = False
    begin_step: (
        Callable[
            [Callable[[AgentState, ControlRef], StepBegin]],
            Awaitable[tuple[AgentState, ControlRef]],
        ]
        | None
    ) = None
    refresh_frame: Callable[[AgentState, ControlRef], _AgicFrame] | None = None

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
    frames: dict[str, _AgicFrame] = {}

    def refresh_frame(state: AgentState, ref: ControlRef) -> _AgicFrame:
        cached = frames.get(state.revision)
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
            current_binding,
            candidate,
            variables=variables,
        )
        execution.require_model_pricing(prepared.model)
        frames[state.revision] = prepared
        return prepared

    prepared = refresh_frame(binding.state, binding.state_ref)
    output_structs = program_structs(prepared.run)
    output_binding = _OutputBinding(
        type_name=prepared.agic.output,
        structs=MappingProxyType(dict(output_structs)),
        structured_output=output_json_schema(
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
        messages=list(prepared.messages),
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
        ref = Pointer.step(
            StepPath(state.prepared.run.run_id, (state.last_step,)),
            "output",
            "value",
        )
        state.output = ref
        state.record_output(ref)
        if result.tool_calls:
            if state.steer_before_next_step():
                _append_canceled_tool_results(state, result.tool_calls)
                continue
            for index, call in enumerate(result.tool_calls):
                try:
                    runtime_tool = state.prepared.runtime_tools.get(call.name)
                    if runtime_tool is not None and runtime_tool.name == RELOAD_TOOL:
                        if state.execution is None:
                            raise RuntimeError("Agic runtime execution is unavailable")
                        await _reload(state.execution, state, call)
                    elif runtime_tool is not None and runtime_tool.name == RUN_TOOL:
                        if state.execution is None:
                            raise RuntimeError("Agic runtime execution is unavailable")
                        await _run(state.execution, state, call)
                    elif runtime_tool is not None and runtime_tool.name == EXECUTE_TOOL:
                        if state.execution is None:
                            raise RuntimeError("Agic runtime execution is unavailable")
                        await _execute_transfer(
                            state.execution,
                            state,
                            call,
                            tool_call_count=len(result.tool_calls),
                        )
                    elif call.name.startswith("_too__"):
                        _reject_runtime_tool(state, call)
                    else:
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


async def _reload(
    execution: _Execution,
    state: _AgicState,
    call: ToolCall,
) -> None:
    """Consume one runtime reload without creating a Step."""

    state.before_tool_call()
    try:
        if call.input:
            raise ToolangError("_too/reload does not accept input")
        output = await execution.executor.model_reload(
            run_id=state.prepared.run.run_id,
        )
        part = ToolResultPart(
            tool_call_id=call.tool_call_id,
            call_id=call.call_id,
            tool_name=call.name,
            tool_family=call.name,
            output=output,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        part = ToolResultPart(
            tool_call_id=call.tool_call_id,
            call_id=call.call_id,
            tool_name=call.name,
            tool_family=call.name,
            error=str(exc) or type(exc).__name__,
        )
    state.messages.append(Message(role="tool", parts=(part,)))


async def _run(
    execution: _Execution,
    state: _AgicState,
    call: ToolCall,
) -> None:
    """Adapt one model runtime call to the ordinary Run Step executor."""

    state.before_tool_call()
    step_index = state.next_step
    state.next_step += 1
    run = state.prepared.run
    path = StepPath(run.run_id, (step_index,))
    requested = call.input.get("runnable")
    statement = RunStmt(
        binding="_",
        runnable=requested if isinstance(requested, str) else "",
        span=Span(line=1),
    )

    def validate() -> None:
        unknown = sorted(set(call.input) - {"runnable", "input"})
        if unknown:
            raise ValueError(f"unknown _too/run input fields: {', '.join(unknown)}")
        if not isinstance(requested, str) or not requested.strip():
            raise ValueError("_too/run requires a non-empty runnable ref")

    try:
        result = await run_step.execute(
            execution,
            binding=run,
            path=path,
            statement=statement,
            locals={},
            controls=(),
            occurrence=None,
            runnable=statement.runnable,
            validate=validate,
            resolution="state",
            raw_input=call.input.get("input", {}),
            inputs=_run_call_inputs(state, call),
            begin_step=state.start_step,
            authorize=lambda target: _authorize_run(state, target),
        )
        part = _run_success_part(execution, call, result)
        state.output = Pointer.step(path, "output", "value")
        state.record_output(state.output)
    except asyncio.CancelledError:
        raise
    except _StepFailed as exc:
        if not isinstance(exc.__cause__, _RunRejected | _ExecutionFailed):
            raise
        details = (
            exc.__cause__.details if isinstance(exc.__cause__, _RunRejected) else {}
        )
        error = str(exc) or type(exc).__name__
        part = ToolResultPart(
            tool_call_id=call.tool_call_id,
            call_id=call.call_id,
            tool_name=call.name,
            tool_family=call.name,
            output={"error": error, **details},
            error=error,
        )
    state.messages.append(Message(role="tool", parts=(part,)))
    state.last_step = step_index


def _authorize_run(state: _AgicState, target: ResolvedRunnable) -> None:
    if not state.prepared.routes.allows("run", target):
        raise ToolangError(f"runnable is not authorized by hands: {target.ref}")


async def _execute_transfer(
    execution: _Execution,
    state: _AgicState,
    call: ToolCall,
    *,
    tool_call_count: int,
) -> None:
    """Commit one authorized same-Run runnable replacement without a Step."""

    state.before_tool_call()
    requested = call.input.get("runnable")
    captured_state = state.prepared.run.state
    captured_ref = state.prepared.run.state_ref
    source = _runtime_call_source(state, call)
    try:
        if tool_call_count != 1:
            raise ToolangError(
                "_too/execute must be the only tool call in its Model Call"
            )
        unknown = sorted(set(call.input) - {"runnable", "input"})
        if unknown:
            raise ValueError(f"unknown _too/execute input fields: {', '.join(unknown)}")
        if not isinstance(requested, str) or not requested.strip():
            raise ValueError("_too/execute requires a non-empty runnable ref")
        name, kind = parse_runnable_ref(requested)
        target = resolve_public_runnable(captured_state, name, kind=kind)
        if not state.prepared.routes.allows("execute", target):
            raise ToolangError(f"runnable is not authorized by handoffs: {target.ref}")
        raw_input = call.input.get("input", {})
        input = execution.resolve_public_input(
            captured_state,
            target.module,
            target.name,
            target.executable,
            raw_input,
        )
        binding, locals = execution.prepare_execute(
            state.prepared.run,
            target,
            input,
            source=source,
            state=captured_state,
            state_ref=captured_ref,
        )
    except asyncio.CancelledError:
        raise
    except (_RunRejected, ToolangError, TypeError, ValueError) as exc:
        message = (str(exc) or type(exc).__name__)[:2048]
        details = exc.details if isinstance(exc, _RunRejected) else {}
        state.messages.append(
            Message(
                role="tool",
                parts=(
                    ToolResultPart(
                        tool_call_id=call.tool_call_id,
                        call_id=call.call_id,
                        tool_name=call.name,
                        tool_family=call.name,
                        output={"error": message, **details},
                        error=message,
                    ),
                ),
            )
        )
        return
    committed = execution.commit_execute(binding, source=source)
    raise _ExecuteCommitted(committed, target.executable, locals)


def _reject_runtime_tool(state: _AgicState, call: ToolCall) -> None:
    """Reject an unknown reserved runtime tool without creating a Tool Step."""

    state.before_tool_call()
    state.messages.append(
        Message(
            role="tool",
            parts=(
                ToolResultPart(
                    tool_call_id=call.tool_call_id,
                    call_id=call.call_id,
                    tool_name=call.name,
                    tool_family=call.name,
                    error=f"unknown inner runtime tool: {call.name}",
                ),
            ),
        )
    )


def _run_success_part(
    execution: _Execution,
    call: ToolCall,
    result: Local,
) -> ToolResultPart:
    record = result.record
    target = record.value if record is not None else None
    if (
        not isinstance(target, TypedPointer)
        or target.pointer.kind != "run"
        or target.pointer.tokens != ("output", "value")
    ):
        raise RuntimeError("Run Step result is missing its child run reference")
    child = execution.store.get_run(run_id=target.pointer.record)
    if child is None:
        raise RuntimeError(f"child run not found: {target.pointer.record}")
    control = execution.store.get_run_control(
        run_id=child.control.target,
        index=child.control.index,
    )
    if control is None or not isinstance(control.payload, RunControlPayload):
        raise RuntimeError(f"child run control not found: {child.id}")
    output_type = (
        record.type
        if record is not None
        else f"{result.type_name or 'Json'}[]"
        if result.shape == "list"
        else result.type_name or "Json"
    )
    encoded = local_to_protocol_data(
        RecordLocal.typed(
            output_type,
            result.value,
            dim=1 if result.shape == "list" else 0,
        )
    )["value"]
    return ToolResultPart(
        tool_call_id=call.tool_call_id,
        call_id=call.call_id,
        tool_name=call.name,
        tool_family=call.name,
        output={
            "run_id": child.id,
            "runnable": control.payload.runnable,
            "output_type": output_type,
            "output": encoded,
        },
    )


def _run_call_inputs(state: _AgicState, call: ToolCall) -> tuple[Pointer, ...]:
    source = state.tool_call_sources.get(call.tool_call_id)
    if source is not None:
        return (
            Pointer.step(
                StepPath(state.prepared.run.run_id, (source[0],)),
                "output",
                "value",
                source[1],
            ),
        )
    if state.last_step is not None:
        return (
            Pointer.step(
                StepPath(state.prepared.run.run_id, (state.last_step,)),
                "output",
                "value",
            ),
        )
    return state.initial_inputs


def _runtime_call_source(state: _AgicState, call: ToolCall) -> Pointer:
    """Return the authoritative Model ToolCall part for one runtime request."""

    source = state.tool_call_sources.get(call.tool_call_id)
    if source is None:
        raise RuntimeError(f"runtime ToolCall source is missing: {call.tool_call_id}")
    return Pointer.step(
        StepPath(state.prepared.run.run_id, (source[0],)),
        "output",
        "value",
        source[1],
    )


def _append_canceled_tool_results(
    state: _AgicState,
    calls: tuple[ToolCall, ...],
) -> None:
    """Complete skipped tool calls in model history without executing them."""

    state.next_model_inputs = tuple(
        Pointer.step(
            StepPath(state.prepared.run.run_id, (source[0],)),
            "output",
            "value",
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
