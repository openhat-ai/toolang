"""Chore inspection and authored-file management routes."""

from collections.abc import Callable, Iterable
from typing import TypeVar, cast

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from toolang.api.app import AgentCoreDep, ChoreDriverDep, ChoresManagerDep
from toolang.api.schemas import (
    ChoreCreateRequest,
    ChorePatchRequest,
)
from toolang.catalog import ChoresManager
from toolang.catalog.chore import ChoreFile
from toolang.up import AgentCore
from toolang.work.authoring import (
    create_chore as create_definition,
    editable_chore,
    new_chore_file,
)
from toolang.work.inspection import ChoreInspection, ChoreRun
from toolang.work.schemas import ChoreDetail, ChoreInfo
from toolang.work.scheduler import ChoreDriver
from toolang.work.state import file_chore

_T = TypeVar("_T")

router = APIRouter(tags=["chores"])


@router.post(
    "/chores",
    summary="Create Chore",
    status_code=201,
    response_model=ChoreDetail,
)
def create_chore(
    core: AgentCoreDep,
    scheduler: ChoreDriverDep,
    payload: ChoreCreateRequest,
) -> ChoreDetail:
    saved = create_definition(core.layout, _new_chore(payload=payload))
    _refresh_chores(scheduler)
    return _chore_detail(core, saved)


@router.post("/chores/{chore_id}/run", summary="Run Chore", status_code=202)
def run_chore(
    scheduler: ChoreDriverDep,
    chore_id: str,
) -> None:
    _scheduler_call(_require_scheduler(scheduler).run_chore_sync, chore_id)


@router.post("/chores/{chore_id}/cancel", summary="Cancel Chore")
def cancel_chore(
    core: AgentCoreDep,
    scheduler: ChoreDriverDep,
    chore_id: str,
) -> None:
    if scheduler is None:
        from toolang.work.store import open_chore_store

        if not core.layout.chore_store.is_file():
            raise HTTPException(status_code=404, detail=f"chore not found: {chore_id}")
        store = open_chore_store(core.layout)
        try:
            record = next(
                (
                    item
                    for item in store.list()
                    if chore_id in {item.chore_id, item.name, f"chore/{item.name}"}
                ),
                None,
            )
            if (
                record is not None
                and record.active_run_id is None
                and record.pending_at is not None
            ):
                record = store.cancel_pending_chore(chore_id=record.chore_id)
        finally:
            store.close()
    else:
        record = _scheduler_call(scheduler.cancel_chore_sync, chore_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"chore not found: {chore_id}")
    if record.active_run_id is not None:
        core.executor.cancel(run_id=record.active_run_id, reason="chore canceled")


@router.patch("/chores/{chore_id}", summary="Update Chore", response_model=ChoreDetail)
def update_chore(
    core: AgentCoreDep,
    manager: ChoresManagerDep,
    scheduler: ChoreDriverDep,
    chore_id: str,
    payload: ChorePatchRequest,
) -> ChoreDetail:
    return _update_chore(
        core,
        manager,
        scheduler,
        editable_chore(core.layout, chore_id),
        payload,
    )


@router.get("/chores", summary="List Chores", response_model=list[ChoreInfo])
def chores(core: AgentCoreDep) -> list[ChoreInfo]:
    return list(_inspection(core).list())


@router.get("/chores/{chore_id}", summary="Get Chore", response_model=ChoreDetail)
def chore_detail(
    core: AgentCoreDep,
    chore_id: str,
) -> ChoreDetail:
    inspection = _inspection(core)
    entry = inspection.get(chore_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"chore not found: {chore_id}")
    return inspection.detail(entry)


@router.delete(
    "/chores/{chore_id}",
    summary="Delete Chore Definition",
    status_code=204,
    response_class=Response,
)
def delete_chore(
    core: AgentCoreDep,
    manager: ChoresManagerDep,
    scheduler: ChoreDriverDep,
    chore_id: str,
) -> None:
    editable_chore(core.layout, chore_id)
    manager.home_authoring.remove(chore_id)
    _refresh_chores(scheduler)


def _refresh_chores(scheduler: ChoreDriver | None) -> None:
    if scheduler is not None:
        _scheduler_call(scheduler.refresh_sync)


def _require_scheduler(scheduler: ChoreDriver | None) -> ChoreDriver:
    if scheduler is None:
        raise HTTPException(
            status_code=409, detail={"code": "driver_disabled", "driver": "chore"}
        )
    return scheduler


def _inspection(core: AgentCore) -> ChoreInspection:
    runs = tuple(cast(Iterable[ChoreRun], core.store.list_runs(limit=None)))
    return ChoreInspection.load(
        layout=core.layout,
        runs=runs,
        error_messages={
            run.id: core.store.resolve_error(run.error)
            for run in runs
            if run.error is not None
        },
    )


def _chore_detail(core: AgentCore, entry: ChoreFile) -> ChoreDetail:
    return _inspection(core).detail(file_chore(entry))


def _new_chore(*, payload: ChoreCreateRequest) -> ChoreFile:
    try:
        return new_chore_file(
            name=payload.name,
            title=payload.title,
            body=payload.body,
            schedule=payload.schedule,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _update_chore(
    core: AgentCore,
    manager: ChoresManager,
    scheduler: ChoreDriver | None,
    entry: ChoreFile,
    payload: ChorePatchRequest,
) -> ChoreDetail:
    fields = ("title", "body", "schedule")
    changes = {
        field: getattr(payload, field)
        for field in fields
        if field in payload.model_fields_set
    }
    try:
        saved = manager.home_authoring.update(entry.patch(changes))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _refresh_chores(scheduler)
    return _chore_detail(core, saved)


def _scheduler_call(call: Callable[..., _T], *args: object) -> _T:
    try:
        return call(*args)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
