"""HTTP routes for Hub activity snapshots, subscriptions and results."""

from collections.abc import AsyncGenerator, Callable
from contextlib import aclosing
import sqlite3
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.sse import EventSourceResponse, ServerSentEvent

from toolang.execution.activity import ActivityQuery, ActivityReader
from toolang.execution.schemas import ActivitySnapshot
from .activity import HubActivity
from .backend import Backend
from .roster import Roster


def activity_query(
    since: str = "session",
    recent: Annotated[float | None, Query(gt=0)] = 1800,
    text: Annotated[str, Query(alias="filter", max_length=240)] = "",
    active: bool = False,
    all_recent: bool = False,
) -> ActivityQuery:
    try:
        return ActivityQuery(since, None if all_recent else recent, text, active)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


def activity_router(
    backend: Backend,
    *,
    roster: Roster | None = None,
    local_reader: Callable[[str], ActivityReader | None] | None = None,
) -> APIRouter:
    reader = HubActivity(backend, roster=roster, local_reader=local_reader)
    router = APIRouter(prefix="/activity", tags=["activity"])

    @router.get("")
    async def snapshot(
        query: Annotated[ActivityQuery, Depends(activity_query)],
    ) -> list[ActivitySnapshot]:
        return await reader.read(query)

    @router.get("/stream", response_class=EventSourceResponse)
    async def stream(
        query: Annotated[ActivityQuery, Depends(activity_query)],
    ) -> AsyncGenerator[ServerSentEvent]:
        async with aclosing(reader.updates(query)) as updates:
            async for event, data in updates:
                yield ServerSentEvent(event=event, data=data)

    @router.get("/result")
    async def result(agent: str, ref: str) -> dict[str, str]:
        if agent not in await reader.agents():
            raise HTTPException(404, "Agent not found")
        lease = await reader.backend.lease(agent)
        local = await reader.local_source(agent, lease)
        if local is not None:
            try:
                return {"text": await local.result(agent, ref)}
            except KeyError as exc:
                raise HTTPException(404, "Execution record not found") from exc
            except (OSError, sqlite3.Error) as exc:
                raise HTTPException(503, "Result unavailable") from exc
            except ValueError as exc:
                raise HTTPException(422, "Result unavailable") from exc
        if not lease.get("endpoint"):
            raise HTTPException(503, "Agent is offline; use inspect against its home")
        try:
            async with httpx.AsyncClient(timeout=2, trust_env=False) as http:
                response = await http.get(
                    lease["endpoint"] + "/api/v1/activity/result", params={"ref": ref}
                )
                response.raise_for_status()
                if lease != await reader.backend.lease(agent):
                    raise HTTPException(409, "Agent restarted; reopen Details")
                return {"text": response.json()["text"]}
        except httpx.HTTPStatusError as exc:
            raise HTTPException(exc.response.status_code, "Result unavailable") from exc
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            raise HTTPException(502, "Result unavailable") from exc

    return router
