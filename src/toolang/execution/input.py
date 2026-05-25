"""Run binding and semantic run-input assembly."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import json
import logging
from typing import TYPE_CHECKING, Any, cast

import frontmatter

from toolang.base.error import ToolangError
from toolang.base.protocols.tool import AgentTool
from toolang.base.types.message import (
    Message,
    TextPart,
    ToolCallPart,
    ToolResultPart,
    message_summary,
    message_text,
)
from toolang.base.types.model import ModelTarget
from .. import agents, caps as cap_store, work
from ..ids import LOCAL_ID_FAMILY, RUN_ID_FAMILY, allocate_id
from ..program import MessageBlock, ParamDecl, SourceSpan, Thunk, ThunkOverlay
from ..state.live import LiveState
from ..state.prepared import PreparedEntry
from ..plugin import normalize_run_loop_name
from ..tools.registry import selected_tool_names, tool_ref_for_model_tool
from .template import render_text_template
from .db import utc_now
from .model import resolve_model, select_model_selectors
from .records import RunLoop
from .records import ThreadPeer
from .snapshot import (
    RunSnapshot,
    SnapshotAgent,
    SnapshotEntry,
    SnapshotProgram,
    SnapshotRun,
    SnapshotTask,
    SnapshotTaskServices,
)

if TYPE_CHECKING:
    from ..state.program import LiveProgram
    from ..up import UptimeContext
    from .runner import RunRequest

_THREAD_THUNK_NAMES = frozenset({"chat", "task", "chore"})
_TEXT_HISTORY_MESSAGE_LIMIT = 32
_TOOL_CONTEXT_RUN_LIMIT = 50
_TOOL_CONTEXT_FACT_LIMIT = 50
_TOOL_CONTEXT_CHAR_LIMIT = 8000
_LOGGER = logging.getLogger("toolang.run")
_DEFAULT_INSTRUCT_TEMPLATE = """
You are the {{runtime.agent.name}} Toolang agent.

