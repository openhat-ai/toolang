"""One messaging router shared by human and agent Hub identities."""

from collections.abc import Callable
from typing import Annotated, Any
from fastapi import APIRouter, Depends, Query
from .messaging import MessagingClient
from .schemas import (
    Conversation,
    CreateGroupRequest,
    HistoryEntry,
    ResolveRequest,
    SendRequest,
    AgentSendRequest,
    target,
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

    @router.get("/groups")
    async def groups(
        client: Client,
        include_preview: Annotated[bool, Query()] = False,
    ) -> list[dict[str, Any]]:
        return await client.contacts(include_preview=include_preview)

    @router.post("/resolve")
    async def resolve(body: ResolveRequest, client: Client) -> dict[str, str]:
        return {"group": await client.resolve(body.target, kind=body.kind)}

    @router.get("/groups/{group}")
    async def conversation(group: str, client: Client) -> Conversation:
        return await client.conversation(group)

    @router.post("/groups", status_code=201)
    async def create_group(body: CreateGroupRequest, client: Client) -> dict[str, Any]:
        return await client.create_group(body.name)

    @router.put("/groups/{group}/membership")
    async def join_group(group: str, client: Client) -> dict[str, Any]:
        return await client.join_group(group)

    @router.delete("/groups/{group}/membership")
    async def leave_group(group: str, client: Client) -> dict[str, Any]:
        return await client.leave_group(group)

    @router.get("/groups/{group}/messages")
    async def messages(
        client: Client,
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
        client: Client, group: str, after: Annotated[str, Query()]
    ) -> dict[str, str | None]:
        return {"notice": await client.check_cursor(group, after)}

    async def deliver(body: SendRequest, client: MessagingClient) -> dict[str, Any]:
        target(body.target)
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
