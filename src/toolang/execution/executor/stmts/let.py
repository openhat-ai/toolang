"""Literal let-statement semantics."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

from toolang.lang.ast import LetStmt
from toolang.lang.input import resolve_input_parts_with_provenance
from toolang.state.state import StatePublication, state_program

from ...calls import prompt_definitions
from ...records import ControlRecord, StepPath
from ...types import Occurrence
from ..common import BoundRun
from ..common import Local
from ..resources import resource_caps
from ..steps import value as value_step

if TYPE_CHECKING:
    from ..executor import _Execution


async def execute(
    execution: _Execution,
    binding: BoundRun,
    locals: Mapping[str, Local],
    path: StepPath,
    statement: LetStmt,
    controls: Sequence[ControlRecord],
    occurrence: Occurrence | None,
) -> Local:
    async def evaluate() -> Local:
        state, _state_ref = execution.state_for_step(path)
        program = state_program(state, binding.module)
        resources = binding.resources
        if resources is None:
            raise RuntimeError(f"run resources missing: {binding.run_id}")
        resolution = resolve_input_parts_with_provenance(
            statement.value,
            program=program,
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
            prompt_definitions=prompt_definitions(
                state,
                module=binding.module,
                program=program,
                caps=(
                    resource_caps(
                        state,
                        resources,
                        module=binding.module,
                    )
                    if isinstance(state, StatePublication)
                    else None
                ),
            ),
        )
        execution.record_prompt_invocations(binding, resolution.prompts)
        return Local(
            resolution.parts,
            "item",
            type_name="Part[]",
        )

    return await value_step.execute(
        execution.emit,
        begin_step=execution.begin_step,
        binding=binding,
        path=path,
        statement=statement,
        locals=locals,
        controls=controls,
        occurrence=occurrence,
        evaluate=evaluate,
    )
