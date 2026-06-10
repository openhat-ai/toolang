"""Construct the model-call payload for one bound run."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import TYPE_CHECKING, Any

from toolang.base.error import ToolangError
from toolang.base.protocols.tool import AgentTool
from toolang.base.types.message import Message, TextPart, message_text
from toolang.base.types.model import ModelTarget

from .. import caps as cap_store, work
from ..program import MessageBlock, Thunk
from ..state.prepared import PreparedEntry
from ..common.template import render_text_template
from . import prompts

if TYPE_CHECKING:
    from ..state.program import LiveProgram
    from ..up import UptimeContext
    from .binding import RunBinding

_DEFAULT_INSTRUCT_TEMPLATE = prompts.load("instruct.default.md")
_DEFAULT_CONTEXT_TEMPLATE = prompts.load("context.default.md")


@dataclass(frozen=True, slots=True)
class ModelCallAssembly:
    """Text and message inputs prepared for one model call."""

    user_template_context: dict[str, object]
    system_template_context: dict[str, object]
    context_text: str
    rendered_messages: tuple[MessageBlock, ...]
    message: Message
    instructions: str


def build_model_call_assembly(
    context: UptimeContext,
    *,
    run: RunBinding,
    thunk: Thunk,
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
        thunk=thunk,
        params=params,
    )
    system_template_context = system_template_context_for_run(
        context,
        run=run,
        thunk=thunk,
        params=params,
        models=models,
        tools=tools,
        psyches=psyches,
        skills=skills,
        services=services,
    )
    context_text = selected_context_text(
        live_program=run.live.program,
        thunk=thunk,
        system_context=system_template_context,
    )
    rendered_messages = render_thunk_messages(
        thunk.messages,
        user_context=user_template_context,
        system_context=system_template_context,
    )
    message = run_message(
        run=run,
        thunk=thunk,
        input_text=input_text,
        rendered_messages=rendered_messages,
        context_text=context_text,
    )
    instructions = assembled_instructions(
        live_program=run.live.program,
        thunk=thunk,
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
    run: RunBinding,
    thunk: Thunk,
    input_text: str,
    rendered_messages: tuple[MessageBlock, ...],
    context_text: str,
) -> Message:
    if run.origin != "script" and run.message is not None:
        return _expanded_run_message(run.message, input_text=input_text, context_text=context_text)
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
        thunk=thunk,
        input_text=input_text,
        rendered_messages=rendered_messages,
        context_text=context_text,
    )
    return Message.user(text)


def user_template_context_for_run(
    context: UptimeContext,
    *,
    run: RunBinding,
    thunk: Thunk,
    params: dict[str, Any],
) -> dict[str, object]:
    return _template_context(
        params=_template_param_values(thunk, params),
        runtime=_runtime_base(
            context,
            run=run,
            thunk=thunk,
        ),
    )


def system_template_context_for_run(
    context: UptimeContext,
    *,
    run: RunBinding,
    thunk: Thunk,
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
        thunk=thunk,
    )
    runtime.update(
        {
            "model": _model_target_to_context(models[0]) if models else None,
            "psyches": [_prepared_entry_to_context(context, item) for item in psyches],
            "has_psyches": bool(psyches),
            "skills": [_prepared_entry_to_context(context, item) for item in skills],
            "has_skills": bool(skills),
            "services": [_prepared_entry_to_context(context, item) for item in services],
            "has_services": bool(services),
        }
    )
    return _template_context(
        params=_template_param_values(thunk, params),
        runtime=runtime,
    )


def render_thunk_messages(
    blocks: tuple[MessageBlock, ...],
    *,
    user_context: dict[str, object],
    system_context: dict[str, object],
) -> tuple[MessageBlock, ...]:
    rendered: list[MessageBlock] = []
    for block in blocks:
        context = system_context if block.kind == "instruct" else user_context
        rendered.append(
            MessageBlock(
                kind=block.kind,
                text=render_text_template(block.text, context).strip(),
                span=block.span,
                explicit=block.explicit,
            )
        )
    return tuple(rendered)


def assembled_instructions(
    *,
    live_program: LiveProgram,
    thunk: Thunk,
    system_context: dict[str, object],
) -> str:
    template = _selected_instruct_template(live_program=live_program, thunk=thunk)
    if not template.strip():
        return ""
    return render_text_template(template, system_context).strip()


def selected_context_text(
    *,
    live_program: LiveProgram,
    thunk: Thunk,
    system_context: dict[str, object],
) -> str:
    template = _selected_context_template(live_program=live_program, thunk=thunk)
    if not template.strip():
        return ""
    return render_text_template(template, system_context).strip()


def recalls_history(thunk: Thunk) -> bool:
    values = recall_values(thunk)
    if not values or "default" in values:
        return True
    return "history" in values


def recall_values(thunk: Thunk) -> tuple[str, ...]:
    directives = thunk.directives_for("recall")
    if not directives:
        return ()
    return tuple(item for item in directives[0].values if item)


def _expanded_run_message(message: Message, *, input_text: str, context_text: str) -> Message:
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
    live_program: LiveProgram,
    thunk: Thunk,
) -> str:
    blocks = thunk.message_blocks("instruct")
    if not blocks:
        return _default_program_instruct_template(live_program)
    block = blocks[0]
    value = block.text.strip()
    if not block.explicit:
        return _join_message_texts(_default_program_instruct_template(live_program), block.text)
    if value == "none":
        return ""
    if value == "default":
        return _default_program_instruct_template(live_program)
    if _looks_like_template_name(value):
        instruct = _program_instruct(live_program, value)
        if instruct is None:
            raise ToolangError(f"Instruct not found: {value}")
        return instruct.body
    return block.text


def _default_program_instruct_template(live_program: LiveProgram) -> str:
    instruct = _program_instruct(live_program, None)
    if instruct is not None:
        return instruct.body
    return _DEFAULT_INSTRUCT_TEMPLATE


def _selected_context_template(
    *,
    live_program: LiveProgram,
    thunk: Thunk,
) -> str:
    block = thunk.context
    if block is None:
        return _default_program_context_template(live_program)
    value = block.text.strip()
    if value == "none":
        return ""
    if value == "default":
        return _default_program_context_template(live_program)
    if _looks_like_template_name(value):
        context = _program_context(live_program, value)
        if context is None:
            raise ToolangError(f"Context not found: {value}")
        return context.body
    return block.text


def _default_program_context_template(live_program: LiveProgram) -> str:
    context = _program_context(live_program, None)
    if context is not None:
        return context.body
    return _DEFAULT_CONTEXT_TEMPLATE


def _program_instruct(live_program: LiveProgram, name: str | None) -> Any | None:
    get_instruct = getattr(live_program, "get_instruct", None)
    if not callable(get_instruct):
        return None
    return get_instruct(name)


def _program_context(live_program: LiveProgram, name: str | None) -> Any | None:
    get_context = getattr(live_program, "get_context", None)
    if not callable(get_context):
        return None
    return get_context(name)


def _looks_like_template_name(value: str) -> bool:
    if not value or any(char.isspace() for char in value):
        return False
    if "\n" in value:
        return False
    first = value[0]
    return (first.isalpha() or first == "_") and all(
        char.isalnum() or char in {"_", "-"}
        for char in value
    )


def _template_param_values(thunk: Thunk, params: dict[str, Any]) -> dict[str, object]:
    values: dict[str, object] = {}
    if thunk.input is not None:
        values[thunk.input.name] = params.get(thunk.input.name)
    for param in thunk.params:
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
    context: UptimeContext,
    *,
    run: RunBinding,
    thunk: Thunk,
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
            "program_source": run.live.program.source_path,
        },
        "agent": {
            "name": context.name,
            "home": str(context.home),
        },
        "thunk": {
            "name": thunk.thunk_name(),
            "output": thunk.output,
        },
        "job": _job_context(context, run),
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
    context: UptimeContext,
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
        "metadata": dict(entry.meta),
        "metadata_items": _metadata_items(entry.meta),
        "scope": cap_store.entry_scope(entry, agent_name=context.name),
        "origin": cap_store.entry_origin(entry),
        "form": cap_store.entry_form(entry),
    }


def _script_message_text(
    *,
    thunk: Thunk,
    input_text: str,
    rendered_messages: tuple[MessageBlock, ...],
    context_text: str,
) -> str:
    user_messages = tuple(item for item in rendered_messages if item.kind == "user")
    authored_text = _message_blocks_body(user_messages)
    if input_text.strip() and any(item.kind == "user" and not item.explicit for item in thunk.messages):
        return _join_message_texts(context_text, authored_text, input_text)
    if authored_text:
        return _join_message_texts(context_text, authored_text)
    return _join_message_texts(context_text, input_text)


def _file_message_text(
    *,
    input_text: str,
    rendered_messages: tuple[MessageBlock, ...],
    context_text: str,
) -> str:
    user_messages = tuple(item for item in rendered_messages if item.kind == "user")
    authored_text = _message_blocks_body(user_messages)
    return _join_message_texts(context_text, authored_text, input_text)


def _join_message_texts(*parts: str) -> str:
    return "\n\n".join(part.strip() for part in parts if part.strip()).strip()


def _message_blocks_body(blocks: tuple[MessageBlock, ...]) -> str:
    return "\n\n".join(block.text.strip() for block in blocks if block.text.strip()).strip()


def _runtime_sandbox(context: UptimeContext) -> str:
    value = context.config.get("runtime.sandbox")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return "none"


def _job_context(
    context: UptimeContext,
    run: RunBinding,
) -> dict[str, object] | None:
    if run.origin == "task":
        return _task_context(context, run)
    if run.origin == "chore":
        return _chore_context(context, run)
    return None


def _task_context(
    context: UptimeContext,
    run: RunBinding,
) -> dict[str, object] | None:
    task_id = work.task_id_from_thread_id(run.thread_id)
    if task_id is None:
        return None
    task = work.find_task(context.root, context.name, task_id)
    if task is None:
        return None
    return {
        "kind": "task",
        "provider": "local",
        "name": task.name.rsplit("/", 1)[-1],
        "body": task.document.body,
        "thread_id": task.document.thread_id(),
        "path": str(task.path),
        "readable": True,
        "writable": True,
        "commentable": False,
    }


def _chore_context(
    context: UptimeContext,
    run: RunBinding,
) -> dict[str, object] | None:
    chore_id = work.chore_id_from_thread_id(run.thread_id)
    if chore_id is None:
        return None
    chore = work.find_chore(context.root, context.name, chore_id)
    if chore is None:
        return None
    return {
        "kind": "chore",
        "provider": "local",
        "name": chore.name.rsplit("/", 1)[-1],
        "title": (chore.document.title or "").strip() or None,
        "body": chore.document.body,
        "schedule": chore.document.schedule,
        "thread_id": run.thread_id,
        "path": str(chore.path),
        "readable": True,
        "writable": False,
        "commentable": False,
    }


def _metadata_items(meta: dict[str, object]) -> list[dict[str, str]]:
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
    return json.dumps(value, ensure_ascii=False, sort_keys=True)
