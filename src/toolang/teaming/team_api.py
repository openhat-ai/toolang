"""HTTP and SSE adapters for the team directory and change feed."""

from collections.abc import AsyncIterator, Callable
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.sse import EventSourceResponse, ServerSentEvent

from .errors import BackendUnavailable, LeaseLost, MessagingError, StorageIntegrityError
from .messaging import MessagingClient
from .schemas import TeamMember
from .team_events import TeamEvents


def team_router(get_client: Callable) -> APIRouter:
    router = APIRouter(prefix="/team", tags=["team"])
    Client = Annotated[MessagingClient, Depends(get_client)]

    @router.get("")
    async def team(client: Client) -> list[TeamMember]:
        return await client.team()

    def event_cursor(after: Annotated[str | None, Query()] = None) -> str | None:
        # Dependencies execute before the streaming response sends its headers.
        if after is not None:
            TeamEvents.parse(after)
        return after

    @router.get("/events", response_class=EventSourceResponse)
    async def events(
        client: Client, after: Annotated[str | None, Depends(event_cursor)]
    ) -> AsyncIterator[ServerSentEvent]:
        feed = TeamEvents(client.backend)
        try:
            async for frame in feed.frames(client, after):
                yield ServerSentEvent(event=frame.event, id=frame.id, data=frame.data)
        except MessagingError as exc:
            code = (
                "recovery_required"
                if isinstance(exc, LeaseLost)
                else "storage_integrity"
                if isinstance(exc, StorageIntegrityError)
                else "backend_unavailable"
                if isinstance(exc, BackendUnavailable)
                else "messaging_error"
            )
            yield ServerSentEvent(event="stream_error", data={"code": code})

    return router
