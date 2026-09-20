"""Build an agic execution frame from its bound resources and runtime facts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
import json
import logging
from typing import TYPE_CHECKING

from toolang.base.protocols.model import ModelAdapter
from toolang.base.protocols.tool import Tool
from toolang.base.types.message import (
    Message,
    message_text,
)
from toolang.base.types.model import Model, ModelRoute, Reasoning
from toolang.base.types.tool import ToolService
from toolang.common.errors import ToolangError
from toolang.lang.ast import AgicDecl
from toolang.lang.types import is_generated_ref
from toolang.plugin.models.provider_resolver import model_route, trimmed_environ
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

from ..assembly import prompting
from ..recall import recall_sources
from .common import BoundRun
from .resources import (
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
    route: ModelRoute
    adapter: ModelAdapter
    environ: Mapping[str, str]
    instructions: str
    inputs: prompting.PromptInputs
    tools: dict[str, Tool]
    routes: AgicRoutes
    services: tuple[ToolService, ...]
    declarations: tuple[RecallControlPayload, ...] = ()
    workspaces: tuple[RecallControlPayload, ...] = ()
    recall: tuple[str, ...] = ("far", "near")
    reasoning: Reasoning | None = None
    output_budget: int | None = None
    input_budget: int | None = None
    context_capacity: int | None = None


def build_agic_frame(
    context: _Execution,
    run: BoundRun,
    agic: AgicDecl,
    *,
    variables: Mapping[str, object],
    far: str = "",
    near: Sequence[Message] = (),
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
    resolved_model = selection.resolve(ref)
    if resolved_model.ref not in model_keys:
        raise ToolangError(f"model ref is outside run resources: {ref}")
    request = run.model_request
    reasoning = (
        resolve_model_reasoning(resolved_model, request.reasoning)
        if request is not None
        else None
    )
    max_output = request.max_output if request is not None else None
    # A run that does not state a control inherits the configured default for
    # the same model. The default request is authored policy, not catalog data.
    default_request = run.setup.defaults.model
    if default_request is not None and default_request.ref == resolved_model.ref:
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
    routes = resolve_agic_routes(run.state, agic)
    runtime_tools = (
        {}
        if is_generated_ref(name)
        else {
            name: tool
            for name, tool in run.setup.tools.runtime.items()
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
    provider = run.setup.providers.get(resolved_model._toolang.provider)
    if provider is None:
        raise ToolangError(
            f"unknown model provider: {resolved_model._toolang.provider}"
        )
    route = model_route(
        provider,
        resolved_model,
        adapters=run.setup.adapters,
        environ=run.setup.envs,
    )
    adapter = run.setup.adapters.get(route.adapter)
    if adapter is None:
        raise ToolangError(f"unknown model adapter: {route.adapter}")
    environ = trimmed_environ(provider, environ=run.setup.envs)
    output = output_budget(resolved_model, demand=max_output, reasoning=reasoning)

    inputs = prompting.PromptInputs(
        run.state,
        run.setup,
        agic,
        module=run.module,
        runnable_name=name,
        model=resolved_model,
        route=route,
        caps=caps,
        facts={
            "date": context.date,
            "timezone": context.timezone,
            "run": {"id": run.run_id, "thread_id": run.thread},
            "far": far,
            "near": [message.to_data() for message in near],
        },
        values=variables,
        runnables=runnables,
    )
    instructions, declarations = prompting.instructions(inputs)
    _, _, prompt_invocations = inputs.rendered_input
    if prompt_invocations:
        context.record_prompt_invocations(run, prompt_invocations)
    prepared = _AgicFrame(
        run=run,
        agic=agic,
        model=resolved_model,
        route=route,
        adapter=adapter,
        environ=environ,
        instructions=instructions,
        inputs=inputs,
        declarations=declarations,
        tools=tools,
        routes=routes,
        services=_tool_services(services, context.setup.envs),
        workspaces=workspace_declarations(run.state.workspaces),
        recall=recall_sources(
            next((item.values for item in agic.directives if item.name == "recall"), ())
        ),
        reasoning=reasoning,
        output_budget=output,
        input_budget=input_budget(resolved_model),
        context_capacity=context_capacity(resolved_model),
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
        "prompt.assembled thread=%s run=%s runnable=%s model=%s tools=%s",
        run.thread,
        run.run_id,
        prepared.inputs.runnable_name,
        prepared.model.ref,
        json.dumps(sorted(prepared.tools), ensure_ascii=False),
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
