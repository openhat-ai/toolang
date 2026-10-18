"""Read-only statistics endpoints; blocking work runs in the API thread pool."""

from collections.abc import Iterator
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from toolang.api.app import AgentCoreDep
from toolang.stats.recent import recent_usage
from toolang.stats.errors import StatsUnavailable
from toolang.stats.schemas import (
    DailyRow,
    ModelsResponse,
    RecentUsage,
    StatsResponse,
    WindowRow,
)
from toolang.stats.service import StatsService, scope_for
from toolang.stats.types import Scope, Window
import time

router = APIRouter(prefix="/stats", tags=["stats"])


def get_stats(request: Request) -> Iterator[StatsService]:
    service = getattr(request.app.state, "stats", None)
    if not isinstance(service, StatsService):
        raise HTTPException(status_code=503, detail="statistics service is unavailable")
    try:
        yield service
    except StatsUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def get_scope(thread_id: str | None = None, root_run_id: str | None = None) -> Scope:
    try:
        return scope_for(thread_id, root_run_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


StatsDep = Annotated[StatsService, Depends(get_stats)]
ScopeDep = Annotated[Scope, Depends(get_scope)]
ModelFilter = Annotated[str | None, Query(min_length=1)]


@router.get("/recent")
def recent(
    core: AgentCoreDep,
    period: Annotated[int, Query(ge=1, le=28800)] = 3600,
) -> RecentUsage:
    try:
        return recent_usage(core.store.db_path, now=int(time.time()), period=period)
    except StatsUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc


@router.get("/summary")
def summary(
    stats: StatsDep, scope: ScopeDep, model_ref: ModelFilter = None
) -> StatsResponse[list[WindowRow]]:
    return stats.summary(scope, model_ref)


@router.get("/models")
def models(stats: StatsDep, scope: ScopeDep, window: Window = "1mo") -> ModelsResponse:
    return stats.models(window, scope)


@router.get("/daily")
def daily(
    stats: StatsDep,
    scope: ScopeDep,
    start_date: date,
    end_date: date,
    timezone: str,
    model_ref: ModelFilter = None,
) -> StatsResponse[list[DailyRow]]:
    return stats.daily(start_date, end_date, timezone, scope, model_ref)
