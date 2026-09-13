"""Construct the four adapter inputs from adopted definitions and bound values."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from functools import cached_property
from html import escape
import json
import re
from typing import cast

from toolang.base.protocols.tool import Tool
from toolang.base.types.message import Message, Part, TextPart
from toolang.base.types.model import ModelTarget
from toolang.base.types.tool import ToolDefinition
from toolang.common.errors import ToolangError
from toolang.common.immutable import mutable_data
from toolang.common.template import render_text_template
from toolang.common.version import toolang_version
from toolang.lang.ast import AgicDecl, Message as AstMessage, Program
from toolang.lang.input import (
    PromptInvocation,
    output_json_schema,
    prompt_definition_identity,
    resolve_input_parts_with_provenance,
)
from toolang.lang.types import Array, Value
from toolang.setup import AgentSetup
from toolang.state.state import (
    AgentState,
    StateCap,
    state_program,
    state_program_source,
)

from . import prompts
from .history import HistorySelection
from .message_buffer import MessageBuffer
from .utils import join_parts, resource_text, strip_parts, text_block
from ..records import ControlRecord, RecallControlPayload
from ..types import (
    ModelMessages,
    StepRef,
    Local,
    PsycheRecallTarget,
    SkillTriggerRecallTarget,
    ServiceTriggerRecallTarget,
)
from ..values import parts_from_local

__all__ = ["instructions", "messages", "tools", "output_schema"]

ROUTE_MAX_BYTES = 32_768
ROUTE_MAX_TARGETS = 64

_PROTOCOL = prompts.load("protocol.md").strip()
_DEFAULT_INSTRUCT_TEMPLATE = prompts.load("defaults/instruct.md")
_DEFAULT_CONTEXT_TEMPLATE = prompts.load("defaults/context.md")
_PRIMARY_REFERENCE_RE = re.compile(r"{{\s*(?:[#^/]\s*)?_(?:\.[A-Za-z_][\w-]*)*\s*}}")


def instructions(
    inputs: PromptInputs,
) -> tuple[str, tuple[RecallControlPayload, ...]]:
    """Render protocol, instruct, and the adopted resident declarations."""
    program, agic = inputs.program, inputs.agic
    parts = [_PROTOCOL]
    if agic.instruct != "none":
        name = "default" if agic.instruct is None else agic.instruct
        declaration = program.find_instruct(name)
        if declaration is None and name != "default":
            raise ToolangError(f"Instruct not found: {name}")
        template = declaration.body if declaration else _DEFAULT_INSTRUCT_TEMPLATE
        content = (
            render_text_template(template, inputs.template_values).strip()
            if template.strip()
            else ""
        )
        if content:
            parts.append(text_block("toolang:instruct", content))

    declarations: list[RecallControlPayload] = []
    for kind, target_type in (
        ("psyche", PsycheRecallTarget),
        ("skill", SkillTriggerRecallTarget),
        ("service", ServiceTriggerRecallTarget),
    ):
        for cap in inputs.caps:
            if cap.kind != kind:
                continue
            content = (
                inputs.psyches[cap.effective_ref]
                if kind == "psyche"
                else "\n".join(
                    [str(cap.meta.get("description") or "")]
                    + [
                        f"{item['key']}: {item['value']}"
                        for item in _metadata_items(cap.meta)
                    ]
                ).strip()
            )
            payload = RecallControlPayload(
                target_type(cap.effective_ref), cap.revision, content
            )
            declarations.append(payload)
            parts.append(
                resource_text(payload.target, payload.revision, payload.content)
            )

    return "\n\n".join(parts), tuple(declarations)


def messages(
    inputs: PromptInputs,
    current: MessageBuffer,
    *,
    step: StepRef,
    controls: Sequence[ControlRecord] = (),
    history: HistorySelection | None = None,
    recall: Sequence[str] = ("far", "near"),
    reset: bool = False,
) -> tuple[list[Message], ModelMessages]:
    """Assemble one staged buffer and its matching durable message description.

    The executor supplies a private candidate buffer and adopts it only after
    committing the call. Historical content is never appended to the live buffer.
    """
    context, initial, _invocations = inputs.rendered_input
    if not current.started:
        current.initialize(initial)
        if history is not None and "near" in recall:
            current.prepend(*history.tail)
    elif context:
        current.append(Message.user(context))
    for control in controls:
        current.append_control(control)

    recorded = current.take_delta(step, reset=reset)
    prefix: list[Message] = []
    templates = []
    if history is not None:
        if history.far and "far" in recall:
            prefix.append(Message.user(history.far))
            if recorded.head == step:
                assert history.far_template is not None
                templates.append(history.far_template)
        if "near" in recall:
            prefix.extend(history.near)
            if recorded.head == step:
                templates.extend(history.templates)
    if templates:
        recorded = replace(recorded, delta=(*templates, *recorded.delta))
    return [*prefix, *current.messages], recorded


def tools(selected: Mapping[str, Tool]) -> tuple[ToolDefinition, ...]:
    """Describe exactly the tools already selected for this frame."""
    return tuple(selected[name].definition() for name in sorted(selected))


def output_schema(
    state: AgentState, agic: AgicDecl, *, module: str
) -> dict[str, object] | None:
    """Derive the output contract once from the invocation's adopted program."""
    program = state_program(state, module)
    return output_json_schema(
        agic.output, structs={item.name: item for item in program.structs}
    )


