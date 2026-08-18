"""Job inspection and authored-file management routes."""

from collections.abc import Callable, Iterable
from typing import TypeVar, cast

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from toolang.api.app import AgentCoreDep, JobSchedulerDep, JobsManagerDep
from toolang.api.schemas import (
    ChoreCreateRequest,
    ChorePatchRequest,
    TaskCreateRequest,
    TaskPatchRequest,
)
from toolang.catalog import JobsManager
from toolang.catalog.job import JobFile
from toolang.catalog.types import JobKind, JobStage
from toolang.up import AgentCore
from toolang.work.authoring import allocate_authored_job_id, new_job_file
from toolang.work.inspection import JobInspection, JobRun
from toolang.work.schemas import JobDetail, JobInfo
from toolang.work.scheduler import JobScheduler

_T = TypeVar("_T")

router = APIRouter(tags=["jobs"])


@router.post("/tasks", summary="Create Task", status_code=201, response_model=JobDetail)
def create_task(
    core: AgentCoreDep,
    manager: JobsManagerDep,
    scheduler: JobSchedulerDep,
    payload: TaskCreateRequest,
) -> JobDetail:
    saved = manager.home_authoring.create(_new_job(core, kind="task", payload=payload))
    _refresh_jobs(scheduler)
    return _job_detail(core, saved)


@router.patch(
    "/tasks/archived/{task_id}",
    summary="Update Archived Task",
    response_model=JobDetail,
)
def update_archived_task(
    core: AgentCoreDep,
    manager: JobsManagerDep,
    scheduler: JobSchedulerDep,
    task_id: str,
    payload: TaskPatchRequest,
) -> JobDetail:
    entry = _require_job(manager, "task", task_id, stage="archived")
    return _update_job(core, manager, scheduler, entry, payload)


@router.delete(
    "/tasks/archived/{task_id}",
    summary="Delete Archived Task",
    status_code=204,
    response_class=Response,
)
def delete_archived_task(
    core: AgentCoreDep,
    manager: JobsManagerDep,
    scheduler: JobSchedulerDep,
    task_id: str,
) -> None:
    _require_job(manager, "task", task_id, stage="archived")
    manager.home_authoring.remove("task", task_id)
    _refresh_jobs(scheduler)


@router.patch("/tasks/{task_id}", summary="Update Task", response_model=JobDetail)
def update_task(
    core: AgentCoreDep,
    manager: JobsManagerDep,
    scheduler: JobSchedulerDep,
    task_id: str,
    payload: TaskPatchRequest,
) -> JobDetail:
    return _update_job(
        core,
        manager,
        scheduler,
        _require_job(manager, "task", task_id),
        payload,
    )


@router.post("/tasks/{task_id}/draft", summary="Draft Task", response_model=JobDetail)
def draft_task(
    core: AgentCoreDep,
    manager: JobsManagerDep,
    scheduler: JobSchedulerDep,
    task_id: str,
) -> JobDetail:
    return _move_job(core, manager, scheduler, "task", task_id, "draft")


@router.post("/tasks/{task_id}/ready", summary="Ready Task", response_model=JobDetail)
def ready_task(
    core: AgentCoreDep,
    manager: JobsManagerDep,
    scheduler: JobSchedulerDep,
    task_id: str,
) -> JobDetail:
    return _move_job(core, manager, scheduler, "task", task_id, "ready")


@router.post(
    "/tasks/{task_id}/archive",
    summary="Archive Task",
    response_model=JobDetail,
)
def archive_task(
    core: AgentCoreDep,
    manager: JobsManagerDep,
    scheduler: JobSchedulerDep,
    task_id: str,
) -> JobDetail:
    return _move_job(core, manager, scheduler, "task", task_id, "archived")


@router.post("/tasks/{task_id}/reopen", summary="Reopen Task")
def reopen_task(
    scheduler: JobSchedulerDep,
    task_id: str,
) -> None:
    _scheduler_call(scheduler.reopen_task_sync, task_id)


@router.post("/tasks/{task_id}/cancel", summary="Cancel Task")
def cancel_task(
    core: AgentCoreDep,
    scheduler: JobSchedulerDep,
    task_id: str,
) -> None:
    record = _scheduler_call(scheduler.cancel_task_sync, task_id)
    if record.active_run_id is not None:
        core.executor.stop(run_id=record.active_run_id, reason="task canceled")


@router.post(
    "/chores",
    summary="Create Chore",
    status_code=201,
    response_model=JobDetail,
)
def create_chore(
    core: AgentCoreDep,
    manager: JobsManagerDep,
    scheduler: JobSchedulerDep,
    payload: ChoreCreateRequest,
) -> JobDetail:
    saved = manager.home_authoring.create(_new_job(core, kind="chore", payload=payload))
    _refresh_jobs(scheduler)
    return _job_detail(core, saved)


