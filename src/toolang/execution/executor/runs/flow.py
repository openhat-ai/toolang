"""Flow run execution."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

from toolang.common.errors import ToolangError
from toolang.lang.ast import FlowDecl, FlowStmt, RepeatStmt
from toolang.lang.input import coerce_output

from ...types import StepPath
from ..common import BoundRun
from ..common import (
    Local,
    apply_steer,
    program_structs,
    statement_has_call,
    update_locals,
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
        if result.shape == "none":
            raise ToolangError(f"flow output is missing; expected {flow.output}")
        source_type = (
            result.record.type
            if result.record is not None
            else (
                f"{result.type_name}[]"
                if result.shape == "list" and result.type_name is not None
                else result.type_name
            )
        )
        preserves_provenance = source_type == flow.output
        result = Local(
            coerce_output(
                result.value,
                flow.output,
                structs=program_structs(binding),
            ),
            result.shape,
            result.ref if preserves_provenance else None,
            (
                flow.output[:-2]
                if result.shape == "list" and flow.output.endswith("[]")
                else flow.output
            ),
            result.record if preserves_provenance else None,
        )
    return result


async def execute_statements(
    execution: _Execution,
    binding: BoundRun,
    statements: Sequence[FlowStmt],
    locals: dict[str, Local],
    *,
    parent: StepPath | None,
    start: int = 0,
    placement: Mapping[str, object] | None = None,
) -> int:
    """Execute statements sequentially and update their shared locals."""

    index = start
    for statement in statements:
        execution.raise_if_stopping(
            binding.run_id,
            call=statement_has_call(statement),
        )
        controls = execution.steer_controls(binding.run_id, statement)
        apply_steer(
            locals,
            controls,
            input_type=locals.get("_", Local()).type_name,
            structs=program_structs(binding),
        )
        path = (
            StepPath(binding.run_id, (index,))
            if parent is None
            else parent.child(index)
        )
        result = await stmts.execute(
            execution,
            binding,
            locals if isinstance(statement, RepeatStmt) else dict(locals),
            path=path,
            statement=statement,
            controls=controls,
            placement=placement,
        )
        if not isinstance(statement, RepeatStmt):
            update_locals(locals, statement.binding, result)
            if statement.binding == "_" and result.ref is not None:
                execution.record_output(binding.run_id, result.ref)
        index += 1
    return index