@dataclass(frozen=True)
class PromptInputs:
    """Bound frame inputs; cache shared variables and authored input once.

    This value owns no runtime state or persistence. The executor caches it with
    its adopted frame across repeated model/tool turns.
    """

    state: AgentState
    setup: AgentSetup
    agic: AgicDecl
    module: str
    model: ModelTarget
    caps: Sequence[StateCap]
    facts: Mapping[str, object]
    values: Mapping[str, object]
    runnables: Sequence[Mapping[str, object]] = ()

    @cached_property
    def program(self) -> Program:
        return state_program(self.state, self.module)

    @cached_property
    def psyches(self) -> dict[str, str]:
        return {
            cap.effective_ref: cap.read_content()
            for cap in self.caps
            if cap.kind == "psyche"
        }

    @cached_property
    def template_values(self) -> dict[str, object]:
        context = dict(self.facts)
        context.update(
            {
                "toolang": {"version": toolang_version()},
                "agent": {"name": self.setup.layout.name},
                "runnable": {
                    "kind": self.agic.kind,
                    "name": self.agic.name,
                    "output": self.agic.output,
                },
                "model": {
                    "ref": self.model.ref,
                    "provider": self.model.provider,
                    "name": self.model.name,
                    "model": self.model.model,
                    "adapter": self.model.adapter,
                    "base_url": self.model.base_url,
                    "tools": self.model.tools,
                    "streaming": self.model.streaming,
                },
            }
        )
        context["run"] = {
            **cast(Mapping[str, object], self.facts.get("run", {})),
            "program_source": state_program_source(self.state, self.module),
        }
        environment = self.setup.environment
        if environment is not None:
            context["environment"] = {
                "sandbox": environment.sandbox,
                "system": environment.system,
                "release": environment.release,
                "machine": environment.machine,
                "container": environment.container,
                "root": str(environment.root),
                "home": str(environment.home),
                "working_directory": str(environment.working_directory),
            }
        for kind, key in (
            ("psyche", "psyches"),
            ("skill", "skills"),
            ("service", "services"),
        ):
            entries = [
                {
                    "name": cap.name,
                    "kind": cap.kind,
                    "ref": cap.effective_ref,
                    "description": cap.meta.get("description"),
                    "content": self.psyches[cap.effective_ref]
                    if kind == "psyche"
                    else None,
                    "revision": cap.revision,
                    "metadata": mutable_data(cap.meta),
                    "metadata_items": _metadata_items(cap.meta),
                }
                for cap in self.caps
                if cap.kind == kind
            ]
            context[key] = entries
            context["has_" + key] = bool(entries)
        return context

    @cached_property
    def rendered_input(
        self,
    ) -> tuple[str, tuple[Message, ...], tuple[PromptInvocation, ...]]:
        """Render context and bound input, retaining prompt invocation provenance."""
        program, agic = self.program, self.agic
        values, context, caps = self.values, self.template_values, self.caps
        types = {
            **(
                {"_": agic.input.type_name or "Part[]"}
                if agic.input is not None
                else {}
            ),
            **{param.name: param.type_name or "Part[]" for param in agic.params},
            "far": "Text",
            "near": "Json",
        }
        bound = {
            name: values.get(name) for name in types if name not in {"far", "near"}
        }
        bound.update({name: context.get(name) for name in ("far", "near")})
        cap_refs = {cap.name: cap.ref for cap in caps if cap.kind == "prompt"}
        definitions = {
            prompt.name: prompt_definition_identity(prompt, ref=cap_refs[prompt.name])
            for prompt in program.caps
            if prompt.kind == "prompt" and prompt.name in cap_refs
        }
        rendered: list[tuple[AstMessage, tuple[Part, ...]]] = []
        invocations: list[PromptInvocation] = []
        for block in agic.messages:
            resolution = resolve_input_parts_with_provenance(
                block.content,
                program=program,
                values=bound,
                types=types,
                prompt_definitions=definitions,
            )
            offset = len(invocations)
            invocations.extend(
                replace(
                    invocation,
                    parent=invocation.parent + offset
                    if invocation.parent is not None
                    else None,
                )
                for invocation in resolution.prompts
            )
            rendered.append((block, strip_parts(resolution.parts)))
        primary: tuple[Part, ...] = ()
        if agic.input is not None and "_" in values:
            value = values["_"]
            if isinstance(value, Message):
                primary = value.parts
            elif isinstance(value, Part):
                primary = (value,)
            elif isinstance(value, Array | tuple | list) and all(
                isinstance(part, Part) for part in value
            ):
                primary = tuple(cast(Sequence[Part], value))
            else:
                primary = parts_from_local(Local(cast(Value, value)))
        prompt_context = "\n".join(
            part
            for part in (
                _render_routes(self.runnables),
                _render_context(program, agic, context),
            )
            if part
        )
        return (
            prompt_context,
            _initial_messages(
                agic=agic,
                rendered=tuple(rendered),
                prompt_context=prompt_context,
                primary=primary,
            ),
            tuple(invocations),
        )


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


