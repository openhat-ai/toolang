"""Versioned API router assembly."""

from fastapi import APIRouter

from toolang.api.routers import agent, caps, jobs, runs, threads

API_PREFIX = "/api/v1"

router = APIRouter(prefix=API_PREFIX)
router.include_router(agent.router)
router.include_router(caps.router)
router.include_router(jobs.router)
router.include_router(runs.router)
router.include_router(threads.router)
