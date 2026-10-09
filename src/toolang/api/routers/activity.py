"""Compact absolute activity over the same committed boundary as records."""

from contextlib import aclosing

from collections.abc import AsyncGenerator
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.sse import EventSourceResponse, ServerSentEvent

from toolang.execution.activity import ActivityQuery, ActivityReader
from toolang.execution.schemas import ActivitySnapshot

router = APIRouter(prefix="/activity", tags=["agent"])


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


def activity_reader(request: Request) -> ActivityReader:
    return request.app.state.activity


@router.get("")
def snapshot(
    query: Annotated[ActivityQuery, Depends(activity_query)],
    reader: Annotated[ActivityReader, Depends(activity_reader)],
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ActivitySnapshot:
    try:
        return reader.read(query, offset)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/batch")
def batch(
    query: Annotated[ActivityQuery, Depends(activity_query)],
    reader: Annotated[ActivityReader, Depends(activity_reader)],
) -> list[ActivitySnapshot]:
    return reader.pages(query)


@router.get("/result")
def result(
    ref: str,
    reader: Annotated[ActivityReader, Depends(activity_reader)],
) -> dict[str, str]:
    try:
        return {"text": reader.result(ref)}
    except KeyError as exc:
        raise HTTPException(404, "Execution record not found") from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/stream", response_class=EventSourceResponse)
async def stream(
    query: Annotated[ActivityQuery, Depends(activity_query)],
    reader: Annotated[ActivityReader, Depends(activity_reader)],
) -> AsyncGenerator[ServerSentEvent]:
    async with aclosing(reader.updates(query)) as updates:
        async for pages in updates:
            for page in pages:
                yield ServerSentEvent(event="activity_page", data=page.model_dump())
            first = pages[0]
            yield ServerSentEvent(
                event="activity_checkpoint",
                data={"agents": [first.agent]},
                id=f"{first.session}:{first.revision}",
            )
