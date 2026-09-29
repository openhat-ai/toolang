"""Execute the runtime compact tool; automatic selection belongs to preflight."""

from __future__ import annotations
from typing import TYPE_CHECKING, Any
from toolang.base.errors import ToolangError
from toolang.setup.models import select_compact_model
from toolang.plugin.models.query import resolve_model, subset_models
from toolang.plugin.models.resolution import resolve_model_reasoning
from toolang.base.types.model import ModelRequest, env_names
from ..compaction import permit
from ..executor.compact import CompactSpec, produce
from ..inspection.history import RunHistory
from ..records import CompactControlPayload
from ..assembly.tool_replies import control_summary
from ..types import RunRef, StepRef, ThreadRef

if TYPE_CHECKING:
    from ..executor.runs.agic import _AgicState


async def execute(state: _AgicState, step: StepRef) -> dict[str, Any]:
    from ..executor.executor import _setup_sandbox
    from ..executor.steps.model import compaction_boundary

    execution = state.execution
    if execution is None:
        raise RuntimeError("Agic runtime execution is unavailable")
    history = execution.message_history()
    target = ThreadRef.parse(history.thread)
    if str(target).startswith("compact_"):
        raise ToolangError("compact must target the calling Run's normal Thread")
    store = execution.store
    lock = store.db_path.with_name(f"{store.db_path.name}.{target}.compact.lock")
    async with permit(lock):
        execution.raise_if_canceling(step.run_id, call=True)
        # Admission may change while waiting (for example after a model reload).
        # Reuse preflight's decision without duplicating its budget policy here.
        end_ref = compaction_boundary(state)
        if end_ref is None:
            return {"controls": []}
        reader = RunHistory(store)
        output = reader.get_compaction(target)
        if output is not None and RunRef(output.result.end) not in history.roots:
            raise ToolangError(
                "published compact horizon is outside the calling Run's history"
            )
        if output is None or history.roots.index(
            RunRef(output.result.end)
        ) < history.roots.index(end_ref):
            frame = state.frame_for_step(*execution.state_snapshot())
            resources = frame.run.agent_resources
            if resources is None:
                raise RuntimeError(f"agent resources missing: {frame.run.run_id}")
            models = subset_models(frame.run.setup.models_effective(), resources.models)
            request = select_compact_model(models, frame.run.setup.compact_model)
            setup = frame.run.setup
            model = resolve_model(models, request.ref)
            request = ModelRequest(
                request.ref,
                resolve_model_reasoning(model, request.reasoning),
                request.max_output,
            )
            route = model._toolang.route
            if not route.ready or route.adapter is None or route.env is None:
                raise ToolangError(f"compact model route is not ready: {model.ref}")
            adapter = setup.adapters().get(route.adapter)
            if adapter is None:
                raise ToolangError(f"compact model adapter not found: {route.adapter}")
            environ = {
                key: setup.envs[key]
                for key in env_names(route.env)
                if key in setup.envs
            }
            spec = CompactSpec(
                target=target,
                roots=tuple(history.roots),
                begin=RunRef(output.result.end) if output else history.roots[0],
                end=end_ref,
                summary=output.result.summary if output else "",
                prior=output.ref if output else None,
                model=model,
                request=request,
                adapter=adapter,
                environ=environ,
                setup=setup.revision,
                state=frame.run.state.revision,
                sandbox=_setup_sandbox(setup),
                limits=frame.run.limits,
                size=4096,
                versions=store.history_versions(
                    [
                        str(root)
                        for root in history.roots[: history.roots.index(end_ref) + 1]
                    ]
                ),
            )
            try:
                output = await produce(
                    store, spec, issue_run=execution.executor.ids.issue_run
                )
            except (ValueError, TypeError) as exc:
                raise ToolangError(f"invalid compact summary: {exc}") from exc
        else:
            # A newer CLI result may already cover more than preflight requested.
            # Validate it against this Run's fixed history before adoption.
            reader.read_compaction(output.ref, target, history.roots)
        controls = execution.compact(step, output.ref)
        return {
            "controls": [
                control_summary(ref, CompactControlPayload(output.ref))
                for ref in controls
            ]
        }
