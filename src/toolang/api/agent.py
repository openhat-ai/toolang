"""Formal agent inspection routes."""

from __future__ import annotations

from fastapi import APIRouter, Request

from toolang.up import process as agents
from . import _views


def create_router() -> APIRouter:
    """Build the formal agent inspection route group."""

    router = APIRouter(prefix="/api/v1")

    @router.get("/profile", tags=["agent"], summary="Get Profile")
    async def profile(request: Request) -> dict[str, object]:
        context = request.app.state.context
        runtime_state = agents.AgentProcess(context.root, context.name).state() or {}
        return {
            "agent": context.name,
            "display_name": context.name,
            "title": None,
            "summary": None,
            "description": None,
            "avatar": None,
            "environment": _views._profile_environment(
                context, runtime_state=runtime_state
            ),
            "metrics": _views._profile_metrics(context),
        }

    return router
