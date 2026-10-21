"""Versioned API router assembly."""

from fastapi import APIRouter

from toolang.api.version import AGENT_API_VERSION
from toolang.api.routers import agent, caps, chores, runs, threads, activity

API_PREFIX = f"/api/v{AGENT_API_VERSION}"

router = APIRouter(prefix=API_PREFIX)
router.include_router(agent.router)
router.include_router(caps.router)
router.include_router(chores.router)
router.include_router(runs.router)
router.include_router(runs.chat_router)
router.include_router(threads.router)
router.include_router(activity.router)