def _render_routes(runnables: Sequence[Mapping[str, object]]) -> str:
    """Render complete per-call authorization snapshots, never partial lists."""
    if len(runnables) > ROUTE_MAX_TARGETS:
        raise ToolangError(
            f"Authorized routes exceed {ROUTE_MAX_TARGETS} targets. "
            "Narrow hands or handoffs."
        )
    parts = []
    for action, tag in (("run", "hands"), ("execute", "handoffs")):
        entries = [
            {key: value for key, value in item.items() if key != "actions"}
            for item in runnables
            if action in cast(Sequence[str], item["actions"])
        ]
        if entries:
            content = json.dumps(
                entries, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            parts.append(
                f'<toolang:{tag} enabled="true">\n'
                f"{escape(content, quote=False)}\n</toolang:{tag}>"
            )
        else:
            parts.append(f'<toolang:{tag} enabled="false"/>')
    result = "\n".join(parts)
    if len(result.encode("utf-8")) > ROUTE_MAX_BYTES:
        raise ToolangError(
            f"Authorized route snapshots exceed {ROUTE_MAX_BYTES} bytes. "
            "Narrow hands or handoffs."
        )
    return result


def _render_context(
    program: Program, agic: AgicDecl, values: Mapping[str, object]
) -> str:
    """Render call context separately so later calls can reuse it without input."""
    if agic.context == "none":
        return ""
    name = "default" if agic.context is None else agic.context
    declaration = program.find_context(name)
    if declaration is None and name != "default":
        raise ToolangError(f"Context not found: {name}")
    template = (
        declaration.body if declaration is not None else _DEFAULT_CONTEXT_TEMPLATE
    )
    content = render_text_template(template, values).strip() if template.strip() else ""
    return text_block("toolang:context", content)


def _initial_messages(
    *,
    agic: AgicDecl,
    rendered: tuple[tuple[AstMessage, tuple[Part, ...]], ...],
    prompt_context: str,
    primary: tuple[Part, ...],
) -> tuple[Message, ...]:
    """Combine authored messages with context and the primary input."""

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
    fallback = Message(role="user", parts=primary if parts == primary else parts)

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
    result: list[Message] = []
    for index, (block, parts) in enumerate(blocks):
        if index == last_user and prompt_context.strip():
            parts = join_parts((TextPart(prompt_context.strip()),), parts)
        if not parts:
            continue
        try:
            result.append(Message(role=block.role, parts=parts))
        except ValueError as exc:
            raise ToolangError(str(exc)) from exc
    if last_user is None and prompt_context.strip():
        result.insert(0, Message.user(prompt_context.strip()))
    return tuple(result) if result else (fallback,)
