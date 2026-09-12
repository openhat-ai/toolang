"""Build an agic execution frame from its bound resources and runtime facts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import json
import logging
from typing import TYPE_CHECKING

from toolang.base.protocols.model import ModelAdapter
from toolang.base.protocols.tool import Tool
from toolang.base.types.message import (
    Message,
    Part,
    TextPart,
    message_text,
)
from toolang.base.types.model import ModelTarget
from toolang.base.types.tool import ToolService
from toolang.common.errors import ToolangError
from toolang.common.immutable import mutable_data
from toolang.common.version import toolang_version
from toolang.lang.ast import AgicDecl
from toolang.plugin.models.resolution import (
    apply_model_parameters,
)
from toolang.plugin.models.budget import input_budget, output_budget
from toolang.state import state as cap_store
from toolang.state.state import (
    StateCap,
    state_program,
    state_program_source,
)

from ..assembly.messages import recall_sources
from ..calls import prompt_definitions
from .common import BoundRun, value_parts, value_text
from ..assembly.prompting import (
    initial_messages,
    render_context,
    render_instructions,
    render_messages,
    render_tool_instructions,
)
from .resources import resource_caps, resource_tools
from .resources import snapshot_model_selection
from ..runnables import AgicRoutes, render_runnable_instructions, resolve_agic_routes

if TYPE_CHECKING:
    from .executor import _Execution

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _AgicFrame:
    """Everything the agent loop needs after accepting one agic run."""

    run: BoundRun
    agic: AgicDecl
    model: ModelTarget
    adapter: ModelAdapter
    instructions: str
    prompt_context: str
    messages: tuple[Message, ...]
    tools: dict[str, Tool]
    routes: AgicRoutes
    services: tuple[ToolService, ...]
    tool_instructions: str = ""
    recall: tuple[str, ...] = ("far", "near")
    far: str = ""
    near: tuple[Message, ...] = ()
    output_budget: int = 4096
    input_budget: int | None = None


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

    resources = run.resources
    if resources is None:
        raise RuntimeError(f"run resources missing: {run.run_id}")
    model_keys = resources.models
    if not model_keys:
        raise ToolangError(f"run resources include no models: {agic.name}")
    selection = snapshot_model_selection(run.setup)
    ref = run.model_request.ref if run.model_request is not None else run.bindings.model
    if ref is None:
        raise ToolangError(f"run requires a model: {agic.name}")
    entry = selection.resolve(ref)
    if entry.key not in model_keys:
        raise ToolangError(f"model ref is outside run resources: {ref}")
    model = entry.target
    if run.model_request is not None:
        model = apply_model_parameters(
            selection,
            model,
            run.model_request.parameters,
        )
    tools = dict(resource_tools(run.setup, resources))
    routes = resolve_agic_routes(run.state, agic)
    runtime_tools = (
        {}
        if agic.name.startswith("<agic:")
        else {
            name: tool
            for name, tool in run.setup.tools.runtime.items()
            if getattr(tool, "model_callable", True)
        }
    )
    tools.update(runtime_tools)
    caps = resource_caps(run.state, resources, module=run.module)
    program = state_program(run.state, run.module)
    psyches = tuple(item for item in caps if item.kind == "psyche")
    skills = tuple(item for item in caps if item.kind == "skill")
    services = tuple(item for item in caps if item.kind == "service")

    body_variables = _body_variables(agic, variables)
    body_types = _body_types(agic)
    history_values = {"far": far, "near": [message.to_data() for message in near]}
    body_variables.update(history_values)
    body_types.update({"far": "Text", "near": "Json"})
    system_runtime = _runtime_context(context, run=run, agic=agic)
    system_runtime.update(history_values)
    system_runtime.update(
        {
            "model": _model_context(model),
            "psyches": [_cap_context(context, item) for item in psyches],
            "has_psyches": bool(psyches),
            "skills": [_cap_context(context, item) for item in skills],
            "has_skills": bool(skills),
            "services": [_cap_context(context, item) for item in services],
            "has_services": bool(services),
        }
    )
    rendered, prompt_invocations = render_messages(
        program,
        agic.messages,
        values=body_variables,
        types=body_types,
        definitions=prompt_definitions(
            run.state,
            module=run.module,
            program=program,
            caps=caps,
        ),
    )
    if prompt_invocations:
        context.record_prompt_invocations(run, prompt_invocations)
    prompt_context = render_context(program, agic, system_runtime)
    messages = initial_messages(
        agic=agic,
        rendered=rendered,
        prompt_context=prompt_context,
        primary=_primary_parts(agic, variables),
    )
    instructions = render_instructions(program, agic, system_runtime)
    tool_instructions = render_tool_instructions(
        render_runnable_instructions(run.state, routes) if runtime_tools else "",
        filesystem=any(
            getattr(tool, "plugin_name", None) == "fs" for tool in tools.values()
        ),
    )
    adapter = run.setup.adapters.get(model.adapter)
    if adapter is None:
        raise ToolangError(f"unknown model adapter: {model.adapter}")
    output = output_budget(model, entry.info)
    prepared = _AgicFrame(
        run=run,
        agic=agic,
        model=model,
        adapter=adapter,
        instructions=instructions,
        tool_instructions=tool_instructions,
        prompt_context=prompt_context,
        messages=messages,
        tools=tools,
        routes=routes,
        services=_tool_services(services, context.setup.envs),
        recall=recall_sources(
            next((item.values for item in agic.directives if item.name == "recall"), ())
        ),
        far=far,
        near=tuple(near),
        output_budget=output,
        input_budget=input_budget(entry.info, output),
    )
    _log_frame(prepared)
    return prepared


def _body_variables(
    agic: AgicDecl,
    source: Mapping[str, object],
) -> dict[str, object]:
    values: dict[str, object] = {}
    if agic.input is not None:
        values["_"] = source.get("_")
    for param in agic.params:
        values[param.name] = source.get(param.name)
    return values


def _body_types(agic: AgicDecl) -> dict[str, str]:
    return {
        **({"_": agic.input.type_name or "Part[]"} if agic.input is not None else {}),
        **{
            parameter.name: parameter.type_name or "Part[]" for parameter in agic.params
        },
    }


def _runtime_context(
    context: _Execution, *, run: BoundRun, agic: AgicDecl
) -> dict[str, object]:
    runtime: dict[str, object] = {
        "date": context.date,
        "timezone": context.timezone,
        "toolang": {"version": toolang_version()},
        "run": {
            "id": run.run_id,
            "thread_id": run.thread,
            "program_source": state_program_source(
                run.state,
                run.module,
            ),
        },
        "agent": {
            "name": run.setup.layout.name,
            "home": str(run.setup.layout.home),
        },
        "runnable": {
            "kind": agic.kind,
            "name": agic.name,
            "output": agic.output,
        },
    }
    environment = run.setup.environment
    if environment is not None:
        runtime["environment"] = {
            "sandbox": environment.sandbox,
            "system": environment.system,
            "release": environment.release,
            "machine": environment.machine,
            "container": environment.container,
            "root": str(environment.root),
            "home": str(environment.home),
            "working_directory": str(environment.working_directory),
        }
    return runtime


def _model_context(model: ModelTarget) -> dict[str, object]:
    family = model.ref.split("/", 1)[0] if "/" in model.ref else ""
    return {
        "ref": model.ref,
        "family": family,
        "provider": model.provider,
        "name": model.name,
        "model": model.model,
        "adapter": model.adapter,
        "base_url": model.base_url,
        "tools": model.tools,
        "streaming": model.streaming,
    }


def _cap_context(context: _Execution, entry: StateCap) -> dict[str, object]:
    description = entry.meta.get("description")
    return {
        "name": entry.name,
        "kind": entry.kind,
        "path": entry.path,
        "ref": cap_store.entry_ref(entry, agent_name=context.layout.name),
        "description": str(description) if description is not None else None,
        "content": entry.read_content() or None if entry.kind == "psyche" else None,
        "metadata": mutable_data(entry.meta),
        "metadata_items": _metadata_items(entry.meta),
        "scope": cap_store.entry_scope(entry, agent_name=context.layout.name),
        "origin": cap_store.entry_origin(entry),
        "form": cap_store.entry_form(entry),
    }


def _metadata_items(meta: Mapping[str, object]) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for key in sorted(meta):
        if key == "description":
            continue
        value = meta[key]
        if value is None:
            continue
        if isinstance(value, str):
            text = value.strip()
        elif isinstance(value, bool):
            text = "true" if value else "false"
        elif isinstance(value, int | float):
            text = str(value)
        else:
            text = json.dumps(mutable_data(value), ensure_ascii=False, sort_keys=True)
        if text:
            items.append({"key": key, "value": text})
    return items


def _primary_parts(
    agic: AgicDecl,
    variables: Mapping[str, object],
) -> tuple[Part, ...]:
    if agic.input is None or "_" not in variables:
        return ()
    value = variables["_"]
    parts = value_parts(value, type_name=agic.input.type_name or "Part[]")
    return parts if parts is not None else (TextPart(value_text(value)),)


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
    _LOGGER.debug(
        "prompt.assembled thread=%s run=%s runnable=%s model=%s tools=%s",
        run.thread,
        run.run_id,
        prepared.agic.name,
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
        prepared.prompt_context,
    )
    _LOGGER.debug(
        "prompt.messages thread=%s run=%s messages=%s",
        run.thread,
        run.run_id,
        json.dumps(
            [
                {"role": message.role, "text": message_text(message.parts)}
                for message in prepared.messages
            ],
            ensure_ascii=False,
        ),
    )