@router.post("/chores/{chore_id}/run", summary="Run Chore", status_code=202)
def run_chore(
    scheduler: JobSchedulerDep,
    chore_id: str,
) -> None:
    _scheduler_call(scheduler.run_chore_sync, chore_id)


@router.post("/chores/{chore_id}/cancel", summary="Cancel Chore")
def cancel_chore(
    core: AgentCoreDep,
    scheduler: JobSchedulerDep,
    chore_id: str,
) -> None:
    record = scheduler.get_sync(chore_id)
    if record is None or record.kind != "chore":
        raise HTTPException(status_code=404, detail=f"chore not found: {chore_id}")
    if record.active_run_id is None:
        raise HTTPException(status_code=409, detail="chore has no active run")
    core.executor.stop(run_id=record.active_run_id, reason="chore canceled")


@router.patch(
    "/chores/archived/{chore_id}",
    summary="Update Archived Chore",
    response_model=JobDetail,
)
def update_archived_chore(
    core: AgentCoreDep,
    manager: JobsManagerDep,
    scheduler: JobSchedulerDep,
    chore_id: str,
    payload: ChorePatchRequest,
) -> JobDetail:
    entry = _require_job(manager, "chore", chore_id, stage="archived")
    return _update_job(core, manager, scheduler, entry, payload)


@router.delete(
    "/chores/archived/{chore_id}",
    summary="Delete Archived Chore",
    status_code=204,
    response_class=Response,
)
def delete_archived_chore(
    core: AgentCoreDep,
    manager: JobsManagerDep,
    scheduler: JobSchedulerDep,
    chore_id: str,
) -> None:
    _require_job(manager, "chore", chore_id, stage="archived")
    manager.home_authoring.remove("chore", chore_id)
    _refresh_jobs(scheduler)


@router.patch("/chores/{chore_id}", summary="Update Chore", response_model=JobDetail)
def update_chore(
    core: AgentCoreDep,
    manager: JobsManagerDep,
    scheduler: JobSchedulerDep,
    chore_id: str,
    payload: ChorePatchRequest,
) -> JobDetail:
    return _update_job(
        core,
        manager,
        scheduler,
        _require_job(manager, "chore", chore_id),
        payload,
    )


@router.post(
    "/chores/{chore_id}/draft",
    summary="Draft Chore",
    response_model=JobDetail,
)
def draft_chore(
    core: AgentCoreDep,
    manager: JobsManagerDep,
    scheduler: JobSchedulerDep,
    chore_id: str,
) -> JobDetail:
    return _move_job(core, manager, scheduler, "chore", chore_id, "draft")


@router.post(
    "/chores/{chore_id}/ready",
    summary="Ready Chore",
    response_model=JobDetail,
)
def ready_chore(
    core: AgentCoreDep,
    manager: JobsManagerDep,
    scheduler: JobSchedulerDep,
    chore_id: str,
) -> JobDetail:
    return _move_job(core, manager, scheduler, "chore", chore_id, "ready")


@router.post(
    "/chores/{chore_id}/archive",
    summary="Archive Chore",
    response_model=JobDetail,
)
def archive_chore(
    core: AgentCoreDep,
    manager: JobsManagerDep,
    scheduler: JobSchedulerDep,
    chore_id: str,
) -> JobDetail:
    return _move_job(core, manager, scheduler, "chore", chore_id, "archived")


@router.get("/jobs", summary="List Jobs", response_model=list[JobInfo])
def jobs(core: AgentCoreDep, kind: JobKind | None = None) -> list[JobInfo]:
    return _job_collection(core, kind=kind, stage="ready")


@router.get(
    "/jobs/archived",
    summary="List Archived Jobs",
    response_model=list[JobInfo],
)
def archived_jobs(
    core: AgentCoreDep,
    kind: JobKind | None = None,
) -> list[JobInfo]:
    return _job_collection(core, kind=kind, stage="archived")


@router.get(
    "/jobs/archived/{job_id}",
    summary="Get Archived Job",
    response_model=JobDetail,
)
def archived_job_detail(
    core: AgentCoreDep,
    manager: JobsManagerDep,
    job_id: str,
) -> JobDetail:
    return _job_detail(
        core,
        _require_any_job(manager, job_id, stage="archived"),
    )


@router.get("/jobs/{job_id}", summary="Get Job", response_model=JobDetail)
def job_detail(
    core: AgentCoreDep,
    manager: JobsManagerDep,
    job_id: str,
) -> JobDetail:
    return _job_detail(core, _require_any_job(manager, job_id))


@router.get("/tasks", summary="List Tasks", response_model=list[JobInfo])
def tasks(core: AgentCoreDep) -> list[JobInfo]:
    return _job_collection(core, kind="task", stage="ready")


@router.get(
    "/tasks/archived",
    summary="List Archived Tasks",
    response_model=list[JobInfo],
)
def archived_tasks(core: AgentCoreDep) -> list[JobInfo]:
    return _job_collection(core, kind="task", stage="archived")


