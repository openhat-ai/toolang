"""Shared executor values and execution helpers."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Literal

from toolang.base.types.message import Message, Part, TextPart, message_text
from toolang.common.errors import ToolangError
from toolang.common.ids import allocate_run_id, allocate_thread_id
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
from toolang.state.state import AgentState
from toolang.up.setup import AgentSetup

from ..events import RunEvent, StepBegin, StepEnd
from ..records import RunControlRecord, RunControlRef
from .request import ExecutableKind, RunRequest
from ..types import StepKind, StepPath

Shape = Literal["none", "item", "list"]
EventEmitter = Callable[[RunEvent], None]


@dataclass(frozen=True, slots=True)
class BoundRun:
    """One request bound to immutable state, setup, and durable IDs."""

    run_id: str
    origin: str
    thread_id: str
    executable_kind: ExecutableKind
    executable_name: str | None
    input: Message
    input_text: str
    model_selector: str | None
    context: dict[str, Any]
    state: AgentState
    setup: AgentSetup
    created_at: str

    @property
    def invoke_params(self) -> dict[str, Any]:
        value = self.context.get("invoke_params")
        if not isinstance(value, dict):
            return {}
        return {str(key): item for key, item in value.items()}

    @property
    def job_context(self) -> dict[str, object] | None:
        value = self.context.get("job")
        if not isinstance(value, Mapping):
            return None
        return {str(key): item for key, item in value.items()}


def bind_run_request(
    request: RunRequest,
    *,
    id_state_path: Path,
    state: AgentState,
    setup: AgentSetup,
) -> BoundRun:
    """Bind one external request to immutable execution inputs."""

    thread_id = request.thread_id or _request_thread_id(id_state_path, request)
    return BoundRun(
        run_id=request.run_id or allocate_run_id(id_state_path),
        origin=request.origin,
        thread_id=thread_id,
        executable_kind=request.executable_kind,
        executable_name=request.executable_name,
        input=request.input,
        input_text=message_text(request.input.parts),
        model_selector=_request_model_selector(request),
        context=dict(request.context),
        state=state,
        setup=setup,
        created_at=utc_now(),
    )


def _request_thread_id(id_state_path: Path, request: RunRequest) -> str:
    return allocate_thread_id(id_state_path, request.origin)


def _request_model_selector(request: RunRequest) -> str | None:
    selector = request.model_selector
    if isinstance(selector, str) and selector.strip():
        return selector.strip()
    return None


@dataclass(frozen=True, slots=True)
class Local:
    """One runtime local and its flow shape."""

    value: Any = None
    shape: Shape = "none"


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
    inputs = tuple(RunControlRef(index=item.index) for item in controls)
    emit(
        StepBegin(
            step=path,
            kind=kind,
            input=tuple(inputs),
            context={
                **statement_context(statement),
                "binding": statement.binding,
                "reads": [] if isinstance(statement, LetStmt) else sorted(locals),
                "placement": dict(placement or {}),
                "source": {"line": statement.span.line},
            },
            started_at=started_at,
        )
    )
    try:
        result = await operation()
    except asyncio.CancelledError:
        emit(
            StepEnd(
                step=path,
                kind=kind,
                status="canceled",
                started_at=started_at,
                finished_at=utc_now(),
            )
        )
        raise
    except Exception as exc:
        emit(
            StepEnd(
                step=path,
                kind=kind,
                status="failed",
                error=str(exc) or type(exc).__name__,
                started_at=started_at,
                finished_at=utc_now(),
            )
        )
        raise
    emit(
        StepEnd(
            step=path,
            kind=kind,
            status="finished",
            output=output_parts(result),
            detail={
                "shape": result.shape,
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
            started_at=started_at,
            finished_at=utc_now(),
        )
    )
    return result


def initial_locals(
    binding: BoundRun, executable: AgicDecl | FlowDecl
) -> dict[str, Local]:
    """Build the initial locals for one executable run."""

    locals = {
        name: Local(value, "item") for name, value in binding.invoke_params.items()
    }
    if executable.input is not None:
        locals["_"] = Local(binding.input_text, "item")
    else:
        locals.setdefault("_", Local())
    return locals


def update_locals(locals: dict[str, Local], binding: str | None, result: Local) -> None:
    """Apply one statement result to its binding."""

    if binding is not None:
        locals[binding] = result


def apply_steer(locals: dict[str, Local], controls: Sequence[RunControlRecord]) -> None:
    """Apply accepted steer inputs to the primary local."""

    for control in controls:
        if control.input is not None:
            locals["_"] = Local(message_text(control.input.parts), "item")


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


def decode_agic_output(
    message: Message | None,
    output_type: str | None,
    *,
    structs: Mapping[str, StructDecl] | None = None,
) -> Any:
    text = message_text(message.parts) if message is not None else ""
    if output_type is None or output_type in {"Text", "Path"}:
        return text
    if output_type == "Part":
        parts = message.parts if message is not None else ()
        if len(parts) != 1:
            raise ToolangError(
                f"agic output is not a Part: expected 1 part, got {len(parts)}"
            )
        return parts[0].to_data()
    if output_type == "Part[]":
        return [part.to_data() for part in message.parts] if message else []
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ToolangError(
            f"agic output is not valid {output_type}: {exc.msg}"
        ) from exc
    validate_output(value, output_type, structs=structs or {})
    return value


def program_structs(binding: BoundRun) -> dict[str, StructDecl]:
    return {item.name: item for item in binding.state.program.structs}


def validate_output(
    value: Any,
    type_name: str,
    *,
    structs: Mapping[str, StructDecl],
    path: str = "output",
) -> None:
    if type_name.endswith("[]"):
        if not isinstance(value, list):
            raise ToolangError(f"{path} is not {type_name}")
        item_type = type_name[:-2]
        for index, item in enumerate(value):
            validate_output(item, item_type, structs=structs, path=f"{path}[{index}]")
        return

    if type_name in {"Text", "Path"}:
        valid = isinstance(value, str)
    elif type_name == "Number":
        valid = not isinstance(value, bool) and isinstance(value, int | float)
    elif type_name == "Boolean":
        valid = isinstance(value, bool)
    elif type_name == "Json":
        valid = is_json_value(value)
    elif type_name == "Part":
        valid = isinstance(value, Mapping) and isinstance(value.get("type"), str)
    elif type_name == "Artifact":
        valid = isinstance(value, Mapping) and is_json_value(value)
    elif struct := structs.get(type_name):
        if not isinstance(value, Mapping):
            valid = False
        else:
            fields = {field.name: field for field in struct.fields}
            unknown = set(value) - set(fields)
            missing = {
                name
                for name, field in fields.items()
                if not field.optional and name not in value
            }
            if unknown:
                names = ", ".join(sorted(str(name) for name in unknown))
                raise ToolangError(f"{path} has unknown {type_name} fields: {names}")
            if missing:
                names = ", ".join(sorted(missing))
                raise ToolangError(f"{path} is missing {type_name} fields: {names}")
            for name, item in value.items():
                validate_output(
                    item,
                    fields[str(name)].type_name,
                    structs=structs,
                    path=f"{path}.{name}",
                )
            return
    else:
        raise ToolangError(f"unknown output type: {type_name}")

    if not valid:
        raise ToolangError(f"{path} is not {type_name}")


def is_json_value(value: Any) -> bool:
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError):
        return False
    return True


def output_parts(local: Local) -> tuple[Part, ...]:
    if local.shape == "none":
        return ()
    return (TextPart(text=value_text(local.value)),)


def value_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, Message):
        return message_text(value.parts)
    if isinstance(value, bool | int | float | list | dict | tuple):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def control_text(control: RunControlRecord | None) -> str:
    if control is None or control.input is None:
        return ""
    return message_text(control.input.parts)
