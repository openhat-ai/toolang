"""FastAPI application assembly for one agent."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager
from typing import Annotated, Any, cast

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from toolang.api.common import RUN_ID_HEADER, SSESendDeadline
from toolang.execution.subscriptions import Subscriptions
from toolang.catalog import CapsManager, ChoresManager
from toolang.catalog.errors import CatalogConflictError, CatalogNotFoundError
from toolang.up import AgentCore
from toolang.driver.chore.scheduler import ChoreDriver

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
    {"name": "chores", "description": "Chore definition and execution endpoints."},
    {"name": "runs", "description": "Run execution and inspection endpoints."},
    {"name": "threads", "description": "Thread lifecycle and inspection endpoints."},
]


def get_agent_core(request: Request) -> AgentCore:
    """Return the process-local core owned by the application."""

    return cast(AgentCore, request.app.state.agent_core)


def get_caps_manager(request: Request) -> CapsManager:
    """Return the capability catalogs owned by the application."""

    return cast(CapsManager, request.app.state.caps_manager)


def get_chores_manager(request: Request) -> ChoresManager:
    """Return the chore catalogs owned by the application."""

    return cast(ChoresManager, request.app.state.chores_manager)


def get_chore_driver(request: Request) -> ChoreDriver | None:
    """Return the runtime scheduler owned by the server lifespan."""

    return cast(ChoreDriver | None, getattr(request.app.state, "chore_driver", None))


AgentCoreDep = Annotated[AgentCore, Depends(get_agent_core)]
CapsManagerDep = Annotated[CapsManager, Depends(get_caps_manager)]
ChoresManagerDep = Annotated[ChoresManager, Depends(get_chores_manager)]
ChoreDriverDep = Annotated[ChoreDriver | None, Depends(get_chore_driver)]


def require_chat_driver(core: AgentCoreDep) -> None:
    if "chat" not in core.drivers:
        raise HTTPException(
            status_code=409, detail={"code": "driver_disabled", "driver": "chat"}
        )


def create_app(
    core: AgentCore,
    caps: CapsManager,
    chores: ChoresManager,
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
    app.state.activity = core.activity
    app.state.caps_manager = caps
    app.state.chores_manager = chores
    app.state.subscriptions = Subscriptions(core.executor.stream, core.store.db_path)
    app.add_middleware(cast(Any, SSESendDeadline))

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
            expose_headers=[RUN_ID_HEADER],
            allow_private_network=True,
        )

    @app.get("/healthz", tags=["agent"], summary="Health Check")
    def healthz() -> dict[str, bool]:
        return {"ok": True}

    from toolang.api.router import router

    app.include_router(router)
    return app
