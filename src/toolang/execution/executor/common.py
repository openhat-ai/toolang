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
from toolang.lang.format import format_statement_head
from toolang.lang.types import Array
from toolang.state.state import AgentState
from toolang.setup import AgentSetup

from ..events import RunEvent, StepBegin, StepEnd
from ..records import RunControlRecord, SteerControlPayload, StopControlPayload
from ..runnables import resolve_runnable
from ..types import (
    AgentResources,
    Local as RecordLocal,
    StepKind,
    StepPath,
    Pointer,
    TypedPointer,
)

Shape = Literal["none", "item", "list"]
EventEmitter = Callable[[RunEvent], Awaitable[None]]
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
    control_index: int = 0
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
    ref: Pointer | None = None
    type_name: str | None = None
    record: RecordLocal | None = None


async def execute_step(
    emit: EventEmitter,
    *,
    kind: StepKind,
    path: StepPath,
    binding: BoundRun,
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
            *(Pointer.control(item.run, item.index, "_") for item in controls),
            *statement_input_refs(binding, statement, locals),
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
    except _ExecutionFailed as exc:
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
                    if result.shape == "list" and isinstance(result.value, Array | list)
                    else {}
                ),
            },
            finished_at=utc_now(),
        )
    )
    return replace(result, ref=Pointer.step(path))


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
                child = resolve_runnable(binding.state.program, child_name)
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
        return statement.scorer
    if isinstance(statement, KeepStmt | DropStmt):
        return statement.predicate
    return None


def initial_locals(
    binding: BoundRun, executable: AgicDecl | FlowDecl
) -> dict[str, Local]:
    """Build the initial locals for one executable run."""

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
    if executable.input is not None and binding.input.primary is not None:
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


def update_locals(locals: dict[str, Local], binding: str | None, result: Local) -> None:
    """Apply one statement result to its binding."""

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
    return {item.name: item for item in binding.state.program.structs}


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
    if not isinstance(control.payload, SteerControlPayload | StopControlPayload):
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
