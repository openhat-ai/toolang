"""Source-only activity adapter, independent of the archival service."""

from typing import Annotated
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query

from toolang.api.app import AgentCoreDep
from toolang.execution.executor import RunExecutor
from toolang.stats.errors import StatsUnavailable
from toolang.up.activity import activity_snapshot
from toolang.up.schemas import ActivitySnapshot

router = APIRouter(prefix="/top", tags=["top"])


def get_top_executor(core: AgentCoreDep) -> RunExecutor:
    return core.executor


def get_top_source(core: AgentCoreDep) -> Path:
    return core.store.db_path


@router.get("/snapshot")
def snapshot(
    executor: Annotated[RunExecutor, Depends(get_top_executor)],
    source: Annotated[Path, Depends(get_top_source)],
    recent: Annotated[int, Query(ge=1, le=900)] = 300,
    before_time: str | None = None,
    after_id: str | None = None,
    completed_at: Annotated[int | None, Query(ge=0)] = None,
) -> ActivitySnapshot:
    if (before_time is None) != (after_id is None):
        raise HTTPException(422, "both completion cursor fields are required")
    try:
        return activity_snapshot(
            source,
            executor=executor,
            recent=recent,
            before=(before_time, after_id) if before_time and after_id else None,
            completed_at=completed_at,
        )
    except (StatsUnavailable, RuntimeError) as exc:
        raise HTTPException(503, str(exc)) from exc
