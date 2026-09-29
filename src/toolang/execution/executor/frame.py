"""Build an agic execution frame from its bound resources and runtime facts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
import json
import logging
from typing import TYPE_CHECKING

from toolang.base.protocols.model import ModelAdapter, ModelOutputOptions
from toolang.base.protocols.tool import Tool
from toolang.base.types.message import (
    Message,
    message_text,
)
from toolang.base.types.model import Model, Reasoning, env_names
from toolang.base.types.tool import ToolService
from toolang.common.errors import ToolangError
from toolang.common.json import dumps
from toolang.lang.ast import AgicDecl
from toolang.lang.types import is_generated_ref
from toolang.plugin.models.query import resolve_model
from toolang.plugin.models.resolution import (
    model_reasoning_effort_applicable,
    resolve_model_reasoning,
)
from toolang.plugin.models.budget import (
    context_capacity,
    input_budget,
    output_budget,
)
from toolang.state.state import (
    StateCap,
)

from ..compaction import bound_latest_step, resolve_target
from ..assembly.history import HistorySelection
from ..assembly import prompting
from ..recall import recall_sources, history_variables
from ..tokens import InputEstimate, TokenCounter, text_tokens
from .common import BoundRun
from .resources import (
    available_workspaces,
    workspace_declarations,
    resource_caps,
    resource_tools,
    snapshot_model_selection,
)
from ..runnables import (
    AgicRoutes,
    parse_runnable_ref,
    runnable_descriptions,
    resolve_agic_routes,
)
from ..records import RecallControlPayload

if TYPE_CHECKING:
    from .executor import _Execution

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _AgicFrame:
    """Everything the agent loop needs after accepting one agic run."""

    run: BoundRun
    agic: AgicDecl
    model: Model
    adapter: ModelAdapter
    environ: Mapping[str, str]
    instructions: str
    inputs: prompting.PromptInputs
    tools: dict[str, Tool]
    routes: AgicRoutes
    services: tuple[ToolService, ...]
    declarations: tuple[RecallControlPayload, ...] = ()
    workspaces: tuple[RecallControlPayload, ...] = ()
    workspace_names: tuple[str, ...] = ()
    recall: tuple[str, ...] = ("far", "near")
    reasoning: Reasoning | None = None
    output_budget: int | None = None
    input_budget: int | None = None
    context_capacity: int | None = None
    input_overhead: int = 0
    thread_model: Model | None = None
    compact_recent: int | None = None
    compact_summary: int | None = None
    history: HistorySelection | None = None


def build_agic_frame(
    context: _Execution,
    run: BoundRun,
    agic: AgicDecl,
    *,
    variables: Mapping[str, object],
    far: str = "",
    near: Sequence[Message] = (),
    history: HistorySelection | None = None,
    estimate: InputEstimate | None = None,
) -> _AgicFrame:
    """Resolve the model-call resources and delegate prompt rendering."""

    name = agic.name
    if name is None:
        if run.bindings.runnable is None:
            raise RuntimeError(f"run runnable binding missing: {run.run_id}")
        name, _kind = parse_runnable_ref(run.bindings.runnable)
    resources = run.resources
    if resources is None:
        raise RuntimeError(f"run resources missing: {run.run_id}")
    model_keys = resources.models
    if not model_keys:
        raise ToolangError(f"run resources include no models: {name}")
    selection = snapshot_model_selection(run.setup)
    ref = run.model_request.ref if run.model_request is not None else run.bindings.model
    if ref is None:
        raise ToolangError(f"run requires a model: {name}")
    resolved_model = resolve_model(selection, ref)
    if resolved_model.ref not in model_keys:
        raise ToolangError(f"model ref is outside run resources: {ref}")
    request = run.model_request
    reasoning = (
        resolve_model_reasoning(resolved_model, request.reasoning)
        if request is not None
        else None
    )
    max_output = request.max_output if request is not None else None
    # A materialized request already includes inheritance and explicit auto resets.
    # Only legacy bindings without a request fall back to the setup default.
    default_request = run.setup.defaults.model
    if (
        request is None
        and default_request is not None
        and default_request.ref == resolved_model.ref
    ):
        if (
            reasoning is None
            and default_request.reasoning is not None
            and model_reasoning_effort_applicable(resolved_model)
        ):
            reasoning = resolve_model_reasoning(
                resolved_model,
                default_request.reasoning,
            )
        if max_output is None:
            max_output = default_request.max_output
    tools = dict(resource_tools(run.setup, resources))
    routes = resolve_agic_routes(
        run.state, agic, hands=run.settings.hands, handoffs=run.settings.handoffs
    )
    runtime_tools = (
        {}
        if is_generated_ref(name)
        else {
            name: tool
            for name, tool in run.setup.tools().runtime.items()
            if getattr(tool, "model_callable", True)
        }
    )
    tools.update(runtime_tools)
    caps = resource_caps(run.state, resources, module=run.module)
    services = tuple(item for item in caps if item.kind == "service")
    if runtime_tools and resolved_model.tool_call is True:
        active = context.active_runnable_identities(run)
        callable_routes = replace(
            routes,
            resolved=tuple(
                route
                for route in routes.resolved
                if route.runnable.qualified not in active
            ),
        )
        runnables = runnable_descriptions(run.state, callable_routes)
    else:
        runnables = ()
    route = resolved_model._toolang.route
    if not route.ready:
        raise ToolangError(f"model {resolved_model.ref!r} has no ready setup route")
    assert route.adapter is not None and route.env is not None
    adapter = run.setup.adapters().get(route.adapter)
    if adapter is None:
        raise ToolangError(f"unknown model adapter: {route.adapter}")
    environ = {
        name: run.setup.envs[name]
        for name in env_names(route.env)
        if name in run.setup.envs
    }
    authored_output = (
        adapter.output_allowance(route.options)
        if isinstance(adapter, ModelOutputOptions)
        else None
    )
    output_source = (
        "max_output"
        if max_output is not None
        else "adapter option"
        if authored_output is not None
        else "automatic"
    )
    try:
        output = output_budget(
            resolved_model.limit,
            demand=max_output if max_output is not None else authored_output,
            reasoning=reasoning,
        )
        admitted_input = input_budget(resolved_model.limit, output)
        thread_ref = context.thread_model_ref() if run.parent is not None else ref
        thread_model = (
            resolve_model(selection, thread_ref) if thread_ref else resolved_model
        )
        window = context_capacity(thread_model.limit)
        compact = run.setup.compact
        trigger = resolve_target(compact.trigger, window)
        recent = resolve_target(compact.recent, window)
        summary = resolve_target(compact.summary, window)
        if trigger is not None:
            if recent is not None and recent >= trigger:
                raise ValueError("compact.recent must be less than compact.trigger")
            admitted_input = (
                min(admitted_input, trigger) if admitted_input is not None else trigger
            )
    except ValueError as exc:
        raise ToolangError(
            f"model {resolved_model.ref} (output source: {output_source}): {exc}"
        ) from exc

    if estimate is not None:
        estimate.bind_model(resolved_model)
    counter = (
        estimate.counter if estimate is not None else TokenCounter(resolved_model.ref)
    )

    inputs = prompting.PromptInputs(
        run.state,
        run.setup,
        agic,
        module=run.module,
        runnable_name=name,
        model=resolved_model,
        caps=caps,
        facts={
            "date": context.date,
            "timezone": context.timezone,
            "run": {"id": run.run_id, "thread_id": run.thread},
            **history_variables(far, near, run.settings.recall),
        },
        values=variables,
        runnables=runnables,
        instruct=run.settings.instruct,
        context=run.settings.context,
    )
    if (
        history is not None
        and history.near
        and admitted_input is not None
        and "near" in recall_sources(run.settings.recall)
    ):
        without_near = replace(
            inputs,
            facts={
                **inputs.facts,
                **history_variables(far, (), run.settings.recall),
            },
        )
        uses_near = run.parent is None or (
            inputs.rendered_input[:2] != without_near.rendered_input[:2]
            or prompting.instructions(inputs)[0]
            != prompting.instructions(without_near)[0]
        )
        if uses_near:
            history = bound_latest_step(
                history,
                max(1, admitted_input // 2),
                counter.message,
            )
            inputs = replace(
                inputs,
                facts={
                    **inputs.facts,
                    **history_variables(history.far, history.near, run.settings.recall),
                },
            )
    instructions, declarations = prompting.instructions(inputs)
    _, _, prompt_invocations = inputs.rendered_input
    if prompt_invocations:
        context.record_prompt_invocations(run, prompt_invocations)
    prepared = _AgicFrame(
        run=run,
        agic=agic,
        model=resolved_model,
        adapter=adapter,
        environ=environ,
        instructions=instructions,
        inputs=inputs,
        declarations=declarations,
        tools=tools,
        routes=routes,
        services=_tool_services(services, context.setup.envs),
        workspaces=workspace_declarations(
            run.setup.workspace_grants(run.state.workspaces)
        ),
        workspace_names=available_workspaces(run.setup, run.state),
        recall=recall_sources(run.settings.recall),
        reasoning=reasoning,
        output_budget=output,
        input_budget=admitted_input,
        thread_model=thread_model,
        compact_recent=recent,
        compact_summary=summary,
        history=history,
        context_capacity=context_capacity(resolved_model.limit),
        input_overhead=text_tokens(dumps(route.options, indent=None))
        if route.options
        else 0,
    )
    _log_frame(prepared)
    return prepared


def _tool_services(
    entries: tuple[StateCap, ...], environ: Mapping[str, str]
) -> tuple[ToolService, ...]:
    result: list[ToolService] = []
    for entry in entries:
        raw = entry.meta.get("env")
        if isinstance(raw, str):
            names = tuple(name.strip() for name in raw.split(",") if name.strip())
        elif isinstance(raw, list | tuple):
            names = tuple(str(name).strip() for name in raw if str(name).strip())
        else:
            names = ()
        result.append(
            ToolService(
                name=entry.name,
                meta=entry.meta,
                environ={name: environ[name] for name in names if name in environ},
            )
        )
    return tuple(result)


def _log_frame(prepared: _AgicFrame) -> None:
    if not _LOGGER.isEnabledFor(logging.DEBUG):
        return
    run = prepared.run
    prompt_context, messages, _ = prepared.inputs.rendered_input
    _LOGGER.debug(
        "prompt.assembled thread=%s run=%s runnable=%s model=%s tools=%s context=%s input_budget=%s output_budget=%s",
        run.thread,
        run.run_id,
        prepared.inputs.runnable_name,
        prepared.model.ref,
        json.dumps(sorted(prepared.tools), ensure_ascii=False),
        prepared.context_capacity,
        prepared.input_budget,
        prepared.output_budget,
    )
    _LOGGER.debug(
        "prompt.instructions thread=%s run=%s text=%s",
        run.thread,
        run.run_id,
        prepared.instructions,
    )
    _LOGGER.debug(
        "prompt.context thread=%s run=%s text=%s",
        run.thread,
        run.run_id,
        prompt_context,
    )
    _LOGGER.debug(
        "prompt.messages thread=%s run=%s messages=%s",
        run.thread,
        run.run_id,
        json.dumps(
            [
                {"role": message.role, "text": message_text(message.parts)}
                for message in messages
            ],
            ensure_ascii=False,
        ),
    )
