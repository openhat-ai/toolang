"""Prepare one agic's model, prompt, messages, tools, and services."""

from __future__ import annotations

from collections.abc import Callable, Hashable, Mapping, Sequence
from dataclasses import dataclass
import json
import logging
from typing import TYPE_CHECKING, Any, TypeVar

from toolang.base.protocols.model import ModelAdapter
from toolang.base.protocols.tool import AgentTool
from toolang.base.types.message import Message, TextPart, message_text
from toolang.base.types.model import ModelTarget
from toolang.base.types.tool import ToolService
from toolang.common.errors import ToolangError
from toolang.common.immutable import mutable_data
from toolang.common.template import render_text_template
from toolang.common.text import join_paragraphs
from toolang.lang.ast import (
    AgicDecl,
    Directive,
    Message as AstMessage,
    Parameter,
    Program,
    Span,
)
from toolang.lang.input import expand_program_input
from toolang.plugin.models.resolution import resolve_model, select_model_selectors
from toolang.plugin.tools.registry import selected_tool_names, tool_ref_for_model_tool
from toolang.state import state as cap_store
from toolang.state.state import PreparedCap

from . import prompts
from .common import BoundRun

if TYPE_CHECKING:
    from .executor import _Execution

_LOGGER = logging.getLogger("toolang.run")
_TEXT_HISTORY_MESSAGE_LIMIT = 32
_DEFAULT_INSTRUCT_TEMPLATE = prompts.load("instruct.default.md")
_DEFAULT_CONTEXT_TEMPLATE = prompts.load("context.default.md")
_RUNTIME_DEFAULT_AGIC = AgicDecl(
    name="default",
    input=Parameter(name="_", type_name="Part[]", span=Span(line=1)),
    span=Span(line=1),
)
_T = TypeVar("_T")


@dataclass(frozen=True, slots=True)
class PreparedAgic:
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


