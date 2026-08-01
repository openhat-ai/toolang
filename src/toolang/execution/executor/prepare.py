"""Prepare one agic's model, prompt, messages, tools, and services."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json
import logging
import re
from typing import TYPE_CHECKING

from toolang.base.protocols.model import ModelAdapter
from toolang.base.protocols.tool import AgentTool
from toolang.base.types.message import (
    Message,
    Percept,
    PerceptPart,
    TextPart,
    message_text,
)
from toolang.base.types.model import ModelTarget
from toolang.base.types.tool import ToolService
from toolang.common.errors import ToolangError
from toolang.common.immutable import mutable_data
from toolang.common.template import render_text_template
from toolang.lang.ast import (
    AgicDecl,
    Directive,
    Message as AstMessage,
    Parameter,
    Program,
    Span,
)
from toolang.lang.input import coerce_input, perceive_input
from toolang.plugin.models.resolution import resolve_model
from toolang.state import state as cap_store
from toolang.state.state import PreparedCap

from . import prompts
from .common import BoundRun, value_percept

if TYPE_CHECKING:
    from .executor import _Execution

_LOGGER = logging.getLogger(__name__)
_TEXT_HISTORY_MESSAGE_LIMIT = 32
_DEFAULT_INSTRUCT_TEMPLATE = prompts.load("instruct.default.md")
_DEFAULT_CONTEXT_TEMPLATE = prompts.load("context.default.md")
_PRIMARY_REFERENCE_RE = re.compile(r"{{\s*(?:[#^/]\s*)?_(?:\.[A-Za-z_][\w-]*)*\s*}}")
_RUNTIME_DEFAULT_AGIC = AgicDecl(
    name="default",
    input=Parameter(name="_", type_name="Part[]", span=Span(line=1)),
    span=Span(line=1),
)


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
    tools: dict[str, AgentTool]
    services: tuple[ToolService, ...]


def prepare_agic(
    context: _Execution,
    run: BoundRun,
    agic: AgicDecl,
    *,
    variables: Mapping[str, object] | None = None,
) -> _AgicFrame:
    """Resolve runtime resources and render the complete model input."""

    args = dict(run.args)

    ceiling = run.ceiling
    if ceiling is None:
        raise RuntimeError(f"run ceiling missing: {run.run_id}")
    model_selectors = ceiling.models
    if not model_selectors:
        raise ToolangError(f"run ceiling allows no models: {agic.name}")
    model = resolve_model(
        context,
        selector=run.model or (model_selectors[0] if model_selectors else None),
        allowed_selectors=model_selectors,
    )
    tools = dict(ceiling.tools)
    caps = ceiling.caps
    psyches = tuple(item for item in caps if item.kind == "psyche")
    skills = tuple(item for item in caps if item.kind == "skill")
    services = tuple(item for item in caps if item.kind == "service")

    if variables is None:
        default_variables: dict[str, object] = dict(args)
        if agic.input is not None:
            percept = value_percept(run.input)
            if percept is None:
                raise ToolangError("user run input is not a Percept")
            default_variables["_"] = coerce_input(
                percept,
                agic.input.type_name or "Part[]",
                structs={item.name: item for item in run.state.program.structs},
            )
        variables = default_variables
    body_variables = _body_variables(agic, variables)
    body_types = _body_types(agic)
    system_runtime = _runtime_context(context, run=run, agic=agic)
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
    rendered = _render_messages(
        run.state.program,
        agic.messages,
        values=body_variables,
        types=body_types,
    )
    prompt_context = _render_context(run.state.program, agic, system_runtime)
    fallback = _run_message(
        run,
        agic=agic,
        rendered=rendered,
        prompt_context=prompt_context,
    )
    history = (
        tuple(
            context.store.recent_conversation_messages(
                thread_id=run.thread,
                limit=_TEXT_HISTORY_MESSAGE_LIMIT,
                exclude_run_id=run.run_id,
            )
        )
        if _recalls_history(agic)
        else ()
    )
    messages = (
        *history,
        *_authored_messages(
            rendered=rendered,
            prompt_context=prompt_context,
            fallback=fallback,
        ),
    )
    instructions = _render_instructions(run.state.program, agic, system_runtime)
    provider = run.setup.providers[model.provider]
    prepared_model = provider.prepare_target(model)
    adapter = run.setup.adapters.get(prepared_model.adapter)
    if adapter is None:
        raise ToolangError(f"unknown model adapter: {prepared_model.adapter}")
    prepared = _AgicFrame(
        run=run,
        agic=agic,
        model=prepared_model,
        adapter=adapter,
        instructions=instructions,
        prompt_context=prompt_context,
        messages=messages,
        tools=tools,
        services=_tool_services(services, context.setup.envs),
    )
    _log_prepared(prepared)
    return prepared


def effective_agics(program: Program) -> tuple[AgicDecl, ...]:
    if program.find_agic("default") is not None:
        return program.agics
    return (*program.agics, _RUNTIME_DEFAULT_AGIC)


def _directives(agic: AgicDecl, name: str) -> tuple[Directive, ...]:
    return tuple(item for item in agic.directives if item.name == name)


def _render_messages(
    program: Program,
    blocks: tuple[AstMessage, ...],
    *,
    values: Mapping[str, object],
    types: Mapping[str, str],
) -> tuple[tuple[AstMessage, Percept], ...]:
    return tuple(
        (
            block,
            _strip_percept(
                perceive_input(
                    block.content,
                    program=program,
                    values=values,
                    types=types,
                )
            ),
        )
        for block in blocks
    )


def _render_instructions(
    program: Program,
    agic: AgicDecl,
    context: dict[str, object],
) -> str:
    name = agic.instruct
    if name == "none":
        return ""
    if name is None or name == "default":
        template = (
            item.body
            if (item := program.find_instruct("default")) is not None
            else _DEFAULT_INSTRUCT_TEMPLATE
        )
    else:
        item = program.find_instruct(name)
        if item is None:
            raise ToolangError(f"Instruct not found: {name}")
        template = item.body
    return render_text_template(template, context).strip() if template.strip() else ""


def _render_context(
    program: Program,
    agic: AgicDecl,
    context: dict[str, object],
) -> str:
    name = agic.context
    if name == "none":
        return ""
    if name is None or name == "default":
        template = (
            item.body
            if (item := program.find_context("default")) is not None
            else _DEFAULT_CONTEXT_TEMPLATE
        )
    else:
        item = program.find_context(name)
        if item is None:
            raise ToolangError(f"Context not found: {name}")
        template = item.body
    return render_text_template(template, context).strip() if template.strip() else ""


def _run_message(
    run: BoundRun,
    *,
    agic: AgicDecl,
    rendered: tuple[tuple[AstMessage, Percept], ...],
    prompt_context: str,
) -> Message:
    implicit = tuple(
        parts
        for block, parts in rendered
        if block.role == "user" and not block.explicit
    )
    authored = _join_percepts(*implicit)
    primary = value_percept(run.input)
    if primary is None:
        raise ToolangError("user run input is not a Percept")
    references_primary = any(
        block.role == "user"
        and not block.explicit
        and _PRIMARY_REFERENCE_RE.search(block.content) is not None
        for block in agic.messages
    )
    parts = _join_percepts(
        (TextPart(prompt_context.strip()),) if prompt_context.strip() else (),
        authored,
        primary if (not authored or not references_primary) else (),
    )
    if parts == primary:
        return run.input
    return Message(role="user", parts=parts)


def _authored_messages(
    *,
    rendered: tuple[tuple[AstMessage, Percept], ...],
    prompt_context: str,
    fallback: Message,
) -> tuple[Message, ...]:
    blocks = tuple(
        (block, parts)
        for block, parts in rendered
        if block.role in {"user", "assistant", "tool"}
    )
    if not any(block.explicit for block, _parts in blocks):
        return (fallback,)
    last_user = next(
        (
            index
            for index in range(len(blocks) - 1, -1, -1)
            if blocks[index][0].role == "user"
        ),
        None,
    )
    messages: list[Message] = []
    for index, (block, parts) in enumerate(blocks):
        if index == last_user and prompt_context.strip():
            parts = _join_percepts((TextPart(prompt_context.strip()),), parts)
        if not parts:
            continue
        try:
            messages.append(Message(role=block.role, parts=parts))
        except ValueError as exc:
            raise ToolangError(str(exc)) from exc
    if last_user is None and prompt_context.strip():
        messages.insert(0, Message.user(prompt_context.strip()))
    return tuple(messages) if messages else (fallback,)


def _recalls_history(agic: AgicDecl) -> bool:
    directives = _directives(agic, "recall")
    values = (
        tuple(value for value in directives[0].values if value) if directives else ()
    )
    return not values or "default" in values or "history" in values


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
        "run": {
            "id": run.run_id,
            "thread_id": run.thread,
            "program_source": run.state.program_source,
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


def _cap_context(context: _Execution, entry: PreparedCap) -> dict[str, object]:
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


def _strip_percept(parts: Percept) -> Percept:
    result = list(parts)
    if result and isinstance(result[0], TextPart):
        result[0] = TextPart(result[0].text.lstrip())
    if result and isinstance(result[-1], TextPart):
        result[-1] = TextPart(result[-1].text.rstrip())
    return tuple(part for part in result if not isinstance(part, TextPart) or part.text)


def _join_percepts(*groups: Percept) -> Percept:
    result: list[PerceptPart] = []
    for group in groups:
        if not group:
            continue
        if result:
            _append_percept_part(result, TextPart("\n\n"))
        for part in group:
            _append_percept_part(result, part)
    return tuple(result)


def _append_percept_part(parts: list[PerceptPart], part: PerceptPart) -> None:
    if isinstance(part, TextPart) and parts and isinstance(parts[-1], TextPart):
        parts[-1] = TextPart(parts[-1].text + part.text)
    else:
        parts.append(part)


def _tool_services(
    entries: tuple[PreparedCap, ...], environ: Mapping[str, str]
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


def _log_prepared(prepared: _AgicFrame) -> None:
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
