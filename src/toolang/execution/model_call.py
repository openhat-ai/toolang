"""Construct the model-call payload for one bound run."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json
from typing import TYPE_CHECKING, Any

from toolang.common.error import ToolangError
from toolang.base.protocols.tool import AgentTool
from toolang.base.types.message import Message, TextPart, message_text
from toolang.base.types.model import ModelTarget

from toolang.catalog import cap as cap_store
from ..common.immutable import mutable_data
from ..lang.ast import AgicDecl, Message as AstMessage, Program
from toolang.state.prepared import PreparedEntry
from .template import render_text_template
from . import prompts
from .effective import directives_for
from .binding import run_job_context

if TYPE_CHECKING:
    from .assembly import SupportsRunAssembly
    from .binding import _Run

_DEFAULT_INSTRUCT_TEMPLATE = prompts.load("instruct.default.md")
_DEFAULT_CONTEXT_TEMPLATE = prompts.load("context.default.md")


@dataclass(frozen=True, slots=True)
class ModelCallAssembly:
    """Text and message inputs prepared for one model call."""

    user_template_context: dict[str, object]
    system_template_context: dict[str, object]
    context_text: str
    rendered_messages: tuple[AstMessage, ...]
    message: Message
    instructions: str


def build_model_call_assembly(
    context: SupportsRunAssembly,
    *,
    run: _Run,
    agic: AgicDecl,
    input_text: str,
    params: dict[str, Any],
    models: tuple[ModelTarget, ...],
    tools: dict[str, AgentTool],
    psyches: tuple[PreparedEntry, ...],
    skills: tuple[PreparedEntry, ...],
    services: tuple[PreparedEntry, ...],
) -> ModelCallAssembly:
    user_template_context = user_template_context_for_run(
        context,
        run=run,
        agic=agic,
        params=params,
    )
    system_template_context = system_template_context_for_run(
        context,
        run=run,
        agic=agic,
        params=params,
        models=models,
        tools=tools,
        psyches=psyches,
        skills=skills,
        services=services,
    )
    context_text = selected_context_text(
        program=run.state.program,
        agic=agic,
        system_context=system_template_context,
    )
    rendered_messages = render_agic_messages(
        agic.messages,
        user_context=user_template_context,
        system_context=system_template_context,
    )
    message = run_message(
        run=run,
        agic=agic,
        input_text=input_text,
        rendered_messages=rendered_messages,
        context_text=context_text,
    )
    instructions = assembled_instructions(
        program=run.state.program,
        agic=agic,
        system_context=system_template_context,
    )
    return ModelCallAssembly(
        user_template_context=user_template_context,
        system_template_context=system_template_context,
        context_text=context_text,
        rendered_messages=rendered_messages,
        message=message,
        instructions=instructions,
    )


def run_message(
    *,
    run: _Run,
    agic: AgicDecl,
    input_text: str,
    rendered_messages: tuple[AstMessage, ...],
    context_text: str,
) -> Message:
    if run.origin != "script" and run.message is not None:
        return _expanded_run_message(
            run.message, input_text=input_text, context_text=context_text
        )
    if run.origin == "file":
        text = _file_message_text(
            input_text=input_text,
            rendered_messages=rendered_messages,
            context_text=context_text,
        )
        return Message.user(text)
    if run.origin != "script":
        return Message.user(_join_message_texts(context_text, input_text))
    text = _script_message_text(
        agic=agic,
        input_text=input_text,
        rendered_messages=rendered_messages,
        context_text=context_text,
    )
    return Message.user(text)


def user_template_context_for_run(
    context: SupportsRunAssembly,
    *,
    run: _Run,
    agic: AgicDecl,
    params: dict[str, Any],
) -> dict[str, object]:
    return _template_context(
        params=_template_param_values(agic, params),
        runtime=_runtime_base(
            context,
            run=run,
            agic=agic,
        ),
    )


def system_template_context_for_run(
    context: SupportsRunAssembly,
    *,
    run: _Run,
    agic: AgicDecl,
    params: dict[str, Any],
    models: tuple[ModelTarget, ...],
    tools: dict[str, AgentTool],
    psyches: tuple[PreparedEntry, ...],
    skills: tuple[PreparedEntry, ...],
    services: tuple[PreparedEntry, ...],
) -> dict[str, object]:
    del tools
    runtime = _runtime_base(
        context,
        run=run,
        agic=agic,
    )
    runtime.update(
        {
            "model": _model_target_to_context(models[0]) if models else None,
            "psyches": [_prepared_entry_to_context(context, item) for item in psyches],
            "has_psyches": bool(psyches),
            "skills": [_prepared_entry_to_context(context, item) for item in skills],
            "has_skills": bool(skills),
            "services": [
                _prepared_entry_to_context(context, item) for item in services
            ],
            "has_services": bool(services),
        }
    )
    return _template_context(
        params=_template_param_values(agic, params),
        runtime=runtime,
    )


def render_agic_messages(
    blocks: tuple[AstMessage, ...],
    *,
    user_context: dict[str, object],
    system_context: dict[str, object],
) -> tuple[AstMessage, ...]:
    del system_context
    rendered: list[AstMessage] = []
    for block in blocks:
        rendered.append(
            AstMessage(
                role=block.role,
                content=render_text_template(block.content, user_context).strip(),
                explicit=block.explicit,
                span=block.span,
                doc=block.doc,
            )
        )
    return tuple(rendered)


def assembled_instructions(
    *,
    program: Program,
    agic: AgicDecl,
    system_context: dict[str, object],
) -> str:
    template = _selected_instruct_template(program=program, agic=agic)
    if not template.strip():
        return ""
    return render_text_template(template, system_context).strip()


def selected_context_text(
    *,
    program: Program,
    agic: AgicDecl,
    system_context: dict[str, object],
) -> str:
    template = _selected_context_template(program=program, agic=agic)
    if not template.strip():
        return ""
    return render_text_template(template, system_context).strip()


def recalls_history(agic: AgicDecl) -> bool:
    values = recall_values(agic)
    if not values or "default" in values:
        return True
    return "history" in values


def recall_values(agic: AgicDecl) -> tuple[str, ...]:
    directives = directives_for(agic, "recall")
    if not directives:
        return ()
    return tuple(item for item in directives[0].values if item)


def _expanded_run_message(
    message: Message, *, input_text: str, context_text: str
) -> Message:
    original_text = message_text(message.parts)
    if not input_text.strip() or input_text == original_text:
        input_text = original_text
    if not context_text.strip() and input_text == original_text:
        return message
    parts = [part for part in message.parts if not isinstance(part, TextPart)]
    return Message(
        role=message.role,
        parts=(TextPart(text=_join_message_texts(context_text, input_text)), *parts),
        meta=dict(message.meta),
    )


def _selected_instruct_template(
    *,
    program: Program,
    agic: AgicDecl,
) -> str:
    name = agic.instruct
    if name is None or name == "default":
        return _default_program_instruct_template(program)
    if name == "none":
        return ""
    instruct = _program_instruct(program, name)
    if instruct is None:
        raise ToolangError(f"Instruct not found: {name}")
    return instruct.body


def _default_program_instruct_template(program: Program) -> str:
    instruct = _program_instruct(program, None)
    if instruct is not None:
        return instruct.body
    return _DEFAULT_INSTRUCT_TEMPLATE


def _selected_context_template(
    *,
    program: Program,
    agic: AgicDecl,
) -> str:
    name = agic.context
    if name is None or name == "default":
        return _default_program_context_template(program)
    if name == "none":
        return ""
    context = _program_context(program, name)
    if context is None:
        raise ToolangError(f"Context not found: {name}")
    return context.body


def _default_program_context_template(program: Program) -> str:
    context = _program_context(program, None)
    if context is not None:
        return context.body
    return _DEFAULT_CONTEXT_TEMPLATE


def _program_instruct(program: Program, name: str | None) -> Any | None:
    return program.get_instruct(name)


def _program_context(program: Program, name: str | None) -> Any | None:
    return program.get_context(name)


def _template_param_values(
    agic: AgicDecl, params: dict[str, Any]
) -> dict[str, object]:
    values: dict[str, object] = {}
    if agic.input is not None:
        values[agic.input.name] = params.get(agic.input.name)
        values["_"] = params.get("_", values[agic.input.name])
    for param in agic.params:
        values[param.name] = params.get(param.name)
    return values


def _template_context(
    *,
    params: dict[str, object],
    runtime: dict[str, object],
) -> dict[str, object]:
    context: dict[str, object] = {"runtime": runtime}
    for name, value in params.items():
        context[name] = value
    return context


def _runtime_base(
    context: SupportsRunAssembly,
    *,
    run: _Run,
    agic: AgicDecl,
) -> dict[str, object]:
    return {
        "origin": run.origin,
        "is_chat": run.origin == "chat",
        "is_script": run.origin == "script",
        "is_task": run.origin == "task",
        "is_chore": run.origin == "chore",
        "sandbox": _runtime_sandbox(context),
        "run": {
            "thread_id": run.thread_id,
            "program_source": str(
                context.home.joinpath("agent.too").relative_to(context.root)
            ),
        },
        "agent": {
            "name": context.name,
            "home": str(context.home),
        },
        "agic": {
            "name": agic.name,
            "output": agic.output,
        },
        "job": run_job_context(run),
    }


def _model_target_to_context(target: ModelTarget) -> dict[str, object]:
    family = target.ref.split("/", 1)[0] if "/" in target.ref else ""
    return {
        "ref": target.ref,
        "family": family,
        "provider": target.provider,
        "name": target.name,
        "model": target.model,
        "adapter": target.adapter,
        "base_url": target.base_url,
        "tools": target.tools,
        "streaming": target.streaming,
    }


def _prepared_entry_to_context(
    context: SupportsRunAssembly,
    entry: PreparedEntry,
) -> dict[str, object]:
    content = entry.content.strip() if entry.kind == "psyche" else ""
    description = entry.meta.get("description")
    return {
        "name": entry.name,
        "kind": entry.kind,
        "path": entry.path,
        "ref": cap_store.entry_ref(entry, agent_name=context.name),
        "description": str(description) if description is not None else None,
        "content": content or None,
        "metadata": mutable_data(entry.meta),
        "metadata_items": _metadata_items(entry.meta),
        "scope": cap_store.entry_scope(entry, agent_name=context.name),
        "origin": cap_store.entry_origin(entry),
        "form": cap_store.entry_form(entry),
    }


def _script_message_text(
    *,
    agic: AgicDecl,
    input_text: str,
    rendered_messages: tuple[AstMessage, ...],
    context_text: str,
) -> str:
    user_messages = tuple(item for item in rendered_messages if item.role == "user")
    authored_text = _message_blocks_body(user_messages)
    if input_text.strip() and any(
        item.role == "user" and not item.explicit for item in agic.messages
    ):
        return _join_message_texts(context_text, authored_text, input_text)
    if authored_text:
        return _join_message_texts(context_text, authored_text)
    return _join_message_texts(context_text, input_text)


def _file_message_text(
    *,
    input_text: str,
    rendered_messages: tuple[AstMessage, ...],
    context_text: str,
) -> str:
    user_messages = tuple(item for item in rendered_messages if item.role == "user")
    authored_text = _message_blocks_body(user_messages)
    return _join_message_texts(context_text, authored_text, input_text)


def _join_message_texts(*parts: str) -> str:
    return "\n\n".join(part.strip() for part in parts if part.strip()).strip()


def _message_blocks_body(blocks: tuple[AstMessage, ...]) -> str:
    return "\n\n".join(
        block.content.strip() for block in blocks if block.content.strip()
    ).strip()


def _runtime_sandbox(context: SupportsRunAssembly) -> str:
    value = context.config.get("runtime.sandbox")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return "none"


def _metadata_items(meta: Mapping[str, object]) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for key in sorted(meta):
        if key == "description":
            continue
        value = _metadata_value(meta[key])
        if value is None:
            continue
        items.append({"key": key, "value": value})
    return items


def _metadata_value(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    return json.dumps(mutable_data(value), ensure_ascii=False, sort_keys=True)
