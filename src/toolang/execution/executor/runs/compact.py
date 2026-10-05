"""Executor-owned compaction: admission and a recorded Step-level loop."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from toolang.base.errors import ModelResponseError, ToolangError
from toolang.base.types.message import Part, ToolResultPart
from toolang.base.types.model import ModelRequest, env_names
from toolang.base.types.policy import RunBindings
from toolang.base.types.run import ModelCallResult, ToolCall
from toolang.common.time import utc_now
from toolang.plugin.models.query import resolve_model, subset_models
from toolang.plugin.models.resolution import resolve_model_reasoning
from toolang.setup.models import select_compact_model
from ... import compaction
from ...assembly.tool_replies import control_summary
from ...assembly.history import render_history_unit
from ...events import PartBegin, PartEnd, StepBegin, StepEnd
from ...inspection.history import RunHistory
from ...records import CompactControlPayload
from ...types import (
    value_for_type,
    AgentResources,
    ErrorMessage,
    FieldRef,
    history_ref,
    history_root,
    history_position,
    ModelStepGiven,
    ModelStepNoted,
    Output,
    RunRef,
    StepRef,
    ThreadRef,
    ToolStepGiven,
)
from ..common import BoundRun, Local

if TYPE_CHECKING:
    from ..executor import _Execution
    from .agic import _AgicState


async def invoke(state: _AgicState, step: StepRef) -> dict[str, Any]:
    """Prepare the internal child and adopt its completed output under one permit."""
    from ..steps.model import (
        compaction_boundary,
        compaction_summary_fits,
    )

    execution = state.execution
    if execution is None:
        raise RuntimeError("Agic runtime execution is unavailable")
    store = execution.store
    owner = store.get_step(ref=step)
    if (
        owner is None
        or not isinstance(owner.given, ToolStepGiven)
        or owner.given.trigger != "runtime"
        or owner.given.call.name != "_toolang__compact"
    ):
        raise ToolangError("compact can only be initiated by model preflight")
    history = execution.message_history()
    target = ThreadRef.parse(history.thread)
    lock = store.db_path.with_name(f"{store.db_path.name}.{target}.compact.lock")
    async with compaction.permit(lock):
        execution.raise_if_canceling(step.run_id, call=True)
        end = compaction_boundary(state)
        if end is None:
            return {"controls": []}
        reader = RunHistory(store)
        output = reader.get_compaction(target)
        if output is not None and history_root(output.result.end) not in history.roots:
            raise ToolangError(
                "published compact horizon is outside the calling Run's history"
            )
        if output is None or history_position(
            output.result.end, history.roots
        ) < history_position(end, history.roots):
            frame = state.frame_for_model()
            parent = frame.run
            resources = parent.agent_resources
            if resources is None:
                raise RuntimeError(f"agent resources missing: {parent.run_id}")
            setup = parent.setup
            models = subset_models(setup.models_effective(), resources.models)
            request = select_compact_model(
                models,
                setup.compact.model,
                default=(frame.thread_model or frame.model).ref,
            )
            if frame.compact_summary is None:
                raise ToolangError(
                    "compact.summary percentage requires thread model limit.context"
                )
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
            selected_units = history.unit_refs(end)
            spec = compaction.CompactSpec(
                target=target,
                roots=tuple(history.roots),
                begin=history_ref(output.result.end) if output else history.roots[0],
                units=selected_units,
                end=end,
                summary=output.result.summary if output else "",
                prior=output.ref if output else None,
                model=model,
                request=request,
                adapter=adapter,
                environ={
                    k: setup.envs[k] for k in env_names(route.env) if k in setup.envs
                },
                setup=setup.revision,
                limits=parent.limits,
                size=frame.compact_summary,
                versions=store.history_versions(
                    [
                        str(r)
                        for r in history.roots[
                            : history.roots.index(history_root(end))
                            + isinstance(end, StepRef)
                        ]
                    ]
                ),
                summary_fits=lambda summary: compaction_summary_fits(
                    state, end, summary
                ),
            )
            saved = compaction.candidate(store, spec, step)
            if saved is not None and saved.status == "succeeded":
                reusable = reader.read_compaction(
                    RunRef(saved.id), target, history.roots
                )
                if not compaction_summary_fits(
                    state, history_ref(reusable.result.end), reusable.result.summary
                ):
                    saved = None
            if saved is None or saved.status != "succeeded":

                def prepare(agent_state, state_ref):
                    binding = replace(
                        parent,
                        run_id=saved.id
                        if saved
                        else execution.executor.ids.issue_run(),
                        bindings=RunBindings(
                            model=model.ref, runnable=compaction.RUNNABLE
                        ),
                        model_request=request,
                        input=spec.input(),
                        control_input=spec.input(),
                        state=agent_state,
                        state_ref=state_ref,
                        parent=step,
                        call="run",
                        occurrence=None,
                        control_index=0,
                        resources=AgentResources(models=(model.ref,)),
                        created_at=saved.created_at if saved else utc_now(),
                    )
                    return binding, spec

                binding, _ = await execution._begin_child(
                    prepare,
                    state_snapshot=(parent.state, parent.state_ref),
                    resume=saved,
                )
                await execution.execute(binding, spec, begun=True)
                ref = RunRef(binding.run_id)
            else:
                ref = RunRef(saved.id)
            output = reader.read_compaction(ref, target, history.roots)
        if not compaction_summary_fits(
            state, history_ref(output.result.end), output.result.summary
        ):
            raise compaction.SummaryTooLarge(
                "completed summary does not fit the calling model"
            )
        controls = execution.compact(step, output.ref)
        return {
            "controls": [
                control_summary(ref, CompactControlPayload(output.ref))
                for ref in controls
            ]
        }


async def execute(
    execution: _Execution, binding: BoundRun, spec: compaction.CompactSpec
) -> Local:
    """Run a bounded reducer, recording each checkpoint through normal events."""
    store = execution.store
    run = store.get_run(run_id=binding.run_id)
    assert run is not None
    cursor, summary = compaction.read_checkpoint(store, run)
    steps = store.list_steps(run_id=run.id)
    calls = sum(s.kind == "model" for s in steps)
    next_index = max((s.index for s in steps if s.parent is None), default=-1) + 1
    for step in steps:
        if step.status == "running":
            await execution.emit(
                StepEnd(
                    step=step.ref,
                    kind=step.kind,
                    status="canceled",
                    finished_at=utc_now(),
                )
            )
    cached_root: RunRef | None = None
    units: dict[RunRef | StepRef, compaction.HistoryUnit] = {}

    def load_unit(ref: RunRef | StepRef) -> compaction.HistoryUnit:
        nonlocal cached_root, units
        root = history_root(ref)
        if cached_root != root:
            record = store.get_run(run_id=str(root))
            if record is None:
                raise ValueError("compact source Run is missing")
            units = {
                u.ref: u for u in compaction.history_units(store, record, render=False)
            }
            cached_root = root
        return render_history_unit(units[ref], store.resolve_value)

    reducer = compaction.Compaction(
        spec.units[cursor : spec.units.index(spec.end)],
        load_unit,
        spec.model,
        size=spec.size,
        summary=summary,
        max_output_tokens=spec.request.max_output,
        reasoning=spec.request.reasoning,
        summary_fits=spec.summary_fits,
    )
    while (call := reducer.next_call()) is not None:
        execution.raise_if_canceling(run.id, call=True)
        if (
            spec.limits.agic_model_calls is not None
            and calls >= spec.limits.agic_model_calls
        ):
            raise ToolangError(
                f"compact model call limit exceeded: {spec.limits.agic_model_calls}"
            )
        calls += 1
        read = StepRef.from_local(run.id, (next_index,))
        model = StepRef.from_local(run.id, (next_index + 1,))
        next_index += 2
        roots = [str(unit.run_id) for unit in reducer.batch]
        refs = [str(unit.ref) for unit in reducer.batch]
        tool = ToolCall(
            str(read), str(read), compaction.READ_TOOL, {"roots": roots, "units": refs}
        )
        result = ToolResultPart(
            tool_call_id=tool.tool_call_id,
            call_id=tool.call_id,
            tool_name=compaction.READ_TOOL,
            tool_family=compaction.READ_TOOL,
            output={
                "roots": roots,
                "units": refs,
                "content": str(FieldRef.from_path(model, "given", "call", "messages")),
            },
        )
        try:
            await execution.emit(
                StepBegin(
                    step=read,
                    kind="tool",
                    given=ToolStepGiven("_toolang", tool, trigger="runtime"),
                    started_at=utc_now(),
                )
            )
            await _emit_part(execution, read, 0, result)
        except (Exception, asyncio.CancelledError) as exc:
            if store.get_step(ref=read) is not None:
                await execution.emit(
                    StepEnd(
                        step=read,
                        kind="tool",
                        status="canceled"
                        if isinstance(exc, asyncio.CancelledError)
                        else "failed",
                        error=ErrorMessage(str(exc)) if str(exc) else None,
                        finished_at=utc_now(),
                    )
                )
            raise
        await execution.emit(
            StepEnd(
                step=read,
                kind="tool",
                status="succeeded",
                output=Output(value_for_type("Part", result), "_"),
                finished_at=utc_now(),
            )
        )
        response: ModelCallResult | None = None
        try:
            await execution.emit(
                StepBegin(
                    step=model,
                    kind="model",
                    given=ModelStepGiven(
                        spec.model.ref,
                        call,
                        setup=spec.setup,
                        state=binding.state.revision,
                    ),
                    started_at=utc_now(),
                )
            )
            execution.require_model_pricing(spec.model)
            response = await spec.adapter.invoke(spec.model, call, environ=spec.environ)
            reducer.validate_summary(response)
            assert response.message is not None
            for index, part in enumerate(response.message.parts):
                await _emit_part(execution, model, index, part)
        except (Exception, asyncio.CancelledError) as exc:
            usage = (
                exc.usage
                if isinstance(exc, ModelResponseError)
                else response.usage
                if response
                else None
            )
            accounting = execution.model_accounting(spec.model, usage)
            if store.get_step(ref=model) is not None:
                await execution.emit(
                    StepEnd(
                        step=model,
                        kind="model",
                        status="canceled"
                        if isinstance(exc, asyncio.CancelledError)
                        else "failed",
                        noted=ModelStepNoted(accounting=accounting),
                        error=ErrorMessage(str(exc)) if str(exc) else None,
                        finished_at=utc_now(),
                    )
                )
            if usage is not None:
                execution.record_model_accounting(spec.model, accounting)
            if isinstance(exc, ModelResponseError):
                reducer.reject(exc)
                continue
            if isinstance(exc, compaction.SummaryTooLarge):
                reducer.retry_summary(exc)
                continue
            raise
        accounting = execution.model_accounting(spec.model, response.usage)
        assert response.message is not None
        try:
            await execution.emit(
                StepEnd(
                    step=model,
                    kind="model",
                    status="succeeded",
                    output=Output(
                        value_for_type("Part[]", tuple(response.message.parts)), "_"
                    ),
                    noted=ModelStepNoted(accounting=accounting),
                    finished_at=utc_now(),
                )
            )
        finally:
            execution.record_model_accounting(spec.model, accounting)
        reducer.accept(response)
    return Local(reducer.summary, type_name="Text")


async def _emit_part(
    execution: _Execution, step: StepRef, index: int, part: Part
) -> None:
    """Close a buffered Part even when its Begin observer is canceled."""
    try:
        await execution.emit(PartBegin(step=step, part=index, part_type=part.type))
    finally:
        await execution.emit(PartEnd(step=step, part=index, data=part))
