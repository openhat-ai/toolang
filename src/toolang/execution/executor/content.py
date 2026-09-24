"""Evaluate flow Content against the current runtime frame."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from toolang.lang.input import resolve_input_parts_with_provenance
from toolang.state.state import state_program

from ..calls import prompt_definitions
from ..types import StepRef
from .common import BoundRun, Local
from .resources import resource_caps

if TYPE_CHECKING:
    from .executor import _Execution


def evaluate_content(
    execution: _Execution,
    binding: BoundRun,
    locals: Mapping[str, Local],
    path: StepRef,
    content: str,
) -> Local:
    state, state_ref = execution.state_for_step(path)
    binding = execution.current_binding(binding, state, state_ref)
    program = state_program(state, binding.module)
    resources = binding.resources
    if resources is None:
        raise RuntimeError(f"run resources missing: {binding.run_id}")
    resolution = resolve_input_parts_with_provenance(
        content,
        program=program,
        values={
            name: local.value for name, local in locals.items() if local.shape != "none"
        }
        | execution.runtime_values(binding, step=path),
        types={
            name: local.type_name
            for name, local in locals.items()
            if local.type_name is not None
        },
        prompt_definitions=prompt_definitions(
            state,
            module=binding.module,
            program=program,
            caps=resource_caps(state, resources, module=binding.module),
        ),
    )
    execution.record_prompt_invocations(binding, resolution.prompts)
    return Local(resolution.parts, "item", type_name="Part[]")
