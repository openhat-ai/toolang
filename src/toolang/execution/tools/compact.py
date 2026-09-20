"""Execute the runtime compact tool; automatic selection belongs to preflight."""

from __future__ import annotations
from dataclasses import replace
from typing import TYPE_CHECKING, Any
from toolang.base.errors import ToolangError
from toolang.base.types.policy import RunBindings
from toolang.common.time import utc_now
from toolang.lang.input import RunnableInput
from toolang.setup.models import select_compact_model
from ..compaction import compact_state, compact_tools, execute_algorithm, permit
from ..inspection.history import RunHistory
from ..records import CompactControlPayload
from ..assembly.tool_replies import control_summary
from ..types import RunRef, StepRef, ThreadRef

if TYPE_CHECKING:
    from ..executor.runs.agic import _AgicState


async def execute(
    state: _AgicState, step: StepRef, thread: str, begin: str | None, end: str
) -> dict[str, Any]:
    from ..executor.executor import RunSpec
    from ..executor.steps.model import compaction_boundary

    target = ThreadRef.parse(thread)
    begin_ref = RunRef.parse(begin) if begin is not None else None
    end_ref = RunRef.parse(end)
    execution = state.execution
    if execution is None:
        raise RuntimeError("Agic runtime execution is unavailable")
    history = execution.message_history()
    if str(target) != history.thread or str(target).startswith("compact_"):
        raise ToolangError("compact must target the calling Run's normal Thread")
    if (
        not history.roots
        or begin_ref not in {None, history.roots[0]}
        or end_ref not in history.roots[1:]
    ):
        raise ToolangError(
            "compact must cover a nonempty prefix and retain a historical root"
        )
    store = execution.store
    lock = store.db_path.with_name(f"{store.db_path.name}.{target}.compact.lock")
    async with permit(lock):
        execution.raise_if_canceling(step.run_id, call=True)
        # Admission may change while waiting (for example after a model reload).
        # Reuse preflight's decision without duplicating its budget policy here.
        boundary = compaction_boundary(state)
        if boundary is None:
            return {"controls": []}
        if boundary != end_ref:
            raise ToolangError("compact range changed while waiting; retry required")
        reader = RunHistory(store)
        output = reader.get_compaction(target)
        if output is None or output.result.end != str(end_ref):
            reuse = (
                output is not None
                and RunRef(output.result.end) in history.roots
                and history.roots.index(RunRef(output.result.end))
                < history.roots.index(end_ref)
            )
            resolved: dict[str, str] = {
                "thread": str(target),
                "begin": str(history.roots[0]),
                "end": end,
            }
            if reuse:
                assert output is not None
                resolved["begin"] = output.result.end
            frame = state.frame_for_step(*execution.state_snapshot())
            resources = frame.run.agent_resources
            if resources is None:
                raise RuntimeError(f"agent resources missing: {frame.run.run_id}")
            models = frame.run.setup.models.subset(resources.models)
            request = select_compact_model(models, frame.run.setup.compact_model)
            compact_thread = f"compact_{target}"
            if store.get_thread(thread_id=compact_thread) is None:
                store.create_thread(
                    thread_id=compact_thread, origin="script", created_at=utc_now()
                )
            # This isolated program has only read-only history tools. In particular
            # it cannot reload into the human's State or transfer out of compact.
            setup = replace(frame.run.setup, models=models, tools=compact_tools())
            spec = RunSpec(
                setup=setup,
                state=compact_state(),
                thread=compact_thread,
                bindings=RunBindings(model=request.ref, runnable="agic:compact"),
                limits=frame.run.limits,
                model_request=request,
                input=RunnableInput(resolved),
            )
            previous = output.result if reuse and output is not None else None
            try:
                _producer, output = await execute_algorithm(
                    execution.executor, spec, roots=history.roots, previous=previous
                )
            except (ValueError, TypeError) as exc:
                raise ToolangError(f"invalid compact summary: {exc}") from exc
        controls = execution.compact(step, output.ref)
        return {
            "controls": [
                control_summary(ref, CompactControlPayload(output.ref))
                for ref in controls
            ]
        }
