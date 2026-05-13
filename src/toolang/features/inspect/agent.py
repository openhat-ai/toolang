"""Formal agent inspection routes."""

from __future__ import annotations

from fastapi import APIRouter, Request

from . import _shared


def create_router() -> APIRouter:
    """Build the formal agent inspection route group."""

    router = APIRouter(prefix="/api/v1")

    @router.get("/profile", tags=["agent"], summary="Get Profile")
    async def profile(request: Request) -> dict[str, object]:
        context = request.app.state.runtime
        runtime_state = _shared.agents.load_runtime_state(context.root, context.name) or {}
        return {
            "agent": context.name,
            "display_name": context.name,
            "title": None,
            "summary": None,
            "description": None,
            "avatar": None,
            "environment": _shared._profile_environment(context, runtime_state=runtime_state),
            "metrics": _shared._profile_metrics(context),
        }

    return router
