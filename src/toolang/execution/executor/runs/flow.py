"""Flow run execution."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from typing import TYPE_CHECKING

from toolang.common.errors import ToolangError
from toolang.lang.ast import (
    FlowDecl,
    FlowStmt,
    RepeatStmt,
    MapStmt,
    KeepStmt,
    DropStmt,
    SortStmt,
    ReduceStmt,
)
from toolang.lang.input import coerce_output

from ...types import Occurrence, StepRef, TypedRef
from ..common import BoundRun
from ..common import (
    _MISSING,
    Local,
    bind_flow_result,
    program_structs,
)
from .. import stmts

if TYPE_CHECKING:
    from ..executor import _Execution


async def execute(
    execution: _Execution,
    binding: BoundRun,
    flow: FlowDecl,
    locals: dict[str, Local],
    *,
    statement_start: int = 0,
    step_start: int = 0,
) -> Local:
    """Execute one complete flow body."""

    await execute_statements(
        execution,
        binding,
        flow.stmts[statement_start:],
        locals,
        parent=None,
        start=step_start,
    )
    result = locals.get("_", Local())
    if flow.output is not None:
        if not result.has_value:
            raise ToolangError(f"flow output is missing; expected {flow.output}")
        source_type = result.type_name
        preserves_provenance = source_type == flow.output
        result = Local(
            coerce_output(
                result.value,
                flow.output,
                structs=program_structs(binding),
            ),
            result.ref if preserves_provenance else None,
            flow.output,
            result.stored if preserves_provenance else _MISSING,
        )
    return result


async def execute_statements(
    execution: _Execution,
    binding: BoundRun,
    statements: Sequence[FlowStmt],
    locals: dict[str, Local],
    *,
    parent: StepRef | None,
    start: int = 0,
    occurrence: Occurrence | None = None,
) -> int:
    """Execute Steps sequentially and bind each committed result between them."""

    index = start
    for statement in statements:
        path = (
            StepRef.from_local(binding.run_id, (index,))
            if parent is None
            else parent.child(index)
        )
        if isinstance(statement, (MapStmt, KeepStmt, DropStmt, SortStmt, ReduceStmt)):
            source = locals.get("_", Local())
            if source.ref is not None:
                locals["_"] = replace(
                    source,
                    ref=execution.store.resolve_value_pointer(
                        TypedRef(source.ref, source.type_name or "Json")
                    ),
                )
        result = await stmts.execute(
            execution,
            binding,
            locals if isinstance(statement, RepeatStmt) else dict(locals),
            path=path,
            statement=statement,
            controls=(),
            occurrence=occurrence,
        )
        bind_flow_result(locals, statement.binding, result)
        if statement.binding == "_" and result.ref is not None:
            execution.record_output(binding.run_id, result.ref)
        index += 1
    return index
