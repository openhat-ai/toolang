"""Run inspection and control routes."""

from fastapi import APIRouter, HTTPException, Query

from toolang.api.app import AgentCoreDep
from toolang.api.conversion import parse_user_message
from toolang.api.schemas import (
    RunCancelRequest,
    RunCommandResult,
    RunCreateRequest,
    RunSteerRequest,
)
from toolang.execution.records import RunRecord
from toolang.execution.schemas import RunControlInfo, RunDetail, RunInfo
from toolang.execution.types import RunStatus
from toolang.up import AgentCore

router = APIRouter(prefix="/runs", tags=["runs"])


@router.get("", summary="List Runs", response_model=list[RunInfo])
def runs(
    core: AgentCoreDep,
    limit: int = Query(default=50),
    thread_id: str | None = None,
    status: RunStatus | None = None,
) -> list[RunInfo]:
    return core.history.list_runs(
        limit=limit,
        thread_id=thread_id,
        status=status,
    )


@router.post("/stream", summary="Execute Run Stream")
async def execute_run_stream(
    core: AgentCoreDep,
    payload: RunCreateRequest,
) -> None:
    del core, payload
    raise HTTPException(
        status_code=501,
        detail="run streaming is being migrated to the canonical run event protocol",
    )


@router.get("/{run_id}", summary="Get Run", response_model=RunDetail)
def run_detail(core: AgentCoreDep, run_id: str) -> RunDetail:
    detail = core.history.get_run(run_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"run not found: {run_id}")
    return detail


@router.get("/{run_id}/events", summary="List Run Events")
def run_events(core: AgentCoreDep, run_id: str) -> None:
    _run_or_404(core, run_id)
    raise HTTPException(
        status_code=501,
        detail="durable run event cursors are not available yet",
    )


@router.get("/{run_id}/stream", summary="Stream Run Events")
async def run_stream(core: AgentCoreDep, run_id: str) -> None:
    _run_or_404(core, run_id)
    raise HTTPException(
        status_code=501,
        detail="run streaming is being migrated to the canonical run event protocol",
    )


@router.post(
    "/{run_id}/cancel",
    summary="Cancel Run",
    response_model=RunCommandResult,
)
def cancel_run(
    core: AgentCoreDep,
    run_id: str,
    payload: RunCancelRequest | None = None,
) -> RunCommandResult:
    run = _running_run_or_409(core, run_id)
    control = core.executor.stop(
        run_id=run.id,
        timing=payload.mode if payload else "immediate",
        request_id=payload.request_id if payload else None,
        reason=payload.reason if payload else None,
    )
    return _control_result(core, run.id, control)


@router.post(
    "/{run_id}/steer",
    summary="Steer Run",
    status_code=202,
    response_model=RunCommandResult,
)
def steer_run(
    core: AgentCoreDep,
    run_id: str,
    payload: RunSteerRequest,
) -> RunCommandResult:
    run = _running_run_or_409(core, run_id)
    control = core.executor.steer(
        run_id=run.id,
        timing=payload.mode,
        request_id=payload.request_id,
        message=parse_user_message(payload.message),
    )
    return _control_result(core, run.id, control)


def _run_or_404(core: AgentCore, run_id: str) -> RunRecord:
    run = core.store.get_run(run_id=run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"run not found: {run_id}")
    return run


def _running_run_or_409(core: AgentCore, run_id: str) -> RunRecord:
    run = _run_or_404(core, run_id)
    if run.status != "running":
        raise HTTPException(status_code=409, detail=f"run is not running: {run_id}")
    return run


def _control_result(core: AgentCore, run_id: str, control) -> RunCommandResult:
    run = _run_or_404(core, run_id)
    detail = core.history.get_run(run_id)
    if detail is None:
        raise HTTPException(
            status_code=500,
            detail=f"run not found after control: {run_id}",
        )
    return RunCommandResult(
        run=detail,
        command=RunControlInfo.from_record(run, control),
    )
