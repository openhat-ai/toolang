"""Construct the four adapter inputs from adopted definitions and bound values."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from functools import cached_property
from hashlib import sha256
import json
import re
from typing import Literal, cast

from toolang.base.protocols.tool import Tool
from toolang.base.utils.workspace_paths import parse_cwd, workspace_uri
from toolang.base.types.message import Message, MessageRecall, Part, TextPart
from toolang.base.types.model import Model
from toolang.base.types.tool import ToolDefinition
from toolang.common.errors import ToolangError
from toolang.common.immutable import mutable_data
from toolang.common.template import render_text_template, require_template_inputs
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
from .history import HistorySelection, summary_message, SUMMARY_PREFIX, SUMMARY_SUFFIX
from .message_buffer import MessageBuffer
from .utils import (
    attribute,
    attributes,
    join_parts,
    resource_text,
    strip_parts,
    text_block,
)
from ..types import PromptSetting
from ..records import ControlRecord, RecallControlPayload
from ..types import (
    ModelMessages,
    MessageTemplate,
    StepRef,
    WorkspaceRecallTarget,
    PsycheRecallTarget,
    SkillTriggerRecallTarget,
    ServiceTriggerRecallTarget,
)
from ..values import parts_from_value

__all__ = ["instructions", "messages", "tools", "output_schema"]

_PROTOCOL = prompts.load("protocol.md").strip()
_DEFAULT_INSTRUCT_TEMPLATE = prompts.load("defaults/instruct.md")
_DEFAULT_CONTEXT_TEMPLATE = prompts.load("defaults/context.md")
_PRIMARY_REFERENCE_RE = re.compile(r"{{\s*(?:[#^/]\s*)?_(?:\.[A-Za-z_][\w-]*)*\s*}}")


def instructions(
    inputs: PromptInputs,
) -> tuple[str, tuple[RecallControlPayload, ...]]:
    """Render protocol, instruct, and the adopted resident declarations."""
    program = (
        inputs.program
        if inputs.instruct is None
        else _prompt_program(inputs.state, inputs.instruct.module, inputs.agic)
    )
    name = (
        inputs.instruct.name
        if inputs.instruct is not None
        else inputs.agic.instruct or "default"
    )
    parts = [_PROTOCOL]
    if name != "none":
        declaration = program.find_instruct(name)
        if declaration is None and name != "default":
            raise ToolangError(f"Instruct not found: {name}")
        template = declaration.body if declaration else _DEFAULT_INSTRUCT_TEMPLATE
        require_template_inputs(template, inputs.template_values)
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
                else json.dumps(
                    {
                        "description": str(cap.meta.get("description") or ""),
                        "metadata": mutable_data(
                            {
                                key: value
                                for key, value in cap.meta.items()
                                if key != "description" and value is not None
                            }
                        ),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
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
    workspace: MessageTemplate | None = None,
    workdir: str | None = None,
) -> tuple[list[Message], ModelMessages]:
    """Assemble one staged buffer and its matching durable message description.

    The executor supplies a private candidate buffer and adopts it only after
    committing the call. Historical content is never appended to the live buffer.
    """
    context, initial, _invocations = inputs.rendered_input
    if not current.started:
        current.initialize(initial)
    for control in controls:
        current.append_control(control)
    recurring = "\n".join(part for part in (workdir, context) if part)
    if workspace is not None:
        current.append_template(
            replace(
                workspace,
                content=(*workspace.content, "\n" + recurring)
                if recurring
                else workspace.content,
            ),
            lambda _ref: "",
        )
    elif recurring:
        current.append(Message.user(recurring))

    recorded = current.take_delta(step, reset=reset)
    prefix: list[Message] = []
    templates = []
    if history is not None:
        if history.far and "far" in recall:
            prefix.append(summary_message(history.far))
            if recorded.head == step:
                assert history.far_template is not None
                templates.append(
                    replace(
                        history.far_template,
                        content=(
                            SUMMARY_PREFIX,
                            *history.far_template.content,
                            SUMMARY_SUFFIX,
                        ),
                    )
                )
        if "near" in recall:
            prefix.extend(history.near)
            if recorded.head == step:
                templates.extend(history.templates)
    if templates:
        recorded = replace(recorded, delta=(*templates, *recorded.delta))
    return [*prefix, *current.messages], recorded


def workspace_message(
    names: Sequence[str], bindings: Sequence[RecallControlPayload]
) -> MessageTemplate:
    """Declare usable workspaces and retain their binding revisions internally."""
    revisions = {
        item.target.ref: item.revision
        for item in bindings
        if isinstance(item.target, WorkspaceRecallTarget) and item.target.ref in names
    }
    encoded = json.dumps(revisions, sort_keys=True, separators=(",", ":"))
    revision = sha256(encoded.encode()).hexdigest()
    value = attribute(",".join(names))
    return MessageTemplate(
        role="user",
        content=(f'<toolang:workspace list="{value}"/>',),
        tag="workspace",
        recall=MessageRecall(encoded, revision),
    )


def workdir_message(cwd: str) -> str:
    """Declare the current workdir on one Model Call."""
    name, relative = parse_cwd(cwd)
    value = workspace_uri(name, relative) if name else ""
    return f'<toolang:workdir path="{attribute(value)}"/>'


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


def _prompt_program(state: AgentState, module: str, agic: AgicDecl) -> Program:
    """A deleted module supplies no text; explicit missing dependencies still fail."""
    modules = getattr(state, "modules", None)
    if modules is not None and module not in modules:
        return Program(span=agic.span)
    return state_program(state, module)


@dataclass(frozen=True)
class PromptInputs:
    """Bound frame inputs; cache shared variables and authored input once.

    This value owns no runtime state or persistence. The executor caches it with
    its adopted frame across repeated model/tool turns.
    """

    state: AgentState
    setup: AgentSetup
    agic: AgicDecl
    runnable_name: str
    module: str
    model: Model
    caps: Sequence[StateCap]
    facts: Mapping[str, object]
    values: Mapping[str, object]
    code: AgentState | None = None
    routes: tuple[str, str, str] = ("ALL", "ALL", "ALL")
    instruct: PromptSetting | None = None
    context: PromptSetting | None = None
    entered_by: Literal["run", "exec"] = "run"

    @cached_property
    def execution_message(self) -> str:
        """Identify the active invocation independently of authored context."""
        runnable = attribute(f"{self.module}::{self.agic.kind}:{self.runnable_name}")
        return (
            f'<toolang:execution runnable="{runnable}" entered_by="{self.entered_by}"/>'
        )

    @cached_property
    def program(self) -> Program:
        program = _prompt_program(self.state, self.module, self.agic)
        if self.code is not None:
            program = replace(
                program, structs=state_program(self.code, self.module).structs
            )
        return program

    @cached_property
    def psyches(self) -> dict[str, str]:
        return {
            cap.effective_ref: cap.read_content()
            for cap in self.caps
            if cap.kind == "psyche"
        }

    @cached_property
    def template_values(self) -> dict[str, object]:
        context = {
            name: _text_template_value(value) for name, value in self.values.items()
        }
        for parameter in self.agic.params:
            if parameter.optional:
                context.setdefault(parameter.name, None)
        context.update(self.facts)
        context.update(
            {
                "toolang": {"version": toolang_version()},
                "agent": {"name": self.setup.layout.name},
                "runnable": {
                    "kind": self.agic.kind,
                    "name": self.runnable_name,
                    "output": self.agic.output,
                },
                "model": {
                    "ref": self.model.ref,
                    "provider": self.model.provider,
                    "name": self.model.name,
                    "model": self.model.id,
                    "adapter": self.model._toolang.route.adapter,
                    "base_url": self.model._toolang.route.api,
                    "tools": self.model.tool_call is True,
                    "streaming": True,
                },
            }
        )
        context["run"] = {
            **cast(Mapping[str, object], self.facts.get("run", {})),
            "program_source": state_program_source(
                self.code or self.state, self.module
            ),
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
            "_far": "Text",
            "_near": "Json[]",
            "_past": "Json[]",
        }
        bound = {name: values.get(name) for name in types}
        bound.update(
            {
                name: value
                for name, value in context.items()
                if name.startswith("_") and name != "_"
            }
        )
        cap_refs = {cap.name: cap.ref for cap in caps if cap.kind == "prompt"}
        definitions = {
            prompt.name: prompt_definition_identity(prompt, ref=cap_refs[prompt.name])
            for prompt in program.caps
            if prompt.kind == "prompt" and prompt.name in cap_refs
        }
        rendered: list[tuple[AstMessage, tuple[Part, ...]]] = []
        invocations: list[PromptInvocation] = []
        for block in agic.messages:
            require_template_inputs(block.content, bound)
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
                primary = parts_from_value(cast(Value, value))
        prompt_context = "\n".join(
            part
            for part in (
                _render_routes(self.routes),
                _render_context(
                    _prompt_program(self.state, self.context.module, agic)
                    if self.context
                    else program,
                    replace(agic, context=self.context.name) if self.context else agic,
                    context,
                ),
                self.execution_message,
            )
            if part
        )
        return (
            prompt_context,
            _initial_messages(
                agic=agic,
                rendered=tuple(rendered),
                primary=primary,
            ),
            tuple(invocations),
        )


def _text_template_value(value: object) -> object:
    if isinstance(value, Part):
        return value.text if isinstance(value, TextPart) else value.to_data()
    if isinstance(value, Array | tuple | list):
        if value and all(isinstance(item, TextPart) for item in value):
            return "".join(item.text for item in cast(Sequence[TextPart], value))
        return [_text_template_value(item) for item in value]
    if isinstance(value, Mapping):
        return {name: _text_template_value(item) for name, item in value.items()}
    return value


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


def _render_routes(scopes: tuple[str, str, str]) -> str:
    """Declare only additional restrictions for this call's visible namespace."""
    if scopes == ("ALL", "ALL", "ALL"):
        return ""
    values = dict(zip(("hands", "handoffs", "spawns"), scopes, strict=True))
    return f"<toolang:routes {attributes(values)}/>"


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
    require_template_inputs(template, values)
    if declaration is None:
        model = cast(Mapping[str, object], values["model"])
        return render_text_template(
            template,
            {
                "date": attribute(str(values["date"])),
                "timezone": attribute(str(values["timezone"])),
                "model": {
                    key: attribute(str(model[key])) for key in ("provider", "name")
                },
            },
        ).strip()
    content = render_text_template(template, values).strip() if template.strip() else ""
    return text_block("toolang:context", content)


def _initial_messages(
    *,
    agic: AgicDecl,
    rendered: tuple[tuple[AstMessage, tuple[Part, ...]], ...],
    primary: tuple[Part, ...],
) -> tuple[Message, ...]:
    """Combine authored messages with primary input, independently of runtime facts."""

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
    result: list[Message] = []
    for block, parts in blocks:
        if not parts:
            continue
        try:
            result.append(Message(role=block.role, parts=parts))
        except ValueError as exc:
            raise ToolangError(str(exc)) from exc
    return tuple(result) if result else (fallback,)
