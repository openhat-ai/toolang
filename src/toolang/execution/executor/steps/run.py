"""Child-run statement steps."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Literal

from toolang.lang.ast import FlowStmt
from toolang.state.state import AgentState

from ...events import StepBegin
from ...records import RunControlRecord, StepPath
from ...types import ControlRef, Occurrence, Pointer
from ..common import BoundRun
from ..common import Local, StepBoundary, _RunRejected, execute_step

if TYPE_CHECKING:
    from ...runnables import ResolvedRunnable
    from ..executor import _Execution


async def execute(
    execution: _Execution,
    *,
    binding: BoundRun,
    path: StepPath,
    statement: FlowStmt,
    locals: Mapping[str, Local],
    controls: Sequence[RunControlRecord],
    occurrence: Occurrence | None,
    runnable: str,
    validate: Callable[[], None] | None = None,
    resolution: Literal["module", "state"] = "module",
    raw_input: object | None = None,
    inputs: Sequence[Pointer] | None = None,
    begin_step: StepBoundary | None = None,
    authorize: Callable[[ResolvedRunnable], None] | None = None,
) -> Local:
    """Evaluate one child-run Step and emit its event boundary."""

    captured: tuple[AgentState, ControlRef] | None = None
    boundary = begin_step or execution.begin_step

    async def capture_boundary(
        build: Callable[[AgentState, ControlRef], StepBegin],
    ) -> tuple[AgentState, ControlRef]:
        def capture(agent_state: AgentState, state_ref: ControlRef) -> StepBegin:
            nonlocal captured
            captured = agent_state, state_ref
            return build(agent_state, state_ref)

        return await boundary(capture)

    async def evaluate() -> Local:
        if captured is None:  # pragma: no cover - Step boundary invariant
            raise RuntimeError("Run Step did not capture Agent State")
        if validate is not None:
            try:
                validate()
            except (TypeError, ValueError) as exc:
                raise _RunRejected(str(exc) or type(exc).__name__) from exc
        if resolution == "module" and raw_input is None:
            return await execution.execute_child(
                binding,
                locals,
                path,
                runnable,
                occurrence,
                state_snapshot=captured,
            )
        return await execution.execute_child(
            binding,
            locals,
            path,
            runnable,
            occurrence,
            resolution=resolution,
            raw_input=raw_input,
            authorize=authorize,
            state_snapshot=captured,
        )

    return await execute_step(
        execution.emit,
        begin_step=capture_boundary,
        kind="run",
        path=path,
        binding=binding,
        statement=statement,
        locals=locals,
        controls=controls,
        occurrence=occurrence,
        evaluate=evaluate,
        inputs=inputs,
    )
