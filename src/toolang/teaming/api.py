"""Root-scoped Hub HTTP transport for shared teaming services."""

from collections.abc import Callable, AsyncIterator
import asyncio
from contextlib import asynccontextmanager
from typing import Annotated, Any, cast
from urllib.parse import quote

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.sse import EventSourceResponse, ServerSentEvent
from starlette.middleware import Middleware
from toolang.common.sse import SSESendDeadline
from toolang.execution.activity import ActivityReader
from toolang.execution.errors import SnapshotLimitError
from toolang.execution.errors import StreamOverflowError
from .events import HubScope
from .subscriptions import HubSubscription

from .errors import (
    BackendUnavailable,
    ConversationAccessDenied,
    StorageIntegrityError,
    LeaseLost,
    MessagingError,
    SendUnconfirmed,
    EventProtocolError,
    EventRecoveryRequired,
    ScopeUnavailable,
)
from .messaging import MessagingClient
from .schemas import target
from .messaging_api import messaging_router
from .lifecycle import HubLifecycle
from .team_api import team_router
from .agent_api import agent_router
from .roster import Roster


def create_app(
    client: MessagingClient,
    *,
    on_ready: Callable[[], None] | None = None,
    roster: Roster | None = None,
    local_activity: Callable[[str], ActivityReader | None] | None = None,
) -> FastAPI:
    """Bind requests to one configured human; own the service for this lifespan."""
    target(client.actor, kind="human")

    async def prepare_request(
        backend: Annotated[str | None, Header(alias="X-Toolang-Backend")] = None,
        human: Annotated[str | None, Header(alias="X-Toolang-Human")] = None,
    ) -> None:
        if (backend is not None and backend != client.config.identity) or (
            human is not None and human != quote(client.actor, safe="")
        ):
            raise HTTPException(
                409,
                {"code": "hub_changed", "detail": "Hub identity changed"},
            )
        # Registration is idempotent; damaged or reset storage fails closed.
        await client.register_human()

    lifecycle = HubLifecycle(client, roster=roster)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with lifecycle.lifespan(on_ready):
            yield

    app = FastAPI(
        title="Toolang Hub API",
        middleware=[Middleware(cast(Any, SSESendDeadline))],
        lifespan=lifespan,
        dependencies=[Depends(prepare_request)],
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.exception_handler(MessagingError)
    async def messaging_error(request: Request, exc: MessagingError) -> JSONResponse:
        if isinstance(exc, LeaseLost):
            status, code = 409, "recovery_required"
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

    @app.get("/healthz")
    async def health() -> dict[str, bool]:
        if not lifecycle.ready or lifecycle.failed:
            raise HTTPException(503, "Hub maintenance is not ready")
        await client.backend.ping()
        return {"ok": True}

    async def event_subscription(
        agent: Annotated[str | None, Query()] = None,
        thread: Annotated[str | None, Query()] = None,
        run: Annotated[str | None, Query()] = None,
        after: Annotated[str | None, Query()] = None,
    ) -> AsyncIterator[HubSubscription]:
        subscription = None
        try:
            subscription = HubSubscription(
                client.backend.events, HubScope(agent, thread, run), after
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
                try:
                    frame = await asyncio.wait_for(subscription.receive(), 15)
                except TimeoutError:
                    yield ServerSentEvent(comment="keep-alive")
                    continue
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

    app.include_router(team_router(lambda: client))
    app.include_router(messaging_router(lambda: client, prefix="/msg"))
    app.include_router(agent_router(client, roster=roster))
    from .activity_api import activity_router

    app.include_router(
        activity_router(client.backend, roster=roster, local_reader=local_activity)
    )
    return app