def prepare_agic(context: _Execution, run: BoundRun, agic: AgicDecl) -> PreparedAgic:
    """Resolve runtime resources and render the complete model input."""

    input_text = (
        expand_program_input(run.state.program, run.input_text)
        if run.input_text
        else ""
    )
    params = dict(run.params)
    if agic.input is not None and input_text:
        params = {**params, "_": input_text}

    model_selectors = _effective_model_selectors(
        context,
        agic=agic,
        base=_activation_model_selectors(context),
    )
    model = resolve_model(
        context,
        selector=run.model
        or (model_selectors[0] if model_selectors else None),
        allowed_selectors=model_selectors,
    )
    tools = _select_tools(dict(run.setup.tools), _directives(agic, "tools"))
    caps = tuple(run.state.caps)
    psyches = _select_caps(_caps(caps, "psyche"), _directives(agic, "psyches"))
    skills = _select_caps(_caps(caps, "skill"), _directives(agic, "skills"))
    services = _select_caps(_caps(caps, "service"), _directives(agic, "services"))

    user_context = _template_context(
        params=_template_params(agic, params),
        runtime=_runtime_context(context, run=run, agic=agic),
    )
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
    system_context = _template_context(
        params=_template_params(agic, params),
        runtime=system_runtime,
    )
    rendered = _render_messages(agic.messages, user_context)
    prompt_context = _render_context(run.state.program, agic, system_context)
    fallback = _run_message(
        run,
        agic=agic,
        input_text=input_text,
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
    instructions = _render_instructions(run.state.program, agic, system_context)
    provider = run.setup.model_providers[model.provider]
    prepared_model = provider.prepare_target(model)
    adapter = run.setup.model_adapters.get(prepared_model.adapter)
    if adapter is None:
        raise ToolangError(f"unknown model adapter: {prepared_model.adapter}")
    prepared = PreparedAgic(
        run=run,
        agic=agic,
        model=prepared_model,
        adapter=adapter,
        instructions=instructions,
        prompt_context=prompt_context,
        messages=messages,
        tools=tools,
        services=_tool_services(services, context.setup.model_environ),
    )
    _log_prepared(prepared)
    return prepared


def effective_agics(program: Program) -> tuple[AgicDecl, ...]:
    if program.find_agic("default") is not None:
        return program.agics
    return (*program.agics, _RUNTIME_DEFAULT_AGIC)


def _activation_model_selectors(context: Any) -> tuple[str, ...]:
    return tuple(item for item in context.setup.model_selectors if item.strip())


def _effective_model_selectors(
    context: _Execution,
    *,
    agic: AgicDecl,
    base: tuple[str, ...],
) -> tuple[str, ...]:
    selected = _select_values((), _directives(agic, "models"), lambda values: values)
    return select_model_selectors(
        context,
        agic_selectors=selected,
        activation_selectors=base,
    )


def _select_tools(
    tools: dict[str, AgentTool], directives: tuple[Directive, ...]
) -> dict[str, AgentTool]:
    refs = {name: tool_ref_for_model_tool(name, tool) for name, tool in tools.items()}
    names = _select_values(
        tuple(tools),
        directives,
        lambda values: selected_tool_names(refs, values),
    )
    return {name: tools[name] for name in names if name in tools}


def _caps(entries: tuple[PreparedCap, ...], kind: str) -> tuple[PreparedCap, ...]:
    return tuple(entry for entry in entries if entry.kind == kind)


def _select_caps(
    entries: tuple[PreparedCap, ...], directives: tuple[Directive, ...]
) -> tuple[PreparedCap, ...]:
    if not entries:
        return ()
    kind = entries[0].kind
    agent_name = _entry_agent_name(entries)
    return _select_values(
        entries,
        directives,
        lambda values: cap_store.select_cap_entries(
            entries,
            values,
            agent_name=agent_name,
            implicit_kind=kind,
        ),
        identity=lambda entry: (entry.kind, entry.name, entry.ref),
    )


def _directives(agic: AgicDecl, name: str) -> tuple[Directive, ...]:
    return tuple(item for item in agic.directives if item.name == name)


def _select_values(
    base: tuple[_T, ...],
    directives: tuple[Directive, ...],
    match: Callable[[tuple[str, ...]], Sequence[_T]],
    *,
    identity: Callable[[_T], Hashable] = lambda item: item,
) -> tuple[_T, ...]:
    current: list[_T] = []
    seen: set[Hashable] = set()
    for item in base:
        key = identity(item)
        if key not in seen:
            current.append(item)
            seen.add(key)
    for directive in directives:
        matches = list(match(tuple(value for value in directive.values if value)))
        if directive.operator == "=":
            current = matches
        elif directive.operator == "+=":
            seen = {identity(item) for item in current}
            for item in matches:
                key = identity(item)
                if key not in seen:
                    current.append(item)
                    seen.add(key)
        elif directive.operator == "-=":
            blocked = {identity(item) for item in matches}
            current = [item for item in current if identity(item) not in blocked]
    return tuple(current)


def _entry_agent_name(entries: tuple[PreparedCap, ...]) -> str:
    for entry in entries:
        path = entry.path or entry.source.path
        _prefix, separator, rest = path.partition("agents/")
        if separator and "/" in rest:
            return rest.split("/", 1)[0]
    return "default"


def _render_messages(
    blocks: tuple[AstMessage, ...], context: dict[str, object]
) -> tuple[AstMessage, ...]:
    return tuple(
        AstMessage(
            role=block.role,
            content=render_text_template(block.content, context).strip(),
            explicit=block.explicit,
            span=block.span,
            doc=block.doc,
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
    input_text: str,
    rendered: tuple[AstMessage, ...],
    prompt_context: str,
) -> Message:
    original = message_text(run.input.parts)
    authored = _message_body(tuple(item for item in rendered if item.role == "user"))
    if input_text.strip() and any(
        item.role == "user" and not item.explicit for item in agic.messages
    ):
        text = join_paragraphs(prompt_context, authored, input_text)
    else:
        text = join_paragraphs(prompt_context, authored or input_text)
    if text == original and not prompt_context.strip() and not authored.strip():
        return run.input
    non_text = tuple(
        part for part in run.input.parts if not isinstance(part, TextPart)
    )
    return Message(
        role=run.input.role,
        parts=(TextPart(text=text), *non_text),
        meta=dict(run.input.meta),
    )


def _authored_messages(
    *,
    rendered: tuple[AstMessage, ...],
    prompt_context: str,
    fallback: Message,
) -> tuple[Message, ...]:
    blocks = tuple(
        block for block in rendered if block.role in {"user", "assistant", "tool"}
    )
    if not any(block.explicit for block in blocks):
        return (fallback,)
    messages: list[Message] = []
    context_pending = bool(prompt_context.strip())
    for block in blocks:
        text = block.content.strip()
        if not text and not (context_pending and block.role == "user"):
            continue
        if context_pending and block.role == "user":
            text = join_paragraphs(prompt_context, text)
            context_pending = False
        messages.append(Message(role=block.role, parts=(TextPart(text=text),)))
    if context_pending:
        messages.insert(0, Message.user(prompt_context.strip()))
    return tuple(messages) if messages else (fallback,)


def _recalls_history(agic: AgicDecl) -> bool:
    directives = _directives(agic, "recall")
    values = (
        tuple(value for value in directives[0].values if value) if directives else ()
    )
    return not values or "default" in values or "history" in values


def _template_params(agic: AgicDecl, params: dict[str, Any]) -> dict[str, object]:
    values: dict[str, object] = {}
    if agic.input is not None:
        values["_"] = params.get("_")
    for param in agic.params:
        values[param.name] = params.get(param.name)
    return values


def _template_context(
    *, params: dict[str, object], runtime: dict[str, object]
) -> dict[str, object]:
    return {"runtime": runtime, **params}


def _runtime_context(
    context: _Execution, *, run: BoundRun, agic: AgicDecl
) -> dict[str, object]:
    return {
        "run": {
            "id": run.run_id,
            "thread_id": run.thread,
            "program_source": run.state.program_source,
        },
        "agent": {"name": run.setup.name, "home": str(run.setup.home)},
        "runnable": {"kind": agic.kind, "name": agic.name},
        "agic": {"name": agic.name, "output": agic.output},
    }


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
        "ref": cap_store.entry_ref(entry, agent_name=context.setup.name),
        "description": str(description) if description is not None else None,
        "content": entry.read_content() or None if entry.kind == "psyche" else None,
        "metadata": mutable_data(entry.meta),
        "metadata_items": _metadata_items(entry.meta),
        "scope": cap_store.entry_scope(entry, agent_name=context.setup.name),
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


def _message_body(blocks: tuple[AstMessage, ...]) -> str:
    return "\n\n".join(
        block.content.strip() for block in blocks if block.content.strip()
    ).strip()


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


def _log_prepared(prepared: PreparedAgic) -> None:
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