@router.get(
    "/tasks/archived/{task_id}",
    summary="Get Archived Task",
    response_model=JobDetail,
)
def archived_task_detail(
    core: AgentCoreDep,
    manager: JobsManagerDep,
    task_id: str,
) -> JobDetail:
    return _job_detail(
        core,
        _require_job(manager, "task", task_id, stage="archived"),
    )


@router.get("/tasks/{task_id}", summary="Get Task", response_model=JobDetail)
def task_detail(
    core: AgentCoreDep,
    manager: JobsManagerDep,
    task_id: str,
) -> JobDetail:
    return _job_detail(core, _require_job(manager, "task", task_id))


@router.get("/chores", summary="List Chores", response_model=list[JobInfo])
def chores(core: AgentCoreDep) -> list[JobInfo]:
    return _job_collection(core, kind="chore", stage="ready")


@router.get(
    "/chores/archived",
    summary="List Archived Chores",
    response_model=list[JobInfo],
)
def archived_chores(core: AgentCoreDep) -> list[JobInfo]:
    return _job_collection(core, kind="chore", stage="archived")


@router.get(
    "/chores/archived/{chore_id}",
    summary="Get Archived Chore",
    response_model=JobDetail,
)
def archived_chore_detail(
    core: AgentCoreDep,
    manager: JobsManagerDep,
    chore_id: str,
) -> JobDetail:
    return _job_detail(
        core,
        _require_job(manager, "chore", chore_id, stage="archived"),
    )


@router.get("/chores/{chore_id}", summary="Get Chore", response_model=JobDetail)
def chore_detail(
    core: AgentCoreDep,
    manager: JobsManagerDep,
    chore_id: str,
) -> JobDetail:
    return _job_detail(core, _require_job(manager, "chore", chore_id))


def _refresh_jobs(scheduler: JobScheduler) -> None:
    _scheduler_call(scheduler.refresh_sync)


def _inspection(core: AgentCore) -> JobInspection:
    runs = tuple(cast(Iterable[JobRun], core.store.list_runs(limit=None)))
    return JobInspection.load(
        layout=core.layout,
        runs=runs,
        error_messages={
            run.id: core.store.resolve_error(run.error)
            for run in runs
            if run.error is not None
        },
    )


def _job_collection(
    core: AgentCore,
    *,
    kind: JobKind | None,
    stage: JobStage,
) -> list[JobInfo]:
    return list(_inspection(core).list(kind=kind, stage=stage))


def _job_detail(core: AgentCore, entry: JobFile) -> JobDetail:
    return _inspection(core).detail(entry)


def _require_any_job(
    manager: JobsManager,
    job_id: str,
    *,
    stage: JobStage = "ready",
) -> JobFile:
    for kind in ("task", "chore"):
        entry = manager.home_authoring.get(kind, job_id, stage=stage)
        if entry is not None:
            return entry
    prefix = "archived " if stage == "archived" else ""
    raise HTTPException(status_code=404, detail=f"{prefix}job not found: {job_id}")


def _require_job(
    manager: JobsManager,
    kind: JobKind,
    job_id: str,
    *,
    stage: JobStage = "ready",
) -> JobFile:
    entry = manager.home_authoring.get(kind, job_id, stage=stage)
    if entry is not None:
        return entry
    prefix = "archived " if stage == "archived" else ""
    raise HTTPException(status_code=404, detail=f"{prefix}{kind} not found: {job_id}")


def _new_job(
    core: AgentCore,
    *,
    kind: JobKind,
    payload: TaskCreateRequest | ChoreCreateRequest,
) -> JobFile:
    try:
        return new_job_file(
            kind=kind,
            job_id=allocate_authored_job_id(core.layout),
            title=payload.title,
            body=payload.body,
            schedule=(
                payload.schedule if isinstance(payload, ChoreCreateRequest) else None
            ),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _update_job(
    core: AgentCore,
    manager: JobsManager,
    scheduler: JobScheduler,
    entry: JobFile,
    payload: TaskPatchRequest | ChorePatchRequest,
) -> JobDetail:
    fields = (
        ("title", "body", "schedule")
        if isinstance(payload, ChorePatchRequest)
        else ("title", "body")
    )
    changes = {
        field: getattr(payload, field)
        for field in fields
        if field in payload.model_fields_set
    }
    try:
        saved = manager.home_authoring.update(entry.patch(changes))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _refresh_jobs(scheduler)
    return _job_detail(core, saved)


def _move_job(
    core: AgentCore,
    manager: JobsManager,
    scheduler: JobScheduler,
    kind: JobKind,
    job_id: str,
    stage: JobStage,
) -> JobDetail:
    manager.home_authoring.move(kind, job_id, stage)
    _refresh_jobs(scheduler)
    return _job_detail(core, _require_job(manager, kind, job_id, stage=stage))


def _scheduler_call(call: Callable[..., _T], *args: object) -> _T:
    try:
        return call(*args)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
