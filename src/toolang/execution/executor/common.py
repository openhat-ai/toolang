"""Shared executor values and execution helpers."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
import json
import re
from typing import Any, Literal, cast

from toolang.base.types.message import (
    Message,
    Part,
    TextPart,
    message_text,
)
from toolang.base.types.policy import AgentCeiling, RunBindings, RunLimits
from toolang.common.errors import ToolangError
from toolang.common.time import utc_now
from toolang.lang.ast import (
    AgicDecl,
    AskStmt,
    DropStmt,
    FlowDecl,
    FlowStmt,
    GatherStmt,
    KeepStmt,
    LetStmt,
    MapStmt,
    RankStmt,
    RepeatStmt,
    RunStmt,
    ScatterStmt,
    SeekStmt,
    SettleStmt,
    StormStmt,
    StructDecl,
)
from toolang.lang.input import RunnableInput
from toolang.lang.types import Array
from toolang.state.state import AgentState, state_program
from toolang.setup import AgentSetup

from ..events import RunEvent, StepBegin, StepEnd
from ..records import RunControlRecord, SteerControlPayload, CancelControlPayload
from ..runnables import resolve_runnable
from ..types import (
    AgentResources,
    CollectionStepNoted,
    ControlRef,
    Local as RecordLocal,
    Occurrence,
    StepKind,
    StepNoted,
    StepPath,
    StepStatus,
    Pointer,
    TypedPointer,
)

Shape = Literal["none", "item", "list"]
FlowTransform = Literal["item", "list", "filter", "sort", "none"]
EventEmitter = Callable[[RunEvent], Awaitable[None]]
StepBoundary = Callable[
    [Callable[[AgentState, ControlRef], StepBegin]],
    Awaitable[tuple[AgentState, ControlRef]],
]
_TEMPLATE_LOCAL_RE = re.compile(
    r"{{\s*(?:[#^/]\s*)?([A-Za-z_][A-Za-z0-9_]*)(?:\.[A-Za-z_][\w-]*)*\s*}}"
)


class _ExecutionFailed(Exception):
    """Carry one durable failure reference across execution layers."""

    def __init__(self, error: Pointer, cause: BaseException) -> None:
        super().__init__(str(cause) or type(cause).__name__)
        self.error = error


class _StepFailed(_ExecutionFailed):
    """Carry one failed-step reference across enclosing execution layers."""

    def __init__(self, step: StepPath, cause: BaseException) -> None:
        super().__init__(Pointer.step(step), cause)


class _RunRejected(Exception):
    """Carry one expected child-run request rejection into its Run Step."""

    def __init__(
        self,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.details = dict(details or {})


@dataclass(frozen=True, slots=True)
class BoundRun:
    """One accepted run bound to immutable execution inputs."""

    run_id: str
    root_run_id: str
    thread: str
    bindings: RunBindings
    input: RunnableInput
    control_locals: tuple[RecordLocal, ...]
    state: AgentState
    state_ref: ControlRef
    setup: AgentSetup
    created_at: str
    module: str = "agent"
    control_index: int = 0
    limits: RunLimits = RunLimits()
    ceilings: tuple[AgentCeiling, ...] = ()
    agent_resources: AgentResources | None = None
    resources: AgentResources | None = None
    flow_resources: AgentResources | None = None
    call: Literal["top", "run"] = "top"
    parent: StepPath | None = None
    occurrence: Occurrence | None = None


@dataclass(frozen=True, slots=True)
class Local:
    """One runtime local and its flow shape."""

    value: Any = None
    shape: Shape = "none"
    ref: Pointer | None = None
    type_name: str | None = None
    record: RecordLocal | None = None


class _ExecuteCommitted(Exception):
    """Transfer execution to a prepared replacement within the same Run."""

    def __init__(
        self,
        binding: BoundRun,
        runnable: AgicDecl | FlowDecl,
        locals: Mapping[str, Local],
    ) -> None:
        super().__init__(binding.bindings.runnable or runnable.name)
        self.binding = binding
        self.runnable = runnable
        self.locals = dict(locals)


async def execute_step(
    emit: EventEmitter,
    *,
    begin_step: StepBoundary | None = None,
    kind: StepKind,
    path: StepPath,
    binding: BoundRun,
    statement: FlowStmt,
    locals: Mapping[str, Local],
    controls: Sequence[RunControlRecord],
    occurrence: Occurrence | None,
    evaluate: Callable[[], Awaitable[Local]],
    note: Callable[[StepStatus], StepNoted] | None = None,
    inputs: Sequence[Pointer] | None = None,
) -> Local:
    """Evaluate, transform, and commit one Flow statement Step."""

    started_at = utc_now()

    def build(agent_state: AgentState, state_ref: ControlRef) -> StepBegin:
        effective_binding = replace(
            binding,
            state=agent_state,
            state_ref=state_ref,
        )
        step_inputs = _unique_step_inputs(
            (
                *(Pointer.control(item.run, item.index, "_") for item in controls),
                *(
                    inputs
                    if inputs is not None
                    else statement_input_refs(effective_binding, statement, locals)
                ),
            )
        )
        return StepBegin(
            step=path,
            kind=kind,
            state=state_ref,
            input=step_inputs,
            occurrence=occurrence,
            given=statement,
            started_at=started_at,
        )

    if begin_step is None:
        await emit(build(binding.state, binding.state_ref))
    else:
        await begin_step(build)
    try:
        evaluated = await evaluate()
        result = transform_flow_result(statement, locals, evaluated)
    except asyncio.CancelledError:
        await emit(
            StepEnd(
                step=path,
                kind=kind,
                status="canceled",
                noted=_flow_step_noted(
                    statement,
                    locals,
                    status="canceled",
                    result=None,
                    note=note,
                ),
                finished_at=utc_now(),
            )
        )
        raise
    except _ExecutionFailed as exc:
        await emit(
            StepEnd(
                step=path,
                kind=kind,
                status="failed",
                noted=_flow_step_noted(
                    statement,
                    locals,
                    status="failed",
                    result=None,
                    note=note,
                ),
                error=exc.error,
                finished_at=utc_now(),
            )
        )
        raise _StepFailed(path, exc) from exc
    except Exception as exc:
        message = str(exc) or type(exc).__name__
        await emit(
            StepEnd(
                step=path,
                kind=kind,
                status="failed",
                noted=_flow_step_noted(
                    statement,
                    locals,
                    status="failed",
                    result=None,
                    note=note,
                ),
                error=message,
                finished_at=utc_now(),
            )
        )
        raise _StepFailed(path, exc) from exc
    output = record_local(result, name=statement.binding)
    await emit(
        StepEnd(
            step=path,
            kind=kind,
            status="succeeded",
            output=output,
            noted=_flow_step_noted(
                statement,
                locals,
                status="succeeded",
                result=result,
                note=note,
            ),
            finished_at=utc_now(),
        )
    )
    return replace(result, ref=Pointer.step(path)) if output is not None else result


def _flow_step_noted(
    statement: FlowStmt,
    locals: Mapping[str, Local],
    *,
    status: StepStatus,
    result: Local | None,
    note: Callable[[StepStatus], StepNoted] | None,
) -> StepNoted:
    if note is not None:
        return note(status)
    if not isinstance(statement, StormStmt | MapStmt | KeepStmt | DropStmt | RankStmt):
        return None
    if isinstance(statement, StormStmt):
        total_items = statement.count
    else:
        source = locals.get("_", Local())
        if source.shape != "list" or not isinstance(source.value, Array | list):
            return None
        total_items = len(source.value)
    output_items = None
    if result is not None and isinstance(result.value, Array | list | tuple):
        output_items = len(result.value)
    return CollectionStepNoted(total_items, output_items)


def transform_flow_result(
    statement: FlowStmt,
    locals: Mapping[str, Local],
    evaluated: Local,
) -> Local:
    """Transform one evaluated value into the statement result."""

    transform = flow_transform(statement)
    if transform == "none":
        return Local()
    if transform == "item":
        output_type = (
            evaluated.record.type
            if evaluated.record is not None
            else (
                f"{evaluated.type_name or 'Json'}[]"
                if evaluated.shape == "list"
                else evaluated.type_name
            )
        )
        return replace(
            evaluated,
            shape="item",
            type_name=output_type,
            record=(
                replace(evaluated.record, dim=0)
                if evaluated.record is not None
                else None
            ),
        )
    if transform == "list":
        values = (
            evaluated.value
            if evaluated.shape == "list"
            else result_list(evaluated, operation=statement.kind)
        )
        output_type = (
            evaluated.record.type
            if evaluated.record is not None
            else (
                f"{evaluated.type_name or 'Json'}[]"
                if evaluated.shape == "list"
                else evaluated.type_name or "Json[]"
            )
        )
        if not output_type.endswith("[]"):
            raise ToolangError(
                f"{statement.kind} requires an array result, got {output_type}"
            )
        return Local(
            values,
            "list",
            type_name=output_type[:-2],
            record=(
                replace(evaluated.record, dim=1)
                if evaluated.record is not None
                else (
                    RecordLocal.typed(
                        type_name=output_type,
                        value=evaluated.ref,
                        dim=1,
                    )
                    if evaluated.ref is not None
                    else None
                )
            ),
        )
    source = locals.get("_", Local())
    items = require_list(locals, operation=statement.kind)
    values = result_list(evaluated, operation=statement.kind)
    if len(values) != len(items):
        value_kind = "decisions" if transform == "filter" else "scores"
        raise ToolangError(
            f"{statement.kind} produced {len(values)} {value_kind} "
            f"for {len(items)} items"
        )
    if transform == "filter":
        matches = [boolean(value, operation=statement.kind) for value in values]
        keep_matches = isinstance(statement, KeepStmt)
        indexes = [
            index for index, matched in enumerate(matches) if matched is keep_matches
        ]
    else:
        scores = [number(value, operation="rank") for value in values]
        entries = sorted(
            zip(scores, range(len(items)), strict=True),
            key=lambda entry: (-entry[0], entry[1]),
        )
        if not isinstance(statement, RankStmt):
            raise TypeError("sort transform requires RankStmt")
        if statement.selection == "top":
            entries = entries[: statement.limit or 0]
        elif statement.selection == "bottom":
            limit = statement.limit or 0
            entries = entries[-limit:] if limit else []
        indexes = [index for _, index in entries]
    return Local(
        [items[index] for index in indexes],
        "list",
        type_name=source.type_name,
        record=(
            RecordLocal.typed(
                type_name=f"{source.type_name or 'Json'}[]",
                value=tuple(source.ref.select(index) for index in indexes),
                dim=1,
            )
            if source.ref is not None
            else None
        ),
    )


def flow_transform(statement: FlowStmt) -> FlowTransform:
    """Return the result transform implied by one lowered statement."""

    if isinstance(statement, RepeatStmt):
        return "none"
    if isinstance(statement, ScatterStmt | StormStmt | MapStmt):
        return "list"
    if isinstance(statement, KeepStmt | DropStmt):
        return "filter"
    if isinstance(statement, RankStmt):
        return "sort"
    return "item"


def statement_input_refs(
    binding: BoundRun,
    statement: FlowStmt,
    locals: Mapping[str, Local],
) -> tuple[Pointer, ...]:
    """Return the durable locals directly read by one flow statement."""

    names: set[str] = set()
    if isinstance(statement, LetStmt):
        names.update(
            match.group(1) for match in _TEMPLATE_LOCAL_RE.finditer(statement.value)
        )
    elif isinstance(statement, RepeatStmt | AskStmt | SeekStmt):
        pass
    else:
        child_name = _statement_child_runnable(statement)
        if child_name is not None:
            try:
                child = resolve_runnable(
                    state_program(binding.state, binding.module),
                    child_name,
                )
            except (ToolangError, ValueError):
                child = None
            if child is not None:
                if child.input is not None:
                    names.add("_")
                names.update(parameter.name for parameter in child.params)
        if isinstance(
            statement,
            KeepStmt
            | DropStmt
            | GatherStmt
            | SettleStmt
            | MapStmt
            | RankStmt
            | StormStmt,
        ):
            names.add("_")
    return tuple(
        local.ref
        for name, local in sorted(locals.items())
        if name in names and local.ref is not None
    )


def _statement_child_runnable(statement: FlowStmt) -> str | None:
    if isinstance(
        statement,
        RunStmt | ScatterStmt | GatherStmt | SettleStmt | MapStmt | StormStmt,
    ):
        return statement.runnable
    if isinstance(statement, RankStmt):
        return statement.runnable
    if isinstance(statement, KeepStmt | DropStmt):
        return statement.runnable
    return None


def initial_locals(
    binding: BoundRun, runnable: AgicDecl | FlowDecl
) -> dict[str, Local]:
    """Build the initial locals for one runnable run."""

    records = {
        local.name: local for local in binding.control_locals if local.name is not None
    }
    locals: dict[str, Local] = {}
    for name, value in binding.input.named.items():
        record = records.get(name)
        if record is None:
            continue
        pointer = Pointer.control(binding.run_id, binding.control_index, name)
        locals[name] = Local(
            value,
            "item",
            pointer,
            record.type,
            RecordLocal.typed(record.type, pointer, name, record.dim),
        )
    if runnable.input is not None and binding.input.primary is not None:
        record = records.get("_")
        if record is None:
            raise RuntimeError(f"run primary control local missing: {binding.run_id}")
        pointer = Pointer.control(binding.run_id, binding.control_index, "_")
        locals["_"] = Local(
            binding.input.primary,
            "item",
            pointer,
            record.type,
            RecordLocal.typed(record.type, pointer, "_", record.dim),
        )
    else:
        locals.setdefault("_", Local())
    return locals


def bind_flow_result(
    locals: dict[str, Local],
    binding: str | None,
    result: Local,
) -> None:
    """Bind one committed Step result between Flow Steps."""

    if binding is not None:
        locals[binding] = result


def statement_has_call(statement: FlowStmt) -> bool:
    """Return whether one statement reaches an external execution checkpoint."""

    if isinstance(
        statement,
        RunStmt
        | SeekStmt
        | AskStmt
        | ScatterStmt
        | StormStmt
        | GatherStmt
        | SettleStmt
        | MapStmt
        | RankStmt,
    ):
        return True
    if isinstance(statement, KeepStmt | DropStmt):
        return statement.runnable is not None
    if isinstance(statement, RepeatStmt):
        return statement.runnable is not None or any(
            statement_has_call(child) for child in statement.stmts
        )
    return False


def require_item(locals: Mapping[str, Local], *, operation: str) -> Any:
    current = locals.get("_", Local())
    if current.shape != "item":
        raise ToolangError(
            f"{operation} requires current shape item, got {current.shape}"
        )
    return current.value


def require_list(locals: Mapping[str, Local], *, operation: str) -> list[Any]:
    current = locals.get("_", Local())
    if current.shape != "list" or not isinstance(current.value, Array | list):
        raise ToolangError(
            f"{operation} requires current shape list, got {current.shape}"
        )
    return list(current.value)


def result_list(result: Local, *, operation: str) -> list[Any]:
    if isinstance(result.value, Array | list | tuple):
        return list(result.value)
    raise ToolangError(f"{operation} requires a list result")


def boolean(value: Any, *, operation: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in {"true", "false"}:
        return value.strip().lower() == "true"
    raise ToolangError(f"{operation} requires a Boolean result")


def number(value: Any, *, operation: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ToolangError(f"{operation} requires a Number result")
    return float(value)


def program_structs(binding: BoundRun) -> dict[str, StructDecl]:
    return {
        item.name: item for item in state_program(binding.state, binding.module).structs
    }


def output_parts(local: Local) -> tuple[Part, ...]:
    if local.shape == "none":
        return ()
    if (
        local.shape == "item"
        and (parts := value_parts(local.value, type_name=local.type_name)) is not None
    ):
        return tuple(parts)
    return (TextPart(text=value_text(local.value)),)


def value_parts(
    value: object,
    *,
    type_name: str | None = None,
) -> tuple[Part, ...] | None:
    """Return canonical parts when one value already represents content."""

    if isinstance(value, Message):
        return value.parts
    if isinstance(value, Part):
        return (value,)
    if isinstance(value, Array | tuple | list):
        if not value:
            return () if type_name == "Part[]" else None
        if all(isinstance(part, Part) for part in value):
            return cast(tuple[Part, ...], tuple(value))
    return None


def value_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, Message):
        return message_text(value.parts)
    if (parts := value_parts(value)) is not None:
        return message_text(parts)
    if isinstance(value, bool | int | float | Array | list | dict | tuple):
        return json.dumps(
            json_value(value),
            ensure_ascii=False,
            separators=(",", ":"),
        )
    return str(value)


def control_text(control: RunControlRecord | None) -> str:
    if control is None:
        return ""
    if not isinstance(control.payload, SteerControlPayload | CancelControlPayload):
        return ""
    primary = next((item for item in control.payload.locals if item.name == "_"), None)
    if primary is None or isinstance(primary.value, TypedPointer):
        return ""
    if isinstance(primary.value, str):
        return primary.value
    parts = value_parts(primary.value, type_name=primary.type)
    return message_text(parts) if parts is not None else ""


def _unique_step_inputs(items: Sequence[Pointer]) -> tuple[Pointer, ...]:
    result: list[Pointer] = []
    for item in items:
        if item not in result:
            result.append(item)
    return tuple(result)


def record_local(local: Local, *, name: str | None) -> RecordLocal | None:
    """Convert one runtime local into its durable output representation."""

    if local.shape == "none":
        return None
    if local.record is not None:
        return replace(local.record, name=name)
    item_type = local.type_name or "Json"
    return RecordLocal.typed(
        type_name=f"{item_type}[]" if local.shape == "list" else item_type,
        value=(
            tuple(local.value)
            if local.shape == "list" and isinstance(local.value, list)
            else local.value
        ),
        name=name,
        dim=1 if local.shape == "list" else 0,
    )


def json_value(value: object) -> object:
    """Return a JSON-compatible representation of one runtime value."""

    if isinstance(value, Part):
        return value.to_data()
    if isinstance(value, Mapping):
        return {str(name): json_value(item) for name, item in value.items()}
    if isinstance(value, Array | tuple | list):
        return [json_value(item) for item in value]
    return value
