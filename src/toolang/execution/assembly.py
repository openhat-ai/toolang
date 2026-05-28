"""Assemble semantic run input from a bound run and live state."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
from typing import TYPE_CHECKING, Any

from toolang.base.protocols.tool import AgentTool
from toolang.base.types.message import Message, message_summary, message_text

from ..program import MessageBlock, Thunk
from ..state.prepared import PreparedEntry
from .binding import RunBinding, invoke_params, run_selected_model_selector
from .context import RunSnapshot, build_run_snapshot
from .effective import (
    activation_default_model_selector,
    effective_model_selectors,
    effective_run_sets,
    log_set_math,
    select_entries,
    select_origin_thunk,
    select_tools,
    thunk_model_refs,
)
from .model import resolve_model
from .model_call import (
    assembled_instructions,
    build_model_call_assembly,
    recall_values,
    recalls_history,
    render_thunk_messages,
)

if TYPE_CHECKING:
    from ..up import UptimeContext

_TEXT_HISTORY_MESSAGE_LIMIT = 32
_LOGGER = logging.getLogger("toolang.run")


@dataclass(frozen=True, slots=True)
class RunInput:
    """One assembled semantic input for one run."""

    run: RunBinding
    thunk: Thunk
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
    def from_binding(cls, context: UptimeContext, run: RunBinding) -> RunInput:
        """Build one semantic run input from one bound run."""

        program = run.live.program
        thunk = select_origin_thunk(
            program,
            origin=run.origin,
            thunk_name=run.thunk_name,
        )
        input_text = program.expand_input(run.input_text) if run.input_text else ""
        history = (
            tuple(
                context.store.recent_conversation_messages(
                    thread_id=run.thread_id,
                    limit=_TEXT_HISTORY_MESSAGE_LIMIT,
                )
            )
            if recalls_history(thunk)
            else ()
        )
        params = invoke_params(run)
        if thunk.input is not None and input_text:
            params = {thunk.input.name: input_text, **params}
        sets = effective_run_sets(context, run=run, thunk=thunk)
        model_math = sets.set_math.get("models")
        if isinstance(model_math, dict):
            model_math = dict(model_math)
            model_math["requested"] = run_selected_model_selector(run)
            sets.set_math["models"] = model_math
        log_set_math(run=run, thunk=thunk, set_math=sets.set_math)
        call = build_model_call_assembly(
            context,
            run=run,
            thunk=thunk,
            input_text=input_text,
            params=params,
            models=sets.models,
            tools=sets.tools,
            psyches=sets.psyches,
            skills=sets.skills,
            services=sets.services,
        )
        bundle = cls(
            run=run,
            thunk=thunk,
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
                thunk,
                tools=sets.tools,
            ),
            debug={
                "run_id": run.run_id,
                "thread_id": run.thread_id,
                "thunk_name": thunk.thunk_name(),
                "input_text": input_text,
                "params": dict(params),
                "message_text": message_summary(call.message.parts),
                "rendered_messages": [
                    {"kind": item.kind, "text": item.text}
                    for item in call.rendered_messages
                ],
                "context_text": call.context_text,
                "recall": list(recall_values(thunk)),
                "models_base": sets.models_base,
                "activation_default_model": activation_default_model_selector(context),
                "requested_model_selector": run_selected_model_selector(run),
                "thunk_model_refs": thunk_model_refs(thunk),
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

        if self.run.origin == "script":
            return (self.message,)
        return (*self.history, self.message)

    def input_message(self) -> Message:
        """Return the caller-facing input message without runtime context."""

        if self.run.message is not None:
            return self.run.message
        return Message.user(self.input_text)

    def model_selector(self, context: UptimeContext) -> str | None:
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

    def effective_model_selectors(self, context: UptimeContext) -> tuple[str, ...]:
        """Return the ordered effective model selectors for this run."""

        return effective_model_selectors(
            context,
            thunk=self.thunk,
            models_base=self.models_base,
        )

    def tools(self) -> dict[str, AgentTool]:
        """Return the effective tool mapping for this run."""

        return select_tools(self.tools_base, self.thunk.overlays_for("tool"))

    def psyches(self) -> tuple[PreparedEntry, ...]:
        """Return the effective psyche entries for this run."""

        return select_entries(self.psyches_base, self.thunk.overlays_for("psyche"))

    def skills(self) -> tuple[PreparedEntry, ...]:
        """Return the effective skill entries for this run."""

        return select_entries(self.skills_base, self.thunk.overlays_for("skill"))

    def services(self) -> tuple[PreparedEntry, ...]:
        """Return the effective service entries for this run."""

        return select_entries(self.services_base, self.thunk.overlays_for("service"))

    def rendered_messages(self) -> tuple[MessageBlock, ...]:
        """Return authored thunk messages rendered with the current params."""

        return render_thunk_messages(
            self.thunk.messages,
            user_context=self.user_template_context,
            system_context=self.system_template_context,
        )

    def instructions(self) -> str:
        """Return the assembled instruction text for this run."""

        return assembled_instructions(
            live_program=self.run.live.program,
            thunk=self.thunk,
            system_context=self.system_template_context,
        )

    def context(self) -> str:
        """Return the assembled context text for this run."""

        return self.context_text


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
        "computed prompt bundle run_id=%s thread_id=%s origin=%s thunk=%s",
        run.run_id,
        run.thread_id,
        run.origin,
        bundle.thunk.thunk_name(),
    )
    _LOGGER.debug("computed prompt tools=%s", json.dumps(sorted(bundle.tools()), ensure_ascii=False))
    _LOGGER.debug("computed prompt instructions=%s", instructions)
    _LOGGER.debug("computed prompt context=%s", context_text)
    _LOGGER.debug(
        "computed prompt messages=%s",
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
