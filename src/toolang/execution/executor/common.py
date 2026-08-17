"""Shared executor values and execution helpers."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
import json
from typing import Any, Literal, cast

from toolang.base.types.message import (
    AudioPart,
    DocumentPart,
    ImagePart,
    Message,
    MessagePart,
    Percept,
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
from toolang.lang.input import RunnableInput, coerce_input, validate_value
from toolang.lang.format import format_statement_head
from toolang.state.state import AgentState
from toolang.setup import AgentSetup

from ..events import RunEvent, StepBegin, StepEnd
from ..records import RunControlRecord, SteerControlPayload, StopControlPayload
from ..types import AgentResources, Local as RecordLocal, StepKind, StepPath, ValuePtr

Shape = Literal["none", "item", "list"]
EventEmitter = Callable[[RunEvent], Awaitable[None]]


class _StepFailed(Exception):
    """Carry one failed-step reference across enclosing execution layers."""

    def __init__(self, step: StepPath, cause: BaseException) -> None:
        super().__init__(str(cause) or type(cause).__name__)
        self.error = ValuePtr.step(step)


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
    setup: AgentSetup
    created_at: str
    limits: RunLimits = RunLimits()
    ceilings: tuple[AgentCeiling, ...] = ()
    agent_resources: AgentResources | None = None
    resources: AgentResources | None = None
    flow_resources: AgentResources | None = None
    call: Literal["top", "run"] = "top"
    parent: StepPath | None = None
    placement: Mapping[str, object] | None = None


@dataclass(frozen=True, slots=True)
class Local:
    """One runtime local and its flow shape."""

    value: Any = None
    shape: Shape = "none"
    ref: ValuePtr | None = None
    type_name: str | None = None
    record: RecordLocal | None = None


async def execute_step(
    emit: EventEmitter,
    *,
    kind: StepKind,
    path: StepPath,
    statement: FlowStmt,
    locals: Mapping[str, Local],
    controls: Sequence[RunControlRecord],
    placement: Mapping[str, object] | None,
    operation: Callable[[], Awaitable[Local]],
) -> Local:
    """Execute one statement step inside its canonical event boundary."""

    started_at = utc_now()
    inputs = _unique_step_inputs(
        (
            *(ValuePtr.control(item.run, item.index, "_") for item in controls),
            *(
                local.ref
                for _name, local in sorted(locals.items())
                if not isinstance(statement, LetStmt) and local.ref is not None
            ),
        )
    )
    await emit(
        StepBegin(
            step=path,
            kind=kind,
            input=inputs,
            placement=dict(placement) if placement is not None else None,
            given={
                **statement_context(statement),
                "binding": statement.binding,
                "source": {
                    "line": statement.span.line,
                    "head": format_statement_head(statement),
                },
            },
            started_at=started_at,
        )
    )
    try:
        result = await operation()
    except asyncio.CancelledError:
        await emit(
            StepEnd(
                step=path,
                kind=kind,
                status="canceled",
                finished_at=utc_now(),
            )
        )
        raise
    except _StepFailed as exc:
        await emit(
            StepEnd(
                step=path,
                kind=kind,
                status="failed",
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
                error=message,
                finished_at=utc_now(),
            )
        )
        raise _StepFailed(path, exc) from exc
    await emit(
        StepEnd(
            step=path,
            kind=kind,
            status="succeeded",
            output=record_local(result, name=statement.binding),
            noted={
                **(
                    {"reshape": reshape}
                    if (reshape := statement_reshape(statement)) is not None
                    else {}
                ),
                **(
                    {"items": len(result.value)}
                    if result.shape == "list" and isinstance(result.value, list)
                    else {}
                ),
            },
            finished_at=utc_now(),
        )
    )
    return replace(result, ref=ValuePtr.step(path))


def initial_locals(
    binding: BoundRun, executable: AgicDecl | FlowDecl
) -> dict[str, Local]:
    """Build the initial locals for one executable run."""

    structs = program_structs(binding)
    params = {parameter.name: parameter for parameter in executable.params}
    locals: dict[str, Local] = {}
    for name, value in binding.input.values.items():
        parameter = params.get(name)
        if parameter is None:
            continue
        validate_value(
            value,
            parameter.type_name or "Part[]",
            structs=structs,
            path=f"argument {name}",
        )
        locals[name] = Local(
            value,
            "item",
            ValuePtr.control(binding.run_id, 0, name),
            parameter.type_name or "Part[]",
        )
    if executable.input is not None:
        locals["_"] = Local(
            coerce_input(
                binding.input.primary,
                executable.input.type_name or "Part[]",
                structs=structs,
            ),
            "item",
            ValuePtr.control(binding.run_id, 0, "_"),
            executable.input.type_name or "Part[]",
        )
    else:
        locals.setdefault("_", Local())
    return locals


def update_locals(locals: dict[str, Local], binding: str | None, result: Local) -> None:
    """Apply one statement result to its binding."""

    if binding is not None:
        locals[binding] = result


def apply_steer(
    locals: dict[str, Local],
    controls: Sequence[RunControlRecord],
    *,
    input_type: str | None,
    structs: Mapping[str, StructDecl],
) -> None:
    """Apply accepted steer inputs to the primary local."""

    for control in controls:
        if isinstance(control.payload, SteerControlPayload):
            primary = next(
                (item for item in control.payload.locals if item.name == "_"), None
            )
            if (
                primary is None
                or isinstance(primary.value, ValuePtr)
                or not isinstance(primary.value, tuple | list)
            ):
                continue
            effective_type = input_type or "Part[]"
            locals["_"] = Local(
                coerce_input(
                    cast(Percept, tuple(primary.value)),
                    effective_type,
                    structs=structs,
                ),
                "item",
                ValuePtr.control(control.run, control.index, "_"),
                effective_type,
            )


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
        return statement.predicate is not None
    if isinstance(statement, RepeatStmt):
        return statement.until is not None or any(
            statement_has_call(child) for child in statement.stmts
        )
    return False


def statement_reshape(statement: FlowStmt) -> str | None:
    """Return the output reshape label for one statement."""

    if isinstance(statement, ScatterStmt):
        return "unfold"
    if isinstance(statement, GatherStmt):
        return "fold"
    if isinstance(statement, KeepStmt):
        return "keep"
    if isinstance(statement, DropStmt):
        return "drop"
    if isinstance(statement, RankStmt):
        return "rank"
    if isinstance(statement, StormStmt | MapStmt):
        return "list"
    return None


def statement_context(statement: FlowStmt) -> dict[str, object]:
    """Return durable statement metadata for one step."""

    context: dict[str, object] = {"statement": statement.kind}
    if statement.doc:
        context["doc"] = statement.doc
    for name in (
        "runnable",
        "agent",
        "count",
        "par",
        "position",
        "predicate",
        "scorer",
        "limit",
        "until",
    ):
        value = getattr(statement, name, None)
        if value is not None:
            context[name] = value
    return context


def require_item(locals: Mapping[str, Local], *, operation: str) -> Any:
    current = locals.get("_", Local())
    if current.shape != "item":
        raise ToolangError(
            f"{operation} requires current shape item, got {current.shape}"
        )
    return current.value


def require_list(locals: Mapping[str, Local], *, operation: str) -> list[Any]:
    current = locals.get("_", Local())
    if current.shape != "list" or not isinstance(current.value, list):
        raise ToolangError(
            f"{operation} requires current shape list, got {current.shape}"
        )
    return list(current.value)


def result_list(result: Local, *, operation: str) -> list[Any]:
    if isinstance(result.value, list | tuple):
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
    return {item.name: item for item in binding.state.program.structs}


def output_parts(local: Local) -> tuple[MessagePart, ...]:
    if local.shape == "none":
        return ()
    if (
        local.shape == "item"
        and (percept := value_percept(local.value, type_name=local.type_name))
        is not None
    ):
        return tuple(percept)
    return (TextPart(text=value_text(local.value)),)


def value_percept(
    value: object,
    *,
    type_name: str | None = None,
) -> Percept | None:
    """Return a canonical percept when one value already represents content."""

    if isinstance(value, Message):
        try:
            return value.percept
        except ValueError as exc:
            raise ToolangError(str(exc)) from exc
    if isinstance(value, (TextPart, ImagePart, AudioPart, DocumentPart)):
        return (value,)
    if isinstance(value, tuple | list):
        if not value:
            return () if type_name == "Part[]" else None
        if all(
            isinstance(part, (TextPart, ImagePart, AudioPart, DocumentPart))
            for part in value
        ):
            return cast(Percept, tuple(value))
    return None


def value_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, Message):
        return message_text(value.parts)
    if (percept := value_percept(value)) is not None:
        return message_text(percept)
    if isinstance(value, bool | int | float | list | dict | tuple):
        return json.dumps(
            json_value(value),
            ensure_ascii=False,
            separators=(",", ":"),
        )
    return str(value)


def control_text(control: RunControlRecord | None) -> str:
    if control is None:
        return ""
    if not isinstance(control.payload, SteerControlPayload | StopControlPayload):
        return ""
    primary = next((item for item in control.payload.locals if item.name == "_"), None)
    if primary is None or isinstance(primary.value, ValuePtr):
        return ""
    if isinstance(primary.value, str):
        return primary.value
    percept = value_percept(primary.value, type_name=primary.type)
    return message_text(percept) if percept is not None else ""


def _unique_step_inputs(items: Sequence[ValuePtr]) -> tuple[ValuePtr, ...]:
    result: list[ValuePtr] = []
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
    return RecordLocal(
        type=f"{item_type}[]" if local.shape == "list" else item_type,
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

    if isinstance(value, (TextPart, ImagePart, AudioPart, DocumentPart)):
        return value.to_data()
    if isinstance(value, Mapping):
        return {str(name): json_value(item) for name, item in value.items()}
    if isinstance(value, tuple | list):
        return [json_value(item) for item in value]
    return value
