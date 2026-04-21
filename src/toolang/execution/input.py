"""Run binding and semantic run-input assembly."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

from toolang.base.protocols.tool import Tool
from toolang.base.types.message import Message
from toolang.base.types.model import ModelTarget
from .. import work
from ..program import MessageBlock, ParamDecl, Thunk, ThunkOverlay
from ..state.live import LiveState
from ..state.prepared import PreparedEntry
from ..strategies import normalize_run_strategy_name
from .template import render_text_template
from .db import utc_now
from .model import resolve_model, select_model_selectors
from .records import RunStrategy
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
    from ..up import UptimeContext
    from .runner import RunRequest


@dataclass(frozen=True, slots=True)
class RunBinding:
    """One run bound to immutable live state and runtime ids."""

    run_id: str
    group: str
    origin: str
    thread_id: str
    thunk_name: str | None
    input_text: str
    run_strategy: RunStrategy
    metadata: dict[str, Any]
    live: LiveState
    created_at: str


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
    tools_base: dict[str, Tool]
    snapshot: RunSnapshot
    psyches_base: tuple[PreparedEntry, ...] = field(default_factory=tuple)
    skills_base: tuple[PreparedEntry, ...] = field(default_factory=tuple)
    services_base: tuple[PreparedEntry, ...] = field(default_factory=tuple)
    debug: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_binding(cls, context: UptimeContext, run: RunBinding) -> RunInput:
        """Build one semantic run input from one bound run."""

        program = run.live.program
        thunk = program.get_thunk(run.thunk_name)
        input_text = program.expand_input(run.input_text) if run.input_text else ""
        history = tuple(
            context.store.recent_conversation_messages(thread_id=run.thread_id, limit=19)
        )
        models_base = _run_model_base(context, run)
        tools_base = _run_tools_base(context, run)
        psyches_base = _cap_entries(run.live, kind="psyche")
        skills_base = _cap_entries(run.live, kind="skill")
        services_base = _cap_entries(run.live, kind="service")
        params = _invoke_params(run)
        if thunk.input is not None and input_text:
            params = {thunk.input.name: input_text, **params}
        effective_tools = _select_tools(tools_base, thunk.overlays_for("tool"))
        effective_models = _effective_model_selectors(
            context,
            thunk=thunk,
            models_base=models_base,
        )
        resolved_models = _resolve_runtime_models(context, effective_models)
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
            psyches=_select_entries(psyches_base, thunk.overlays_for("psyche")),
            skills=_select_entries(skills_base, thunk.overlays_for("skill")),
            services=_select_entries(services_base, thunk.overlays_for("service")),
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
                "message_text": message.content or "",
                "rendered_messages": [
                    {"kind": item.kind, "text": item.text}
                    for item in rendered_messages
                ],
                "models_base": models_base,
                "activation_default_model": _activation_default_model_selector(context),
                "thunk_model_refs": _thunk_model_refs(thunk),
                "effective_model_selectors": effective_models,
                "tool_names": sorted(effective_tools),
                "psyche_names": [entry.name for entry in _select_entries(psyches_base, thunk.overlays_for("psyche"))],
                "skill_names": [entry.name for entry in _select_entries(skills_base, thunk.overlays_for("skill"))],
                "service_names": [entry.name for entry in _select_entries(services_base, thunk.overlays_for("service"))],
            },
        )

    def messages(self) -> tuple[Message, ...]:
        """Return the ordered input message history for one model call."""

        if self.run.origin == "script":
            return (self.message,)
        return (*self.history, self.message)

    def model_selector(self, context: UptimeContext) -> str | None:
        """Return the primary effective model selector for this run."""

        selectors = self.effective_model_selectors(context)
        return selectors[0] if selectors else None

    def effective_model_selectors(self, context: UptimeContext) -> tuple[str, ...]:
        """Return the ordered effective model selectors for this run."""

        return _effective_model_selectors(
            context,
            thunk=self.thunk,
            models_base=self.models_base,
        )

    def tools(self) -> dict[str, Tool]:
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

        if self.run.origin == "script":
            return _script_instructions(self.snapshot, self)
        return _thread_instructions(self.snapshot, self)


def bind_run_request(
    context: UptimeContext,
    request: RunRequest,
    *,
    live: LiveState | None = None,
) -> RunBinding:
    """Bind one queued run request to immutable runtime inputs."""

    bound_live = live or context.live
    thread_id = request.thread_id or f"{request.origin}:{uuid4().hex}"
    run_strategy = cast(RunStrategy, normalize_run_strategy_name(request.run_strategy))
    return RunBinding(
        run_id=uuid4().hex,
        group=request.group,
        origin=request.origin,
        thread_id=thread_id,
        thunk_name=request.thunk_name,
        input_text=request.thunk,
        run_strategy=run_strategy,
        metadata=dict(request.metadata),
        live=bound_live,
        created_at=utc_now(),
    )


def _run_message(
    *,
    run: RunBinding,
    thunk: Thunk,
    input_text: str,
    rendered_messages: tuple[MessageBlock, ...],
) -> Message:
    if run.origin != "script":
        return Message.user(input_text)
    text = _script_message_text(
        thunk=thunk,
        input_text=input_text,
        rendered_messages=rendered_messages,
    )
    return Message.user(text)


def _run_model_base(context: UptimeContext, run: RunBinding) -> tuple[str, ...]:
    requested = _run_requested_model_selectors(run)
    if requested:
        return requested
    return _activation_allowed_model_selectors(context)


def _run_tools_base(context: UptimeContext, run: RunBinding) -> dict[str, Tool]:
    if run.origin == "script":
        return {}
    return context.tools


def _run_requested_model_selectors(run: RunBinding) -> tuple[str, ...]:
    raw_models = run.metadata.get("models")
    if isinstance(raw_models, tuple):
        return tuple(item for item in raw_models if isinstance(item, str) and item.strip())
    if isinstance(raw_models, list):
        return tuple(item for item in raw_models if isinstance(item, str) and item.strip())
    for key in ("model", "model_selector"):
        value = run.metadata.get(key)
        if isinstance(value, str) and value.strip():
            return (value.strip(),)
    return ()


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
    return select_model_selectors(
        context,
        thunk_selectors=_thunk_model_refs(thunk),
        activation_selectors=models_base,
        default_selector=_activation_default_model_selector(context),
    )


def _thunk_model_refs(thunk: Thunk) -> tuple[str, ...]:
    return _apply_string_overlays((), thunk.overlays_for("model"))


def _select_tools(
    tools_base: dict[str, Tool],
    overlays: tuple[ThunkOverlay, ...],
) -> dict[str, Tool]:
    names = _apply_string_overlays(tuple(tools_base), overlays)
    return {
        name: tools_base[name]
        for name in names
        if name in tools_base
    }


def _select_entries(
    base: tuple[PreparedEntry, ...],
    overlays: tuple[ThunkOverlay, ...],
) -> tuple[PreparedEntry, ...]:
    names = _apply_string_overlays(tuple(entry.name for entry in base), overlays)
    selected: list[PreparedEntry] = []
    by_name = {entry.name: entry for entry in base}
    for name in names:
        entry = by_name.get(name)
        if entry is not None:
            selected.append(entry)
    return tuple(selected)


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
    tools: dict[str, Tool],
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
    return {
        "origin": run.origin,
        "group": run.group,
        "thread_id": run.thread_id,
        "agent": {
            "name": context.name,
            "kind": "resident",
            "home": str(context.home),
            "room": str(context.room),
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


def _tool_to_context(tool: Tool) -> dict[str, object]:
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
    return {
        "name": entry.name,
        "kind": entry.kind,
        "path": entry.path,
        "ref": entry.ref if entry.source.form == "remote" else None,
        "description": str(entry.meta.get("description")) if entry.meta.get("description") is not None else None,
        "source": entry.source.form,
        "scope": "agent" if entry.path.startswith(f"agents/{context.name}/") else "global",
    }


def _render_thunk_messages(
    blocks: tuple[MessageBlock, ...],
    *,
    user_context: dict[str, object],
    system_context: dict[str, object],
) -> tuple[MessageBlock, ...]:
    rendered: list[MessageBlock] = []
    for block in blocks:
        context = system_context if block.kind == "system" else user_context
        rendered.append(
            MessageBlock(
                kind=block.kind,
                text=render_text_template(block.text, context).strip(),
                span=block.span,
                explicit=block.explicit,
            )
        )
    return tuple(rendered)


def _runtime_snapshot(
    context: UptimeContext,
    run: RunBinding,
    thunk: Thunk,
    *,
    tools: dict[str, Tool],
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
            run_strategy=run.run_strategy,
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


def _script_instructions(snapshot: RunSnapshot, run_input: RunInput) -> str:
    return _instructions(
        snapshot,
        run_input,
        mode_lines=(
            "You are the Toolang runtime.",
            "Execute the selected thunk once.",
            "Treat this as a script-style run, not an ongoing thread.",
        ),
    )


def _thread_instructions(snapshot: RunSnapshot, run_input: RunInput) -> str:
    return _instructions(
        snapshot,
        run_input,
        mode_lines=(
            "You are the Toolang runtime.",
            "Continue the selected thunk within the current thread.",
            "Treat the conversation history as durable run context.",
        ),
    )


def _instructions(
    snapshot: RunSnapshot,
    run_input: RunInput,
    *,
    mode_lines: tuple[str, ...],
) -> str:
    task_prompt = _task_prompt(snapshot)
    rendered_messages = run_input.rendered_messages()
    system_messages = tuple(item for item in rendered_messages if item.kind == "system")
    source_path = getattr(run_input.run.live.program, "source_path", "") or "<unknown>"
    sections = [
        *mode_lines,
        f"Selected thunk: {_thunk_name(run_input.thunk)}.",
        f"Program source path: {source_path}.",
        task_prompt or "",
    ]
    if run_input.thunk.overlays:
        sections.extend(
            [
                "Thunk overlays:",
                "\n".join(_overlay_lines(run_input.thunk.overlays)),
            ]
        )
    authored_system_messages = tuple(item for item in run_input.thunk.messages if item.kind == "system")
    if _message_blocks_text(system_messages) != _message_blocks_text(authored_system_messages):
        sections.extend(
            [
                "System messages:",
                _message_blocks_body(authored_system_messages),
                "Rendered system messages:",
                _message_blocks_body(system_messages),
            ]
        )
    elif system_messages:
        sections.extend(
            [
                "System messages:",
                _message_blocks_body(system_messages),
            ]
        )
    return "\n\n".join(section for section in sections if section.strip())


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
            status=task.document.status,
            requester=task.document.requester,
            thread_id=task.document.thread_id(),
            path=str(task.path),
        ),
        SnapshotTaskServices(
            provider="local",
            read=True,
            write=True,
            comment=True,
            path=str(task.path),
        ),
    )


def _task_prompt(snapshot: RunSnapshot) -> str | None:
    task = snapshot.task
    services = snapshot.task_services
    if task is None or services is None:
        return None

    provider = _task_text(task.provider) or "unknown"
    can_read = services.read
    can_write = services.write
    can_comment = services.comment
    local_path = _task_text(services.path) or _task_text(task.path)
    lines = [
        "Task execution protocol:",
        "- You are handling one task-driven run.",
        "- Understand the current task before acting.",
        "- Keep the task itself as the durable record of progress and outcome.",
        f"- Task provider: {provider}.",
        f"- Task read available: {'yes' if can_read else 'no'}.",
        f"- Task write available: {'yes' if can_write else 'no'}.",
        f"- Task comment available: {'yes' if can_comment else 'no'}.",
    ]
    if provider == "local":
        lines.extend(
            [
                "- This task is backed by a local markdown file.",
                f"- Update the task file directly at: {local_path or '<unknown path>'}.",
                "- Keep front matter minimal: id, requester, status, paused.",
                "- Move status from todo to doing when work starts.",
                "- Move status to done or cancelled before finishing.",
                "- Use the markdown body as the durable task input and append progress or outcome notes there.",
            ]
        )
    if not can_write:
        lines.append("- If task write is unavailable, you may proceed, but you must clearly state that the task could not be updated.")
    else:
        lines.append("- Update the task at important milestones and before finishing.")
    return "\n".join(lines)


def _overlay_lines(overlays: tuple[ThunkOverlay, ...]) -> tuple[str, ...]:
    op_map = {
        "set": "=",
        "add": "+=",
        "remove": "-=",
    }
    return tuple(
        f"{overlay.kind} {op_map[overlay.op]} {', '.join(overlay.items)}"
        for overlay in overlays
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


def _message_blocks_text(blocks: tuple[MessageBlock, ...]) -> str:
    sections = [
        f"{block.kind}:\n{block.text}".strip()
        for block in blocks
        if block.text.strip()
    ]
    return "\n\n".join(sections).strip()


def _task_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text


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
