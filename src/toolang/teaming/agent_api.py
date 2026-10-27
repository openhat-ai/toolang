"""Hub-owned agent presence and publication routes."""

from collections.abc import Callable
from typing import Annotated, Any, TypeVar

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import TypeAdapter, ValidationError

from .errors import EventRecoveryRequired
from .messaging import MessagingClient
from .messaging_api import messaging_router
from .team_api import team_router
from .publication import validate_publication
from .records import MAX_BYTES
from .schemas import (
    AgentRegistration,
    TalkCursor,
    CommitRequest,
    Generation,
    StagingRequest,
)

T = TypeVar("T")
_COMMIT = TypeAdapter(CommitRequest)
_STAGING = TypeAdapter(StagingRequest)


async def _body(request: Request, adapter: TypeAdapter[T]) -> T:
    # Bound bytes before JSON decoding, including escaped projection strings.
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > 2 * MAX_BYTES:
            raise HTTPException(
                413,
                {
                    "code": "snapshot_limit",
                    "detail": "Publication upload exceeds budget",
                },
            )
        raw.extend(chunk)
    try:
        return adapter.validate_json(raw)
    except ValidationError as exc:
        raise HTTPException(
            400, {"code": "protocol_error", "detail": "Invalid publication request"}
        ) from exc


def agent_router(get_client: Callable) -> APIRouter:
    router = APIRouter(prefix="/agents/{agent}", tags=["agents"])
    Client = Annotated[MessagingClient, Depends(get_client)]

    async def active_client(client: Client) -> MessagingClient:
        if await client.backend.events.online_token(client.actor) != client.token:
            raise EventRecoveryRequired("Agent lease lost")
        return client

    ActiveClient = Annotated[MessagingClient, Depends(active_client)]

    @router.put("/lease")
    async def register(body: AgentRegistration, client: Client) -> dict[str, bool]:
        await client.register(body.owner, endpoint=body.endpoint)
        return {"ok": True}

    @router.patch("/lease")
    async def renew(client: ActiveClient) -> dict[str, bool]:
        await client.renew()
        return {"ok": True}

    @router.delete("/lease")
    async def unregister(client: Client) -> dict[str, bool]:
        await client.unregister()
        return {"ok": True}

    @router.get("/talk/cursors/{convo_id}")
    async def talk_cursor(convo_id: str, client: ActiveClient) -> TalkCursor:
        return TalkCursor(cursor=await client.talk_cursor(convo_id))

    @router.put("/talk/cursors/{convo_id}")
    async def advance_talk_cursor(
        convo_id: str, body: TalkCursor, client: ActiveClient
    ) -> TalkCursor:
        await client.advance_talk_cursor(convo_id, body.cursor)
        return body

    @router.get("/events/state")
    async def state(client: ActiveClient) -> dict[str, Any]:
        events = client.backend.events
        await events.initialize()
        meta, origins, _ = await events.capture(client.actor)
        return {"meta": meta, "origin": origins.get(client.actor)}

    @router.put("/events/staging/{generation}")
    async def stage(
        generation: Generation, request: Request, client: ActiveClient
    ) -> dict[str, bool]:
        body = await _body(request, _STAGING)
        validate_publication(body, agent=client.actor)
        await client.backend.events.stage(
            {
                **body.model_dump(),
                "agent": client.actor,
                "token": client.token,
                "generation": generation,
            }
        )
        return {"ok": True}

    @router.post("/events/commits")
    async def commit(request: Request, client: ActiveClient) -> dict[str, str]:
        body = await _body(request, _COMMIT)
        validate_publication(body, agent=client.actor)
        sid = await client.backend.events.commit(
            {**body.model_dump(), "agent": client.actor, "token": client.token}
        )
        await client.renew()
        return {"stream_id": sid}

    @router.delete("/events/staging/{generation}")
    async def abandon(generation: Generation, client: ActiveClient) -> dict[str, bool]:
        await client.backend.events.abandon(
            client.actor, generation, token=client.token
        )
        return {"ok": True}

    router.include_router(team_router(active_client))
    router.include_router(messaging_router(active_client, prefix="/msg", agent=True))
    return router
