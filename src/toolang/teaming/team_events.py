"""Independent resumable team-change subscriptions, separate from execution."""

from collections.abc import AsyncGenerator
from dataclasses import dataclass
import re

from .backend import Backend
from .errors import ConversationAccessDenied, MessagingError
from .messaging import MessagingClient
from .schemas import stream_id


@dataclass(frozen=True)
class TeamEventFrame:
    event: str
    data: dict
    id: str | None = None


class TeamEvents:
    def __init__(self, backend: Backend):
        self.backend = backend

    async def checkpoint(self) -> str:
        epoch, tail = await self.backend.team_event_position()
        return f"t1.{epoch}.{tail}"

    @staticmethod
    def parse(cursor: str) -> tuple[str, str]:
        match = re.fullmatch(r"t1\.([0-9a-f]{32})\.([0-9]{1,20}-[0-9]{1,20})", cursor)
        if match is None:
            raise MessagingError("Invalid team event cursor")
        epoch, after = match.groups()
        # Reject alternative zero-padded representations.
        parts = stream_id(after)
        if any(part >= 1 << 64 for part in parts) or after != "-".join(map(str, parts)):
            raise MessagingError("Invalid team event cursor")
        return epoch, after

    async def replay(
        self, cursor: str, *, actor: str | None = None, token: str = ""
    ) -> list[tuple[str, dict]] | None:
        epoch, after = self.parse(cursor)
        rows = await self.backend.team_event_replay(
            epoch, after, actor=actor, token=token
        )
        if rows is None:
            return None
        return [(f"t1.{epoch}.{sid}", data) for sid, data in rows]

    async def wait(self, cursor: str) -> None:
        _, after = self.parse(cursor)
        await self.backend.wait_team_events(after)

    async def frames(
        self, client: MessagingClient, after: str | None
    ) -> AsyncGenerator[TeamEventFrame]:
        cursor = after or await self.checkpoint()
        if after is None:
            yield TeamEventFrame(event="checkpoint", id=cursor, data={"cursor": cursor})
        while True:
            rows = await self.replay(cursor, actor=client.actor, token=client._lease)
            if rows is None:
                yield TeamEventFrame(
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
                        except ConversationAccessDenied:
                            visible = False
                cursor = next_cursor
                if visible and data["type"] != "stream.initialized":
                    yield TeamEventFrame(event="change", id=cursor, data=data)
            if rows:
                yield TeamEventFrame(
                    event="checkpoint", id=cursor, data={"cursor": cursor}
                )
            else:
                await self.wait(cursor)