Runtime:
- Origin: {{runtime.origin}}
- Thread: {{runtime.thread_id}}
- Toolang root: {{runtime.agent.root}}
- Agent home: {{runtime.agent.home}}
- Agent room: {{runtime.agent.room}}
- Sandbox: {{runtime.sandbox}}
{{#runtime.server.endpoint}}- Endpoint: {{runtime.server.endpoint}}
{{/runtime.server.endpoint}}- Program source: {{runtime.program.source_path}}

Psyches:
{{#runtime.psyches}}
{{name}}:
{{content}}

{{/runtime.psyches}}
{{^runtime.psyches}}
- none

{{/runtime.psyches}}
Skills:
{{#runtime.skills}}
- {{name}} (scope={{scope}}, origin={{origin}}, form={{form}}, ref={{ref}})
{{#description}}
  description={{description}}
{{/description}}
{{#metadata_items}}
  {{key}}={{value}}
{{/metadata_items}}
{{/runtime.skills}}
{{^runtime.skills}}
- none

{{/runtime.skills}}
Services:
{{#runtime.services}}
- {{name}} (scope={{scope}}, origin={{origin}}, form={{form}}, ref={{ref}})
{{#description}}
  description={{description}}
{{/description}}
{{#metadata_items}}
  {{key}}={{value}}
{{/metadata_items}}
{{/runtime.services}}
{{^runtime.services}}
- none

{{/runtime.services}}
Tools:
{{#runtime.tools}}
- {{name}}: {{description}}
{{/runtime.tools}}
{{^runtime.tools}}
- none

{{/runtime.tools}}

Tool Result Reuse:
- Before calling a tool, check the visible prior messages and Prior Tool Results in this thread for successful tool results that already answer the request or provide reusable IDs, schemas, configuration, or other stable inputs.
- Reuse applicable prior tool results instead of repeating the same tool call.
- Call a tool again when the needed result is missing, failed, stale, expired, invalid for the current request, or the user explicitly asks to refresh it.
""".strip()
_DEFAULT_INSTRUCT_TAILS = {
    "script": """
Treat the user message as the current script input.
Work directly against the thunk contract and keep the response focused on that invocation.
Do not call tools or inspect files just to explore the environment.
Use tools only when they materially help with the script invocation.
""".strip(),
    "chat": """
Respond helpfully, clearly, and directly to the user's message.
Do not call tools or inspect files just to explore the environment.
Use tools only when the user's request requires them.
""".strip(),
    "task": """
Treat the user's message as the current task input.
{{#runtime.job}}
Current task:
- Name: {{name}}
- State: {{state}}
- Stage: {{stage}}
{{#path}}
- Path: {{path}}
{{/path}}
- Before finishing, update this task with agent_state__task_update.
- Set stage=done only when the task acceptance criteria are actually complete.
- Set stage=failed when the task is blocked, impossible, or incomplete after your attempt.
- If this task mirrors a remote work item, follow the remote item's description and acceptance criteria. Do not mark the local task done just because you fetched or verified the remote item. For non-terminal remote statuses such as Backlog, Todo, or In Progress, keep the local stage runnable (`todo` or `running`), not `done`. Mark it done only after the remote work is complete or the remote status is terminal. Reply or comment on the remote item with the outcome when appropriate, update the remote status when supported, then set the local task stage to match the remote outcome.
{{/runtime.job}}
Work the task directly and keep progress or outcome notes precise.
Do not call tools or inspect files just to explore the environment.
Use tools only when they materially help with the task.
""".strip(),
    "chore": """
Treat the user's message as the current chore input.
{{#runtime.job}}
Current chore:
- Name: {{name}}
{{#title}}
- Title: {{title}}
{{/title}}
{{#schedule}}
- Schedule: {{schedule}}
{{/schedule}}
{{#path}}
- Path: {{path}}
{{/path}}
{{/runtime.job}}
Complete the chore directly and keep the result concise.
When creating or updating local tasks that mirror remote work items, include the remote title, description, link, update timestamp, status, and clear execution instructions: complete the remote item's requested work, reply or comment on the remote item with the result when appropriate, update the remote status when supported, and keep the local task stage aligned with the remote status. Before creating a mirror task, list existing active and archived tasks and match by remote_ref, remote URL, or remote id; update the existing mirror instead of creating another local task for the same remote item. Treat remote status and local stage as sync inputs, not just remote updated timestamps: if the remote item has a non-terminal status but the local task is `done` or `failed`, update the local task back to `todo` even when remote updatedAt did not change. If an existing mirror task is already `running`, update its body/status metadata if needed but do not set its stage back to `todo`; let the active run finish.
Do not call tools or inspect files just to explore the environment.
Use tools only when they materially help with the chore.
""".strip(),
}


@dataclass(frozen=True, slots=True)
class RunBinding:
    """One run bound to immutable live state and runtime ids."""

    run_id: str
    group: str
    origin: str
    thread_id: str
    thunk_name: str | None
    input_text: str
    message: Message | None
    model_selector: str | None
    run_loop: RunLoop
    metadata: dict[str, Any]
    live: LiveState
    created_at: str


@dataclass(frozen=True, slots=True)
class _ToolHistoryFact:
    key: str
    text: str


@dataclass(frozen=True, slots=True)
class RunInput:
    """One assembled semantic input for one run."""

    run: RunBinding
    thunk: Thunk
    input_text: str
    message: Message
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
    tool_history_context: str = ""
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
        history = tuple(
            context.store.recent_text_conversation_messages(
                thread_id=run.thread_id,
                limit=_TEXT_HISTORY_MESSAGE_LIMIT,
            )
        )
        tool_history_context = _tool_history_context(context, thread_id=run.thread_id)
        models_base = _activation_allowed_model_selectors(context)
        tools_base = _run_tools_base(context, run)
        psyches_base = _cap_entries(run.live, kind="psyche")
        skills_base = _cap_entries(run.live, kind="skill")
        services_base = _cap_entries(run.live, kind="service")
        params = _invoke_params(run)
        if thunk.input is not None and input_text:
            params = {thunk.input.name: input_text, **params}
        effective_tools, tool_math = _select_tools_with_trace(
            tools_base,
            thunk.overlays_for("tool"),
        )
        effective_models, model_math = _model_set_math(
            context,
            thunk=thunk,
            models_base=models_base,
        )
        model_math["requested"] = _run_selected_model_selector(run)
        resolved_models = _resolve_runtime_models(context, effective_models)
        effective_psyches, psyche_math = _select_entries_with_trace(
            psyches_base,
            thunk.overlays_for("psyche"),
        )
        effective_skills, skill_math = _select_entries_with_trace(
            skills_base,
            thunk.overlays_for("skill"),
        )
        effective_services, service_math = _select_entries_with_trace(
            services_base,
            thunk.overlays_for("service"),
        )
        set_math: dict[str, object] = {
            "models": model_math,
            "tools": tool_math,
            "psyches": psyche_math,
            "skills": skill_math,
            "services": service_math,
        }
        _log_set_math(run=run, thunk=thunk, set_math=set_math)
        user_template_context = _user_template_context(
            context,
            run=run,
            thunk=thunk,
            params=params,
        )
        system_template_context = _system_template_context(
            context,
            run=run,
            thunk=thunk,
            params=params,
            models=resolved_models,
            tools=effective_tools,
            psyches=effective_psyches,
            skills=effective_skills,
            services=effective_services,
        )
        rendered_messages = _render_thunk_messages(
            thunk.messages,
            user_context=user_template_context,
            system_context=system_template_context,
        )
        message = _run_message(
            run=run,
            thunk=thunk,
            input_text=input_text,
            rendered_messages=rendered_messages,
        )
        return cls(
            run=run,
            thunk=thunk,
            input_text=input_text,
            message=message,
            params=params,
            user_template_context=user_template_context,
            system_template_context=system_template_context,
            history=history,
            models_base=models_base,
            tools_base=tools_base,
            psyches_base=psyches_base,
            skills_base=skills_base,
            services_base=services_base,
            tool_history_context=tool_history_context,
            snapshot=_runtime_snapshot(
                context,
                run,
                thunk,
                tools=effective_tools,
            ),
            debug={
                "run_id": run.run_id,
                "thread_id": run.thread_id,
                "thunk_name": _thunk_name(thunk),
                "input_text": input_text,
                "params": dict(params),
                "message_text": message_summary(message.parts),
                "rendered_messages": [
                    {"kind": item.kind, "text": item.text}
                    for item in rendered_messages
                ],
                "models_base": models_base,
                "activation_default_model": _activation_default_model_selector(context),
                "requested_model_selector": _run_selected_model_selector(run),
                "thunk_model_refs": _thunk_model_refs(thunk),
                "effective_model_selectors": effective_models,
                "tool_names": sorted(effective_tools),
                "psyche_names": [entry.name for entry in effective_psyches],
                "skill_names": [entry.name for entry in effective_skills],
                "service_names": [entry.name for entry in effective_services],
                "set_math": set_math,
                "tool_history_context": tool_history_context,
                "instructions": _assembled_instructions(
                    live_program=run.live.program,
                    origin=run.origin,
                    thunk=thunk,
                    rendered_messages=rendered_messages,
                    system_context=system_template_context,
                    tool_history_context=tool_history_context,
                ),
            },
        )

    def messages(self) -> tuple[Message, ...]:
        """Return the ordered input message history for one model call."""

        if self.run.origin == "script":
            return (self.message,)
        return (*self.history, self.message)

    def model_selector(self, context: UptimeContext) -> str | None:
        """Return the primary effective model selector for this run."""

        allowed = self.effective_model_selectors(context)
        selector = _run_selected_model_selector(self.run)
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

        return _effective_model_selectors(
            context,
            thunk=self.thunk,
            models_base=self.models_base,
        )

    def tools(self) -> dict[str, AgentTool]:
        """Return the effective tool mapping for this run."""

        return _select_tools(self.tools_base, self.thunk.overlays_for("tool"))

    def psyches(self) -> tuple[PreparedEntry, ...]:
        """Return the effective psyche entries for this run."""

        return _select_entries(self.psyches_base, self.thunk.overlays_for("psyche"))

    def skills(self) -> tuple[PreparedEntry, ...]:
        """Return the effective skill entries for this run."""

        return _select_entries(self.skills_base, self.thunk.overlays_for("skill"))

    def services(self) -> tuple[PreparedEntry, ...]:
        """Return the effective service entries for this run."""

        return _select_entries(self.services_base, self.thunk.overlays_for("service"))

    def rendered_messages(self) -> tuple[MessageBlock, ...]:
        """Return authored thunk messages rendered with the current params."""

        return _render_thunk_messages(
            self.thunk.messages,
            user_context=self.user_template_context,
            system_context=self.system_template_context,
        )

    def instructions(self) -> str:
        """Return the assembled instruction text for this run."""

        return _assembled_instructions(
            live_program=self.run.live.program,
            origin=self.run.origin,
            thunk=self.thunk,
            rendered_messages=self.rendered_messages(),
            system_context=self.system_template_context,
            tool_history_context=self.tool_history_context,
        )


def _tool_history_context(context: UptimeContext, *, thread_id: str) -> str:
    runs = sorted(
        context.store.list_runs(thread_id=thread_id, limit=_TOOL_CONTEXT_RUN_LIMIT),
        key=lambda item: item.created_at,
    )
    if not runs:
        return ""
    steps_by_run = context.store.list_steps_for_runs(run_ids=tuple(run.run_id for run in runs))
    calls_by_id: dict[str, ToolCallPart] = {}
    facts_by_key: dict[str, _ToolHistoryFact] = {}
    for run in runs:
        for step in steps_by_run.get(run.run_id, ()):
            for part in step.output:
                if isinstance(part, ToolCallPart):
                    calls_by_id[part.tool_call_id] = part
                    continue
                if not isinstance(part, ToolResultPart):
                    continue
                fact = _tool_history_fact(part, calls_by_id.get(part.tool_call_id))
                if fact is None:
                    continue
                if fact.key in facts_by_key:
                    del facts_by_key[fact.key]
                facts_by_key[fact.key] = fact
    facts = list(facts_by_key.values())[-_TOOL_CONTEXT_FACT_LIMIT:]
    if not facts:
        return ""
    lines = [
        "Prior Tool Results:",
        "Use these reusable facts from visible prior runs in this thread before deciding whether to call a tool again.",
    ]
    total = sum(len(line) + 1 for line in lines)
    for fact in facts:
        line = f"- {fact.text}"
        remaining = _TOOL_CONTEXT_CHAR_LIMIT - total
        if remaining <= 0:
            break
        if len(line) > remaining:
            line = _limit_text(line, remaining)
        lines.append(line)
        total += len(line) + 1
    return "\n".join(lines)


def _tool_history_fact(
    result: ToolResultPart,
    call: ToolCallPart | None,
) -> _ToolHistoryFact | None:
    call_input = dict(call.input) if call is not None else {}
    if result.tool_name == "service_use__init":
        return _service_use_init_fact(result, call_input)
    if result.tool_name == "service_use__tool_list":
        return _service_use_tool_list_fact(result, call_input)
    if result.tool_name == "service_use__tool_call":
        return _service_use_tool_call_fact(result, call_input)
    return _generic_tool_fact(result, call_input)


def _service_use_init_fact(
    result: ToolResultPart,
    call_input: Mapping[str, object],
) -> _ToolHistoryFact:
    service = _service_name(result.output, call_input)
    status = _tool_result_status(result.output)
    if status == "failed":
        text = f"service_use__init service={service or '<unknown>'} failed: {_error_summary(result.output)}."
        return _ToolHistoryFact(key=f"service_use__init:{service}:failed", text=text)
    text = f"service_use__init service={service or '<unknown>'} succeeded; session is available."
    return _ToolHistoryFact(key=f"service_use__init:{service}:success", text=text)


def _service_use_tool_list_fact(
    result: ToolResultPart,
    call_input: Mapping[str, object],
) -> _ToolHistoryFact:
    service = _service_name(result.output, call_input)
    status = _tool_result_status(result.output)
    if status == "failed":
        text = f"service_use__tool_list service={service or '<unknown>'} failed: {_error_summary(result.output)}."
        return _ToolHistoryFact(key=f"service_use__tool_list:{service}:failed", text=text)
    tools = _service_tool_summaries(result.output)
    if tools:
        text = (
            f"service_use__tool_list service={service or '<unknown>'} succeeded; "
            f"reuse these schemas unless stale: {_join_with_limit(tools, 5000)}."
        )
    else:
        text = f"service_use__tool_list service={service or '<unknown>'} succeeded."
    return _ToolHistoryFact(key=f"service_use__tool_list:{service}:success", text=text)


def _service_use_tool_call_fact(
    result: ToolResultPart,
    call_input: Mapping[str, object],
) -> _ToolHistoryFact:
    service = _optional_text(call_input.get("service"))
    service_tool = _optional_text(call_input.get("tool_name"))
    service_input = _as_mapping(call_input.get("input")) or {}
    key_base = f"service_use__tool_call:{service}:{service_tool}:{_short_json(service_input, limit=240)}"
    label = f"service_use__tool_call {service or '<unknown>'}.{service_tool or '<unknown>'}"
    status = _tool_result_status(result.output)
    if status == "failed":
        text = (
            f"{label} with input={_short_json(service_input, limit=320)} failed: "
            f"{_error_summary(result.output)}. Do not repeat this shape unless corrected."
        )
        return _ToolHistoryFact(key=f"{key_base}:failed", text=text)
    if service_tool == "list_teams":
        teams = _linear_team_summaries(result.output)
        if teams:
            text = f"{label} succeeded; known teams: {_join_with_limit(teams, 1200)}."
            return _ToolHistoryFact(key=f"service_use__tool_call:{service}:list_teams:success", text=text)
    if service_tool == "save_issue":
        issue = _linear_issue_summary(result.output)
        if issue:
            text = f"{label} succeeded; created issue {issue}."
            return _ToolHistoryFact(key=f"{key_base}:success", text=text)
    text = (
        f"{label} with input={_short_json(service_input, limit=320)} succeeded; "
        f"output summary: {_output_summary(result.output, limit=700)}."
    )
    return _ToolHistoryFact(key=f"{key_base}:success", text=text)


def _generic_tool_fact(
    result: ToolResultPart,
    call_input: Mapping[str, object],
) -> _ToolHistoryFact | None:
    status = _tool_result_status(result.output)
    input_summary = _short_json(call_input, limit=320)
    if status == "failed":
        text = (
            f"{result.tool_name} input={input_summary} failed: {_error_summary(result.output)}. "
            "Do not repeat this shape unless corrected."
        )
        return _ToolHistoryFact(key=f"{result.tool_name}:{input_summary}:failed", text=text)
    summary = _output_summary(result.output, limit=700)
    if not summary:
        return None
    text = f"{result.tool_name} input={input_summary} succeeded; output summary: {summary}."
    return _ToolHistoryFact(key=f"{result.tool_name}:{input_summary}:success", text=text)


def _service_name(
    output: Mapping[str, object],
    call_input: Mapping[str, object],
) -> str | None:
    service = _optional_text(call_input.get("service"))
    if service is not None:
        return service
    result = _as_mapping(output.get("result"))
    if result is None:
        return None
    return _optional_text(result.get("service"))


def _service_tool_summaries(output: Mapping[str, object]) -> list[str]:
    result = _as_mapping(output.get("result"))
    service_result = _as_mapping(result.get("result")) if result is not None else None
    tools = service_result.get("tools") if service_result is not None else None
    if not isinstance(tools, list):
        return []
    summaries: list[str] = []
    for item in tools:
        tool = _as_mapping(item)
        if tool is None:
            continue
        name = _optional_text(tool.get("name"))
        if name is None:
            continue
        schema = _as_mapping(tool.get("inputSchema"))
        required = _string_list(schema.get("required")) if schema is not None else []
        properties = _as_mapping(schema.get("properties")) if schema is not None else None
        property_names = list(properties) if properties is not None else []
        optional = [name for name in property_names if name not in required]
        summaries.append(
            f"{name}(required: {_comma_or_none(required)}; optional: {_comma_or_none(optional[:8])})"
        )
    return summaries


def _linear_team_summaries(output: Mapping[str, object]) -> list[str]:
    content = _as_mapping(_service_content_json(output))
    if content is None:
        return []
    teams = content.get("teams")
    if not isinstance(teams, list):
        return []
    results: list[str] = []
    for item in teams:
        team = _as_mapping(item)
        if team is None:
            continue
        name = _optional_text(team.get("name"))
        team_id = _optional_text(team.get("id"))
        if name is None:
            continue
        results.append(f"{name} ({team_id})" if team_id else name)
    return results


def _linear_issue_summary(output: Mapping[str, object]) -> str | None:
    content = _as_mapping(_service_content_json(output))
    if content is None:
        return None
    issue_id = _optional_text(content.get("id"))
    title = _optional_text(content.get("title"))
    url = _optional_text(content.get("url"))
    team = _as_mapping(content.get("team"))
    team_name = _optional_text(team.get("name")) if team is not None else None
    state = _as_mapping(content.get("status")) or _as_mapping(content.get("state"))
    state_name = _optional_text(state.get("name")) if state is not None else None
    parts = [item for item in (issue_id, title, f"team={team_name}" if team_name else None, state_name, url) if item]
    return " | ".join(parts) if parts else None


def _service_content_json(output: Mapping[str, object]) -> object | None:
    text = _service_content_text(output)
    if text is None:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _service_content_text(output: Mapping[str, object]) -> str | None:
    result = _as_mapping(output.get("result"))
    service_result = _as_mapping(result.get("result")) if result is not None else None
    content = service_result.get("content") if service_result is not None else None
    if not isinstance(content, list):
        return None
    for item in content:
        entry = _as_mapping(item)
        if entry is None:
            continue
        text = _optional_text(entry.get("text"))
        if text is not None:
            return text
    return None


def _tool_result_status(output: Mapping[str, object]) -> str:
    if output.get("ok") is False:
        return "failed"
    if _contains_error_flag(output):
        return "failed"
    text = _service_content_text(output)
    if text is not None and text.strip().lower().startswith("error:"):
        return "failed"
    return "succeeded"


def _contains_error_flag(value: object) -> bool:
    mapping = _as_mapping(value)
    if mapping is not None:
        if mapping.get("isError") is True or mapping.get("is_error") is True:
            return True
        return any(_contains_error_flag(item) for item in mapping.values())
    if isinstance(value, list):
        return any(_contains_error_flag(item) for item in value)
    return False


def _error_summary(output: Mapping[str, object]) -> str:
    error = output.get("error")
    if error is not None:
        return _limit_text(str(error), 500)
    text = _service_content_text(output)
    if text is not None:
        return _limit_text(" ".join(text.split()), 500)
    return _output_summary(output, limit=500) or "unknown error"


def _output_summary(output: Mapping[str, object], *, limit: int) -> str:
    content_text = _service_content_text(output)
    if content_text is not None:
        return _limit_text(" ".join(content_text.split()), limit)
    return _short_json(output, limit=limit)


def _as_mapping(value: object) -> Mapping[str, object] | None:
    return cast(Mapping[str, object], value) if isinstance(value, Mapping) else None


def _optional_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


def _comma_or_none(values: list[str]) -> str:
    return ", ".join(values) if values else "none"


def _short_json(value: object, *, limit: int) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except TypeError:
        text = str(value)
    return _limit_text(text, limit)


def _join_with_limit(values: list[str], limit: int) -> str:
    items: list[str] = []
    total = 0
    for value in values:
        addition = len(value) + (2 if items else 0)
        if total + addition > limit:
            remaining = len(values) - len(items)
            if remaining > 0:
                items.append(f"... {remaining} more")
            break
        items.append(value)
        total += addition
    return ", ".join(items)


def _limit_text(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit <= 3:
        return "." * limit
    return f"{text[: limit - 3]}..."


def bind_run_request(
    context: UptimeContext,
    request: RunRequest,
    *,
    live: LiveState | None = None,
) -> RunBinding:
    """Bind one queued run request to immutable runtime inputs."""

    bound_live = live or context.live
    thread_id = request.thread_id or _new_thread_id(context, request.origin)
    thread_peer = _request_thread_peer(request.metadata)
    context.store.ensure_thread(
        thread_id=thread_id,
        origin=request.origin,
        peer=thread_peer,
    )
    run_loop = normalize_run_loop_name(request.run_loop)
    return RunBinding(
        run_id=request.run_id or allocate_run_id(context),
        group=request.group,
        origin=request.origin,
        thread_id=thread_id,
        thunk_name=request.thunk_name,
        input_text=_request_input_text(request),
        message=request.message,
        model_selector=_request_model_selector(request),
        run_loop=run_loop,
        metadata=dict(request.metadata),
        live=bound_live,
        created_at=utc_now(),
    )


def allocate_run_id(context: UptimeContext) -> str:
    value = allocate_id(
        agents.agent_id_state_path(context.root, context.name),
        family=RUN_ID_FAMILY,
    ).value
    return f"run_{value}"


def _new_thread_id(context: UptimeContext, origin: str) -> str:
    value = allocate_id(
        agents.agent_id_state_path(context.root, context.name),
        family=LOCAL_ID_FAMILY,
    ).value
    return f"{_thread_id_kind(origin)}_{value}"


def _thread_id_kind(origin: str) -> str:
    text = "".join(char for char in origin.strip().lower() if char.isalnum())
    return text or "thread"


def _request_thread_peer(metadata: Mapping[str, Any]) -> ThreadPeer | None:
    raw = metadata.get("thread_peer")
    if not isinstance(raw, Mapping):
        return None
    return ThreadPeer.from_data(cast(Mapping[str, Any], raw))


def select_origin_thunk(
    program: LiveProgram,
    *,
    origin: str,
    thunk_name: str | None = None,
) -> Thunk:
    """Return the effective thunk for one run origin."""

    if thunk_name is not None:
        return program.get_thunk(thunk_name)
    if origin in _THREAD_THUNK_NAMES:
        thunk = _find_named_thunk(program.thunks, origin)
        if thunk is not None:
            return thunk
        return _default_thread_thunk(origin)
    return program.get_thunk(None)


def effective_origin_model_selectors(
    context: UptimeContext,
    *,
    origin: str,
    thunk_name: str | None = None,
) -> tuple[str, ...]:
    """Return effective model selectors for one run origin before per-run selection."""

    thunk = select_origin_thunk(
        context.live.program,
        origin=origin,
        thunk_name=thunk_name,
    )
    return _effective_model_selectors(
        context,
        thunk=thunk,
        models_base=_activation_allowed_model_selectors(context),
    )


def _find_named_thunk(thunks: tuple[Thunk, ...], name: str) -> Thunk | None:
    for thunk in thunks:
        if thunk.thunk_name() == name:
            return thunk
    return None


def _default_thread_thunk(origin: str) -> Thunk:
    return Thunk(
        name=origin,
        span=_synthetic_span(),
    )


def _default_instruct_template(origin: str) -> str:
    tail = _DEFAULT_INSTRUCT_TAILS.get(origin) or _DEFAULT_INSTRUCT_TAILS["script"]
    return f"{_DEFAULT_INSTRUCT_TEMPLATE}\n\n{tail}".strip()


def _synthetic_span() -> SourceSpan:
    return SourceSpan(0)


def _run_message(
    *,
    run: RunBinding,
    thunk: Thunk,
    input_text: str,
    rendered_messages: tuple[MessageBlock, ...],
) -> Message:
    if run.origin != "script" and run.message is not None:
        return _expanded_run_message(run.message, input_text=input_text)
    if run.origin != "script":
        return Message.user(input_text)
    text = _script_message_text(
        thunk=thunk,
        input_text=input_text,
        rendered_messages=rendered_messages,
    )
    return Message.user(text)


def _expanded_run_message(message: Message, *, input_text: str) -> Message:
    original_text = message_text(message.parts)
    if not input_text.strip() or input_text == original_text:
        return message
    parts = [part for part in message.parts if not isinstance(part, TextPart)]
    return Message(
        role=message.role,
        parts=(TextPart(text=input_text), *parts),
        meta=dict(message.meta),
    )


def _run_tools_base(context: UptimeContext, run: RunBinding) -> dict[str, AgentTool]:
    del run
    return context.tools


def _request_input_text(request: RunRequest) -> str:
    if request.thunk:
        return request.thunk
    if request.message is None:
        return ""
    return message_text(request.message.parts)


def _request_model_selector(request: RunRequest) -> str | None:
    if isinstance(request.model_selector, str) and request.model_selector.strip():
        return request.model_selector.strip()
    return None


def _run_selected_model_selector(run: RunBinding) -> str | None:
    if isinstance(run.model_selector, str) and run.model_selector.strip():
        return run.model_selector.strip()
    for key in ("model", "model_selector"):
        value = run.metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _activation_default_model_selector(context: UptimeContext) -> str | None:
    value = context.config.get("models.default_selector")
    if not isinstance(value, str):
        return None
    selector = value.strip()
    return selector or None


def _activation_allowed_model_selectors(context: UptimeContext) -> tuple[str, ...]:
    value = context.config.get("models.allowed_selectors")
    if isinstance(value, tuple):
        return tuple(item for item in value if isinstance(item, str) and item.strip())
    if isinstance(value, list):
        return tuple(item for item in value if isinstance(item, str) and item.strip())
    return ()


def _effective_model_selectors(
    context: UptimeContext,
    *,
    thunk: Thunk,
    models_base: tuple[str, ...],
) -> tuple[str, ...]:
    effective, _math = _model_set_math(
        context,
        thunk=thunk,
        models_base=models_base,
    )
    return effective


def _model_set_math(
    context: UptimeContext,
    *,
    thunk: Thunk,
    models_base: tuple[str, ...],
) -> tuple[tuple[str, ...], dict[str, object]]:
    thunk_selectors, thunk_steps = _apply_string_overlays_with_trace(
        (),
        thunk.overlays_for("model"),
    )
    effective = select_model_selectors(
        context,
        thunk_selectors=thunk_selectors,
        activation_selectors=models_base,
        default_selector=_activation_default_model_selector(context),
    )
    return (
        effective,
        {
            "activation_ceiling": list(models_base),
            "activation_default": _activation_default_model_selector(context),
            "requested": None,
            "thunk_overlay_base": [],
            "thunk_overlay_steps": thunk_steps,
            "thunk_selectors": list(thunk_selectors),
            "effective": list(effective),
        },
    )


def _thunk_model_refs(thunk: Thunk) -> tuple[str, ...]:
    return _apply_string_overlays((), thunk.overlays_for("model"))


def _select_tools(
    tools_base: dict[str, AgentTool],
    overlays: tuple[ThunkOverlay, ...],
) -> dict[str, AgentTool]:
    selected, _math = _select_tools_with_trace(tools_base, overlays)
    return selected


def _select_tools_with_trace(
    tools_base: dict[str, AgentTool],
    overlays: tuple[ThunkOverlay, ...],
) -> tuple[dict[str, AgentTool], dict[str, object]]:
    names, steps = _apply_tool_overlays_with_trace(tools_base, overlays)
    selected = {
        name: tools_base[name]
        for name in names
        if name in tools_base
    }
    return (
        selected,
        {
            "activation_ceiling": list(tools_base),
            "overlay_steps": steps,
            "effective": list(selected),
        },
    )


def _apply_tool_overlays(
    tools_base: dict[str, AgentTool],
    overlays: tuple[ThunkOverlay, ...],
) -> tuple[str, ...]:
    names, _steps = _apply_tool_overlays_with_trace(tools_base, overlays)
    return names


def _apply_tool_overlays_with_trace(
    tools_base: dict[str, AgentTool],
    overlays: tuple[ThunkOverlay, ...],
) -> tuple[tuple[str, ...], list[dict[str, object]]]:
    current = list(tools_base)
    refs_by_model_name = {
        name: tool_ref_for_model_tool(name, tool)
        for name, tool in tools_base.items()
    }
    steps: list[dict[str, object]] = []
    for overlay in overlays:
        selectors = tuple(item for item in overlay.items if item)
        before = tuple(current)
        matches = selected_tool_names(refs_by_model_name, selectors)
        if overlay.op == "set":
            current = list(matches)
        elif overlay.op == "add":
            for name in matches:
                if name not in current:
                    current.append(name)
        elif overlay.op == "remove":
            blocked = set(matches)
            current = [name for name in current if name not in blocked]
        steps.append(
            _overlay_step(
                overlay=overlay,
                selectors=selectors,
                matches=matches,
                before=before,
                after=tuple(current),
            )
        )
    return tuple(current), steps


def _select_entries(
    base: tuple[PreparedEntry, ...],
    overlays: tuple[ThunkOverlay, ...],
) -> tuple[PreparedEntry, ...]:
    selected, _math = _select_entries_with_trace(base, overlays)
    return selected


def _select_entries_with_trace(
    base: tuple[PreparedEntry, ...],
    overlays: tuple[ThunkOverlay, ...],
) -> tuple[tuple[PreparedEntry, ...], dict[str, object]]:
    entries, steps = _apply_cap_overlays_with_trace(base, overlays)
    return (
        entries,
        {
            "activation_ceiling": [_entry_label(entry) for entry in base],
            "overlay_steps": steps,
            "effective": [_entry_label(entry) for entry in entries],
        },
    )


def _apply_cap_overlays(
    base: tuple[PreparedEntry, ...],
    overlays: tuple[ThunkOverlay, ...],
) -> tuple[PreparedEntry, ...]:
    entries, _steps = _apply_cap_overlays_with_trace(base, overlays)
    return entries


def _apply_cap_overlays_with_trace(
    base: tuple[PreparedEntry, ...],
    overlays: tuple[ThunkOverlay, ...],
) -> tuple[tuple[PreparedEntry, ...], list[dict[str, object]]]:
    current = list(base)
    kind = base[0].kind if base else None
    agent_name = _entry_agent_name(base)
    steps: list[dict[str, object]] = []
    for overlay in overlays:
        selectors = tuple(item for item in overlay.items if item)
        before = tuple(current)
        matches = cap_store.select_cap_entries(
            base,
            selectors,
            agent_name=agent_name,
            implicit_kind=kind,
        )
        if overlay.op == "set":
            current = list(matches)
        elif overlay.op == "add":
            seen = {_entry_identity(entry) for entry in current}
            for entry in matches:
                identity = _entry_identity(entry)
                if identity not in seen:
                    current.append(entry)
                    seen.add(identity)
        elif overlay.op == "remove":
            blocked = {_entry_identity(entry) for entry in matches}
            current = [entry for entry in current if _entry_identity(entry) not in blocked]
        steps.append(
            _overlay_step(
                overlay=overlay,
                selectors=selectors,
                matches=tuple(_entry_label(entry) for entry in matches),
                before=tuple(_entry_label(entry) for entry in before),
                after=tuple(_entry_label(entry) for entry in current),
            )
        )
    return tuple(current), steps


def _apply_string_overlays_with_trace(
    base: tuple[str, ...],
    overlays: tuple[ThunkOverlay, ...],
) -> tuple[tuple[str, ...], list[dict[str, object]]]:
    current = list(dict.fromkeys(item for item in base if item))
    steps: list[dict[str, object]] = []
    for overlay in overlays:
        overlay_items = tuple(item for item in overlay.items if item)
        before = tuple(current)
        if overlay.op == "set":
            current = list(dict.fromkeys(overlay_items))
        elif overlay.op == "add":
            for item in overlay_items:
                if item not in current:
                    current.append(item)
        elif overlay.op == "remove":
            blocked = set(overlay_items)
            current = [item for item in current if item not in blocked]
        steps.append(
            _overlay_step(
                overlay=overlay,
                selectors=overlay_items,
                matches=overlay_items,
                before=before,
                after=tuple(current),
            )
        )
    return tuple(current), steps


def _overlay_step(
    *,
    overlay: ThunkOverlay,
    selectors: tuple[str, ...],
    matches: tuple[str, ...],
    before: tuple[str, ...],
    after: tuple[str, ...],
) -> dict[str, object]:
    return {
        "op": overlay.op,
        "line": overlay.span.line,
        "selectors": list(selectors),
        "matches": list(matches),
        "before": list(before),
        "after": list(after),
    }


def _entry_label(entry: PreparedEntry) -> str:
    return f"{entry.kind}/{entry.name}"


def _log_set_math(*, run: RunBinding, thunk: Thunk, set_math: dict[str, object]) -> None:
    if not _LOGGER.isEnabledFor(logging.INFO):
        return
    _LOGGER.info(
        "activation set math run_id=%s thunk=%s %s",
        run.run_id,
        _thunk_name(thunk),
        _set_math_summary(set_math),
    )
    if _LOGGER.isEnabledFor(logging.DEBUG):
        _LOGGER.debug(
            "activation set math detail run_id=%s thunk=%s math=%s",
            run.run_id,
            _thunk_name(thunk),
            json.dumps(set_math, ensure_ascii=False, sort_keys=True),
        )


def _set_math_summary(set_math: dict[str, object]) -> str:
    parts: list[str] = []
    for domain in ("models", "tools", "psyches", "skills", "services"):
        value = set_math.get(domain)
        if isinstance(value, dict):
            parts.append(_domain_set_math_summary(domain, cast(dict[str, object], value)))
    return "; ".join(parts)


def _domain_set_math_summary(domain: str, value: dict[str, object]) -> str:
    base = value.get("activation_ceiling")
    effective = value.get("effective")
    base_count = len(base) if isinstance(base, list) else 0
    effective_count = len(effective) if isinstance(effective, list) else 0
    steps = value.get("thunk_overlay_steps")
    if not isinstance(steps, list):
        steps = value.get("overlay_steps")
    expression = _set_math_expression(cast(list[object], steps) if isinstance(steps, list) else [])
    return f"{domain} {base_count} {expression} -> {effective_count}"


def _set_math_expression(steps: list[object]) -> str:
    if not steps:
        return "activation"
    expressions: list[str] = []
    for step in steps:
        if not isinstance(step, dict):
            continue
        step_data = cast(dict[str, object], step)
        op = _overlay_op_symbol(step_data.get("op"))
        selectors = step_data.get("selectors")
        selector_text = (
            ",".join(str(item) for item in selectors)
            if isinstance(selectors, list) and selectors
            else "-"
        )
        expressions.append(f"{op} {selector_text}")
    return " ; ".join(expressions) if expressions else "activation"


def _overlay_op_symbol(value: object) -> str:
    if value == "set":
        return "="
    if value == "add":
        return "+="
    if value == "remove":
        return "-="
    return str(value)


def _entry_identity(entry: PreparedEntry) -> tuple[str, str, str]:
    return (entry.kind, entry.name, entry.ref)


def _entry_agent_name(entries: tuple[PreparedEntry, ...]) -> str:
    for entry in entries:
        path = entry.path or entry.source.path
        prefix, separator, rest = path.partition("agents/")
        del prefix
        if separator and "/" in rest:
            return rest.split("/", 1)[0]
    return "default"


def _apply_string_overlays(
    base: tuple[str, ...],
    overlays: tuple[ThunkOverlay, ...],
) -> tuple[str, ...]:
    current = list(dict.fromkeys(item for item in base if item))
    for overlay in overlays:
        overlay_items = [item for item in overlay.items if item]
        if overlay.op == "set":
            current = list(dict.fromkeys(overlay_items))
            continue
        if overlay.op == "add":
            for item in overlay_items:
                if item not in current:
                    current.append(item)
            continue
        if overlay.op == "remove":
            blocked = set(overlay_items)
            current = [item for item in current if item not in blocked]
    return tuple(current)


def _cap_entries(live: LiveState, *, kind: str) -> tuple[PreparedEntry, ...]:
    return tuple(entry for entry in live.cap_entries if entry.kind == kind)


def _resolve_runtime_models(
    context: UptimeContext,
    selectors: tuple[str, ...],
) -> tuple[ModelTarget, ...]:
    return tuple(
        resolve_model(
            context,
            selector=selector,
        )
        for selector in selectors
    )


def _user_template_context(
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


def _system_template_context(
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
    runtime = _runtime_base(
        context,
        run=run,
        thunk=thunk,
    )
    runtime.update(
        {
            "model": _model_target_to_context(models[0]) if models else None,
            "models": [_model_target_to_context(item) for item in models],
            "tools": [
                _tool_to_context(item)
                for item in sorted(tools.values(), key=lambda entry: entry.name)
            ],
            "psyches": [_prepared_entry_to_context(context, item) for item in psyches],
            "skills": [_prepared_entry_to_context(context, item) for item in skills],
            "services": [_prepared_entry_to_context(context, item) for item in services],
        }
    )
    return _template_context(
        params=_template_param_values(thunk, params),
        runtime=runtime,
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
    host = context.config.get("server.host")
    port = context.config.get("server.port")
    endpoint = context.config.get("server.endpoint")
    return {
        "origin": run.origin,
        "group": run.group,
        "thread_id": run.thread_id,
        "sandbox": _runtime_sandbox(context),
        "agent": {
            "name": context.name,
            "kind": "resident",
            "root": str(context.root),
            "home": str(context.home),
            "room": str(context.room),
        },
        "server": {
            "host": host if isinstance(host, str) else None,
            "port": port if isinstance(port, int) else None,
            "endpoint": endpoint if isinstance(endpoint, str) and endpoint.strip() else None,
        },
        "program": {
            "source_path": run.live.program.source_path,
        },
        "thunk": {
            "name": thunk.thunk_name(),
            "input": _param_to_context(thunk.input) if thunk.input is not None else None,
            "params": [_param_to_context(item) for item in thunk.params],
            "output": thunk.output,
        },
        "job": _job_context(context, run),
    }


def _param_to_context(param: ParamDecl | None) -> dict[str, object] | None:
    if param is None:
        return None
    return {
        "name": param.name,
        "optional": param.optional,
        "type_name": param.type_name,
    }


def _model_target_to_context(target: ModelTarget) -> dict[str, object]:
    return {
        "ref": target.ref,
        "provider": target.provider,
        "name": target.name,
        "model": target.model,
        "adapter": target.adapter,
        "base_url": target.base_url,
        "tools": target.tools,
        "streaming": target.streaming,
    }


def _tool_to_context(tool: AgentTool) -> dict[str, object]:
    definition = tool.definition()
    return {
        "name": definition.name,
        "description": definition.description,
        "parameters": dict(definition.parameters),
    }


def _prepared_entry_to_context(
    context: UptimeContext,
    entry: PreparedEntry,
) -> dict[str, object]:
    content = _entry_content(context, entry)
    description = entry.meta.get("description")
    return {
        "name": entry.name,
        "kind": entry.kind,
        "path": entry.path,
        "ref": cap_store.entry_ref(entry, agent_name=context.name),
        "description": str(description) if description is not None else None,
        "content": content,
        "metadata": dict(entry.meta),
        "metadata_items": _metadata_items(entry.meta),
        "scope": cap_store.entry_scope(entry, agent_name=context.name),
        "origin": cap_store.entry_origin(entry),
        "form": cap_store.entry_form(entry),
    }


def _render_thunk_messages(
    blocks: tuple[MessageBlock, ...],
    *,
    user_context: dict[str, object],
    system_context: dict[str, object],
) -> tuple[MessageBlock, ...]:
    rendered: list[MessageBlock] = []
    for block in blocks:
        context = system_context if block.kind in {"instruct", "system"} else user_context
        rendered.append(
            MessageBlock(
                kind=block.kind,
                text=render_text_template(block.text, context).strip(),
                span=block.span,
                explicit=block.explicit,
            )
        )
    return tuple(rendered)


def _assembled_instructions(
    *,
    live_program: LiveProgram,
    origin: str,
    thunk: Thunk,
    rendered_messages: tuple[MessageBlock, ...],
    system_context: dict[str, object],
    tool_history_context: str,
) -> str:
    parts = [
        _render_instruct_template(
            live_program=live_program,
            origin=origin,
            thunk=thunk,
            system_context=system_context,
        ),
        _message_blocks_body(tuple(item for item in rendered_messages if item.kind == "system")),
    ]
    instructions = _join_message_texts(*parts)
    if not tool_history_context:
        return instructions
    if instructions:
        return f"{instructions}\n\n{tool_history_context}"
    return tool_history_context


def _render_instruct_template(
    *,
    live_program: LiveProgram,
    origin: str,
    thunk: Thunk,
    system_context: dict[str, object],
) -> str:
    template = _selected_instruct_template(
        live_program=live_program,
        origin=origin,
        thunk=thunk,
    )
    if not template.strip():
        return ""
    return render_text_template(template, system_context).strip()


def _selected_instruct_template(
    *,
    live_program: LiveProgram,
    origin: str,
    thunk: Thunk,
) -> str:
    blocks = thunk.message_blocks("instruct")
    if not blocks:
        return _default_program_instruct_template(live_program, origin=origin)
    block = blocks[0]
    value = block.text.strip()
    if value == "none":
        return ""
    if value == "default":
        return _default_program_instruct_template(live_program, origin=origin)
    if _looks_like_instruct_name(value):
        instruct = _program_instruct(live_program, value)
        if instruct is None:
            raise ToolangError(f"Instruct not found: {value}")
        return instruct.body
    return block.text


def _default_program_instruct_template(live_program: LiveProgram, *, origin: str) -> str:
    instruct = _program_instruct(live_program, None)
    if instruct is not None:
        return instruct.body
    return _default_instruct_template(origin)


def _program_instruct(live_program: LiveProgram, name: str | None) -> Any | None:
    get_instruct = getattr(live_program, "get_instruct", None)
    if not callable(get_instruct):
        return None
    return get_instruct(name)


def _looks_like_instruct_name(value: str) -> bool:
    if not value or any(char.isspace() for char in value):
        return False
    if "\n" in value:
        return False
    first = value[0]
    return (first.isalpha() or first == "_") and all(
        char.isalnum() or char in {"_", "-"}
        for char in value
    )


def _runtime_snapshot(
    context: UptimeContext,
    run: RunBinding,
    thunk: Thunk,
    *,
    tools: dict[str, AgentTool],
) -> RunSnapshot:
    task_snapshot = _task_snapshot(context, run)
    return RunSnapshot(
        agent=SnapshotAgent(
            name=context.name,
            root=str(context.root),
            home=str(context.home),
        ),
        run=SnapshotRun(
            run_id=run.run_id,
            group=run.group,
            origin=run.origin,
            thread_id=run.thread_id,
            run_loop=run.run_loop,
            live_fingerprint=run.live.fingerprint,
            invoke_params=_invoke_params(run),
            invoke_parts=_invoke_parts(run),
        ),
        program=SnapshotProgram(
            source_path=run.live.program.source_path,
            thunk=_thunk_to_data(thunk),
        ),
        caps=tuple(SnapshotEntry(payload=entry.to_snapshot()) for entry in run.live.cap_entries),
        jobs=tuple(SnapshotEntry(payload=entry.to_snapshot()) for entry in run.live.job_entries),
        tools=tuple(
            tool.definition().name for tool in sorted(tools.values(), key=lambda item: item.name)
        ),
        task=task_snapshot[0] if task_snapshot is not None else None,
        task_services=task_snapshot[1] if task_snapshot is not None else None,
    )


def _task_snapshot(
    context: UptimeContext, run: RunBinding
) -> tuple[SnapshotTask, SnapshotTaskServices] | None:
    if run.origin != "task":
        return None
    task_id = work.task_id_from_thread_id(run.thread_id)
    if task_id is None:
        return None
    task = work.find_task(context.root, context.name, task_id)
    if task is None:
        return None
    return (
        SnapshotTask(
            provider="local",
            ref=task.document.thread_id(),
            name=task.name.rsplit("/", 1)[-1],
            body=task.document.body,
            state=task.document.state,
            stage=task.document.stage,
            thread_id=task.document.thread_id(),
            path=str(task.path),
        ),
        SnapshotTaskServices(
            provider="local",
            read=True,
            write=True,
            comment=False,
            path=str(task.path),
        ),
    )


def _script_message_text(
    *,
    thunk: Thunk,
    input_text: str,
    rendered_messages: tuple[MessageBlock, ...],
) -> str:
    user_messages = tuple(item for item in rendered_messages if item.kind == "user")
    authored_text = _message_blocks_body(user_messages)
    if input_text.strip() and any(item.kind == "user" and not item.explicit for item in thunk.messages):
        return _join_message_texts(authored_text, input_text)
    if authored_text:
        return authored_text
    return input_text


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
        "state": task.document.state,
        "stage": task.document.stage,
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
        "state": chore.document.state,
        "schedule": chore.document.schedule,
        "thread_id": run.thread_id,
        "path": str(chore.path),
        "readable": True,
        "writable": False,
        "commentable": False,
    }


def _entry_content(
    context: UptimeContext,
    entry: PreparedEntry,
) -> str | None:
    if entry.kind != "psyche":
        return None
    entry_path = context.root / entry.path
    if not entry_path.is_file():
        return None
    try:
        post = frontmatter.loads(entry_path.read_text(encoding="utf-8"))
    except OSError:
        return None
    content = post.content.strip()
    return content or None


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


def _invoke_params(run: RunBinding) -> dict[str, Any]:
    value = run.metadata.get("invoke_params")
    if not isinstance(value, dict):
        return {}
    return {str(key): item for key, item in value.items()}


def _invoke_parts(run: RunBinding) -> tuple[dict[str, Any], ...]:
    value = run.metadata.get("invoke_parts")
    if not isinstance(value, list):
        return ()
    items: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        items.append({str(key): part for key, part in item.items()})
    return tuple(items)


def _thunk_name(thunk: Thunk) -> str:
    return thunk.thunk_name()


def _thunk_to_data(thunk: Thunk) -> dict[str, object]:
    return {
        "name": _thunk_name(thunk),
        "input": (
            {
                "name": param.name,
                "optional": param.optional,
                "type_name": param.type_name,
            }
            if (param := thunk.input) is not None
            else None
        ),
        "params": [
            {
                "name": item.name,
                "optional": item.optional,
                "type_name": item.type_name,
            }
            for item in thunk.params
        ],
        "output": thunk.output,
        "overlays": [
            {
                "kind": item.kind,
                "op": item.op,
                "items": list(item.items),
                "line": item.span.line,
            }
            for item in thunk.overlays
        ],
        "messages": [
            {
                "kind": item.kind,
                "text": item.text,
                "line": item.span.line,
                "explicit": item.explicit,
            }
            for item in thunk.messages
        ],
    }
