"""Formal control API routes."""

from __future__ import annotations

from fastapi import APIRouter

from . import caps, jobs


def create_router() -> APIRouter:
    """Build the formal control route group."""

    router = APIRouter()
    router.include_router(caps.create_router())
    router.include_router(jobs.create_router())
    return router
