"""Source-only activity adapter, independent of the archival service."""

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query

from toolang.api.app import AgentCoreDep
from toolang.stats.errors import StatsUnavailable
from toolang.up.activity import activity_snapshot
from toolang.up.schemas import ActivitySnapshot

router = APIRouter(prefix="/top", tags=["activity"])


@router.get("/snapshot")
def snapshot(
    core: AgentCoreDep,
    recent: Annotated[int, Query(ge=1, le=900)] = 300,
    before_time: str | None = None,
    after_id: str | None = None,
    completed_at: Annotated[int | None, Query(ge=0)] = None,
) -> ActivitySnapshot:
    if (before_time is None) != (after_id is None):
        raise HTTPException(422, "both completion cursor fields are required")
    try:
        return activity_snapshot(
            core.store.db_path,
            core=core,
            recent=recent,
            before=(before_time, after_id) if before_time and after_id else None,
            completed_at=completed_at,
        )
    except (StatsUnavailable, RuntimeError) as exc:
        raise HTTPException(503, str(exc)) from exc
