"""Formal agent inspection and local job API routes."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from collections.abc import AsyncIterator, Container, Sequence
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from toolang.base.types.message import Message, TextPart, message_summary, parts_to_data
from ....common.ids import LOCAL_ID_FAMILY, allocate_id
from ....execution.detail import (
    RunDetail,
    ThreadInfo,
    run_detail_from_record,
    thread_info_from_runs,
    thread_info_from_record,
)
from ....execution.events import MessageData, run_message_data
from ....execution.input import allocate_run_id
from ....execution.records import (
    RunCommandMode,
    RunCommandRecord,
    ModelCallStepPayload,
    RunRecord,
    RunStatus,
    RuntimeStepPayload,
    StepRecord,
    step_input_items_to_data,
    step_payload_to_data,
)
from ....execution.runner import RunRequest
from ....execution.stream import event_data
from .... import agents, caps, jobs, templates, work
from ....state.durable import scan_durable_state
from ....state.prepared import PreparedEntry, load_prepared_state
from ...registry import ROUTER_COMPONENTS, RUNNER_COMPONENTS, TRIGGER_COMPONENTS, component_group, normalize_component_names
from .._streaming import ShutdownAwareStreamingResponse

if TYPE_CHECKING:
    from ....up import UptimeContext
    from ....execution.runner import RunOutcome
    from ....execution.records import RunRecord

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
    schedule: str = work.DEFAULT_CHORE_SCHEDULE


class ChorePatchRequest(_ApiModel):
    title: str | None = None
    body: str | None = None
    schedule: str | None = None


def create_router() -> APIRouter:
    """Build the formal agent API route group."""

    router = APIRouter(prefix="/api/v1")

    @router.get("/profile", tags=["agent"], summary="Get Profile")
    async def profile(request: Request) -> dict[str, object]:
        context = request.app.state.runtime
        runtime_state = agents.load_runtime_state(context.root, context.name) or {}
        return {
            "agent": context.name,
            "display_name": context.name,
            "title": None,
            "summary": None,
            "description": None,
            "avatar": None,
            "environment": _profile_environment(context, runtime_state=runtime_state),
            "metrics": _profile_metrics(context),
        }

    @router.get("/caps", tags=["caps"], summary="Get Caps Summary")
    async def caps_summary(request: Request) -> dict[str, object]:
        context = request.app.state.runtime
        collections = {
            "psyches": _cap_collection(context, kind="psyche"),
            "skills": _cap_collection(context, kind="skill"),
            "services": _cap_collection(context, kind="service"),
            "prompts": _cap_collection(context, kind="prompt"),
        }
        return {
            "agent": context.name,
            **collections,
            "counts": {key: len(value) for key, value in collections.items()},
        }

    @router.get("/psyches", tags=["caps"], summary="List Psyches")
    @router.get("/skills", tags=["caps"], summary="List Skills")
    @router.get("/services", tags=["caps"], summary="List Services")
    @router.get("/prompts", tags=["caps"], summary="List Prompts")
    async def cap_list(request: Request) -> dict[str, object]:
        context = request.app.state.runtime
        kind = _collection_kind(str(request.url.path).rsplit("/", 1)[-1])
        return {"items": _cap_collection(context, kind=kind)}

    @router.get("/psyches/templates", tags=["caps"], summary="List Psyche Templates")
    @router.get("/skills/templates", tags=["caps"], summary="List Skill Templates")
    @router.get("/services/templates", tags=["caps"], summary="List Service Templates")
    @router.get("/prompts/templates", tags=["caps"], summary="List Prompt Templates")
    async def cap_template_list(request: Request) -> dict[str, object]:
        collection = str(request.url.path).split("/")[3]
        kind = _collection_kind(collection)
        return {"items": [_template_summary(item) for item in templates.list_templates(kind)]}

    @router.get("/psyches/templates/{template_name}", tags=["caps"], summary="Get Psyche Template")
    @router.get("/skills/templates/{template_name}", tags=["caps"], summary="Get Skill Template")
    @router.get("/services/templates/{template_name}", tags=["caps"], summary="Get Service Template")
    @router.get("/prompts/templates/{template_name}", tags=["caps"], summary="Get Prompt Template")
    async def cap_template_detail(request: Request, template_name: str) -> dict[str, object]:
        collection = str(request.url.path).split("/")[3]
        kind = _collection_kind(collection)
        try:
            template = templates.load_template(kind, template_name)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"item": _template_detail(template)}

    @router.get("/psyches/{name}", tags=["caps"], summary="Get Psyche")
    @router.get("/skills/{name}", tags=["caps"], summary="Get Skill")
    @router.get("/services/{name}", tags=["caps"], summary="Get Service")
    @router.get("/prompts/{name}", tags=["caps"], summary="Get Prompt")
    async def cap_detail(request: Request, name: str) -> dict[str, object]:
        context = request.app.state.runtime
        collection = str(request.url.path).split("/")[3]
        kind = _collection_kind(collection)
        entry = _live_entry_by_name(context, kind=kind, name=name)
        return {"item": _cap_detail_item(context, entry)}

    @router.get("/runs", tags=["activity"], summary="List Runs")
    async def runs(
        request: Request,
        limit: int = Query(default=50),
        thread_id: str | None = None,
        status: RunStatus | None = None,
    ) -> dict[str, object]:
        context = request.app.state.runtime
        runs = context.store.list_runs(limit=limit, thread_id=thread_id, status=status)
        steps_by_run = context.store.list_steps_for_runs(run_ids=tuple(item.run_id for item in runs))
        commands_by_run = {run.run_id: context.store.list_commands(run_id=run.run_id) for run in runs}
        items = [
            _run_item(
                item,
                inputs=commands_by_run.get(item.run_id, ()),
                steps=steps_by_run.get(item.run_id, ()),
            )
            for item in runs
        ]
        return {"items": items}

    @router.get("/runs/{run_id}", tags=["activity"], summary="Get Run")
    async def run_detail(request: Request, run_id: str) -> dict[str, object]:
        context = request.app.state.runtime
        run = context.store.get_run(run_id=run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"run not found: {run_id}")
        return {
            **_run_detail_data(_run_detail(context, run)),
            "event_cursor": context.store.latest_event_cursor(domain="run", domain_id=run_id),
        }

    @router.get("/runs/{run_id}/events", tags=["activity"], summary="List Run Events")
    async def run_events(request: Request, run_id: str, after: int | None = None, limit: int = Query(default=100)) -> dict[str, object]:
        context = request.app.state.runtime
        _run_or_404(context, run_id)
        events = context.store.list_events(domain="run", domain_id=run_id, after=after, limit=limit)
        return {
            "cursor": context.store.latest_event_cursor(domain="run", domain_id=run_id),
            "items": [event_data(item) for item in events],
        }

    @router.get("/runs/{run_id}/stream", tags=["activity"], summary="Stream Run Events")
    async def run_stream(request: Request, run_id: str, after: int | None = None) -> ShutdownAwareStreamingResponse:
        context = request.app.state.runtime
        _run_or_404(context, run_id)
        return _event_stream_response(
            request,
            context.events.stream(domain="run", domain_id=run_id, after=after),
        )

    @router.post("/runs/{run_id}/cancel", tags=["activity"], summary="Cancel Run")
    async def cancel_run(request: Request, run_id: str, payload: RunCancelRequest | None = None) -> dict[str, object]:
        context = request.app.state.runtime
        run = _run_or_404(context, run_id)
        if run.status != "running":
            raise HTTPException(status_code=409, detail=f"run is not running: {run_id}")
        command_record = context.store.append_command(
            run_id=run.run_id,
            kind="stop",
            mode=_input_mode(payload.mode if payload else "immediate"),
            request_id=payload.request_id if payload else None,
        )
        input_payload = _input_event_payload(run, command_record)
        context.events.publish(domain="run", domain_id=run.run_id, type="run_command", payload=input_payload)
        context.events.publish(domain="thread", domain_id=run.thread_id, type="run_command", payload=input_payload)
        run = context.store.cancel_run(run_id=run_id, error=payload.reason if payload else None)
        event_payload = _run_event_payload(run)
        context.events.publish(domain="run", domain_id=run.run_id, type="run_end", payload=event_payload)
        context.events.publish(domain="thread", domain_id=run.thread_id, type="run_end", payload=event_payload)
        context.events.publish(domain="agent", domain_id=context.name, type="thread_update", payload=event_payload)
        return {
            "run": _run_item(
                run,
                inputs=context.store.list_commands(run_id=run.run_id),
                steps=context.store.list_steps(run_id=run.run_id),
            ),
            "input": input_payload,
        }

    @router.post("/runs/{run_id}/rewind", tags=["activity"], summary="Rewind Thread")
    async def rewind_thread(request: Request, run_id: str, payload: RunRestartRequest) -> dict[str, object]:
        context = request.app.state.runtime
        run = _run_or_404(context, run_id)
        _require_branchable_thread(context, run)
        message = _input_message(payload.message) if payload.message is not None else None
        new_run_id = allocate_run_id(context) if message is not None else None
        _cancel_running_replaced_runs(context, anchor=run, reason="Run was rewound.")
        superseded = context.store.supersede_thread_from_run(
            run_id=run.run_id,
            superseded={"type": "rewound", "by": new_run_id, "from_run_id": run.run_id},
        )
        if message is not None and new_run_id is not None:
            context.runner.enqueue(
                RunRequest(
                    group="chat",
                    origin="chat",
                    run_id=new_run_id,
                    thread_id=run.thread_id,
                    message=message,
                    metadata={"request_id": payload.request_id} if payload.request_id is not None else {},
                )
            )
        event_payload = {
            "from_run_id": run.run_id,
            "new_run_id": new_run_id,
            "thread_id": run.thread_id,
            "superseded_run_ids": [item.run_id for item in superseded],
        }
        if message is not None:
            event_payload["message"] = message.to_data()
        context.events.publish(domain="thread", domain_id=run.thread_id, type="thread_rewind", payload=event_payload)
        context.events.publish(domain="agent", domain_id=context.name, type="thread_update", payload=event_payload)
        return {
            "run_id": new_run_id,
            "thread_id": run.thread_id,
            "superseded_run_ids": [item.run_id for item in superseded],
            "message": message.to_data() if message is not None else None,
        }

    @router.post("/runs/{run_id}/fork", tags=["activity"], summary="Fork Thread")
    async def fork_thread(request: Request, run_id: str, payload: RunRestartRequest) -> dict[str, object]:
        context = request.app.state.runtime
        run = _run_or_404(context, run_id)
        _require_branchable_thread(context, run)
        message = _input_message(payload.message) if payload.message is not None else None
        new_run_id = allocate_run_id(context) if message is not None else None
        new_thread_id = _fork_thread_id(context, source_thread_id=run.thread_id)
        context.store.ensure_thread(
            thread_id=new_thread_id,
            origin="chat",
            parent=json.dumps(
                {"type": "fork", "thread_id": run.thread_id, "from_run_id": run.run_id},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )
        copied_runs = _copy_fork_history(
            context,
            source_run=run,
            target_thread_id=new_thread_id,
            include_anchor=payload.include_anchor,
        )
        if message is not None and new_run_id is not None:
            context.runner.enqueue(
                RunRequest(
                    group="chat",
                    origin="chat",
                    run_id=new_run_id,
                    thread_id=new_thread_id,
                    message=message,
                    metadata={"request_id": payload.request_id} if payload.request_id is not None else {},
                )
            )
        event_payload = {
            "from_run_id": run.run_id,
            "include_anchor": payload.include_anchor,
            "source_thread_id": run.thread_id,
            "thread_id": new_thread_id,
            "run_id": new_run_id,
            "copied_run_ids": [item.run_id for item in copied_runs],
        }
        if message is not None:
            event_payload["message"] = message.to_data()
        context.events.publish(domain="thread", domain_id=run.thread_id, type="thread_fork", payload=event_payload)
        context.events.publish(domain="thread", domain_id=new_thread_id, type="thread_forked", payload=event_payload)
        context.events.publish(domain="agent", domain_id=context.name, type="thread_update", payload=event_payload)
        return {
            "run_id": new_run_id,
            "thread_id": new_thread_id,
            "source_thread_id": run.thread_id,
            "from_run_id": run.run_id,
            "include_anchor": payload.include_anchor,
            "copied_run_ids": [item.run_id for item in copied_runs],
            "message": message.to_data() if message is not None else None,
        }

    @router.post("/runs/{run_id}/steer", tags=["activity"], summary="Steer Run")
    async def steer_run(request: Request, run_id: str, payload: RunSteerRequest) -> dict[str, object]:
        context = request.app.state.runtime
        run = _run_or_404(context, run_id)
        if run.status != "running":
            raise HTTPException(status_code=409, detail=f"run is not running: {run_id}")
        message = _input_message(payload.message)
        command_record = context.store.append_command(
            run_id=run.run_id,
            kind="steer",
            mode=_input_mode(payload.mode),
            request_id=payload.request_id,
            message=message,
        )
        event_payload = _input_event_payload(run, command_record)
        context.events.publish(domain="run", domain_id=run.run_id, type="run_command", payload=event_payload)
        context.events.publish(domain="thread", domain_id=run.thread_id, type="run_command", payload=event_payload)
        return {"input": event_payload}

    @router.get("/instruct/{prompt_hash}", tags=["activity"], summary="Get Instruct Prompt")
    async def instruct_prompt(request: Request, prompt_hash: str) -> dict[str, object]:
        context = request.app.state.runtime
        body = context.store.get_prompt(prompt_hash=prompt_hash)
        if body is None:
            raise HTTPException(status_code=404, detail=f"instruct not found: {prompt_hash}")
        return {"hash": prompt_hash, "body": body}

    @router.get("/context/{prompt_hash}", tags=["activity"], summary="Get Context Prompt")
    async def context_prompt(request: Request, prompt_hash: str) -> dict[str, object]:
        context = request.app.state.runtime
        body = context.store.get_prompt(prompt_hash=prompt_hash)
        if body is None:
            raise HTTPException(status_code=404, detail=f"context not found: {prompt_hash}")
        return {"hash": prompt_hash, "body": body}

    @router.get("/threads", tags=["activity"], summary="List Threads")
    async def threads(
        request: Request,
        limit: int = Query(default=50),
        origin: str | None = None,
        channel: str | None = None,
        status: str | None = None,
    ) -> dict[str, object]:
        context = request.app.state.runtime
        items = _thread_items(context)
        if origin is not None:
            items = [item for item in items if item.origin == origin]
        if channel is not None:
            items = [item for item in items if item.channel == channel]
        if status is not None:
            items = [item for item in items if item.status == status]
        return {"items": [asdict(item) for item in items[:limit]]}

    @router.get("/threads/{thread_id}", tags=["activity"], summary="Get Thread")
    async def thread_detail(request: Request, thread_id: str, limit: int = Query(default=50)) -> dict[str, object]:
        context = request.app.state.runtime
        items = _thread_items(context)
        info = next((item for item in items if item.id == thread_id), None)
        if info is None:
            raise HTTPException(status_code=404, detail=f"thread not found: {thread_id}")
        thread_runs = context.store.list_thread_runs_chronological(thread_id=thread_id)
        if limit is not None:
            thread_runs = thread_runs[-limit:]
        runs = [
            _run_detail(context, item)
            for item in thread_runs
        ]
        return {
            "info": asdict(info),
            "runs": [_run_detail_data(item) for item in runs],
            "event_cursor": context.store.latest_event_cursor(domain="thread", domain_id=thread_id),
        }

    @router.get("/threads/{thread_id}/events", tags=["activity"], summary="List Thread Events")
    async def thread_events(request: Request, thread_id: str, after: int | None = None, limit: int = Query(default=100)) -> dict[str, object]:
        context = request.app.state.runtime
        _thread_or_404(context, thread_id)
        events = context.store.list_events(domain="thread", domain_id=thread_id, after=after, limit=limit)
        return {
            "cursor": context.store.latest_event_cursor(domain="thread", domain_id=thread_id),
            "items": [event_data(item) for item in events],
        }

    @router.get("/threads/{thread_id}/stream", tags=["activity"], summary="Stream Thread Events")
    async def thread_stream(request: Request, thread_id: str, after: int | None = None) -> ShutdownAwareStreamingResponse:
        context = request.app.state.runtime
        _thread_or_404(context, thread_id)
        return _event_stream_response(
            request,
            context.events.stream(domain="thread", domain_id=thread_id, after=after),
        )

    @router.get("/events", tags=["activity"], summary="List Events")
    async def events(request: Request, limit: int = Query(default=100)) -> dict[str, object]:
        context = request.app.state.runtime
        return {
            "items": [asdict(item) for item in context.store.list_updates(limit=limit)]
        }

    @router.get("/events/stream", tags=["activity"], summary="Stream Events")
    async def events_stream(request: Request) -> ShutdownAwareStreamingResponse:
        return ShutdownAwareStreamingResponse(
            _guarded_stream(_events_stream()),
            shutdown_signal=getattr(request.app.state, "shutdown_signal", None),
            media_type="text/event-stream",
        )

    @router.get("/agent/events", tags=["activity"], summary="List Agent Events")
    async def agent_events(request: Request, after: int | None = None, limit: int = Query(default=100)) -> dict[str, object]:
        context = request.app.state.runtime
        events = context.store.list_events(domain="agent", domain_id=context.name, after=after, limit=limit)
        return {
            "cursor": context.store.latest_event_cursor(domain="agent", domain_id=context.name),
            "items": [event_data(item) for item in events],
        }

    @router.get("/agent/stream", tags=["activity"], summary="Stream Agent Events")
    async def agent_stream(request: Request, after: int | None = None) -> ShutdownAwareStreamingResponse:
        context = request.app.state.runtime
        return _event_stream_response(
            request,
            context.events.stream(domain="agent", domain_id=context.name, after=after),
        )

    @router.get("/jobs", tags=["jobs"], summary="List Jobs")
    async def jobs(
        request: Request,
        kind: JobKind | None = None,
    ) -> dict[str, object]:
        context = request.app.state.runtime
        items = _job_collection(context, archived=False)
        if kind is not None:
            items = [item for item in items if item["kind"] == kind]
        return {"items": items}

    @router.get("/jobs/archived", tags=["jobs"], summary="List Archived Jobs")
    async def archived_jobs(request: Request, kind: JobKind | None = None) -> dict[str, object]:
        context = request.app.state.runtime
        items = _job_collection(context, archived=True)
        if kind is not None:
            items = [item for item in items if item["kind"] == kind]
        return {"items": items}

    @router.get("/jobs/archived/{job_id}", tags=["jobs"], summary="Get Archived Job")
    async def archived_job_detail(request: Request, job_id: str) -> dict[str, object]:
        context = request.app.state.runtime
        kind, entry = _find_archived_job_or_404(context, job_id)
        return {"item": _job_detail_item(context, kind=kind, entry=entry)}

    @router.patch("/jobs/archived/{job_id}", tags=["jobs"], summary="Update Archived Job")
    async def update_archived_job(request: Request, job_id: str, payload: JobPatchRequest) -> dict[str, object]:
        context = request.app.state.runtime
        kind, entry = _find_archived_job_or_404(context, job_id)
        return _update_job(context, kind=kind, entry=entry, payload=payload)

    @router.delete("/jobs/archived/{job_id}", tags=["jobs"], summary="Delete Archived Job")
    async def delete_archived_job(request: Request, job_id: str) -> dict[str, object]:
        context = request.app.state.runtime
        kind, entry = _find_archived_job_or_404(context, job_id)
        removed = (
            work.remove_archived_task(context.root, context.name, job_id)
            if kind == "task"
            else work.remove_archived_chore(context.root, context.name, job_id)
        )
        if not removed:
            raise HTTPException(status_code=404, detail=f"archived job not found: {job_id}")
        _append_job_update(context, kind=kind, item_id=job_id, action="deleted", path=entry.path)
        return {"deleted": True, "id": job_id, "kind": kind}

    @router.get("/jobs/{job_id}", tags=["jobs"], summary="Get Job")
    async def job_detail(request: Request, job_id: str) -> dict[str, object]:
        context = request.app.state.runtime
        kind, entry = _find_job_or_404(context, job_id)
        return {"item": _job_detail_item(context, kind=kind, entry=entry)}

    @router.patch("/jobs/{job_id}", tags=["jobs"], summary="Update Job")
    async def update_job(request: Request, job_id: str, payload: JobPatchRequest) -> dict[str, object]:
        context = request.app.state.runtime
        kind, entry = _find_job_or_404(context, job_id)
        return _update_job(context, kind=kind, entry=entry, payload=payload)

    @router.get("/tasks", tags=["jobs"], summary="List Tasks")
    async def tasks(request: Request) -> dict[str, object]:
        context = request.app.state.runtime
        return {"items": _task_collection(context, archived=False)}

    @router.post("/tasks", tags=["jobs"], summary="Create Task", status_code=201)
    async def create_task(request: Request, payload: TaskCreateRequest) -> dict[str, object]:
        context = request.app.state.runtime
        document = _task_document_from_create(context, payload)
        path = work.task_path(context.root, context.name, document.task_id())
        document.save(path)
        _append_job_update(context, kind="task", item_id=document.task_id(), action="created", path=path)
        entry = _find_task_or_404(context, document.task_id())
        return {"item": _task_detail_item(context, entry)}

    @router.get("/tasks/archived", tags=["jobs"], summary="List Archived Tasks")
    async def archived_tasks(request: Request) -> dict[str, object]:
        context = request.app.state.runtime
        return {"items": _task_collection(context, archived=True)}

    @router.get("/tasks/archived/{task_id}", tags=["jobs"], summary="Get Archived Task")
    async def archived_task_detail(request: Request, task_id: str) -> dict[str, object]:
        context = request.app.state.runtime
        entry = _find_archived_task_or_404(context, task_id)
        return {"item": _task_detail_item(context, entry)}

    @router.patch("/tasks/archived/{task_id}", tags=["jobs"], summary="Update Archived Task")
    async def update_archived_task(request: Request, task_id: str, payload: TaskPatchRequest) -> dict[str, object]:
        context = request.app.state.runtime
        entry = _find_archived_task_or_404(context, task_id)
        return _update_task(context, entry=entry, payload=payload)

    @router.delete("/tasks/archived/{task_id}", tags=["jobs"], summary="Delete Archived Task")
    async def delete_archived_task(request: Request, task_id: str) -> dict[str, object]:
        context = request.app.state.runtime
        entry = _find_archived_task_or_404(context, task_id)
        if not work.remove_archived_task(context.root, context.name, task_id):
            raise HTTPException(status_code=404, detail=f"archived task not found: {task_id}")
        _append_job_update(context, kind="task", item_id=task_id, action="deleted", path=entry.path)
        return {"deleted": True, "id": task_id, "kind": "task"}

    @router.get("/tasks/{task_id}", tags=["jobs"], summary="Get Task")
    async def task_detail(request: Request, task_id: str) -> dict[str, object]:
        context = request.app.state.runtime
        entry = _find_task_or_404(context, task_id)
        return {"item": _task_detail_item(context, entry)}

    @router.patch("/tasks/{task_id}", tags=["jobs"], summary="Update Task")
    async def update_task(request: Request, task_id: str, payload: TaskPatchRequest) -> dict[str, object]:
        context = request.app.state.runtime
        entry = _find_task_or_404(context, task_id)
        return _update_task(context, entry=entry, payload=payload)

    @router.get("/chores", tags=["jobs"], summary="List Chores")
    async def chores(request: Request) -> dict[str, object]:
        context = request.app.state.runtime
        return {"items": _chore_collection(context, archived=False)}

    @router.post("/chores", tags=["jobs"], summary="Create Chore", status_code=201)
    async def create_chore(request: Request, payload: ChoreCreateRequest) -> dict[str, object]:
        context = request.app.state.runtime
        document = _chore_document_from_create(context, payload)
        path = work.chore_path(context.root, context.name, document.chore_id())
        document.save(path)
        _append_job_update(context, kind="chore", item_id=document.chore_id(), action="created", path=path)
        entry = _find_chore_or_404(context, document.chore_id())
        return {"item": _chore_detail_item(context, entry)}

    @router.get("/chores/archived", tags=["jobs"], summary="List Archived Chores")
    async def archived_chores(request: Request) -> dict[str, object]:
        context = request.app.state.runtime
        return {"items": _chore_collection(context, archived=True)}

    @router.get("/chores/archived/{chore_id}", tags=["jobs"], summary="Get Archived Chore")
    async def archived_chore_detail(request: Request, chore_id: str) -> dict[str, object]:
        context = request.app.state.runtime
        entry = _find_archived_chore_or_404(context, chore_id)
        return {"item": _chore_detail_item(context, entry)}

    @router.patch("/chores/archived/{chore_id}", tags=["jobs"], summary="Update Archived Chore")
    async def update_archived_chore(request: Request, chore_id: str, payload: ChorePatchRequest) -> dict[str, object]:
        context = request.app.state.runtime
        entry = _find_archived_chore_or_404(context, chore_id)
        return _update_chore(context, entry=entry, payload=payload)

    @router.delete("/chores/archived/{chore_id}", tags=["jobs"], summary="Delete Archived Chore")
    async def delete_archived_chore(request: Request, chore_id: str) -> dict[str, object]:
        context = request.app.state.runtime
        entry = _find_archived_chore_or_404(context, chore_id)
        if not work.remove_archived_chore(context.root, context.name, chore_id):
            raise HTTPException(status_code=404, detail=f"archived chore not found: {chore_id}")
        _append_job_update(context, kind="chore", item_id=chore_id, action="deleted", path=entry.path)
        return {"deleted": True, "id": chore_id, "kind": "chore"}

    @router.get("/chores/{chore_id}", tags=["jobs"], summary="Get Chore")
    async def chore_detail(request: Request, chore_id: str) -> dict[str, object]:
        context = request.app.state.runtime
        entry = _find_chore_or_404(context, chore_id)
        return {"item": _chore_detail_item(context, entry)}

    @router.patch("/chores/{chore_id}", tags=["jobs"], summary="Update Chore")
    async def update_chore(request: Request, chore_id: str, payload: ChorePatchRequest) -> dict[str, object]:
        context = request.app.state.runtime
        entry = _find_chore_or_404(context, chore_id)
        return _update_chore(context, entry=entry, payload=payload)

    @router.get("/will", tags=["jobs"], summary="Get Will")
    async def will() -> dict[str, object]:
        return {"item": None}

    return router


def snapshot_context(
    context: UptimeContext,
    *,
    enabled_components: Sequence[str] | None = None,
    enabled_features: Sequence[str] | None = None,
) -> dict[str, object]:
    """Return the internal runtime snapshot used by tests and diagnostics."""

    components = enabled_components if enabled_components is not None else enabled_features
    if components is None:
        raise TypeError("enabled_components is required")
    components = normalize_component_names(tuple(components))
    durable = scan_durable_state(context.root, context.name)
    prepared = load_prepared_state(context.root, context.name)
    runner_snapshot = context.runner.snapshot()
    live_operational_facts: dict[str, object] = {
        "queue_pending": len(context.runner),
        "active_runs": _runner_in_flight(runner_snapshot),
        "completed_runs": len(context.runner.completed()),
    }
    durable_operational_facts: dict[str, object] = {
        "prepared_fingerprint": prepared.fingerprint,
        **live_operational_facts,
    }
    recent_runs = context.store.list_runs(limit=20)
    recent_steps = context.store.list_steps_for_runs(run_ids=tuple(item.run_id for item in recent_runs))
    recent_commands = {run.run_id: context.store.list_commands(run_id=run.run_id) for run in recent_runs}
    return {
        "enabled_components": list(components),
        "router_components": _select_components(components, "router", ROUTER_COMPONENT_LEAVES),
        "runner_components": _select_components(components, "runner", RUNNER_COMPONENT_LEAVES),
        "trigger_components": _select_components(components, "trigger", TRIGGER_COMPONENT_LEAVES),
        "queue_pending": len(context.runner),
        "durable": {
            "toolang_root": str(durable.toolang_root),
            "agent_name": durable.agent_name,
            "fingerprint": durable.fingerprint,
            "scanned_at": durable.scanned_at,
            "definitions": {
                "program_source": durable.program_source,
                "config_paths": list(durable.config_paths),
                "shared_entries": [entry.to_snapshot() for entry in _authored_entries(context, visibility="shared")],
                "private_entries": [entry.to_snapshot() for entry in _authored_entries(context, visibility="private")],
            },
            "operational_facts": durable_operational_facts,
        },
        "prepared": prepared.to_snapshot(),
        "live": context.live.to_snapshot(operational_facts=live_operational_facts),
        "runner": runner_snapshot,
        "channels": _channel_items(context),
        "execution": {
            "recent_updates": [asdict(item) for item in context.store.list_updates(limit=20)],
            "recent_runs": [asdict(item) for item in recent_runs],
            "recent_messages": [
                asdict(item)
                for run in sorted(recent_runs, key=lambda item: item.created_at)
                for item in run_message_data(
                    run,
                    inputs=recent_commands.get(run.run_id, ()),
                    steps=recent_steps.get(run.run_id, ()),
                )
            ],
        },
        "completed_runs": [_run_outcome_data(result) for result in context.runner.completed()],
    }


def _cap_collection(context: UptimeContext, *, kind: CapKind) -> list[dict[str, object]]:
    return [
        _cap_summary_item(context, entry)
        for entry in context.live.cap_entries
        if entry.kind == kind
    ]


def _job_collection(context: UptimeContext, *, archived: bool) -> list[dict[str, object]]:
    return [
        *_task_collection(context, archived=archived),
        *_chore_collection(context, archived=archived),
    ]


def _task_collection(context: UptimeContext, *, archived: bool) -> list[dict[str, object]]:
    entries = (
        work.list_archived_tasks(context.root, context.name)
        if archived
        else work.list_tasks(context.root, context.name)
    )
    return [
        _task_item(context, entry)
        for entry in entries
    ]


def _chore_collection(context: UptimeContext, *, archived: bool) -> list[dict[str, object]]:
    entries = (
        work.list_archived_chores(context.root, context.name)
        if archived
        else work.list_chores(context.root, context.name)
    )
    return [
        _chore_item(context, entry)
        for entry in entries
    ]


def _task_item(context: UptimeContext, entry: work.TaskEntry) -> dict[str, object]:
    document = entry.document
    job = _job_record(context, kind="task", job_id=document.task_id(), lifecycle=entry.lifecycle)
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


def _task_detail_item(context: UptimeContext, entry: work.TaskEntry) -> dict[str, object]:
    return {
        **_task_item(context, entry),
        "body": entry.document.body,
    }


def _chore_item(context: UptimeContext, entry: work.ChoreEntry) -> dict[str, object]:
    document = entry.document
    job = _job_record(context, kind="chore", job_id=document.chore_id(), lifecycle=entry.lifecycle)
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


def _chore_detail_item(context: UptimeContext, entry: work.ChoreEntry) -> dict[str, object]:
    return {
        **_chore_item(context, entry),
        "body": entry.document.body,
    }


def _job_detail_item(
    context: UptimeContext,
    *,
    kind: JobKind,
    entry: work.TaskEntry | work.ChoreEntry,
) -> dict[str, object]:
    if kind == "task":
        return _task_detail_item(context, cast(work.TaskEntry, entry))
    return _chore_detail_item(context, cast(work.ChoreEntry, entry))


def _find_job_or_404(context: UptimeContext, job_id: str) -> tuple[JobKind, work.TaskEntry | work.ChoreEntry]:
    task = work.find_task(context.root, context.name, job_id)
    if task is not None:
        return "task", task
    chore = work.find_chore(context.root, context.name, job_id)
    if chore is not None:
        return "chore", chore
    raise HTTPException(status_code=404, detail=f"job not found: {job_id}")


def _find_archived_job_or_404(context: UptimeContext, job_id: str) -> tuple[JobKind, work.TaskEntry | work.ChoreEntry]:
    task = work.find_archived_task(context.root, context.name, job_id)
    if task is not None:
        return "task", task
    chore = work.find_archived_chore(context.root, context.name, job_id)
    if chore is not None:
        return "chore", chore
    raise HTTPException(status_code=404, detail=f"archived job not found: {job_id}")


def _find_task_or_404(
    context: UptimeContext,
    task_id: str,
) -> work.TaskEntry:
    entry = work.find_task(context.root, context.name, task_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"task not found: {task_id}")
    return entry


def _find_archived_task_or_404(context: UptimeContext, task_id: str) -> work.TaskEntry:
    entry = work.find_archived_task(context.root, context.name, task_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"archived task not found: {task_id}")
    return entry


def _find_chore_or_404(
    context: UptimeContext,
    chore_id: str,
) -> work.ChoreEntry:
    entry = work.find_chore(context.root, context.name, chore_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"chore not found: {chore_id}")
    return entry


def _find_archived_chore_or_404(context: UptimeContext, chore_id: str) -> work.ChoreEntry:
    entry = work.find_archived_chore(context.root, context.name, chore_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"archived chore not found: {chore_id}")
    return entry


def _task_document_from_create(context: UptimeContext, payload: TaskCreateRequest) -> work.TaskFile:
    try:
        return work.TaskFile(
            id=work.allocate_job_id(context.root, context.name),
            title=payload.title,
            body=payload.body,
        )
    except ValidationError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


def _chore_document_from_create(context: UptimeContext, payload: ChoreCreateRequest) -> work.ChoreFile:
    try:
        return work.ChoreFile(
            id=work.allocate_job_id(context.root, context.name),
            title=payload.title,
            body=payload.body,
            schedule=payload.schedule,
        )
    except ValidationError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


def _update_job(
    context: UptimeContext,
    *,
    kind: JobKind,
    entry: work.TaskEntry | work.ChoreEntry,
    payload: JobPatchRequest,
) -> dict[str, object]:
    if kind == "task":
        return _update_task(context, entry=cast(work.TaskEntry, entry), payload=payload)
    return _update_chore(context, entry=cast(work.ChoreEntry, entry), payload=payload)


def _update_task(
    context: UptimeContext,
    *,
    entry: work.TaskEntry,
    payload: TaskPatchRequest | JobPatchRequest,
) -> dict[str, object]:
    document = _patch_task_document(entry.document, payload)
    path = work.save_task_entry(context.root, context.name, entry, document)
    _append_job_update(context, kind="task", item_id=document.task_id(), action="updated", path=path)
    updated = work.find_task(context.root, context.name, document.task_id(), lifecycle=entry.lifecycle)
    if updated is None:
        raise HTTPException(status_code=404, detail=f"task not found after update: {document.task_id()}")
    return {"item": _task_detail_item(context, updated)}


def _update_chore(
    context: UptimeContext,
    *,
    entry: work.ChoreEntry,
    payload: ChorePatchRequest | JobPatchRequest,
) -> dict[str, object]:
    document = _patch_chore_document(entry.document, payload)
    path = work.save_chore_entry(context.root, context.name, entry, document)
    _append_job_update(context, kind="chore", item_id=document.chore_id(), action="updated", path=path)
    updated = work.find_chore(context.root, context.name, document.chore_id(), lifecycle=entry.lifecycle)
    if updated is None:
        raise HTTPException(status_code=404, detail=f"chore not found after update: {document.chore_id()}")
    return {"item": _chore_detail_item(context, updated)}


def _patch_task_document(document: work.TaskFile, payload: TaskPatchRequest | JobPatchRequest) -> work.TaskFile:
    if "schedule" in payload.model_fields_set:
        raise HTTPException(status_code=400, detail="tasks do not support schedule")
    updates = {
        field: getattr(payload, field)
        for field in ("title", "body")
        if field in payload.model_fields_set
    }
    try:
        return work.TaskFile.model_validate(document.model_copy(update=updates).model_dump(mode="python"))
    except ValidationError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


def _patch_chore_document(document: work.ChoreFile, payload: ChorePatchRequest | JobPatchRequest) -> work.ChoreFile:
    updates = {
        field: getattr(payload, field)
        for field in ("title", "body", "schedule")
        if field in payload.model_fields_set
    }
    try:
        return work.ChoreFile.model_validate(document.model_copy(update=updates).model_dump(mode="python"))
    except ValidationError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


def _append_job_update(
    context: UptimeContext,
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
    context.events.publish(
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
    context: UptimeContext,
    *,
    thread_id: str,
    job: jobs.JobRecord | None = None,
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
    context: UptimeContext,
    *,
    kind: work.JobKind,
    job_id: str,
    lifecycle: work.JobLifecycle,
) -> jobs.JobRecord | None:
    if lifecycle != "ready":
        return None
    store = jobs.open_job_store(context.root, context.name)
    try:
        store.reconcile(toolang_root=context.root, agent_name=context.name, kind=kind)
        return store.get(job_id=job_id, kind=kind)
    finally:
        store.close()


def _agent_relative_path(context: UptimeContext, path: Path) -> str:
    try:
        return str(path.relative_to(context.home))
    except ValueError:
        return str(path)


def _path_updated_at(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime_ns / 1_000_000_000, tz=timezone.utc).isoformat()


def _cap_summary_item(context: UptimeContext, entry: PreparedEntry) -> dict[str, object]:
    item: dict[str, object] = {
        "name": entry.name,
        "description": str(entry.meta["description"]) if entry.meta.get("description") is not None else None,
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


def _cap_detail_item(context: UptimeContext, entry: PreparedEntry) -> dict[str, object]:
    item = _cap_summary_item(context, entry)
    content_path = context.root / entry.path
    content = content_path.read_text(encoding="utf-8") if content_path.is_file() else None
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


def _live_entry_by_name(context: UptimeContext, *, kind: CapKind, name: str) -> PreparedEntry:
    for entry in context.live.cap_entries:
        if entry.kind == kind and entry.name == name:
            return entry
    raise HTTPException(status_code=404, detail=f"{kind} not found: {name}")


def _run_item(run: RunRecord, *, inputs: Sequence[RunCommandRecord], steps: Sequence) -> dict[str, object]:
    detail = run_detail_from_record(run, inputs=inputs, steps=steps)
    input_text = message_summary(detail.input.parts) if detail.input is not None else ""
    last_step_message = next(
        (item.message for item in reversed(detail.output.steps) if item.message is not None),
        None,
    )
    summary = (
        message_summary(last_step_message.parts)
        if last_step_message is not None
        else input_text
    )
    if run.status == "failed" and run.error and (not summary or summary == input_text):
        summary = run.error
    return {
        "id": run.run_id,
        "origin": run.origin,
        "thread_id": run.thread_id,
        "input_text": input_text,
        "summary": summary,
        "status": run.status,
        "type": "run",
        "error": run.error,
        "superseded": run.superseded,
        "failure": _run_failure_data(status=run.status, error=run.error, steps=steps),
        "created_at": run.created_at,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "updated_at": run.finished_at or run.started_at,
    }


def _run_detail_data(run_detail: RunDetail) -> dict[str, object]:
    output_steps: list[dict[str, object]] = []
    step_records = [item.record for item in run_detail.output.steps]
    input_items: list[dict[str, object]] = []
    for item in run_detail.inputs:
        payload = asdict(item)
        if item.message is not None:
            payload["message"] = item.message.to_data()
        input_items.append(payload)
    for item in run_detail.output.steps:
        payload: dict[str, object] = {
            "record": _step_record_data(item.record),
            "message": item.message.to_data() if item.message is not None else None,
        }
        output_steps.append(payload)
    virtual_failure_step = _virtual_runtime_failure_step(run_detail, steps=step_records)
    if virtual_failure_step is not None:
        step_records.append(virtual_failure_step)
        output_steps.append(
            {
                "record": _step_record_data(virtual_failure_step),
                "message": None,
                "virtual": True,
            }
        )
    return {
        "info": asdict(run_detail.info),
        "input": run_detail.input.to_data() if run_detail.input is not None else None,
        "inputs": input_items,
        "output": {
            "status": run_detail.output.status,
            "error": run_detail.output.error,
            "failure": _run_failure_data(
                status=run_detail.output.status,
                error=run_detail.output.error,
                steps=step_records,
            ),
            "steps": output_steps,
        },
    }


def _virtual_runtime_failure_step(
    run_detail: RunDetail,
    *,
    steps: Sequence[StepRecord],
) -> StepRecord | None:
    error = run_detail.output.error
    if run_detail.output.status != "failed" or error is None:
        return None
    if any(item.kind == "system" and item.status == "failed" and item.error == error for item in steps):
        return None
    step_index = max((item.step_index for item in steps), default=0) + 1
    timestamp = run_detail.info.finished_at or run_detail.info.updated_at
    return StepRecord(
        run_id=run_detail.info.id,
        step_index=step_index,
        kind="system",
        status="failed",
        input=(),
        output=(TextPart(text=error),),
        started_at=timestamp,
        finished_at=timestamp,
        payload=RuntimeStepPayload(),
        error=error,
    )


def _step_record_data(step: StepRecord) -> dict[str, object]:
    return {
        "run_id": step.run_id,
        "step_index": step.step_index,
        "kind": step.kind,
        "status": step.status,
        "input": step_input_items_to_data(step.input),
        "output": parts_to_data(step.output),
        "payload": step_payload_to_data(step.payload),
        "error": step.error,
        "started_at": step.started_at,
        "finished_at": step.finished_at,
    }


def _run_failure_data(
    *,
    status: str,
    error: str | None,
    steps: Sequence,
) -> dict[str, object] | None:
    if status != "failed" and error is None:
        return None
    failed_step = next(
        (item for item in reversed(steps) if getattr(item, "status", None) == "failed"),
        None,
    )
    step_error = getattr(failed_step, "error", None) if failed_step is not None else None
    reason = error or step_error or "Run failed."
    payload: dict[str, object] = {"reason": reason}
    if failed_step is not None:
        payload["step_index"] = failed_step.step_index
        payload["step_kind"] = failed_step.kind
        if step_error is not None:
            payload["step_error"] = step_error
    return payload


def _profile_metrics(context: UptimeContext) -> dict[str, object]:
    threads = _thread_items(context)
    runs = context.store.list_runs(limit=None)
    steps_by_run = context.store.list_steps_for_runs(run_ids=tuple(item.run_id for item in runs))
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
                if isinstance(step.payload, ModelCallStepPayload):
                    input_tokens += step.payload.input_tokens
                    output_tokens += step.payload.output_tokens
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
    context: UptimeContext,
    *,
    runtime_state: dict[str, object],
) -> dict[str, object]:
    return {
        "sandbox": _runtime_sandbox_spec(runtime_state),
        "home": str(context.home),
        "endpoint": _runtime_endpoint(context, runtime_state=runtime_state),
    }


def _run_detail(context: UptimeContext, run: RunRecord):
    raw_steps = context.store.list_steps(run_id=run.run_id)
    inputs = context.store.list_commands(run_id=run.run_id)
    return run_detail_from_record(run, steps=raw_steps, inputs=inputs)


def _run_messages(
    context: UptimeContext,
    *,
    run: RunRecord,
    raw_steps: Sequence,
) -> list[MessageData]:
    return run_message_data(
        run,
        inputs=context.store.list_commands(run_id=run.run_id),
        steps=raw_steps,
    )


def _thread_items(context: UptimeContext) -> list[ThreadInfo]:
    recent_runs = context.store.list_runs(limit=None)
    thread_ids = sorted({run.thread_id for run in recent_runs})
    runs = [
        run
        for thread_id in thread_ids
        for run in context.store.list_thread_runs_chronological(thread_id=thread_id)
    ]
    steps_by_run = context.store.list_steps_for_runs(run_ids=tuple(item.run_id for item in runs))
    commands_by_run = {run.run_id: context.store.list_commands(run_id=run.run_id) for run in runs}
    grouped_runs: dict[str, list[RunRecord]] = {}
    for run in runs:
        grouped_runs.setdefault(run.thread_id, []).append(run)
    thread_records = {item.thread_id: item for item in context.store.list_threads()}
    items: list[ThreadInfo] = []
    for thread_id, runs in grouped_runs.items():
        items.append(
            thread_info_from_runs(
                thread_id,
                runs,
                commands_by_run=commands_by_run,
                steps_by_run=steps_by_run,
                thread=thread_records.get(thread_id),
            )
        )
    for thread_id, thread in thread_records.items():
        if thread_id not in grouped_runs:
            items.append(thread_info_from_record(thread))
    return sorted(items, key=lambda item: item.updated_at, reverse=True)


def _run_or_404(context: UptimeContext, run_id: str):
    run = context.store.get_run(run_id=run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"run not found: {run_id}")
    return run


def _thread_or_404(context: UptimeContext, thread_id: str) -> None:
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
        raise HTTPException(status_code=422, detail="run input message role must be user")
    return message


def _input_mode(value: str) -> RunCommandMode:
    if value not in {"immediate", "next_step", "next_call"}:
        raise HTTPException(status_code=422, detail=f"unsupported run input mode: {value}")
    return cast(RunCommandMode, value)


def _input_event_payload(run: RunRecord, input: RunCommandRecord) -> dict[str, object]:
    payload: dict[str, object] = {
        "run_id": run.run_id,
        "thread_id": run.thread_id,
        "ref": {"kind": "command", "index": input.index},
        "kind": input.kind,
        "created_at": input.created_at,
    }
    if input.mode is not None:
        payload["mode"] = input.mode
    if input.request_id is not None:
        payload["request_id"] = input.request_id
    if input.message is not None:
        payload["message"] = input.message.to_data()
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


def _event_stream_response(request: Request, stream: AsyncIterator[str]) -> ShutdownAwareStreamingResponse:
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


def _require_branchable_thread(context: UptimeContext, run: RunRecord) -> None:
    thread = context.store.get_thread(thread_id=run.thread_id)
    origin = thread.origin if thread is not None else run.origin
    if run.thread_id.startswith(("task_", "chore_")) or origin != "chat":
        raise HTTPException(status_code=409, detail=f"thread cannot be rewound or forked: {run.thread_id}")


def _cancel_running_replaced_runs(context: UptimeContext, *, anchor: RunRecord, reason: str) -> None:
    runs = [
        item
        for item in sorted(
            context.store.list_runs(thread_id=anchor.thread_id, limit=None),
            key=lambda run: run.created_at,
        )
        if item.created_at >= anchor.created_at and item.status == "running"
    ]
    for run in runs:
        canceled = context.store.cancel_run(run_id=run.run_id, error=reason)
        payload = _run_event_payload(canceled)
        context.events.publish(domain="run", domain_id=canceled.run_id, type="run_end", payload=payload)
        context.events.publish(domain="thread", domain_id=canceled.thread_id, type="run_end", payload=payload)


def _copy_fork_history(
    context: UptimeContext,
    *,
    source_run: RunRecord,
    target_thread_id: str,
    include_anchor: bool = False,
) -> tuple[RunRecord, ...]:
    source_runs = context.store.list_thread_runs_before(run_id=source_run.run_id)
    if include_anchor:
        source_runs = (*source_runs, source_run)
    target_run_ids = tuple(allocate_run_id(context) for _ in source_runs)
    return context.store.copy_runs_to_thread(
        source_run_ids=tuple(run.run_id for run in source_runs),
        target_thread_id=target_thread_id,
        target_run_ids=target_run_ids,
    )


def _fork_thread_id(context: UptimeContext, *, source_thread_id: str) -> str:
    prefix = source_thread_id.split("_", 1)[0].strip() or "thread"
    value = allocate_id(
        agents.agent_id_state_path(context.root, context.name),
        family=LOCAL_ID_FAMILY,
    ).value
    return f"{prefix}_{value}"


def _runtime_endpoint(
    context: UptimeContext,
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


def _channel_items(context: UptimeContext) -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    for name in sorted(context.channel_bindings):
        binding = context.channel_bindings[name]
        plugin = context.channel_plugins.get(name)
        channel_context = context.channel_context(name)
        state_path = channel_context.room / "state.json"
        health = (
            plugin.health(channel_context).to_data()
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
        raise HTTPException(status_code=404, detail=f"unsupported cap collection: {collection}")
    return kind


def _authored_entries(context: UptimeContext, *, visibility: str) -> tuple[PreparedEntry, ...]:
    return caps.list_entries(context.root, context.name, visibility=cast(Literal["shared", "private"], visibility))


def _select_components(enabled_components: Sequence[str], namespace: str, allowed: Container[str]) -> list[str]:
    prefix = f"{namespace}."
    return [
        component.removeprefix(prefix)
        for component in enabled_components
        if component.startswith(prefix) and component.removeprefix(prefix) in allowed
    ]


def _run_outcome_data(result: RunOutcome) -> dict[str, object]:
    return {
        "run_id": result.run_id,
        "group": result.group,
        "origin": result.origin,
        "input_text": result.input_text,
        "thunk_name": result.thunk_name,
        "thread_id": result.thread_id,
        "status": result.status,
        "output_text": result.output_text,
        "error": result.error,
        "live_fingerprint": result.live_fingerprint,
    }


def _runner_in_flight(runner_snapshot: dict[str, object]) -> int:
    concurrency_groups = cast(list[dict[str, object]], runner_snapshot["concurrency_groups"])
    return sum(cast(int, item["in_flight"]) for item in concurrency_groups)
