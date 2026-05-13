"""Formal inspect API routes."""

from __future__ import annotations

from fastapi import APIRouter

from . import agent, caps, execution, jobs


def create_router() -> APIRouter:
    """Build the formal inspect route group."""

    router = APIRouter()
    router.include_router(agent.create_router())
    router.include_router(caps.create_router())
    router.include_router(execution.create_router())
    router.include_router(jobs.create_router())
    return router
