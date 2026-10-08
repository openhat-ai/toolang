"""Root-scoped Hub HTTP transport for shared teaming services."""

from collections.abc import Callable
from contextlib import asynccontextmanager
import secrets
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .errors import BackendUnavailable, MessagingError, SendUnconfirmed
from .messaging import MessagingClient
from .schemas import (
    Conversation,
    CreateGroupRequest,
    HistoryEntry,
    ResolveRequest,
    SendRequest,
    target,
)


def create_app(
    client: MessagingClient,
    *,
    token: str,
    on_ready: Callable[[], None] | None = None,
) -> FastAPI:
    """Bind requests to one configured human; own the service for this lifespan."""
    target(client.actor, kind="human")
    if not token:
        raise ValueError("Hub bearer token must not be empty")

    async def authenticate(
        authorization: Annotated[str | None, Header()] = None,
    ) -> None:
        if not secrets.compare_digest(
            (authorization or "").encode(), f"Bearer {token}".encode()
        ):
            raise HTTPException(401, "Hub authentication required")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with client:
            await client.check_backend()
            if on_ready is not None:
                on_ready()
            yield

    app = FastAPI(
        title="Toolang Hub API",
        lifespan=lifespan,
        dependencies=[Depends(authenticate)],
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.exception_handler(MessagingError)
    async def messaging_error(request: Request, exc: MessagingError) -> JSONResponse:
        if isinstance(exc, SendUnconfirmed):
            status, code = 502, "send_unconfirmed"
        elif isinstance(exc, BackendUnavailable):
            status, code = 503, "backend_unavailable"
        else:
            status, code = 400, "messaging_error"
        return JSONResponse({"code": code, "detail": str(exc)}, status_code=status)

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
        await client.check_backend()
        return {"ok": True}

    router = APIRouter(prefix="/msg", tags=["messaging"])

    @router.get("/targets")
    async def targets() -> dict[str, Any]:
        return await client.targets()

    @router.get("/agents")
    async def agents() -> dict[str, str]:
        return await client.agents()

    @router.get("/groups")
    async def groups(
        include_preview: Annotated[bool, Query()] = False,
    ) -> list[dict[str, Any]]:
        return await client.contacts(include_preview=include_preview)

    @router.post("/resolve")
    async def resolve(body: ResolveRequest) -> dict[str, str]:
        return {"group": await client.resolve(body.target, kind=body.kind)}

    @router.get("/groups/{group}")
    async def conversation(group: str) -> Conversation:
        return await client.conversation(group)

    @router.post("/groups", status_code=201)
    async def create_group(body: CreateGroupRequest) -> dict[str, Any]:
        return await client.create_group(body.name)

    @router.put("/groups/{group}/membership")
    async def join_group(group: str) -> dict[str, Any]:
        return await client.join_group(group)

    @router.delete("/groups/{group}/membership")
    async def leave_group(group: str) -> dict[str, Any]:
        return await client.leave_group(group)

    @router.get("/groups/{group}/messages")
    async def messages(
        group: str,
        after: Annotated[str | None, Query()] = None,
        count: Annotated[int, Query(ge=1, le=1000)] = 200,
    ) -> list[HistoryEntry]:
        rows = (
            await client.history(group, count=count)
            if after is None
            else await client.read(group, after=after, count=count)
        )
        return [
            HistoryEntry(stream_id=sid, data=data.get("data")) for sid, data in rows
        ]

    @router.get("/groups/{group}/cursor")
    async def cursor(
        group: str, after: Annotated[str, Query()]
    ) -> dict[str, str | None]:
        return {"notice": await client.check_cursor(group, after)}

    @router.post("/messages", status_code=201)
    async def send(body: SendRequest) -> dict[str, Any]:
        target(body.target)
        return await client.send(
            body.target,
            body=body.body,
            in_reply_to=body.in_reply_to,
            message_id=body.id,
        )

    app.include_router(router)
    return app
