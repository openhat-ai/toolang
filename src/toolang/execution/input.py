"""Run binding and semantic run-input assembly."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

from toolang.base.protocols.tool import Tool
from toolang.base.types.message import Message
from .. import work
from ..strategies import normalize_run_strategy_name
from ..state.live import LiveState
from ..state.program import LiveProgram, ProgramThunk
from .snapshot import (
    RunSnapshot,
    SnapshotAgent,
    SnapshotEntry,
    SnapshotProgram,
    SnapshotRun,
    SnapshotTask,
    SnapshotTaskServices,
)
from .model import select_model_selectors
from .records import RunStrategy
from .db import utc_now

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
    """One assembled semantic input for one run strategy."""

    run: RunBinding
    model: str | None
    input: Message
    instructions: str
    messages: list[Message]
    snapshot: RunSnapshot
    tools: dict[str, Tool]
    debug: dict[str, Any]


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


def assemble_run_input(context: UptimeContext, run: RunBinding) -> RunInput:
    """Assemble one semantic run input from bound inputs and live state."""

    program = run.live.program
    thunk = program.get_thunk(run.thunk_name)
    input_text = program.expand_input(run.input_text) if run.input_text else ""
    history_messages = context.store.recent_conversation_messages(thread_id=run.thread_id, limit=19)
    tools = _run_tools(context, run)
    requested_model_selectors = _run_requested_model_selectors(run)
    activation_model_selectors = requested_model_selectors or _activation_allowed_model_selectors(context)
    thunk_model_selectors = thunk.model_selectors()
    effective_model_selectors = select_model_selectors(
        context,
        thunk_selectors=thunk_model_selectors,
        activation_selectors=activation_model_selectors,
        default_selector=_activation_default_model_selector(context),
    )
    requested_model = effective_model_selectors[0]
    snapshot = _runtime_snapshot(context, run, program, thunk, tools)
    instructions = _instructions(snapshot, program, thunk)
    user_message = (
        input_text
        if input_text
        else "Execute the selected thunk with no external user message."
    )
    input_message = Message.user(user_message)
    return RunInput(
        run=run,
        model=requested_model,
        input=input_message,
        instructions=instructions,
        messages=[
            *history_messages,
            input_message,
        ],
        snapshot=snapshot,
        tools=tools,
        debug={
            "run_id": run.run_id,
            "thread_id": run.thread_id,
            "thunk_name": thunk.name,
            "input_text": input_text,
            "model": requested_model,
            "activation_default_model": _activation_default_model_selector(context),
            "activation_model_selectors": activation_model_selectors,
            "requested_model_selectors": requested_model_selectors,
            "thunk_model_selectors": thunk_model_selectors,
            "effective_model_selectors": effective_model_selectors,
            "tool_names": sorted(tools),
        },
    )


def _run_tools(context: UptimeContext, run: RunBinding) -> dict[str, Tool]:
    if run.origin == "invoke" or run.group == "invoke":
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


def _runtime_snapshot(
    context: UptimeContext,
    run: RunBinding,
    program: LiveProgram,
    thunk: ProgramThunk,
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
            source_path=program.source_path,
            thunk=thunk.to_data(),
        ),
        caps=tuple(SnapshotEntry(payload=entry.to_snapshot()) for entry in run.live.cap_entries),
        jobs=tuple(SnapshotEntry(payload=entry.to_snapshot()) for entry in run.live.job_entries),
        tools=tuple(
            tool.definition().name for tool in sorted(tools.values(), key=lambda item: item.name)
        ),
        task=task_snapshot[0] if task_snapshot is not None else None,
        task_services=task_snapshot[1] if task_snapshot is not None else None,
    )


def _instructions(
    snapshot: RunSnapshot,
    program: LiveProgram,
    thunk: ProgramThunk,
) -> str:
    task_prompt = _task_prompt(snapshot)
    sections = [
        "You are the Toolang runtime.",
        "Follow the selected thunk.",
        "Run input assembly is read-only.",
        "Runtime snapshot:",
        json.dumps(_snapshot_to_data(snapshot), indent=2, ensure_ascii=False),
        task_prompt or "",
        "Program source:",
        program.source_text.strip() or f"agent {program.prepared.agent_name}",
        "Thunk body:",
        thunk.body,
    ]
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


def _task_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text


def _snapshot_to_data(snapshot: RunSnapshot) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "agent": {
            "name": snapshot.agent.name,
            "root": snapshot.agent.root,
            "home": snapshot.agent.home,
        },
        "run": {
            "run_id": snapshot.run.run_id,
            "group": snapshot.run.group,
            "origin": snapshot.run.origin,
            "thread_id": snapshot.run.thread_id,
            "run_strategy": snapshot.run.run_strategy,
            "live_fingerprint": snapshot.run.live_fingerprint,
            "invoke_params": dict(snapshot.run.invoke_params),
            "invoke_parts": [dict(item) for item in snapshot.run.invoke_parts],
        },
        "program": {
            "source_path": snapshot.program.source_path,
            "thunk": dict(snapshot.program.thunk),
        },
        "caps": [dict(entry.payload) for entry in snapshot.caps],
        "jobs": [dict(entry.payload) for entry in snapshot.jobs],
        "tools": list(snapshot.tools),
    }
    if snapshot.task is not None:
        payload["task"] = {
            "provider": snapshot.task.provider,
            "ref": snapshot.task.ref,
            "name": snapshot.task.name,
            "body": snapshot.task.body,
            "status": snapshot.task.status,
            "requester": snapshot.task.requester,
            "thread_id": snapshot.task.thread_id,
            "path": snapshot.task.path,
        }
    if snapshot.task_services is not None:
        payload["task_services"] = {
            "provider": snapshot.task_services.provider,
            "read": snapshot.task_services.read,
            "write": snapshot.task_services.write,
            "comment": snapshot.task_services.comment,
            "path": snapshot.task_services.path,
        }
    return payload


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
