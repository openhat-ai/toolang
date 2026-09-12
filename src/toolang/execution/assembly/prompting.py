"""Prepare provider-neutral, adapter-ready model calls.

Prompt resources define static content. History owns message composition;
adapters own provider-specific serialization.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
import json
from typing import cast

from toolang.base.protocols.tool import Tool
from toolang.base.types.message import Message, Part
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
from .history import initial_messages
from .types import PreparedPrompt
from .utils import resource_text, strip_parts, text_block
from ..records import RecallControlPayload
from ..types import (
    PsycheRecallTarget,
    SkillTriggerRecallTarget,
    ServiceTriggerRecallTarget,
    RunnableRecallTarget,
)

RUNNABLE_INFO_MAX_BYTES = 32_768
RUNNABLE_INFO_MAX_ENTRIES = 64

_PROTOCOL = prompts.load("protocol.md").strip()
_DEFAULT_INSTRUCT_TEMPLATE = prompts.load("defaults/instruct.md")
_DEFAULT_CONTEXT_TEMPLATE = prompts.load("defaults/context.md")
_SKILL_TEMPLATE = prompts.load("skills.md")
_SERVICE_TEMPLATE = prompts.load("services.md")


def build_model_call(
    prompt: PreparedPrompt,
    *,
    messages: Sequence[Message],
    tools: Mapping[str, Tool],
    tools_enabled: bool,
    output_schema: dict[str, object] | None,
    continuation: ModelContinuation | None,
    max_output_tokens: int,
) -> ModelCall:
    """Prepare all adapter fields without selecting history or serializing tools."""
    return ModelCall(
        instructions=prompt.instructions,
        messages=list(messages),
        tools=_tool_definitions(tools) if tools_enabled else (),
        output_schema=deepcopy(output_schema),
        continuation=continuation,
        max_output_tokens=max_output_tokens,
    )


def prepare_prompt(
    program: Program,
    agic: AgicDecl,
    context: Mapping[str, object],
    *,
    rendered: tuple[tuple[AstMessage, tuple[Part, ...]], ...],
    primary: tuple[Part, ...],
    runnables: Sequence[Mapping[str, object]] = (),
) -> PreparedPrompt:
    """Render resident instructions and the initial authored messages once."""
    prompt_context = _render_context(program, agic, context)
    declarations = resource_declarations(context, runnables=runnables)
    return PreparedPrompt(
        instructions=_render_instructions(
            program, agic, context, declarations=declarations
        ),
        context=prompt_context,
        messages=initial_messages(
            agic=agic,
            rendered=rendered,
            prompt_context=prompt_context,
            primary=primary,
        ),
        declarations=declarations,
    )


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


def _tool_definitions(tools: Mapping[str, Tool]) -> tuple[ToolDefinition, ...]:
    return tuple(tools[name].definition() for name in sorted(tools))


def resource_declarations(
    context: Mapping[str, object],
    *,
    runnables: Sequence[Mapping[str, object]] = (),
) -> tuple[RecallControlPayload, ...]:
    """Describe selected resources without putting guidance in trigger bodies."""
    result: list[RecallControlPayload] = []
    for key, target_type, template in (
        ("psyches", PsycheRecallTarget, None),
        ("skills", SkillTriggerRecallTarget, _SKILL_TEMPLATE),
        ("services", ServiceTriggerRecallTarget, _SERVICE_TEMPLATE),
    ):
        for item in cast(Sequence[Mapping[str, object]], context.get(key, ())):
            content = (
                str(item.get("content") or "")
                if template is None
                else render_text_template(template, item).strip()
            )
            result.append(
                RecallControlPayload(
                    target_type(str(item["ref"])), str(item["revision"]), content
                )
            )
    size = 0
    for item in runnables[:RUNNABLE_INFO_MAX_ENTRIES]:
        content = json.dumps(
            item, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        payload = RecallControlPayload(
            RunnableRecallTarget(str(item["ref"])),
            sha256(content.encode()).hexdigest(),
            content,
        )
        size += (
            len(
                resource_text(payload.target, payload.revision, payload.content).encode(
                    "utf-8"
                )
            )
            + 2
        )
        if size > RUNNABLE_INFO_MAX_BYTES:
            break
        result.append(payload)
    return tuple(result)


def _render_instructions(
    program: Program,
    agic: AgicDecl,
    context: Mapping[str, object],
    *,
    declarations: Sequence[RecallControlPayload] = (),
) -> str:
    instruct = text_block(
        "toolang:instruct", _render_selected_instruct(program, agic, context)
    )
    return "\n\n".join(
        part
        for part in (
            _PROTOCOL,
            instruct,
            *(
                resource_text(item.target, item.revision, item.content)
                for item in declarations
            ),
        )
        if part
    )


def _render_selected_instruct(
    program: Program,
    agic: AgicDecl,
    context: Mapping[str, object],
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
    context: Mapping[str, object],
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
    return text_block("toolang:context", content)
