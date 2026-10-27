"""Root-scoped Hub HTTP transport for shared teaming services."""

from collections.abc import Callable, AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
import re
from typing import Annotated, Any, cast
from urllib.parse import unquote

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.sse import EventSourceResponse, ServerSentEvent
from starlette.middleware import Middleware
from toolang.common.sse import SSESendDeadline
from toolang.execution.errors import SnapshotLimitError
from toolang.execution.errors import StreamOverflowError
from .events import HubScope
from .subscriptions import HubSubscription

from .errors import (
    BackendUnavailable,
    ConversationAccessDenied,
    ConversationRulesConflict,
    StorageIntegrityError,
    LeaseLost,
    MessagingError,
    SendUnconfirmed,
    EventProtocolError,
    EventRecoveryRequired,
    ScopeUnavailable,
)
from .messaging import MessagingClient
from .schemas import HUB_API_VERSION, HubInfo, HubPartitionInfo, target
from .messaging_api import messaging_router
from .lifecycle import HubLifecycle
from .team_api import team_router
from .agent_api import agent_router
from .backend import Backend
from .backend.factory import create_backend
from .config import BackendConfig


def create_app(
    config: BackendConfig,
    *,
    version: str,
    backend: Backend | None = None,
    on_ready: Callable[[], None] | None = None,
    on_stop: Callable[[], None] | None = None,
) -> FastAPI:
    """Own one backend; bind participant identities at request boundaries."""
    store = backend if backend is not None else create_backend(config)
    info = HubInfo(version=version, api_version=HUB_API_VERSION)

    def selection(
        root: Annotated[str | None, Header(alias="X-Toolang-Root")] = None,
        identity: Annotated[str | None, Header(alias="X-Toolang-Backend")] = None,
    ) -> BackendConfig | None:
        if root is None:
            return None
        try:
            if re.search(r"%(?![0-9a-fA-F]{2})", root):
                raise ValueError("Invalid root encoding")
            selected = replace(config, root=unquote(root, errors="strict"))
        except (ValueError, MessagingError) as exc:
            raise HTTPException(
                400, {"code": "messaging_error", "detail": "Invalid Toolang root"}
            ) from exc
        if identity is not None and identity != selected.identity:
            raise HTTPException(
                409, {"code": "hub_changed", "detail": "Hub identity changed"}
            )
        return selected

    Selection = Annotated[BackendConfig | None, Depends(selection)]

    async def partition(
        root: Annotated[str, Header(alias="X-Toolang-Root")],
        identity: Annotated[str | None, Header(alias="X-Toolang-Backend")] = None,
    ) -> tuple[BackendConfig, Backend]:
        selected = selection(root, identity)
        assert selected is not None
        scoped = store.for_root(selected.root)
        return selected, scoped

    Partition = Annotated[tuple[BackendConfig, Backend], Depends(partition)]

    async def human_client(
        scope: Partition,
        human: Annotated[str | None, Header(alias="X-Toolang-Human")] = None,
    ) -> MessagingClient:
        try:
            actor = unquote(human) if human is not None else ""
            target(actor, kind="human")
        except MessagingError as exc:
            raise HTTPException(
                400,
                {
                    "code": "messaging_error",
                    "detail": "A valid human actor is required",
                },
            ) from exc
        selected, scoped = scope
        await scoped.initialize()
        client = MessagingClient(selected, actor=actor, backend=scoped)
        await client.register_human()
        return client

    async def agent_client(
        agent: str,
        scope: Partition,
        lease: Annotated[
            str, Header(alias="X-Toolang-Agent-Lease", min_length=1, max_length=128)
        ],
    ) -> MessagingClient:
        target(agent, kind="agent")
        selected, scoped = scope
        await scoped.initialize()
        return MessagingClient(selected, actor=agent, token=lease, backend=scoped)

    lifecycle = HubLifecycle(store)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            async with lifecycle.lifespan(on_ready):
                yield
        finally:
            if on_stop:
                on_stop()

    app = FastAPI(
        title="Toolang Hub API",
        middleware=[Middleware(cast(Any, SSESendDeadline))],
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.exception_handler(MessagingError)
    async def messaging_error(request: Request, exc: MessagingError) -> JSONResponse:
        if isinstance(exc, LeaseLost):
            status, code = 409, "recovery_required"
        elif isinstance(exc, ConversationRulesConflict):
            status, code = 409, "conversation_rules_conflict"
        elif isinstance(exc, ConversationAccessDenied):
            status, code = 403, "conversation_access_denied"
        elif isinstance(exc, SendUnconfirmed):
            status, code = 502, "send_unconfirmed"
        elif isinstance(exc, StorageIntegrityError):
            status, code = 503, "storage_integrity"
        elif isinstance(exc, BackendUnavailable):
            status, code = 503, "backend_unavailable"
        else:
            status, code = 400, "messaging_error"
        return JSONResponse({"code": code, "detail": str(exc)}, status_code=status)

    @app.exception_handler(EventRecoveryRequired)
    async def recovery_error(
        request: Request, exc: EventRecoveryRequired
    ) -> JSONResponse:
        return JSONResponse(
            {"code": "recovery_required", "detail": str(exc)}, status_code=409
        )

    @app.exception_handler(EventProtocolError)
    async def protocol_error(request: Request, exc: EventProtocolError) -> JSONResponse:
        return JSONResponse(
            {"code": "protocol_error", "detail": str(exc)}, status_code=400
        )

    @app.exception_handler(SnapshotLimitError)
    async def upload_limit(request: Request, exc: SnapshotLimitError) -> JSONResponse:
        return JSONResponse(
            {"code": "snapshot_limit", "detail": str(exc)}, status_code=413
        )

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> JSONResponse:
        data = exc.detail if isinstance(exc.detail, dict) else {"detail": exc.detail}
        return JSONResponse(data, status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # Avoid reflecting message bodies or rejected actor fields into errors.
        return JSONResponse(
            {"code": "messaging_error", "detail": "Invalid Hub request"},
            status_code=400,
        )

    @app.get("/info")
    async def server_info(selected: Selection) -> HubInfo | HubPartitionInfo:
        if selected is None:
            return info
        return HubPartitionInfo(
            **info.model_dump(), root=selected.root, identity=selected.identity
        )

    @app.get("/healthz")
    async def health() -> dict[str, bool]:
        if not lifecycle.ready or lifecycle.failed:
            raise HTTPException(503, "Hub maintenance is not ready")
        await store.ping()
        return {"ok": True}

    async def event_subscription(
        scope: Partition,
        agent: Annotated[str | None, Query()] = None,
        thread: Annotated[str | None, Query()] = None,
        run: Annotated[str | None, Query()] = None,
        after: Annotated[str | None, Query()] = None,
    ) -> AsyncIterator[HubSubscription]:
        await scope[1].initialize()
        subscription = None
        try:
            subscription = HubSubscription(
                scope[1].events, HubScope(agent, thread, run), after
            )
            await subscription.prepare()
        except ScopeUnavailable as exc:
            raise HTTPException(
                404, {"code": "scope_unavailable", "detail": str(exc)}
            ) from exc
        except (SnapshotLimitError, StreamOverflowError) as exc:
            raise HTTPException(
                503, {"code": "snapshot_limit", "detail": str(exc)}
            ) from exc
        except (EventProtocolError, BackendUnavailable) as exc:
            raise HTTPException(
                503,
                {
                    "code": "protocol_error"
                    if isinstance(exc, EventProtocolError)
                    else "backend_unavailable",
                    "detail": str(exc),
                },
            ) from exc
        except (ValueError, MessagingError) as exc:
            raise HTTPException(
                400, {"code": "invalid_request", "detail": str(exc)}
            ) from exc
        try:
            yield subscription
        finally:
            if subscription is not None:
                subscription.close()

    @app.get("/events/stream", response_class=EventSourceResponse)
    async def events(
        subscription: Annotated[HubSubscription, Depends(event_subscription)],
    ) -> AsyncIterator[ServerSentEvent]:
        try:
            while True:
                frame = await subscription.receive()
                yield ServerSentEvent(event=frame.event, data=frame.data, id=frame.id)
        except StopAsyncIteration:
            return
        except (
            StreamOverflowError,
            SnapshotLimitError,
            BackendUnavailable,
            ConversationAccessDenied,
            StorageIntegrityError,
            ScopeUnavailable,
            EventProtocolError,
            EventRecoveryRequired,
        ) as exc:
            code = (
                "overflow"
                if isinstance(exc, (StreamOverflowError, EventRecoveryRequired))
                else "snapshot_limit"
                if isinstance(exc, SnapshotLimitError)
                else "backend_unavailable"
                if isinstance(exc, BackendUnavailable)
                else "scope_unavailable"
                if isinstance(exc, ScopeUnavailable)
                else "protocol_error"
            )
            yield ServerSentEvent(event="stream_error", data={"code": code})
        finally:
            subscription.close()

    app.include_router(team_router(human_client))
    app.include_router(messaging_router(human_client, prefix="/msg"))
    app.include_router(agent_router(agent_client))
    return app
