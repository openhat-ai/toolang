"""Literal let-statement semantics."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

from toolang.lang.ast import LetStmt
from toolang.lang.input import resolve_input_parts

from ...records import RunControlRecord, StepPath
from ...types import Occurrence
from ..common import BoundRun
from ..common import Local
from ..steps import value as value_step

if TYPE_CHECKING:
    from ..executor import _Execution


async def execute(
    execution: _Execution,
    binding: BoundRun,
    locals: Mapping[str, Local],
    path: StepPath,
    statement: LetStmt,
    controls: Sequence[RunControlRecord],
    occurrence: Occurrence | None,
) -> Local:
    async def evaluate() -> Local:
        return Local(
            resolve_input_parts(
                statement.value,
                program=binding.state.program,
                values={
                    name: local.value
                    for name, local in locals.items()
                    if local.shape != "none"
                },
                types={
                    name: local.type_name
                    for name, local in locals.items()
                    if local.type_name is not None
                },
            ),
            "item",
            type_name="Part[]",
        )

    return await value_step.execute(
        execution.emit,
        binding=binding,
        path=path,
        statement=statement,
        locals=locals,
        controls=controls,
        occurrence=occurrence,
        evaluate=evaluate,
    )
