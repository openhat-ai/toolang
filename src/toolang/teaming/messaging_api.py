"""One messaging router shared by human and agent Hub identities."""

from collections.abc import Callable
from typing import Annotated, Any
from fastapi import APIRouter, Depends, Query
from .messaging import MessagingClient
from .schemas import (
    Conversation,
    CreateConversationRequest,
    HistoryEntry,
    ResolveRequest,
    SendRequest,
    AgentSendRequest,
    RenameConversationRequest,
    Resolution,
)


def messaging_router(
    get_client: Callable, *, prefix: str, agent: bool = False
) -> APIRouter:
    Client = Annotated[MessagingClient, Depends(get_client)]
    router = APIRouter(prefix=prefix, tags=["messaging"])

    @router.get("/targets")
    async def targets(client: Client) -> dict[str, Any]:
        return await client.targets()

    @router.get("/agents")
    async def agents(client: Client) -> dict[str, str]:
        return await client.agents()

    @router.get("/conversations")
    async def conversations(
        client: Client,
        include_preview: Annotated[bool, Query()] = False,
    ) -> list[dict[str, Any]]:
        return await client.contacts(include_preview=include_preview)

    @router.get("/stats")
    async def statistics(client: Client) -> dict[str, int | str | None]:
        return await client.statistics()

    @router.get("/conversations/{conversation}/stats")
    async def conversation_statistics(
        conversation: str, client: Client
    ) -> dict[str, int | str | None]:
        return await client.statistics(conversation)

    @router.patch("/conversations/{conversation}")
    async def rename(
        conversation: str, body: RenameConversationRequest, client: Client
    ) -> Conversation:
        return await client.rename_conversation(
            conversation, body.name, revision=body.revision
        )

    @router.post("/resolve")
    async def resolve(body: ResolveRequest, client: Client) -> Resolution:
        return await client.resolve(body.target, kind=body.kind, create=body.create)

    @router.get("/conversations/{conversation}")
    async def conversation(conversation: str, client: Client) -> Conversation:
        return await client.conversation(conversation)

    @router.post("/conversations", status_code=201)
    async def create_conversation(
        body: CreateConversationRequest, client: Client
    ) -> Conversation:
        return await client.create_conversation(
            body.name, participants=body.participants
        )

    @router.put("/conversations/{conversation}/participants")
    async def join_conversation(conversation: str, client: Client) -> dict[str, Any]:
        return await client.join_conversation(conversation)

    @router.delete("/conversations/{conversation}/participants")
    async def leave_conversation(conversation: str, client: Client) -> dict[str, Any]:
        return await client.leave_conversation(conversation)

    @router.get("/conversations/{conversation}/messages")
    async def messages(
        client: Client,
        conversation: str,
        after: Annotated[str | None, Query()] = None,
        count: Annotated[int, Query(ge=1, le=1000)] = 200,
    ) -> list[HistoryEntry]:
        rows = (
            await client.history(conversation, count=count)
            if after is None
            else await client.read(conversation, after=after, count=count)
        )
        return [
            HistoryEntry(stream_id=sid, data=data.get("data")) for sid, data in rows
        ]

    @router.get("/conversations/{conversation}/cursor")
    async def cursor(
        client: Client, conversation: str, after: Annotated[str, Query()]
    ) -> dict[str, str | None]:
        return {"notice": await client.check_cursor(conversation, after)}

    async def deliver(body: SendRequest, client: MessagingClient) -> dict[str, Any]:
        origin = (
            {"run": body.run, "thread": body.thread}
            if isinstance(body, AgentSendRequest)
            else {}
        )
        return await client.send(
            body.target,
            body=body.body,
            in_reply_to=body.in_reply_to,
            message_id=body.id,
            participants=body.participants,
            **origin,
        )

    if agent:

        @router.post("/messages", status_code=201)
        async def agent_send(body: AgentSendRequest, client: Client) -> dict[str, Any]:
            return await deliver(body, client)
    else:

        @router.post("/messages", status_code=201)
        async def human_send(body: SendRequest, client: Client) -> dict[str, Any]:
            return await deliver(body, client)

    return router
