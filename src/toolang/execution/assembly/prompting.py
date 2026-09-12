"""Prepare provider-neutral ModelCalls for adapters.

build_model_call combines cached prompt content with per-call messages, tool
definitions, and output settings. Adapters own provider-specific serialization.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import replace
from html import escape
import re

from toolang.base.protocols.tool import Tool
from toolang.base.types.message import Message, Part, TextPart
from toolang.base.types.run import ModelCall, ModelContinuation
from toolang.base.types.tool import ToolDefinition
from toolang.common.errors import ToolangError
from toolang.common.template import render_text_template
from toolang.lang.ast import AgicDecl, Message as AstMessage, Program
from toolang.lang.input import (
    PromptDefinitionIdentity,
    PromptInvocation,
    resolve_input_parts_with_provenance,
)

from . import prompts
from .types import PreparedPrompt
from .utils import (
    assemble_messages,
    escape_markup_value,
    join_parts,
    strip_parts,
    text_block,
)
from ..records import (
    CancelControlPayload,
    ControlRecord,
    RecallControlPayload,
    SteerControlPayload,
)
from ..types import (
    FieldRef,
    MessageTemplate,
    RulesRecallTarget,
    TypedRef,
    value_type,
)

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
_PRIMARY_REFERENCE_RE = re.compile(r"{{\s*(?:[#^/]\s*)?_(?:\.[A-Za-z_][\w-]*)*\s*}}")


def build_model_call(
    prompt: PreparedPrompt,
    *,
    messages: Sequence[Message],
    far: str,
    near: Sequence[Message],
    recall: Sequence[str],
    tools: Mapping[str, Tool],
    tools_enabled: bool,
    output_schema: dict[str, object] | None,
    continuation: ModelContinuation | None,
    max_output_tokens: int,
) -> ModelCall:
    """Build the complete adapter input without applying runtime policy.

    The caller selects history and decides whether tools are enabled, including
    during output repair. Tool schemas stay structured, never prompt text.
    """

    return ModelCall(
        instructions=prompt.instructions_with_tools
        if tools_enabled
        else prompt.instructions,
        messages=assemble_messages(far, near, messages, recall),
        tools=_tool_definitions(tools) if tools_enabled else (),
        output_schema=deepcopy(output_schema),
        continuation=continuation,
        max_output_tokens=max_output_tokens,
    )


def prepare_prompt(
    program: Program,
    agic: AgicDecl,
    context: dict[str, object],
    *,
    rendered: tuple[tuple[AstMessage, tuple[Part, ...]], ...],
    primary: tuple[Part, ...],
    runnable_instructions: str,
    filesystem: bool,
) -> PreparedPrompt:
    """Render reusable frame content after authored provenance is recorded."""

    prompt_context = _render_context(program, agic, context)
    messages = _initial_messages(
        agic=agic,
        rendered=rendered,
        prompt_context=prompt_context,
        primary=primary,
    )
    instructions = _render_instructions(program, agic, context)
    with_tools = (
        _render_instructions(
            program,
            agic,
            context,
            runnable_instructions=runnable_instructions,
            filesystem=filesystem,
        )
        if runnable_instructions or filesystem
        else instructions
    )
    return PreparedPrompt(instructions, with_tools, prompt_context, messages)


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
    """Resolve authored input so the caller can record prompt provenance first."""

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
        rendered.append((block, strip_parts(resolution.parts)))
    return tuple(rendered), tuple(invocations)


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
        ("steer", "The user supplied updated input for the current task.")
        if isinstance(payload, SteerControlPayload)
        else ("cancel", "The user canceled this run.")
    )
    opening = f'<{tag} description="{description}"'
    return MessageTemplate(
        "user", (opening + ">", *content, f"</{tag}>") if content else (opening + "/>",)
    )


def _tool_definitions(tools: Mapping[str, Tool]) -> tuple[ToolDefinition, ...]:
    definitions = {name: tool.definition() for name, tool in tools.items()}
    return tuple(definitions[name] for name in sorted(definitions))


def _render_instructions(
    program: Program,
    agic: AgicDecl,
    context: dict[str, object],
    *,
    runnable_instructions: str = "",
    filesystem: bool = False,
) -> str:
    markup_context = {key: escape_markup_value(value) for key, value in context.items()}
    protocol = render_text_template(
        _PROTOCOL_TEMPLATE,
        {
            **markup_context,
            # Runtime-framed routes are already escaped; do not reinterpret them.
            "runnable_instructions": runnable_instructions,
            "filesystem": filesystem,
        },
    ).strip()
    instruct = _render_selected_instruct(program, agic, context)
    agent = text_block("agent-instructions", instruct)
    psyches = render_text_template(_PSYCHES_TEMPLATE, markup_context).strip()
    catalog = (
        render_text_template(_CAPABILITY_CATALOG_TEMPLATE, markup_context).strip()
        if context.get("has_skills") or context.get("has_services")
        else ""
    )
    return "\n\n".join(part for part in (protocol, agent, psyches, catalog) if part)


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
    content = (
        render_text_template(template, context).strip() if template.strip() else ""
    )
    return text_block("context", content)


def _initial_messages(
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
    authored = join_parts(*implicit)
    references_primary = any(
        block.role == "user"
        and not block.explicit
        and _PRIMARY_REFERENCE_RE.search(block.content) is not None
        for block in agic.messages
    )
    parts = join_parts(
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
            parts = join_parts((TextPart(prompt_context.strip()),), parts)
        if not parts:
            continue
        try:
            messages.append(Message(role=block.role, parts=parts))
        except ValueError as exc:
            raise ToolangError(str(exc)) from exc
    if last_user is None and prompt_context.strip():
        messages.insert(0, Message.user(prompt_context.strip()))
    return tuple(messages) if messages else (fallback,)
