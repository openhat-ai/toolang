"""Render prepared execution data into model instructions and messages."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from html import escape
import re

from toolang.base.types.message import Message, Part, TextPart
from toolang.common.errors import ToolangError
from toolang.common.template import render_text_template
from toolang.lang.ast import AgicDecl, Message as AstMessage, Program
from toolang.lang.input import (
    PromptDefinitionIdentity,
    PromptInvocation,
    resolve_input_parts_with_provenance,
)

from . import prompts
from ..records import (
    CancelControlPayload,
    ControlRecord,
    RecallControlPayload,
    SteerControlPayload,
)
from ..types import FieldRef, MessageTemplate, RulesRecallTarget, TypedRef, value_type

_PROTOCOL_TEMPLATE = prompts.load("protocol.md")
_DEFAULT_INSTRUCT_TEMPLATE = prompts.load("defaults/instruct.md")
_PSYCHES_TEMPLATE = prompts.load("psyches.md")
_CAPABILITY_CATALOG_TEMPLATE = (
    "<capability-catalog>\n"
    + prompts.load("skills.md")
    + "\n\n"
    + prompts.load("services.md")
    + "\n</capability-catalog>"
)
_DEFAULT_CONTEXT_TEMPLATE = prompts.load("defaults/context.md")
_TOOLS_TEMPLATE = prompts.load("tools.md")
_PRIMARY_REFERENCE_RE = re.compile(r"{{\s*(?:[#^/]\s*)?_(?:\.[A-Za-z_][\w-]*)*\s*}}")


def render_messages(
    program: Program,
    blocks: tuple[AstMessage, ...],
    *,
    values: Mapping[str, object],
    types: Mapping[str, str],
    definitions: Mapping[str, PromptDefinitionIdentity],
) -> tuple[
    tuple[tuple[AstMessage, tuple[Part, ...]], ...],
    tuple[PromptInvocation, ...],
]:
    rendered: list[tuple[AstMessage, tuple[Part, ...]]] = []
    invocations: list[PromptInvocation] = []
    for block in blocks:
        resolution = resolve_input_parts_with_provenance(
            block.content,
            program=program,
            values=values,
            types=types,
            prompt_definitions=definitions,
        )
        offset = len(invocations)
        invocations.extend(
            replace(
                invocation,
                parent=(
                    invocation.parent + offset
                    if invocation.parent is not None
                    else None
                ),
            )
            for invocation in resolution.prompts
        )
        rendered.append((block, _strip_parts(resolution.parts)))
    return tuple(rendered), tuple(invocations)


def render_instructions(
    program: Program,
    agic: AgicDecl,
    context: dict[str, object],
) -> str:
    markup_context = {
        key: _escape_markup_value(value) for key, value in context.items()
    }
    protocol = render_text_template(_PROTOCOL_TEMPLATE, markup_context).strip()
    instruct = _render_selected_instruct(program, agic, context)
    agent = _text_block("agent-instructions", instruct)
    psyches = render_text_template(_PSYCHES_TEMPLATE, markup_context).strip()
    catalog = (
        render_text_template(_CAPABILITY_CATALOG_TEMPLATE, markup_context).strip()
        if context.get("has_skills") or context.get("has_services")
        else ""
    )
    return "\n\n".join(part for part in (protocol, agent, psyches, catalog) if part)


def _escape_markup_value(value: object) -> object:
    """Escape bundled template values without changing authored rendering."""

    if isinstance(value, str):
        return escape(value, quote=True)
    if isinstance(value, Mapping):
        return {key: _escape_markup_value(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_escape_markup_value(item) for item in value]
    return value


def _text_block(tag: str, content: str) -> str:
    """Keep rendered text inside a runtime-owned instruction or data block."""

    return f"<{tag}>\n{escape(content, quote=False)}\n</{tag}>" if content else ""


def _render_selected_instruct(
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


def render_context(
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
    content = (
        render_text_template(template, context).strip() if template.strip() else ""
    )
    return _text_block("context", content)


def _run_message(
    *,
    agic: AgicDecl,
    rendered: tuple[tuple[AstMessage, tuple[Part, ...]], ...],
    prompt_context: str,
    primary: tuple[Part, ...],
) -> Message:
    implicit = tuple(
        parts
        for block, parts in rendered
        if block.role == "user" and not block.explicit
    )
    authored = _join_parts(*implicit)
    references_primary = any(
        block.role == "user"
        and not block.explicit
        and _PRIMARY_REFERENCE_RE.search(block.content) is not None
        for block in agic.messages
    )
    parts = _join_parts(
        (TextPart(prompt_context.strip()),) if prompt_context.strip() else (),
        authored,
        primary if (not authored or not references_primary) else (),
    )
    if parts == primary:
        return Message(role="user", parts=primary)
    return Message(role="user", parts=parts)


def _authored_messages(
    *,
    rendered: tuple[tuple[AstMessage, tuple[Part, ...]], ...],
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
            parts = _join_parts((TextPart(prompt_context.strip()),), parts)
        if not parts:
            continue
        try:
            messages.append(Message(role=block.role, parts=parts))
        except ValueError as exc:
            raise ToolangError(str(exc)) from exc
    if last_user is None and prompt_context.strip():
        messages.insert(0, Message.user(prompt_context.strip()))
    return tuple(messages) if messages else (fallback,)


def render_tool_instructions(runnable: str, *, filesystem: bool) -> str:
    """Join already framed tool guidance without escaping it again."""

    instructions = (runnable, prompts.load("filesystem.md") if filesystem else "")
    return render_text_template(
        _TOOLS_TEMPLATE, {"instructions": [item for item in instructions if item]}
    ).strip()


def initial_messages(
    *,
    agic: AgicDecl,
    rendered: tuple[tuple[AstMessage, tuple[Part, ...]], ...],
    prompt_context: str,
    primary: tuple[Part, ...],
) -> tuple[Message, ...]:
    """Combine authored messages with context and the primary input."""

    fallback = _run_message(
        agic=agic,
        rendered=rendered,
        prompt_context=prompt_context,
        primary=primary,
    )
    return _authored_messages(
        rendered=rendered,
        prompt_context=prompt_context,
        fallback=fallback,
    )


def _strip_parts(parts: tuple[Part, ...]) -> tuple[Part, ...]:
    result = list(parts)
    if result and isinstance(result[0], TextPart):
        result[0] = TextPart(result[0].text.lstrip())
    if result and isinstance(result[-1], TextPart):
        result[-1] = TextPart(result[-1].text.rstrip())
    return tuple(part for part in result if not isinstance(part, TextPart) or part.text)


def _join_parts(*groups: tuple[Part, ...]) -> tuple[Part, ...]:
    result: list[Part] = []
    for group in groups:
        if not group:
            continue
        if result:
            _append_part(result, TextPart("\n\n"))
        for part in group:
            _append_part(result, part)
    return tuple(result)


def _append_part(parts: list[Part], part: Part) -> None:
    if isinstance(part, TextPart) and parts and isinstance(parts[-1], TextPart):
        parts[-1] = TextPart(parts[-1].text + part.text)
    else:
        parts.append(part)


def control_message(control: ControlRecord) -> MessageTemplate | None:
    """Describe a control fact; the caller decides when it is visible."""

    payload = control.payload
    if isinstance(payload, RecallControlPayload):
        target = payload.target
        attrs = (
            {"workspace": target.workspace, "path": target.path}
            if isinstance(target, RulesRecallTarget)
            else {"ref": target.ref}
        )
        attrs["revision"] = payload.revision
        attributes = " ".join(
            f'{key}="{escape(value, quote=True)}"' for key, value in attrs.items()
        )
        return MessageTemplate(
            "user",
            (
                f"<{target.kind} {attributes}>",
                TypedRef(FieldRef.from_path(control.ref, "payload", "content"), "Text"),
                f"</{target.kind}>",
            ),
        )
    if not isinstance(payload, SteerControlPayload | CancelControlPayload):
        return None
    primary = payload.input.get("_")
    content = (
        (
            TypedRef(
                FieldRef.from_path(control.ref, "payload", "input", "_"),
                primary.type if isinstance(primary, TypedRef) else value_type(primary),
            ),
        )
        if primary is not None
        else ()
    )
    tag, description = (
        ("steer", prompts.load("steer.md"))
        if isinstance(payload, SteerControlPayload)
        else ("cancel", prompts.load("cancel.md"))
    )
    opening = f'<{tag} description="{description}"'
    return MessageTemplate(
        "user", (opening + ">", *content, f"</{tag}>") if content else (opening + "/>",)
    )


def output_repair_message(type_name: str | None) -> Message:
    """Request one corrected response without changing its output contract."""

    if type_name is None:  # pragma: no cover - guarded by _can_repair_output
        raise ValueError("output repair requires a declared type")
    return Message.user(
        render_text_template(prompts.load("output-repair.md"), {"type": type_name})
    )
