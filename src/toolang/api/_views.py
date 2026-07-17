"""Formal agent inspection and local job API routes."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from collections.abc import AsyncIterator, Container, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from toolang.base.types.message import Message
from toolang.execution.detail import ExecutionProjector, ThreadInfo, run_message_data
from toolang.execution.binding import allocate_run_id
from toolang.execution.records import (
    CommandApply,
    CommandRecord,
    RunRecord,
)
from toolang import templates
from toolang.catalog import cap as caps
from toolang.catalog.job import JobCatalog
import toolang.work.definitions as job_definitions
from toolang.work.state import AgentJobs
from toolang.work.store import JobRecord, open_job_store
from toolang.state.durable import scan_durable_state
from toolang.state.prepared import PreparedEntry, load_prepared_locks
from toolang.agent.features import (
    ROUTER_COMPONENTS,
    RUNNER_COMPONENTS,
    TRIGGER_COMPONENTS,
    component_group,
    normalize_component_names,
)
from toolang.agent.context import channel_context
from ._streaming import ShutdownAwareStreamingResponse

if TYPE_CHECKING:
    from toolang.agent.context import ComponentState

CapKind = Literal["psyche", "skill", "service", "prompt"]
JobKind = Literal["task", "chore"]
ROUTER_COMPONENT_LEAVES = frozenset(component_group(ROUTER_COMPONENTS, "router"))
RUNNER_COMPONENT_LEAVES = frozenset(component_group(RUNNER_COMPONENTS, "runner"))
TRIGGER_COMPONENT_LEAVES = frozenset(component_group(TRIGGER_COMPONENTS, "trigger"))
COLLECTION_TO_KIND: dict[str, CapKind] = {
    "psyches": "psyche",
    "skills": "skill",
    "services": "service",
    "prompts": "prompt",
}


class RunInputMessagePayload(BaseModel):
    """One user-authored run input message."""

    role: str = "user"
    parts: list[dict[str, object]]
    meta: dict[str, object] = Field(default_factory=dict)


class RunCancelRequest(BaseModel):
    """Request run cancellation."""

    reason: str | None = None
    mode: str = "immediate"
    request_id: str | None = None


class RunRestartRequest(BaseModel):
    """Request one replacement or forked chat run with a new input."""

    request_id: str | None = None
    message: RunInputMessagePayload | None = None
    include_anchor: bool = False


class RunSteerRequest(BaseModel):
    """Request one steering message for a running run."""

    request_id: str | None = None
    mode: str = "next_step"
    message: RunInputMessagePayload


class _ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TaskCreateRequest(_ApiModel):
    title: str | None = None
    body: str = ""


class TaskPatchRequest(_ApiModel):
    title: str | None = None
    body: str | None = None


class JobPatchRequest(_ApiModel):
    title: str | None = None
    body: str | None = None
    schedule: str | None = None


class ChoreCreateRequest(_ApiModel):
    title: str | None = None
    body: str = ""
    schedule: str = job_definitions.DEFAULT_CHORE_SCHEDULE


class ChorePatchRequest(_ApiModel):
    title: str | None = None
    body: str | None = None
    schedule: str | None = None


def snapshot_context(
    context: ComponentState,
    *,
    enabled_components: Sequence[str] | None = None,
    enabled_features: Sequence[str] | None = None,
) -> dict[str, object]:
    """Return the internal runtime snapshot used by tests and diagnostics."""

    components = (
        enabled_components if enabled_components is not None else enabled_features
    )
    if components is None:
        raise TypeError("enabled_components is required")
    components = normalize_component_names(tuple(components))
    durable = scan_durable_state(context.root, context.name)
    prepared = load_prepared_locks(context.root, context.name)
    runs = context.store.list_runs(limit=None)
    operational_facts: dict[str, object] = {
        "active_runs": sum(run.status in {"pending", "running"} for run in runs),
        "completed_runs": sum(
            run.status in {"finished", "failed", "canceled"} for run in runs
        ),
    }
    durable_operational_facts: dict[str, object] = {
        "prepared_fingerprint": prepared.fingerprint,
        **operational_facts,
    }
    recent_runs = context.store.list_runs(limit=20)
    recent_steps = context.store.list_steps_for_runs(
        run_ids=tuple(item.run_id for item in recent_runs)
    )
    recent_commands = {
        run.run_id: context.store.list_commands(run_id=run.run_id)
        for run in recent_runs
    }
    return {
        "enabled_components": list(components),
        "router_components": _select_components(
            components, "router", ROUTER_COMPONENT_LEAVES
        ),
        "runner_components": _select_components(
            components, "runner", RUNNER_COMPONENT_LEAVES
        ),
        "trigger_components": _select_components(
            components, "trigger", TRIGGER_COMPONENT_LEAVES
        ),
        "durable": {
            "toolang_root": str(durable.toolang_root),
            "agent_name": durable.agent_name,
            "fingerprint": durable.fingerprint,
            "scanned_at": durable.scanned_at,
            "definitions": {
                "program_source": durable.program_path,
                "config_paths": list(durable.config_paths),
                "shared_entries": [
                    entry.to_snapshot()
                    for entry in _authored_entries(context, visibility="shared")
                ],
                "private_entries": [
                    entry.to_snapshot()
                    for entry in _authored_entries(context, visibility="private")
                ],
            },
            "operational_facts": durable_operational_facts,
        },
        "prepared": prepared.to_snapshot(),
        "state": {
            **context.get_agent_state().to_snapshot(),
            **operational_facts,
        },
        "channels": _channel_items(context),
        "execution": {
            "recent_updates": [
                asdict(item) for item in context.store.list_updates(limit=20)
            ],
            "recent_runs": [asdict(item) for item in recent_runs],
            "recent_messages": [
                item.to_data()
                for run in sorted(recent_runs, key=lambda item: item.created_at)
                for item in run_message_data(
                    run,
                    inputs=recent_commands.get(run.run_id, ()),
                    steps=recent_steps.get(run.run_id, ()),
                )
            ],
        },
    }


def _cap_collection(
    context: ComponentState, *, kind: CapKind
) -> list[dict[str, object]]:
    return [
        _cap_summary_item(context, entry)
        for entry in context.get_agent_state().caps
        if entry.kind == kind
    ]


def _job_collection(
    context: ComponentState, *, archived: bool
) -> list[dict[str, object]]:
    return [
        *_task_collection(context, archived=archived),
        *_chore_collection(context, archived=archived),
    ]


def _task_collection(
    context: ComponentState, *, archived: bool
) -> list[dict[str, object]]:
    entries = JobCatalog(context.root, context.name).list(
        kind="task", lifecycle="archived" if archived else "ready"
    )
    return [_task_item(context, entry) for entry in entries]


def _chore_collection(
    context: ComponentState, *, archived: bool
) -> list[dict[str, object]]:
    entries = JobCatalog(context.root, context.name).list(
        kind="chore", lifecycle="archived" if archived else "ready"
    )
    return [_chore_item(context, entry) for entry in entries]


def _task_item(
    context: ComponentState, entry: job_definitions.TaskEntry
) -> dict[str, object]:
    document = entry.document
    job = _job_record(
        context, kind="task", job_id=document.task_id(), lifecycle=entry.lifecycle
    )
    return {
        "id": document.task_id(),
        "kind": "task",
        "lifecycle": entry.lifecycle,
        "status": job.status if job is not None else None,
        "remote_ref": document.remote_ref(),
        "remote_status": document.remote_status(),
        "title": document.display_title(fallback_name=entry.name.rsplit("/", 1)[-1]),
        "path": _agent_relative_path(context, entry.path),
        "updated_at": _path_updated_at(entry.path),
        "runtime": _job_runtime(context, thread_id=document.thread_id(), job=job),
    }


def _task_detail_item(
    context: ComponentState, entry: job_definitions.TaskEntry
) -> dict[str, object]:
    return {
        **_task_item(context, entry),
        "body": entry.document.body,
    }


def _chore_item(
    context: ComponentState, entry: job_definitions.ChoreEntry
) -> dict[str, object]:
    document = entry.document
    job = _job_record(
        context, kind="chore", job_id=document.chore_id(), lifecycle=entry.lifecycle
    )
    return {
        "id": document.chore_id(),
        "kind": "chore",
        "lifecycle": entry.lifecycle,
        "status": job.status if job is not None else None,
        "schedule": document.schedule,
        "title": document.display_title(fallback_name=entry.name.rsplit("/", 1)[-1]),
        "path": _agent_relative_path(context, entry.path),
        "updated_at": _path_updated_at(entry.path),
        "runtime": _job_runtime(context, thread_id=document.thread_id(), job=job),
    }


def _chore_detail_item(
    context: ComponentState, entry: job_definitions.ChoreEntry
) -> dict[str, object]:
    return {
        **_chore_item(context, entry),
        "body": entry.document.body,
    }


def _job_detail_item(
    context: ComponentState,
    *,
    kind: JobKind,
    entry: job_definitions.TaskEntry | job_definitions.ChoreEntry,
) -> dict[str, object]:
    if kind == "task":
        return _task_detail_item(context, cast(job_definitions.TaskEntry, entry))
    return _chore_detail_item(context, cast(job_definitions.ChoreEntry, entry))


def _find_job_or_404(
    context: ComponentState, job_id: str
) -> tuple[JobKind, job_definitions.TaskEntry | job_definitions.ChoreEntry]:
    catalog = JobCatalog(context.root, context.name)
    task = catalog.get("task", job_id)
    if task is not None:
        return "task", task
    chore = catalog.get("chore", job_id)
    if chore is not None:
        return "chore", chore
    raise HTTPException(status_code=404, detail=f"job not found: {job_id}")


def _find_archived_job_or_404(
    context: ComponentState, job_id: str
) -> tuple[JobKind, job_definitions.TaskEntry | job_definitions.ChoreEntry]:
    catalog = JobCatalog(context.root, context.name)
    task = catalog.get("task", job_id, lifecycle="archived")
    if task is not None:
        return "task", task
    chore = catalog.get("chore", job_id, lifecycle="archived")
    if chore is not None:
        return "chore", chore
    raise HTTPException(status_code=404, detail=f"archived job not found: {job_id}")


def _find_task_or_404(
    context: ComponentState,
    task_id: str,
) -> job_definitions.TaskEntry:
    entry = JobCatalog(context.root, context.name).get("task", task_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"task not found: {task_id}")
    return entry


def _find_archived_task_or_404(
    context: ComponentState, task_id: str
) -> job_definitions.TaskEntry:
    entry = JobCatalog(context.root, context.name).get(
        "task", task_id, lifecycle="archived"
    )
    if entry is None:
        raise HTTPException(
            status_code=404, detail=f"archived task not found: {task_id}"
        )
    return entry


def _find_chore_or_404(
    context: ComponentState,
    chore_id: str,
) -> job_definitions.ChoreEntry:
    entry = JobCatalog(context.root, context.name).get("chore", chore_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"chore not found: {chore_id}")
    return entry


def _find_archived_chore_or_404(
    context: ComponentState, chore_id: str
) -> job_definitions.ChoreEntry:
    entry = JobCatalog(context.root, context.name).get(
        "chore", chore_id, lifecycle="archived"
    )
    if entry is None:
        raise HTTPException(
            status_code=404, detail=f"archived chore not found: {chore_id}"
        )
    return entry


def _task_document_from_create(
    context: ComponentState, payload: TaskCreateRequest
) -> job_definitions.TaskFile:
    try:
        return job_definitions.TaskFile(
            id=JobCatalog(context.root, context.name).allocate_id(),
            title=payload.title,
            body=payload.body,
        )
    except ValidationError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


def _chore_document_from_create(
    context: ComponentState, payload: ChoreCreateRequest
) -> job_definitions.ChoreFile:
    try:
        return job_definitions.ChoreFile(
            id=JobCatalog(context.root, context.name).allocate_id(),
            title=payload.title,
            body=payload.body,
            schedule=payload.schedule,
        )
    except ValidationError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


def _update_job(
    context: ComponentState,
    *,
    kind: JobKind,
    entry: job_definitions.TaskEntry | job_definitions.ChoreEntry,
    payload: JobPatchRequest,
) -> dict[str, object]:
    if kind == "task":
        return _update_task(
            context, entry=cast(job_definitions.TaskEntry, entry), payload=payload
        )
    return _update_chore(
        context, entry=cast(job_definitions.ChoreEntry, entry), payload=payload
    )


def _update_task(
    context: ComponentState,
    *,
    entry: job_definitions.TaskEntry,
    payload: TaskPatchRequest | JobPatchRequest,
) -> dict[str, object]:
    document = _patch_task_document(entry.document, payload)
    catalog = JobCatalog(context.root, context.name)
    path = catalog.save(entry, document)
    _append_job_update(
        context, kind="task", item_id=document.task_id(), action="updated", path=path
    )
    updated = catalog.get("task", document.task_id(), lifecycle=entry.lifecycle)
    if updated is None:
        raise HTTPException(
            status_code=404, detail=f"task not found after update: {document.task_id()}"
        )
    return {
        "item": _task_detail_item(context, updated)
    }


def _update_chore(
    context: ComponentState,
    *,
    entry: job_definitions.ChoreEntry,
    payload: ChorePatchRequest | JobPatchRequest,
) -> dict[str, object]:
    document = _patch_chore_document(entry.document, payload)
    catalog = JobCatalog(context.root, context.name)
    path = catalog.save(entry, document)
    _append_job_update(
        context, kind="chore", item_id=document.chore_id(), action="updated", path=path
    )
    updated = catalog.get("chore", document.chore_id(), lifecycle=entry.lifecycle)
    if updated is None:
        raise HTTPException(
            status_code=404,
            detail=f"chore not found after update: {document.chore_id()}",
        )
    return {
        "item": _chore_detail_item(context, updated)
    }


def _patch_task_document(
    document: job_definitions.TaskFile, payload: TaskPatchRequest | JobPatchRequest
) -> job_definitions.TaskFile:
    if "schedule" in payload.model_fields_set:
        raise HTTPException(status_code=400, detail="tasks do not support schedule")
    updates = {
        field: getattr(payload, field)
        for field in ("title", "body")
        if field in payload.model_fields_set
    }
    try:
        return job_definitions.TaskFile.model_validate(
            document.model_copy(update=updates).model_dump(mode="python")
        )
    except ValidationError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


def _patch_chore_document(
    document: job_definitions.ChoreFile, payload: ChorePatchRequest | JobPatchRequest
) -> job_definitions.ChoreFile:
    updates = {
        field: getattr(payload, field)
        for field in ("title", "body", "schedule")
        if field in payload.model_fields_set
    }
    try:
        return job_definitions.ChoreFile.model_validate(
            document.model_copy(update=updates).model_dump(mode="python")
        )
    except ValidationError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


def _append_job_update(
    context: ComponentState,
    *,
    kind: JobKind,
    item_id: str,
    action: str,
    path: Path,
) -> None:
    context.store.append_update(
        kind=cast(Literal["task_changed", "chore_changed"], f"{kind}_changed"),
        payload={
            "id": item_id,
            "kind": kind,
            "action": action,
            "path": _agent_relative_path(context, path),
        },
    )
    context.store.append_event(
        domain="agent",
        domain_id=context.name,
        type=f"{kind}_update",
        payload={
            "id": item_id,
            "kind": kind,
            "action": action,
            "path": _agent_relative_path(context, path),
        },
    )


def _job_runtime(
    context: ComponentState,
    *,
    thread_id: str,
    job: JobRecord | None = None,
) -> dict[str, object]:
    runs = context.store.list_runs(limit=None, thread_id=thread_id)
    ordered = sorted(runs, key=lambda item: item.created_at, reverse=True)
    last = next((item for item in ordered), None)
    return {
        "thread_id": thread_id,
        "last_run": _last_run_item(last) if last is not None else None,
        "next_run": (
            {"at": job.next_run_at}
            if job is not None and job.next_run_at is not None
            else None
        ),
    }


def _last_run_item(run: RunRecord) -> dict[str, object]:
    return {
        "id": run.run_id,
        "status": run.status,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
    }


def _job_record(
    context: ComponentState,
    *,
    kind: job_definitions.JobKind,
    job_id: str,
    lifecycle: job_definitions.JobLifecycle,
) -> JobRecord | None:
    if lifecycle != "ready":
        return None
    store = open_job_store(context.root, context.name)
    try:
        store.reconcile(
            jobs=AgentJobs.load(context.root, context.name, context.get_agent_state().program),
            kind=kind,
        )
        return store.get(job_id=job_id, kind=kind)
    finally:
        store.close()


def _agent_relative_path(context: ComponentState, path: Path) -> str:
    try:
        return str(path.relative_to(context.home))
    except ValueError:
        return str(path)


def _path_updated_at(path: Path) -> str:
    return datetime.fromtimestamp(
        path.stat().st_mtime_ns / 1_000_000_000, tz=timezone.utc
    ).isoformat()


def _cap_summary_item(
    context: ComponentState, entry: PreparedEntry
) -> dict[str, object]:
    item: dict[str, object] = {
        "name": entry.name,
        "description": str(entry.meta["description"])
        if entry.meta.get("description") is not None
        else None,
        "scope": caps.entry_scope(entry, agent_name=context.name),
        "origin": caps.entry_origin(entry),
        "form": caps.entry_form(entry),
        "ref": caps.entry_ref(entry, agent_name=context.name),
        "definition_file": caps.entry_definition_file(entry),
        "editable": entry.source.form == "file",
    }
    line = caps.entry_line(entry)
    if line is not None:
        item["line"] = line
    return item


def _cap_detail_item(
    context: ComponentState, entry: PreparedEntry
) -> dict[str, object]:
    item = _cap_summary_item(context, entry)
    content_path = context.root / entry.path
    content = (
        content_path.read_text(encoding="utf-8") if content_path.is_file() else None
    )
    files = None
    if entry.shape == "dir":
        files = sorted(
            str(item.relative_to(content_path.parent))
            for item in content_path.parent.rglob("*")
            if item.is_file()
        )
    return {
        **item,
        "kind": entry.kind,
        "content": content,
        "files": files,
    }


def _template_summary(template: templates.TemplateSpec) -> dict[str, object]:
    return {
        "kind": template.kind,
        "name": template.name,
        "title": template.title,
        "description": template.description,
        "path": template.path,
    }


def _template_detail(template: templates.TemplateSpec) -> dict[str, object]:
    return {
        **_template_summary(template),
        "content": template.raw_text,
    }


def _state_entry_by_name(
    context: ComponentState, *, kind: CapKind, name: str
) -> PreparedEntry:
    for entry in context.get_agent_state().caps:
        if entry.kind == kind and entry.name == name:
            return entry
    raise HTTPException(status_code=404, detail=f"{kind} not found: {name}")


def _profile_metrics(context: ComponentState) -> dict[str, object]:
    threads = ExecutionProjector(context.store).list_threads(limit=None)
    runs = context.store.list_runs(limit=None)
    steps_by_run = context.store.list_steps_for_runs(
        run_ids=tuple(item.run_id for item in runs)
    )
    thread_counts = {"chat": 0, "chore": 0, "task": 0}
    step_total = 0
    model_total = 0
    tool_total = 0
    system_total = 0
    input_tokens = 0
    output_tokens = 0

    for thread in threads:
        thread_counts[_thread_metric_kind(thread)] += 1

    for step_items in steps_by_run.values():
        for step in step_items:
            step_total += 1
            if step.kind == "model":
                model_total += 1
                usage = step.detail.get("usage")
                if isinstance(usage, Mapping):
                    input_tokens += int(usage.get("input_tokens", 0) or 0)
                    output_tokens += int(usage.get("output_tokens", 0) or 0)
            elif step.kind == "tool":
                tool_total += 1
            else:
                system_total += 1

    return {
        "threads": {
            "total": len(threads),
            **thread_counts,
        },
        "steps": {
            "total": step_total,
            "model": model_total,
            "tool": tool_total,
            "system": system_total,
        },
        "tokens": {
            "input": input_tokens,
            "output": output_tokens,
            "total": input_tokens + output_tokens,
        },
    }


def _profile_environment(
    context: ComponentState,
    *,
    runtime_state: dict[str, object],
) -> dict[str, object]:
    return {
        "sandbox": _runtime_sandbox_spec(runtime_state),
        "home": str(context.home),
        "endpoint": _runtime_endpoint(context, runtime_state=runtime_state),
    }


def _run_or_404(context: ComponentState, run_id: str):
    run = context.store.get_run(run_id=run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"run not found: {run_id}")
    return run


def _thread_or_404(context: ComponentState, thread_id: str) -> None:
    if context.store.get_thread(thread_id=thread_id) is not None:
        return
    if context.store.list_runs(thread_id=thread_id, limit=1):
        return
    raise HTTPException(status_code=404, detail=f"thread not found: {thread_id}")


def _input_message(payload: RunInputMessagePayload) -> Message:
    data = payload.model_dump(mode="python")
    try:
        message = Message.from_data(data)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if message.role != "user":
        raise HTTPException(
            status_code=422, detail="run input message role must be user"
        )
    return message


def _input_apply(value: str) -> CommandApply:
    if value not in {"immediate", "next_step", "next_call"}:
        raise HTTPException(
            status_code=422, detail=f"unsupported run input mode: {value}"
        )
    return "now" if value == "immediate" else cast(CommandApply, value)


def _input_event_payload(run: RunRecord, input: CommandRecord) -> dict[str, object]:
    event_type = {
        "start": "run_starting",
        "steer": "run_steering",
        "stop": "run_stopping",
    }[input.kind]
    payload: dict[str, object] = {
        "run": run.id,
        "thread": run.thread,
        "type": event_type,
        "cmd": input.index,
        "kind": input.kind,
        "apply": input.apply,
        "context": dict(input.context),
        "created_at": input.created_at,
    }
    if input.input is not None:
        payload["input"] = input.input.to_data()
    return payload


def _run_event_payload(run) -> dict[str, object]:
    return {
        "run_id": run.run_id,
        "thread_id": run.thread_id,
        "origin": run.origin,
        "status": run.status,
        "error": run.error,
        "created_at": run.created_at,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
    }


def _event_stream_response(
    request: Request, stream: AsyncIterator[str]
) -> ShutdownAwareStreamingResponse:
    return ShutdownAwareStreamingResponse(
        _guarded_stream(stream),
        shutdown_signal=getattr(request.app.state, "shutdown_signal", None),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _thread_metric_kind(thread: ThreadInfo) -> Literal["chat", "chore", "task"]:
    if thread.id.startswith("task_") or thread.origin == "task":
        return "task"
    if thread.id.startswith("chore_") or thread.origin == "chore":
        return "chore"
    return "chat"


def _copy_fork_history(
    context: ComponentState,
    *,
    source_run: RunRecord,
    target_thread_id: str,
    include_anchor: bool = False,
) -> tuple[RunRecord, ...]:
    source_runs = context.store.list_thread_runs_before(run_id=source_run.run_id)
    if include_anchor:
        source_runs = (*source_runs, source_run)
    target_run_ids = tuple(
        allocate_run_id(context.root, context.name) for _ in source_runs
    )
    return context.store.copy_runs_to_thread(
        source_run_ids=tuple(run.run_id for run in source_runs),
        target_thread_id=target_thread_id,
        target_run_ids=target_run_ids,
    )


def _runtime_endpoint(
    context: ComponentState,
    *,
    runtime_state: dict[str, object] | None = None,
) -> str | None:
    if runtime_state is not None:
        endpoint = runtime_state.get("endpoint")
        if isinstance(endpoint, str) and endpoint.strip():
            return endpoint.strip()
    host = context.config.get("server.host")
    port = context.config.get("server.port")
    if isinstance(host, str) and isinstance(port, int):
        return f"http://{host}:{port}"
    return None


def _runtime_sandbox_spec(runtime_state: dict[str, object]) -> str:
    sandbox = runtime_state.get("sandbox")
    if isinstance(sandbox, dict):
        sandbox_data = {str(key): value for key, value in sandbox.items()}
        selector = sandbox_data.get("selector")
        if isinstance(selector, dict):
            selector_data = {str(key): value for key, value in selector.items()}
            driver = selector_data.get("driver")
            target = selector_data.get("target")
            if isinstance(driver, str) and driver.strip():
                if isinstance(target, str) and target.strip():
                    return f"{driver.strip()}:{target.strip()}"
                return driver.strip()
    return "none"


def _channel_items(context: ComponentState) -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    for name in sorted(context.channel_bindings):
        binding = context.channel_bindings[name]
        plugin = context.channel_plugins.get(name)
        bound_context = channel_context(context, name)
        state_path = bound_context.room / "state.json"
        health = (
            plugin.health(bound_context).to_data()
            if plugin is not None
            else {"ok": False, "detail": "not loaded", "meta": {}}
        )
        items.append(
            {
                "name": name,
                "plugin": binding.plugin,
                "config_keys": sorted(binding.config),
                "poll_state_path": str(state_path),
                "health": health,
            }
        )
    return items


async def _events_stream() -> AsyncIterator[str]:
    yield ": ok\n\n"


async def _guarded_stream(
    stream: AsyncIterator[str],
) -> AsyncIterator[str]:
    try:
        async for chunk in stream:
            yield chunk
    except asyncio.CancelledError:
        return
    finally:
        aclose = getattr(stream, "aclose", None)
        if callable(aclose):
            await cast(Any, aclose)()


def _collection_kind(collection: str) -> CapKind:
    kind = COLLECTION_TO_KIND.get(collection)
    if kind is None:
        raise HTTPException(
            status_code=404, detail=f"unsupported cap collection: {collection}"
        )
    return kind


def _authored_entries(
    context: ComponentState, *, visibility: str
) -> tuple[PreparedEntry, ...]:
    return caps.list_entries(
        context.root,
        context.name,
        visibility=cast(Literal["shared", "private"], visibility),
    )


def _select_components(
    enabled_components: Sequence[str], namespace: str, allowed: Container[str]
) -> list[str]:
    prefix = f"{namespace}."
    return [
        component.removeprefix(prefix)
        for component in enabled_components
        if component.startswith(prefix) and component.removeprefix(prefix) in allowed
    ]
