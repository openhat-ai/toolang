"""Formal execution inspection routes."""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, HTTPException, Query, Request

from . import _shared


def create_router() -> APIRouter:
    """Build the formal execution inspection route group."""

    router = APIRouter(prefix="/api/v1")

    @router.get("/runs", tags=["activity"], summary="List Runs")
    async def runs(request: Request, limit: int = Query(default=50), thread_id: str | None = None) -> dict[str, object]:
        context = request.app.state.runtime
        runs = context.store.list_runs(limit=limit, thread_id=thread_id)
        steps_by_run = context.store.list_steps_for_runs(run_ids=tuple(item.run_id for item in runs))
        inputs_by_run = {run.run_id: context.store.list_inputs(run_id=run.run_id) for run in runs}
        items = [
            _shared._run_item(
                item,
                inputs=inputs_by_run.get(item.run_id, ()),
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
            **_shared._run_detail_data(_shared._run_detail(context, run)),
            "event_cursor": context.store.latest_event_cursor(domain="run", domain_id=run_id),
        }

    @router.get("/runs/{run_id}/events", tags=["activity"], summary="List Run Events")
    async def run_events(request: Request, run_id: str, after: int | None = None, limit: int = Query(default=100)) -> dict[str, object]:
        context = request.app.state.runtime
        _shared._run_or_404(context, run_id)
        events = context.store.list_events(domain="run", domain_id=run_id, after=after, limit=limit)
        return {
            "cursor": context.store.latest_event_cursor(domain="run", domain_id=run_id),
            "items": [_shared.event_data(item) for item in events],
        }

    @router.get("/runs/{run_id}/stream", tags=["activity"], summary="Stream Run Events")
    async def run_stream(request: Request, run_id: str, after: int | None = None) -> _shared.ShutdownAwareStreamingResponse:
        context = request.app.state.runtime
        _shared._run_or_404(context, run_id)
        return _shared._event_stream_response(
            request,
            context.events.stream(domain="run", domain_id=run_id, after=after),
        )

    @router.post("/runs/{run_id}/stop", tags=["activity"], summary="Stop Run")
    async def stop_run(request: Request, run_id: str, payload: _shared.RunCancelRequest | None = None) -> dict[str, object]:
        context = request.app.state.runtime
        run = _shared._run_or_404(context, run_id)
        input_record = context.store.append_input(
            run_id=run.run_id,
            action="stop",
            mode=_shared._input_mode(payload.mode if payload else "immediate"),
            request_id=payload.request_id if payload else None,
        )
        input_payload = _shared._input_event_payload(run, input_record)
        context.events.publish(domain="run", domain_id=run.run_id, type="run_input", payload=input_payload)
        context.events.publish(domain="thread", domain_id=run.thread_id, type="run_input", payload=input_payload)
        if run.status == "running":
            run = context.store.cancel_run(run_id=run_id, error=payload.reason if payload else None)
            event_payload = _shared._run_event_payload(run)
            context.events.publish(domain="run", domain_id=run.run_id, type="run_end", payload=event_payload)
            context.events.publish(domain="thread", domain_id=run.thread_id, type="run_end", payload=event_payload)
            context.events.publish(domain="agent", domain_id=context.name, type="thread_update", payload=event_payload)
        return {
            "run": _shared._run_item(
                run,
                inputs=context.store.list_inputs(run_id=run.run_id),
                steps=context.store.list_steps(run_id=run.run_id),
            ),
            "input": input_payload,
        }

    @router.post("/runs/{run_id}/restart", tags=["activity"], summary="Restart Run")
    async def restart_run(request: Request, run_id: str, payload: _shared.RunRestartRequest) -> dict[str, object]:
        context = request.app.state.runtime
        run = _shared._run_or_404(context, run_id)
        if run.origin != "chat":
            raise HTTPException(status_code=409, detail=f"run restarts are only supported for chat runs: {run_id}")
        if run.superseded is not None:
            raise HTTPException(status_code=409, detail=f"run is already superseded: {run_id}")
        message = _shared._input_message(payload.message)
        new_run_id = _shared.allocate_run_id(context)
        if run.status == "running":
            run = context.store.cancel_run(run_id=run.run_id, error="Run was restarted.")
            run_end_payload = _shared._run_event_payload(run)
            context.events.publish(domain="run", domain_id=run.run_id, type="run_end", payload=run_end_payload)
            context.events.publish(domain="thread", domain_id=run.thread_id, type="run_end", payload=run_end_payload)
        run = context.store.supersede_run(
            run_id=run.run_id,
            superseded={"type": "replaced", "by": new_run_id},
        )
        context.runner.enqueue(
            _shared.RunRequest(
                group="chat",
                origin="chat",
                run_id=new_run_id,
                thread_id=run.thread_id,
                message=message,
                metadata={"request_id": payload.request_id} if payload.request_id is not None else {},
            )
        )
        event_payload = {
            "run_id": run.run_id,
            "replacement_run_id": new_run_id,
            "thread_id": run.thread_id,
            "superseded": run.superseded,
            "message": message.to_data(),
        }
        context.events.publish(domain="run", domain_id=run.run_id, type="run_restart", payload=event_payload)
        context.events.publish(domain="thread", domain_id=run.thread_id, type="run_restart", payload=event_payload)
        context.events.publish(domain="agent", domain_id=context.name, type="thread_update", payload=event_payload)
        return {
            "run_id": new_run_id,
            "previous_run": _shared._run_item(
                run,
                inputs=context.store.list_inputs(run_id=run.run_id),
                steps=context.store.list_steps(run_id=run.run_id),
            ),
            "message": message.to_data(),
        }

    @router.post("/runs/{run_id}/steer", tags=["activity"], summary="Steer Run")
    async def steer_run(request: Request, run_id: str, payload: _shared.RunSteerRequest) -> dict[str, object]:
        context = request.app.state.runtime
        run = _shared._run_or_404(context, run_id)
        if run.status != "running":
            raise HTTPException(status_code=409, detail=f"run is not running: {run_id}")
        message = _shared._input_message(payload.message)
        input_record = context.store.append_input(
            run_id=run.run_id,
            action="steer",
            mode=_shared._input_mode(payload.mode),
            request_id=payload.request_id,
            message=message,
        )
        event_payload = _shared._input_event_payload(run, input_record)
        context.events.publish(domain="run", domain_id=run.run_id, type="run_input", payload=event_payload)
        context.events.publish(domain="thread", domain_id=run.thread_id, type="run_input", payload=event_payload)
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
    async def threads(request: Request, limit: int = Query(default=50), origin: str | None = None) -> dict[str, object]:
        context = request.app.state.runtime
        items = _shared._thread_items(context)
        if origin is not None:
            items = [item for item in items if item.origin == origin]
        return {"items": [asdict(item) for item in items[:limit]]}

    @router.get("/threads/{thread_id}", tags=["activity"], summary="Get Thread")
    async def thread_detail(request: Request, thread_id: str, limit: int = Query(default=50)) -> dict[str, object]:
        context = request.app.state.runtime
        items = _shared._thread_items(context)
        info = next((item for item in items if item.id == thread_id), None)
        if info is None:
            raise HTTPException(status_code=404, detail=f"thread not found: {thread_id}")
        runs = [
            _shared._run_detail(context, item)
            for item in sorted(
                context.store.list_runs(limit=limit, thread_id=thread_id),
                key=lambda run: run.created_at,
            )
        ]
        return {
            "info": asdict(info),
            "runs": [_shared._run_detail_data(item) for item in runs],
            "event_cursor": context.store.latest_event_cursor(domain="thread", domain_id=thread_id),
        }

    @router.get("/threads/{thread_id}/events", tags=["activity"], summary="List Thread Events")
    async def thread_events(request: Request, thread_id: str, after: int | None = None, limit: int = Query(default=100)) -> dict[str, object]:
        context = request.app.state.runtime
        _shared._thread_or_404(context, thread_id)
        events = context.store.list_events(domain="thread", domain_id=thread_id, after=after, limit=limit)
        return {
            "cursor": context.store.latest_event_cursor(domain="thread", domain_id=thread_id),
            "items": [_shared.event_data(item) for item in events],
        }

    @router.get("/threads/{thread_id}/stream", tags=["activity"], summary="Stream Thread Events")
    async def thread_stream(request: Request, thread_id: str, after: int | None = None) -> _shared.ShutdownAwareStreamingResponse:
        context = request.app.state.runtime
        _shared._thread_or_404(context, thread_id)
        return _shared._event_stream_response(
            request,
            context.events.stream(domain="thread", domain_id=thread_id, after=after),
        )

    @router.get("/events", tags=["activity"], summary="List Events")
    async def events(request: Request, limit: int = Query(default=100)) -> dict[str, object]:
        context = request.app.state.runtime
        return {"items": [asdict(item) for item in context.store.list_updates(limit=limit)]}

    @router.get("/events/stream", tags=["activity"], summary="Stream Events")
    async def events_stream(request: Request) -> _shared.ShutdownAwareStreamingResponse:
        return _shared.ShutdownAwareStreamingResponse(
            _shared._guarded_stream(_shared._events_stream()),
            shutdown_signal=getattr(request.app.state, "shutdown_signal", None),
            media_type="text/event-stream",
        )

    @router.get("/agent/events", tags=["activity"], summary="List Agent Events")
    async def agent_events(request: Request, after: int | None = None, limit: int = Query(default=100)) -> dict[str, object]:
        context = request.app.state.runtime
        events = context.store.list_events(domain="agent", domain_id=context.name, after=after, limit=limit)
        return {
            "cursor": context.store.latest_event_cursor(domain="agent", domain_id=context.name),
            "items": [_shared.event_data(item) for item in events],
        }

    @router.get("/agent/stream", tags=["activity"], summary="Stream Agent Events")
    async def agent_stream(request: Request, after: int | None = None) -> _shared.ShutdownAwareStreamingResponse:
        context = request.app.state.runtime
        return _shared._event_stream_response(
            request,
            context.events.stream(domain="agent", domain_id=context.name, after=after),
        )

    return router
