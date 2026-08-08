"""FastAPI application assembly for one agent."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager
from typing import Annotated, Any, cast

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from toolang.api.common import LiveEventRelay
from toolang.catalog import CapsManager, JobsManager
from toolang.catalog.errors import CatalogConflictError, CatalogNotFoundError
from toolang.up import AgentCore
from toolang.work.scheduler import JobScheduler

DEFAULT_CORS_ORIGINS = (
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "https://too.run",
)
OPENAPI_TAGS = [
    {
        "name": "agent",
        "description": "Agent profile, health, and operational update endpoints.",
    },
    {"name": "caps", "description": "Capability inspection and mutation endpoints."},
    {"name": "jobs", "description": "Task and chore management endpoints."},
    {"name": "runs", "description": "Run execution and inspection endpoints."},
    {"name": "threads", "description": "Thread lifecycle and inspection endpoints."},
]


def get_agent_core(request: Request) -> AgentCore:
    """Return the process-local core owned by the application."""

    return cast(AgentCore, request.app.state.agent_core)


def get_caps_manager(request: Request) -> CapsManager:
    """Return the capability catalogs owned by the application."""

    return cast(CapsManager, request.app.state.caps_manager)


def get_jobs_manager(request: Request) -> JobsManager:
    """Return the job catalogs owned by the application."""

    return cast(JobsManager, request.app.state.jobs_manager)


def get_job_scheduler(request: Request) -> JobScheduler:
    """Return the runtime scheduler owned by the server lifespan."""

    return cast(JobScheduler, request.app.state.job_scheduler)


def get_live_events(request: Request) -> LiveEventRelay:
    """Return the process-local live event relay."""

    return cast(LiveEventRelay, request.app.state.live_events)


AgentCoreDep = Annotated[AgentCore, Depends(get_agent_core)]
CapsManagerDep = Annotated[CapsManager, Depends(get_caps_manager)]
JobsManagerDep = Annotated[JobsManager, Depends(get_jobs_manager)]
JobSchedulerDep = Annotated[JobScheduler, Depends(get_job_scheduler)]
LiveEventRelayDep = Annotated[LiveEventRelay, Depends(get_live_events)]


def create_app(
    core: AgentCore,
    caps: CapsManager,
    jobs: JobsManager,
    *,
    lifespan: Callable[[FastAPI], AbstractAsyncContextManager[None]] | None = None,
    cors_allowed_origins: Sequence[str] = DEFAULT_CORS_ORIGINS,
) -> FastAPI:
    """Create one FastAPI app around explicitly owned agent services."""

    app = FastAPI(
        title="Toolang Agent API",
        lifespan=lifespan,
        openapi_tags=OPENAPI_TAGS,
    )
    app.state.agent_core = core
    app.state.caps_manager = caps
    app.state.jobs_manager = jobs
    live_events = LiveEventRelay()
    app.state.live_events = live_events
    core.threads.listener = live_events

    @app.exception_handler(CatalogNotFoundError)
    async def catalog_not_found(
        _request: Request, exc: CatalogNotFoundError
    ) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(CatalogConflictError)
    async def catalog_conflict(
        _request: Request, exc: CatalogConflictError
    ) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    origins = list(cors_allowed_origins)
    if origins:
        app.add_middleware(
            cast(Any, CORSMiddleware),
            allow_origins=origins,
            allow_methods=["*"],
            allow_headers=["*"],
            allow_private_network=True,
        )

    @app.get("/healthz", tags=["agent"], summary="Health Check")
    def healthz() -> dict[str, bool]:
        return {"ok": True}

    from toolang.api.router import router

    app.include_router(router)
    return app
