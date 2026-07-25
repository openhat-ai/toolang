"""FastAPI application assembly for one agent runtime."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
import threading
from typing import Annotated, Any, cast

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from toolang.catalog.cap import AuthoredCaps
from toolang.catalog.config import WiredCaps
from toolang.catalog.errors import CatalogConflictError, CatalogNotFoundError
from toolang.catalog.job import AuthoredJobs
from toolang.common.layout import AgentLayout
from toolang.execution.events import RunStarting, TraceEvent
from toolang.execution.executor import Executor
from toolang.execution.records import CommandRecord, RunRecord
from toolang.execution.reply import ReplySink
from toolang.execution.executor.request import RunRequest
from toolang.state.watcher import StateWatcher
from toolang.setup import SetupWatcher

DEFAULT_CORS_ORIGINS = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "https://too.run",
]
OPENAPI_TAGS = [
    {
        "name": "agent",
        "description": "Agent profile, health, and operational update endpoints.",
    },
    {"name": "chat", "description": "Chat submission and streaming endpoints."},
    {"name": "caps", "description": "Capability inspection and mutation endpoints."},
    {"name": "jobs", "description": "Task and chore management endpoints."},
    {"name": "runs", "description": "Run execution and inspection endpoints."},
    {"name": "threads", "description": "Thread lifecycle and inspection endpoints."},
]


@dataclass(slots=True)
class ApiContext:
    """Process-local dependencies owned by one FastAPI application."""

    layout: AgentLayout
    executor: Executor
    setup_watcher: SetupWatcher
    state_watcher: StateWatcher
    authored_jobs: AuthoredJobs
    private_authored_caps: AuthoredCaps
    shared_authored_caps: AuthoredCaps
    private_wired_caps: WiredCaps
    shared_wired_caps: WiredCaps
    host: str
    port: int
    cors_allowed_origins: tuple[str, ...]
    shutdown_signal: threading.Event | None = None
    run_tasks: set[asyncio.Task[RunRecord]] = field(default_factory=set, init=False)

    def spawn_run(
        self,
        request: RunRequest,
        *,
        reply: ReplySink | None = None,
    ) -> asyncio.Task[RunRecord]:
        """Create and retain one run task owned by the API runtime."""

        task = asyncio.create_task(
            self.executor.run(request, self.state_watcher.current(), reply=reply)
        )
        self.run_tasks.add(task)
        task.add_done_callback(self.run_tasks.discard)
        return task

    async def submit_run(
        self,
        request: RunRequest,
        *,
        reply: ReplySink | None = None,
    ) -> tuple[RunRecord, CommandRecord]:
        """Spawn one run and wait until its start command is durably accepted."""

        if request.run_id is None:
            raise ValueError("API run submission requires an allocated run id")
        acceptance = _RunAcceptance(request.run_id, reply=reply)
        task = self.spawn_run(request, reply=acceptance)
        await acceptance.wait(task)
        run = self.executor.store.get_run(run_id=request.run_id)
        command = self.executor.store.get_command(run_id=request.run_id, index=0)
        if run is None or command is None:
            raise RuntimeError(f"accepted run projection missing: {request.run_id}")
        return run, command


def get_api_context(request: Request) -> ApiContext:
    """Return the process-local context owned by the current application."""

    return cast(ApiContext, request.app.state.context)


ApiContextDep = Annotated[ApiContext, Depends(get_api_context)]


class _RunAcceptance:
    """Observe durable start acceptance while preserving a caller reply sink."""

    def __init__(self, run_id: str, *, reply: ReplySink | None) -> None:
        self.run_id = run_id
        self.reply = reply
        self.wants_stream = reply.wants_stream if reply is not None else False
        self._accepted: asyncio.Future[None] = (
            asyncio.get_running_loop().create_future()
        )

    def on_event(self, event: TraceEvent) -> None:
        if (
            isinstance(event, RunStarting)
            and event.run == self.run_id
            and not self._accepted.done()
        ):
            self._accepted.set_result(None)
        if self.reply is not None:
            self.reply.on_event(event)

    async def wait(self, task: asyncio.Task[RunRecord]) -> None:
        done, _pending = await asyncio.wait(
            (self._accepted, task), return_when=asyncio.FIRST_COMPLETED
        )
        if self._accepted in done:
            return
        await task
        raise RuntimeError(f"run completed without start acceptance: {self.run_id}")


def create_app(
    context: ApiContext,
    *,
    lifespan: Callable[[FastAPI], AbstractAsyncContextManager[None]] | None = None,
    shutdown_signal: threading.Event | None = None,
) -> FastAPI:
    """Create one FastAPI app for an existing runtime context."""

    origins = list(context.cors_allowed_origins or DEFAULT_CORS_ORIGINS)
    app = FastAPI(
        title="Toolang Agent API",
        lifespan=lifespan,
        openapi_tags=OPENAPI_TAGS,
    )
    context.shutdown_signal = shutdown_signal
    app.state.context = context

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
