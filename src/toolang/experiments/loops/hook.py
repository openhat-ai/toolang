"""Hook loop routes."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from ..base.types.channel import HookRequest


class HookRunRequest(BaseModel):
    """Minimal hook-triggered run request."""

    thread_id: str | None = None
    thunk: str = Field(min_length=1)


def create_router() -> APIRouter:
    """Build the hook route group."""

    router = APIRouter(prefix="/hook", tags=["hook"])

    @router.post("/runs", status_code=202, summary="Submit Hook Run")
    async def submit_hook_run(request: Request, payload: HookRunRequest) -> dict[str, object]:
        context = request.app.state.runtime
        pending = context.enqueue_run(
            "hook",
            thread_id=payload.thread_id,
            thunk=payload.thunk,
        )
        return {"status": "queued", "pending": pending}

    @router.api_route(
        "/{binding_name}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        summary="Submit Hook Delivery",
    )
    async def submit_hook_delivery(request: Request, binding_name: str) -> dict[str, object]:
        context = request.app.state.runtime
        binding = context.channel_bindings.get(binding_name)
        plugin = context.channel_plugins.get(binding_name)
        if binding is None or plugin is None:
            raise HTTPException(status_code=404, detail=f"unknown hook binding: {binding_name}")
        body = await request.body()
        channel_context = context.channel_context(binding_name)
        delivery = plugin.decode_hook(
            HookRequest(
                method=request.method,
                path=request.url.path,
                headers={key.lower(): value for key, value in request.headers.items()},
                query={key: value for key, value in request.query_params.items()},
                body=body,
                content_type=request.headers.get("content-type"),
            ),
            channel_context,
        )
        if delivery is None:
            return {"status": "ignored", "binding": binding_name, "plugin": binding.plugin}
        pending = context.enqueue_delivery("hook", binding_name, delivery)
        return {"status": "queued", "binding": binding_name, "pending": pending}

    return router
