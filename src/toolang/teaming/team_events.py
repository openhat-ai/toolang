"""Independent resumable team-change subscriptions, separate from execution."""

from collections.abc import AsyncGenerator, AsyncIterator, Callable
import json
import re
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.sse import EventSourceResponse, ServerSentEvent

from .backend import Backend
from .errors import MessagingError, StorageIntegrityError
from .keys import TEAM_EVENTS
from .messaging import MessagingClient
from .schemas import stream_id
from . import storage_scripts


class TeamEvents:
    def __init__(self, backend: Backend):
        self.backend = backend

    async def checkpoint(self) -> str:
        epoch, tail = await self.backend._operation(
            storage_scripts.READ, {"action": "metadata"}
        )
        return f"t1.{epoch}.{tail}"

    @staticmethod
    def parse(cursor: str) -> tuple[str, str]:
        match = re.fullmatch(r"t1\.([0-9a-f]{32})\.([0-9]+-[0-9]+)", cursor)
        if match is None:
            raise MessagingError("Invalid team event cursor")
        epoch, after = match.groups()
        # Reject alternative zero-padded representations.
        parts = stream_id(after)
        if any(part >= 1 << 64 for part in parts) or after != "-".join(map(str, parts)):
            raise MessagingError("Invalid team event cursor")
        return epoch, after

    async def replay(self, cursor: str) -> list[tuple[str, dict]] | None:
        epoch, after = self.parse(cursor)
        result = await self.backend._operation(
            storage_scripts.REPLAY, dict(epoch=epoch, after=after)
        )
        if result[0] == "resync":
            return None
        rows = []
        for sid, values in result[1]:
            fields = dict(zip(values[::2], values[1::2], strict=True))
            data = json.loads(fields["data"])
            rows.append((f"t1.{epoch}.{sid}", data))
        return rows

    async def wait(self, cursor: str) -> None:
        _, after = self.parse(cursor)
        await self.backend._call(
            "XREAD", "COUNT", 1, "BLOCK", 1000, "STREAMS", TEAM_EVENTS, after
        )

    async def frames(
        self, client: MessagingClient, after: str | None
    ) -> AsyncGenerator[ServerSentEvent]:
        cursor = after or await self.checkpoint()
        if after is None:
            yield ServerSentEvent(
                event="checkpoint", id=cursor, data={"cursor": cursor}
            )
        while True:
            rows = await self.replay(cursor)
            if rows is None:
                yield ServerSentEvent(
                    event="resync_required",
                    data={"reason": "Team event history changed or expired"},
                )
                return
            for next_cursor, data in rows:
                visible = True
                conversation = data.get("conversation")
                if conversation and client.actor.startswith("agent:"):
                    own_removal = (
                        data["type"] == "conversation.member_removed"
                        and data["payload"].get("member") == client.actor
                    )
                    if not own_removal:
                        try:
                            await client.conversation(conversation)
                        except StorageIntegrityError:
                            raise
                        except MessagingError:
                            visible = False
                cursor = next_cursor
                if visible and data["type"] != "stream.initialized":
                    yield ServerSentEvent(event="change", id=cursor, data=data)
            if rows:
                yield ServerSentEvent(
                    event="checkpoint", id=cursor, data={"cursor": cursor}
                )
            else:
                await self.wait(cursor)


def team_router(get_client: Callable) -> APIRouter:
    router = APIRouter(prefix="/team", tags=["team"])
    Client = Annotated[MessagingClient, Depends(get_client)]

    @router.get("")
    async def team(client: Client) -> list[dict]:
        return await client.team()

    @router.get("/events", response_class=EventSourceResponse)
    async def events(
        client: Client, after: Annotated[str | None, Query()] = None
    ) -> AsyncIterator[ServerSentEvent]:
        feed = TeamEvents(client._backend)
        if after:
            feed.parse(after)
        async for frame in feed.frames(client, after):
            yield frame

    return router
