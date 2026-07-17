"""Assemble semantic run input from a bound run and state state."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from toolang.base.protocols.model import ModelProvider
from toolang.base.protocols.tool import AgentTool
from toolang.base.types.message import Message, TextPart, message_summary, message_text
from toolang.base.types.model import ModelAlias, ModelTarget

from ..lang.ast import AgicDecl, Message as AstMessage
from toolang.state.prepared import PreparedEntry
from ..lang.source import expand_program_input
from .binding import _Run, invoke_params, run_selected_model_selector
from .context import RunSnapshot, build_run_snapshot
from .effective import (
    activation_default_model_selector,
    effective_model_selectors,
    effective_run_sets,
    directives_for,
    log_set_math,
    select_entries,
    select_origin_agic,
    select_tools,
    agic_model_refs,
)
from toolang.plugin.models.resolution import resolve_model
from .model_call import (
    assembled_instructions,
    build_model_call_assembly,
    recall_values,
    recalls_history,
    render_agic_messages,
)

if TYPE_CHECKING:
    from .store import RunStore

_TEXT_HISTORY_MESSAGE_LIMIT = 32
_LOGGER = logging.getLogger("toolang.run")


class ConfigView(Protocol):
    """Read-only activation config used by run assembly."""

    def get(self, key: str, default: object | None = None) -> object | None: ...


class SupportsRunAssembly(Protocol):
    """Runtime resources needed to assemble one immutable run input."""

    root: Path
    name: str
    home: Path
    id_state_path: Path
    store: RunStore
    model_providers: Mapping[str, ModelProvider]
    model_aliases: Mapping[str, ModelAlias]
    default_models: tuple[str, ...]
    model_environ: Mapping[str, str]
    model_cache_dir: Path
    model_cache_refresh: bool
    config: ConfigView


@dataclass(frozen=True, slots=True)
class RunInput:
    """One assembled semantic input for one run."""

    run: _Run
    agic: AgicDecl
    input_text: str
    message: Message
    context_text: str
    params: dict[str, Any]
    user_template_context: dict[str, object]
    system_template_context: dict[str, object]
    history: tuple[Message, ...]
    models_base: tuple[str, ...]
    tools_base: dict[str, AgentTool]
    snapshot: RunSnapshot
    psyches_base: tuple[PreparedEntry, ...] = field(default_factory=tuple)
    skills_base: tuple[PreparedEntry, ...] = field(default_factory=tuple)
    services_base: tuple[PreparedEntry, ...] = field(default_factory=tuple)
    debug: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_binding(cls, context: SupportsRunAssembly, run: _Run) -> RunInput:
        """Build one semantic run input from one bound run."""

        program = run.state.program
        agic = select_origin_agic(
            program,
            origin=run.origin,
            agic_name=run.executable_name,
        )
        return cls.from_agic(context, run, agic)

    @classmethod
    def from_agic(
        cls,
        context: SupportsRunAssembly,
        run: _Run,
        agic: AgicDecl,
    ) -> RunInput:
        """Build one semantic run input from a resolved agic object."""

        program = run.state.program
        input_text = (
            expand_program_input(program, run.input_text) if run.input_text else ""
        )
        history = (
            tuple(
                context.store.recent_conversation_messages(
                    thread_id=run.thread_id,
                    limit=_TEXT_HISTORY_MESSAGE_LIMIT,
                    exclude_run_id=run.run_id,
                )
            )
            if recalls_history(agic)
            else ()
        )
        params = invoke_params(run)
        if agic.input is not None and input_text:
            params = {**params, agic.input.name: input_text}
            params.setdefault("_", input_text)
        sets = effective_run_sets(context, run=run, agic=agic)
        model_math = sets.set_math.get("models")
        if isinstance(model_math, dict):
            model_math = dict(model_math)
            model_math["requested"] = run_selected_model_selector(run)
            sets.set_math["models"] = model_math
        model_context_targets = _model_context_targets(
            context,
            run=run,
            model_selectors=sets.model_selectors,
            models=sets.models,
        )
        log_set_math(run=run, agic=agic, set_math=sets.set_math)
        call = build_model_call_assembly(
            context,
            run=run,
            agic=agic,
            input_text=input_text,
            params=params,
            models=model_context_targets,
            tools=sets.tools,
            psyches=sets.psyches,
            skills=sets.skills,
            services=sets.services,
        )
        bundle = cls(
            run=run,
            agic=agic,
            input_text=input_text,
            message=call.message,
            context_text=call.context_text,
            params=params,
            user_template_context=call.user_template_context,
            system_template_context=call.system_template_context,
            history=history,
            models_base=sets.models_base,
            tools_base=sets.tools_base,
            psyches_base=sets.psyches_base,
            skills_base=sets.skills_base,
            services_base=sets.services_base,
            snapshot=build_run_snapshot(
                context,
                run,
                agic,
                tools=sets.tools,
            ),
            debug={
                "run_id": run.run_id,
                "thread_id": run.thread_id,
                "agic_name": agic.name,
                "input_text": input_text,
                "params": dict(params),
                "message_text": message_summary(call.message.parts),
                "rendered_messages": [
                    {"role": item.role, "content": item.content}
                    for item in call.rendered_messages
                ],
                "context_text": call.context_text,
                "recall": list(recall_values(agic)),
                "models_base": sets.models_base,
                "activation_default_model": activation_default_model_selector(context),
                "requested_model_selector": run_selected_model_selector(run),
                "agic_model_refs": agic_model_refs(agic),
                "effective_model_selectors": sets.model_selectors,
                "tool_names": sorted(sets.tools),
                "psyche_names": [entry.name for entry in sets.psyches],
                "skill_names": [entry.name for entry in sets.skills],
                "service_names": [entry.name for entry in sets.services],
                "set_math": sets.set_math,
                "instructions": call.instructions,
            },
        )
        _log_model_call_assembly(
            bundle,
            instructions=call.instructions,
            context_text=call.context_text,
        )
        return bundle

    def messages(self) -> tuple[Message, ...]:
        """Return the ordered input message history for one model call."""

        authored = _authored_messages(
            rendered_messages=self.rendered_messages(),
            context_text=self.context_text,
            fallback=self.message,
        )
        return (*self.history, *authored)

    def input_message(self) -> Message:
        """Return the caller-facing input message without runtime context."""

        if self.run.message is not None:
            return self.run.message
        return Message.user(self.input_text)

    def model_selector(self, context: SupportsRunAssembly) -> str | None:
        """Return the primary effective model selector for this run."""

        allowed = self.effective_model_selectors(context)
        selector = run_selected_model_selector(self.run)
        if selector is not None:
            resolve_model(
                context,
                selector=selector,
                allowed_selectors=allowed,
            )
            return selector
        return allowed[0] if allowed else None

    def effective_model_selectors(
        self, context: SupportsRunAssembly
    ) -> tuple[str, ...]:
        """Return the ordered effective model selectors for this run."""

        return effective_model_selectors(
            context,
            agic=self.agic,
            models_base=self.models_base,
        )

    def tools(self) -> dict[str, AgentTool]:
        """Return the effective tool mapping for this run."""

        return select_tools(self.tools_base, directives_for(self.agic, "tool"))

    def psyches(self) -> tuple[PreparedEntry, ...]:
        """Return the effective psyche entries for this run."""

        return select_entries(self.psyches_base, directives_for(self.agic, "psyche"))

    def skills(self) -> tuple[PreparedEntry, ...]:
        """Return the effective skill entries for this run."""

        return select_entries(self.skills_base, directives_for(self.agic, "skill"))

    def services(self) -> tuple[PreparedEntry, ...]:
        """Return the effective service entries for this run."""

        return select_entries(self.services_base, directives_for(self.agic, "service"))

    def rendered_messages(self) -> tuple[AstMessage, ...]:
        """Return authored agic messages rendered with the current params."""

        return render_agic_messages(
            self.agic.messages,
            user_context=self.user_template_context,
            system_context=self.system_template_context,
        )

    def instructions(self) -> str:
        """Return the assembled instruction text for this run."""

        return assembled_instructions(
            program=self.run.state.program,
            agic=self.agic,
            system_context=self.system_template_context,
        )

    def context(self) -> str:
        """Return the assembled context text for this run."""

        return self.context_text


def _model_context_targets(
    context: SupportsRunAssembly,
    *,
    run: _Run,
    model_selectors: tuple[str, ...],
    models: tuple[ModelTarget, ...],
) -> tuple[ModelTarget, ...]:
    selector = run_selected_model_selector(run)
    if selector is None:
        return models
    selected = resolve_model(
        context, selector=selector, allowed_selectors=model_selectors
    )
    selected_identity = _model_context_identity(selected)
    return (
        selected,
        *(
            model
            for model in models
            if _model_context_identity(model) != selected_identity
        ),
    )


def _model_context_identity(model: ModelTarget) -> tuple[str, str, str, str | None]:
    return (
        model.ref,
        model.provider,
        model.model,
        model.base_url,
    )


def _log_model_call_assembly(
    bundle: RunInput,
    *,
    instructions: str,
    context_text: str,
) -> None:
    if not _LOGGER.isEnabledFor(logging.DEBUG):
        return
    run = bundle.run
    _LOGGER.debug(
        "prompt.assembled thread=%s run=%s origin=%s agic=%s",
        run.thread_id,
        run.run_id,
        run.origin,
        bundle.agic.name,
    )
    _LOGGER.debug(
        "prompt.tools thread=%s run=%s tools=%s",
        run.thread_id,
        run.run_id,
        json.dumps(sorted(bundle.tools()), ensure_ascii=False),
    )
    _LOGGER.debug(
        "prompt.instructions thread=%s run=%s text=%s",
        run.thread_id,
        run.run_id,
        instructions,
    )
    _LOGGER.debug(
        "prompt.context thread=%s run=%s text=%s",
        run.thread_id,
        run.run_id,
        context_text,
    )
    _LOGGER.debug(
        "prompt.messages thread=%s run=%s messages=%s",
        run.thread_id,
        run.run_id,
        json.dumps(
            [
                {
                    "role": message.role,
                    "text": message_text(message.parts),
                }
                for message in bundle.messages()
            ],
            ensure_ascii=False,
        ),
    )


def _authored_messages(
    *,
    rendered_messages: tuple[AstMessage, ...],
    context_text: str,
    fallback: Message,
) -> tuple[Message, ...]:
    conversation_blocks = tuple(
        block
        for block in rendered_messages
        if block.role in {"user", "assistant", "tool"}
    )
    if not any(block.explicit for block in conversation_blocks):
        return (fallback,)

    messages: list[Message] = []
    context_pending = bool(context_text.strip())
    for block in conversation_blocks:
        text = block.content.strip()
        if not text and not (context_pending and block.role == "user"):
            continue
        if context_pending and block.role == "user":
            text = _join_message_texts(context_text, text)
            context_pending = False
        messages.append(
            Message(
                role=block.role,
                parts=(TextPart(text=text),),
            )
        )
    if context_pending:
        messages.insert(0, Message.user(context_text.strip()))
    return tuple(messages) if messages else (fallback,)


def _join_message_texts(*parts: str) -> str:
    return "\n\n".join(part.strip() for part in parts if part.strip()).strip()
