"""FastAPI application assembly for one agent runtime."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
import threading
from typing import Any, cast

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from toolang.api.context import ApiContext
from toolang.api import agent, cap_commands, caps, chat, job_commands, jobs, runs

DEFAULT_CORS_ORIGINS = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "https://too.run",
]

OPENAPI_TAGS = [
    {"name": "agent", "description": "Agent profile and health endpoints."},
    {"name": "chat", "description": "Chat submission and streaming endpoints."},
    {"name": "caps", "description": "Capability inspection and mutation endpoints."},
    {"name": "jobs", "description": "Task and chore inspection endpoints."},
    {"name": "activity", "description": "Thread, run, and event history endpoints."},
]


def create_app(
    context: ApiContext,
    *,
    lifespan: Callable[[FastAPI], AbstractAsyncContextManager[None]] | None = None,
    shutdown_signal: threading.Event | None = None,
) -> FastAPI:
    """Create one FastAPI app for an existing runtime context."""

    enabled = context.enabled_components
    raw_origins = context.config.get("web.cors_allowed_origins")
    origins = (
        [item for item in raw_origins if isinstance(item, str) and item.strip()]
        if isinstance(raw_origins, list)
        else DEFAULT_CORS_ORIGINS
    )
    app = FastAPI(
        title="Toolang Agent API",
        lifespan=lifespan,
        openapi_tags=OPENAPI_TAGS,
    )
    context.shutdown_signal = shutdown_signal
    app.state.context = context
    if origins:
        app.add_middleware(
            cast(Any, CORSMiddleware),
            allow_origins=origins,
            allow_methods=["*"],
            allow_headers=["*"],
            allow_private_network=True,
        )

    @app.get("/healthz", tags=["agent"], summary="Health Check")
    def healthz() -> dict[str, object]:
        return {"ok": True, "enabled_components": list(enabled)}

    if "router.chat" in enabled:
        app.include_router(chat.create_router())
    if "router.manage" in enabled:
        app.include_router(cap_commands.create_router())
        app.include_router(job_commands.create_router())
    if "router.inspect" in enabled:
        app.include_router(agent.create_router())
        app.include_router(caps.create_router())
        app.include_router(jobs.create_router())
        app.include_router(runs.create_router())
    return app
