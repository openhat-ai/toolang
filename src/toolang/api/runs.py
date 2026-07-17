"""Formal execution inspection routes."""

from __future__ import annotations

from dataclasses import asdict
import json

from fastapi import APIRouter, HTTPException, Query, Request

from toolang.agent import local as agents
from toolang.common.ids import LOCAL_ID_FAMILY, allocate_id
from toolang.execution.records import RunStatus
from toolang.execution.request import RunRequest
from toolang.execution.stream import event_data, stream_events
from . import _views


def create_router() -> APIRouter:
    """Build the formal execution inspection route group."""

    router = APIRouter(prefix="/api/v1")

    @router.get("/runs", tags=["activity"], summary="List Runs")
    async def runs(
        request: Request,
        limit: int = Query(default=50),
        thread_id: str | None = None,
        status: RunStatus | None = None,
    ) -> dict[str, object]:
        context = request.app.state
        runs = context.store.list_runs(limit=limit, thread_id=thread_id, status=status)
        steps_by_run = context.store.list_steps_for_runs(
            run_ids=tuple(item.run_id for item in runs)
        )
        commands_by_run = {
            run.run_id: context.store.list_commands(run_id=run.run_id) for run in runs
        }
        items = [
            _views._run_item(
                item,
                inputs=commands_by_run.get(item.run_id, ()),
                steps=steps_by_run.get(item.run_id, ()),
            )
            for item in runs
        ]
        return {"items": items}

    @router.get("/runs/{run_id}", tags=["activity"], summary="Get Run")
    async def run_detail(request: Request, run_id: str) -> dict[str, object]:
        context = request.app.state
        run = context.store.get_run(run_id=run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"run not found: {run_id}")
        detail = _views._run_detail_data(_views._run_detail(context, run))
        return {
            **_views._with_run_prompt_bodies(context.store, detail),
            "event_cursor": context.store.latest_event_cursor(
                domain="run", domain_id=run_id
            ),
        }

    @router.get("/runs/{run_id}/events", tags=["activity"], summary="List Run Events")
    async def run_events(
        request: Request,
        run_id: str,
        after: int | None = None,
        limit: int = Query(default=100),
    ) -> dict[str, object]:
        context = request.app.state
        _views._run_or_404(context, run_id)
        events = context.store.list_events(
            domain="run", domain_id=run_id, after=after, limit=limit
        )
        return {
            "cursor": context.store.latest_event_cursor(domain="run", domain_id=run_id),
            "items": [event_data(item) for item in events],
        }

    @router.get("/runs/{run_id}/stream", tags=["activity"], summary="Stream Run Events")
    async def run_stream(
        request: Request, run_id: str, after: int | None = None
    ) -> _views.ShutdownAwareStreamingResponse:
        context = request.app.state
        _views._run_or_404(context, run_id)
        return _views._event_stream_response(
            request,
            stream_events(context.store, domain="run", domain_id=run_id, after=after),
        )

    @router.post("/runs/{run_id}/cancel", tags=["activity"], summary="Cancel Run")
    async def cancel_run(
        request: Request, run_id: str, payload: _views.RunCancelRequest | None = None
    ) -> dict[str, object]:
        context = request.app.state
        run = _views._run_or_404(context, run_id)
        if run.status != "running":
            raise HTTPException(status_code=409, detail=f"run is not running: {run_id}")
        command_record, run = await context.executor.stop(
            run_id=run.run_id,
            apply=_views._input_apply(payload.mode if payload else "immediate"),
            request_id=payload.request_id if payload else None,
            reason=payload.reason if payload else None,
        )
        input_payload = _views._input_event_payload(run, command_record)
        if payload is not None and payload.reason is not None:
            input_payload["reason"] = payload.reason
        return {
            "run": _views._run_item(
                run,
                inputs=context.store.list_commands(run_id=run.run_id),
                steps=context.store.list_steps(run_id=run.run_id),
            ),
            "input": input_payload,
        }

    @router.post("/runs/{run_id}/rewind", tags=["activity"], summary="Rewind Thread")
    async def rewind_thread(
        request: Request, run_id: str, payload: _views.RunRestartRequest
    ) -> dict[str, object]:
        context = request.app.state
        run = _views._run_or_404(context, run_id)
        _require_branchable_thread(context, run)
        message = (
            _views._input_message(payload.message)
            if payload.message is not None
            else None
        )
        new_run_id = (
            _views.allocate_run_id(context.root, context.name)
            if message is not None
            else None
        )
        await _cancel_running_replaced_runs(
            context, anchor=run, reason="Run was rewound."
        )
        superseded = context.store.supersede_thread_from_run(
            run_id=run.run_id,
            superseded={"type": "rewound", "by": new_run_id, "from_run_id": run.run_id},
        )
        if message is not None and new_run_id is not None:
            context.executor.start(
                RunRequest(
                    group="chat",
                    origin="chat",
                    run_id=new_run_id,
                    thread_id=run.thread_id,
                    message=message,
                    metadata={"request_id": payload.request_id}
                    if payload.request_id is not None
                    else {},
                ),
                context.get_agent_state(),
            )
        event_payload = {
            "from_run_id": run.run_id,
            "new_run_id": new_run_id,
            "thread_id": run.thread_id,
            "superseded_run_ids": [item.run_id for item in superseded],
        }
        if message is not None:
            event_payload["message"] = message.to_data()
        context.store.append_event(
            domain="thread",
            domain_id=run.thread_id,
            type="thread_rewind",
            payload=event_payload,
        )
        context.store.append_event(
            domain="agent",
            domain_id=context.name,
            type="thread_update",
            payload=event_payload,
        )
        return {
            "run_id": new_run_id,
            "thread_id": run.thread_id,
            "superseded_run_ids": [item.run_id for item in superseded],
            "message": message.to_data() if message is not None else None,
        }

    @router.post("/runs/{run_id}/fork", tags=["activity"], summary="Fork Thread")
    async def fork_thread(
        request: Request, run_id: str, payload: _views.RunRestartRequest
    ) -> dict[str, object]:
        context = request.app.state
        run = _views._run_or_404(context, run_id)
        _require_branchable_thread(context, run)
        message = (
            _views._input_message(payload.message)
            if payload.message is not None
            else None
        )
        new_run_id = (
            _views.allocate_run_id(context.root, context.name)
            if message is not None
            else None
        )
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
        copied_runs = _views._copy_fork_history(
            context,
            source_run=run,
            target_thread_id=new_thread_id,
            include_anchor=payload.include_anchor,
        )
        if message is not None and new_run_id is not None:
            context.executor.start(
                RunRequest(
                    group="chat",
                    origin="chat",
                    run_id=new_run_id,
                    thread_id=new_thread_id,
                    message=message,
                    metadata={"request_id": payload.request_id}
                    if payload.request_id is not None
                    else {},
                ),
                context.get_agent_state(),
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
        context.store.append_event(
            domain="thread",
            domain_id=run.thread_id,
            type="thread_fork",
            payload=event_payload,
        )
        context.store.append_event(
            domain="thread",
            domain_id=new_thread_id,
            type="thread_forked",
            payload=event_payload,
        )
        context.store.append_event(
            domain="agent",
            domain_id=context.name,
            type="thread_update",
            payload=event_payload,
        )
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
    async def steer_run(
        request: Request, run_id: str, payload: _views.RunSteerRequest
    ) -> dict[str, object]:
        context = request.app.state
        run = _views._run_or_404(context, run_id)
        if run.status != "running":
            raise HTTPException(status_code=409, detail=f"run is not running: {run_id}")
        message = _views._input_message(payload.message)
        command_record = context.executor.steer(
            run_id=run.run_id,
            apply=_views._input_apply(payload.mode),
            request_id=payload.request_id,
            message=message,
        )
        event_payload = _views._input_event_payload(run, command_record)
        return {"input": event_payload}

    @router.get(
        "/instruct/{prompt_hash}", tags=["activity"], summary="Get Instruct Prompt"
    )
    async def instruct_prompt(request: Request, prompt_hash: str) -> dict[str, object]:
        context = request.app.state
        body = context.store.get_prompt(prompt_hash=prompt_hash)
        if body is None:
            raise HTTPException(
                status_code=404, detail=f"instruct not found: {prompt_hash}"
            )
        return {"hash": prompt_hash, "body": body}

    @router.get(
        "/context/{prompt_hash}", tags=["activity"], summary="Get Context Prompt"
    )
    async def context_prompt(request: Request, prompt_hash: str) -> dict[str, object]:
        context = request.app.state
        body = context.store.get_prompt(prompt_hash=prompt_hash)
        if body is None:
            raise HTTPException(
                status_code=404, detail=f"context not found: {prompt_hash}"
            )
        return {"hash": prompt_hash, "body": body}

    @router.get("/threads", tags=["activity"], summary="List Threads")
    async def threads(
        request: Request,
        limit: int = Query(default=50),
        origin: str | None = None,
        channel: str | None = None,
        status: str | None = None,
    ) -> dict[str, object]:
        context = request.app.state
        items = _views._thread_items(context)
        if origin is not None:
            items = [item for item in items if item.origin == origin]
        if channel is not None:
            items = [item for item in items if item.channel == channel]
        if status is not None:
            items = [item for item in items if item.status == status]
        return {"items": [asdict(item) for item in items[:limit]]}

    @router.get("/threads/{thread_id}", tags=["activity"], summary="Get Thread")
    async def thread_detail(
        request: Request, thread_id: str, limit: int = Query(default=50)
    ) -> dict[str, object]:
        context = request.app.state
        items = _views._thread_items(context)
        info = next((item for item in items if item.id == thread_id), None)
        if info is None:
            raise HTTPException(
                status_code=404, detail=f"thread not found: {thread_id}"
            )
        thread_runs = context.store.list_thread_runs_chronological(thread_id=thread_id)
        if limit is not None:
            thread_runs = thread_runs[-limit:]
        runs = [_views._run_detail(context, item) for item in thread_runs]
        return {
            "info": asdict(info),
            "runs": [
                _views._with_run_prompt_bodies(
                    context.store, _views._run_detail_data(item)
                )
                for item in runs
            ],
            "event_cursor": context.store.latest_event_cursor(
                domain="thread", domain_id=thread_id
            ),
        }

    @router.get(
        "/threads/{thread_id}/events", tags=["activity"], summary="List Thread Events"
    )
    async def thread_events(
        request: Request,
        thread_id: str,
        after: int | None = None,
        limit: int = Query(default=100),
    ) -> dict[str, object]:
        context = request.app.state
        _views._thread_or_404(context, thread_id)
        events = context.store.list_events(
            domain="thread", domain_id=thread_id, after=after, limit=limit
        )
        return {
            "cursor": context.store.latest_event_cursor(
                domain="thread", domain_id=thread_id
            ),
            "items": [event_data(item) for item in events],
        }

    @router.get(
        "/threads/{thread_id}/stream", tags=["activity"], summary="Stream Thread Events"
    )
    async def thread_stream(
        request: Request, thread_id: str, after: int | None = None
    ) -> _views.ShutdownAwareStreamingResponse:
        context = request.app.state
        _views._thread_or_404(context, thread_id)
        return _views._event_stream_response(
            request,
            stream_events(
                context.store,
                domain="thread",
                domain_id=thread_id,
                after=after,
            ),
        )

    @router.get("/events", tags=["activity"], summary="List Events")
    async def events(
        request: Request, limit: int = Query(default=100)
    ) -> dict[str, object]:
        context = request.app.state
        return {
            "items": [asdict(item) for item in context.store.list_updates(limit=limit)]
        }

    @router.get("/events/stream", tags=["activity"], summary="Stream Events")
    async def events_stream(request: Request) -> _views.ShutdownAwareStreamingResponse:
        return _views.ShutdownAwareStreamingResponse(
            _views._guarded_stream(_views._events_stream()),
            shutdown_signal=getattr(request.app.state, "shutdown_signal", None),
            media_type="text/event-stream",
        )

    @router.get("/agent/events", tags=["activity"], summary="List Agent Events")
    async def agent_events(
        request: Request, after: int | None = None, limit: int = Query(default=100)
    ) -> dict[str, object]:
        context = request.app.state
        events = context.store.list_events(
            domain="agent", domain_id=context.name, after=after, limit=limit
        )
        return {
            "cursor": context.store.latest_event_cursor(
                domain="agent", domain_id=context.name
            ),
            "items": [event_data(item) for item in events],
        }

    @router.get("/agent/stream", tags=["activity"], summary="Stream Agent Events")
    async def agent_stream(
        request: Request, after: int | None = None
    ) -> _views.ShutdownAwareStreamingResponse:
        context = request.app.state
        return _views._event_stream_response(
            request,
            stream_events(
                context.store,
                domain="agent",
                domain_id=context.name,
                after=after,
            ),
        )

    return router


def _require_branchable_thread(context, run) -> None:
    thread = context.store.get_thread(thread_id=run.thread_id)
    origin = thread.origin if thread is not None else run.origin
    if run.thread_id.startswith(("task_", "chore_")) or origin != "chat":
        raise HTTPException(
            status_code=409,
            detail=f"thread cannot be rewound or forked: {run.thread_id}",
        )


async def _cancel_running_replaced_runs(context, *, anchor, reason: str) -> None:
    runs = [
        item
        for item in sorted(
            context.store.list_runs(thread_id=anchor.thread_id, limit=None),
            key=lambda run: run.created_at,
        )
        if item.created_at >= anchor.created_at and item.status == "running"
    ]
    for run in runs:
        await context.executor.stop(run_id=run.run_id, reason=reason)


def _fork_thread_id(context, *, source_thread_id: str) -> str:
    prefix = source_thread_id.split("_", 1)[0].strip() or "thread"
    value = allocate_id(
        agents.agent_id_state_path(context.root, context.name),
        family=LOCAL_ID_FAMILY,
    ).value
    return f"{prefix}_{value}"
