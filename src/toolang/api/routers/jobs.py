"""Job inspection and management routes."""

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from toolang.api.app import ApiContextDep
from toolang.api.schemas import (
    ChoreCreateRequest,
    ChorePatchRequest,
    TaskCreateRequest,
    TaskPatchRequest,
)
from toolang.catalog.job import JobFile
from toolang.catalog.types import JobKind, JobStage
from toolang.base.types.message import Message
from toolang.common.ids import allocate_run_id
from toolang.work.state import AgentJobs
from toolang.work.authoring import (
    allocate_authored_job_id,
    new_job_file,
)
from toolang.work.inspection import JobInspection
from toolang.work.schemas import JobDetail, JobInfo
from toolang.work.store import open_job_store
from toolang.execution.history import RunHistory
from toolang.execution.executor.request import RunRequest
from toolang.execution.schemas import RunCommandResult, RunControlInfo


router = APIRouter(tags=["jobs"])


@router.post("/tasks", summary="Create Task", status_code=201, response_model=JobDetail)
def create_task(
    context: ApiContextDep,
    payload: TaskCreateRequest,
) -> JobDetail:
    document = _task_document_from_create(context, payload)
    saved = context.authored_jobs.create(document)
    _reconcile_jobs(context, kind="task")
    entry = _find_task_or_404(context, saved.id)
    return _task_detail_item(context, entry)


@router.patch(
    "/tasks/archived/{task_id}",
    summary="Update Archived Task",
    response_model=JobDetail,
)
def update_archived_task(
    context: ApiContextDep,
    task_id: str,
    payload: TaskPatchRequest,
) -> JobDetail:
    entry = _find_archived_task_or_404(context, task_id)
    return _update_task(context, entry=entry, payload=payload)


@router.delete(
    "/tasks/archived/{task_id}",
    summary="Delete Archived Task",
    status_code=204,
    response_class=Response,
)
def delete_archived_task(context: ApiContextDep, task_id: str) -> None:
    _find_archived_task_or_404(context, task_id)
    context.authored_jobs.remove("task", task_id)
    _reconcile_jobs(context, kind="task")


@router.patch("/tasks/{task_id}", summary="Update Task", response_model=JobDetail)
def update_task(
    context: ApiContextDep,
    task_id: str,
    payload: TaskPatchRequest,
) -> JobDetail:
    entry = _find_task_or_404(context, task_id)
    return _update_task(context, entry=entry, payload=payload)


@router.post("/tasks/{task_id}/draft", summary="Draft Task", response_model=JobDetail)
def draft_task(context: ApiContextDep, task_id: str) -> JobDetail:
    catalog = context.authored_jobs
    catalog.move("task", task_id, "draft")
    _reconcile_jobs(context, kind="task")
    entry = catalog.get("task", task_id, stage="draft")
    if entry is None:
        raise HTTPException(
            status_code=404, detail=f"task not found after draft: {task_id}"
        )
    return _task_detail_item(context, entry)


@router.post("/tasks/{task_id}/ready", summary="Ready Task", response_model=JobDetail)
def ready_task(context: ApiContextDep, task_id: str) -> JobDetail:
    context.authored_jobs.move("task", task_id, "ready")
    _reconcile_jobs(context, kind="task")
    entry = _find_task_or_404(context, task_id)
    return _task_detail_item(context, entry)


@router.post(
    "/tasks/{task_id}/archive", summary="Archive Task", response_model=JobDetail
)
def archive_task(context: ApiContextDep, task_id: str) -> JobDetail:
    context.authored_jobs.move("task", task_id, "archived")
    _reconcile_jobs(context, kind="task")
    entry = _find_archived_task_or_404(context, task_id)
    return _task_detail_item(context, entry)


@router.post("/tasks/{task_id}/reopen", summary="Reopen Task", response_model=JobDetail)
def reopen_task(context: ApiContextDep, task_id: str) -> JobDetail:
    store = open_job_store(context.root, context.name)
    try:
        store.reopen_task(
            jobs=AgentJobs.load(
                context.root, context.name, context.state_watcher.current().program
            ),
            task_id=task_id,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    finally:
        store.close()
    return _task_detail_item(context, _find_task_or_404(context, task_id))


@router.post("/tasks/{task_id}/cancel", summary="Cancel Task", response_model=JobDetail)
async def cancel_task(context: ApiContextDep, task_id: str) -> JobDetail:
    store = open_job_store(context.root, context.name)
    try:
        store.reconcile(
            jobs=AgentJobs.load(
                context.root, context.name, context.state_watcher.current().program
            ),
            kind="task",
        )
        record = store.get(job_id=task_id, kind="task")
        if record is None:
            raise HTTPException(status_code=404, detail=f"task not found: {task_id}")
        if record.status == "todo":
            store.cancel_pending_task(task_id=task_id)
        elif record.status == "running" and record.last_run_id is not None:
            await context.executor.stop(run_id=record.last_run_id)
        else:
            raise HTTPException(
                status_code=409,
                detail=f"task cannot be canceled from status: {record.status}",
            )
    finally:
        store.close()
    return _task_detail_item(context, _find_task_or_404(context, task_id))


@router.post(
    "/chores", summary="Create Chore", status_code=201, response_model=JobDetail
)
def create_chore(
    context: ApiContextDep,
    payload: ChoreCreateRequest,
) -> JobDetail:
    document = _chore_document_from_create(context, payload)
    saved = context.authored_jobs.create(document)
    _reconcile_jobs(context, kind="chore")
    entry = _find_chore_or_404(context, saved.id)
    return _chore_detail_item(context, entry)


@router.post(
    "/chores/{chore_id}/run",
    summary="Run Chore",
    status_code=202,
    response_model=RunCommandResult,
)
async def run_chore(context: ApiContextDep, chore_id: str) -> RunCommandResult:
    store = open_job_store(context.root, context.name)
    try:
        claimed = store.claim_chore_manual(
            jobs=AgentJobs.load(
                context.root, context.name, context.state_watcher.current().program
            ),
            chore_id=chore_id,
            run_id=allocate_run_id(context.executor.id_state_path),
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    finally:
        store.close()
    run, command = await context.submit_run(
        RunRequest(
            origin="chore",
            input=Message.user(claimed.definition.input),
            run_id=claimed.run_id,
            thread_id=claimed.job.thread_id,
            context={
                "job": claimed.definition.run_metadata(),
                "job_trigger": "manual",
            },
        ),
    )
    detail = RunHistory(context.executor.store).get_run(run.id)
    if detail is None:
        raise HTTPException(
            status_code=500,
            detail=f"run not found after acceptance: {run.id}",
        )
    return RunCommandResult(
        run=detail,
        command=RunControlInfo.from_record(run, command),
    )


@router.post(
    "/chores/{chore_id}/cancel", summary="Cancel Chore", response_model=JobDetail
)
async def cancel_chore(context: ApiContextDep, chore_id: str) -> JobDetail:
    store = open_job_store(context.root, context.name)
    try:
        store.reconcile(
            jobs=AgentJobs.load(
                context.root, context.name, context.state_watcher.current().program
            ),
            kind="chore",
        )
        record = store.get(job_id=chore_id, kind="chore")
        if record is None:
            raise HTTPException(status_code=404, detail=f"chore not found: {chore_id}")
        if record.status == "running" and record.last_run_id is not None:
            await context.executor.stop(run_id=record.last_run_id)
        else:
            raise HTTPException(
                status_code=409,
                detail=f"chore cannot be canceled from status: {record.status}",
            )
    finally:
        store.close()
    return _chore_detail_item(context, _find_chore_or_404(context, chore_id))


@router.patch(
    "/chores/archived/{chore_id}",
    summary="Update Archived Chore",
    response_model=JobDetail,
)
def update_archived_chore(
    context: ApiContextDep,
    chore_id: str,
    payload: ChorePatchRequest,
) -> JobDetail:
    entry = _find_archived_chore_or_404(context, chore_id)
    return _update_chore(context, entry=entry, payload=payload)


@router.delete(
    "/chores/archived/{chore_id}",
    summary="Delete Archived Chore",
    status_code=204,
    response_class=Response,
)
def delete_archived_chore(context: ApiContextDep, chore_id: str) -> None:
    _find_archived_chore_or_404(context, chore_id)
    context.authored_jobs.remove("chore", chore_id)
    _reconcile_jobs(context, kind="chore")


@router.patch("/chores/{chore_id}", summary="Update Chore", response_model=JobDetail)
def update_chore(
    context: ApiContextDep,
    chore_id: str,
    payload: ChorePatchRequest,
) -> JobDetail:
    entry = _find_chore_or_404(context, chore_id)
    return _update_chore(context, entry=entry, payload=payload)


@router.post(
    "/chores/{chore_id}/draft", summary="Draft Chore", response_model=JobDetail
)
def draft_chore(context: ApiContextDep, chore_id: str) -> JobDetail:
    catalog = context.authored_jobs
    catalog.move("chore", chore_id, "draft")
    _reconcile_jobs(context, kind="chore")
    entry = catalog.get("chore", chore_id, stage="draft")
    if entry is None:
        raise HTTPException(
            status_code=404, detail=f"chore not found after draft: {chore_id}"
        )
    return _chore_detail_item(context, entry)


@router.post(
    "/chores/{chore_id}/ready", summary="Ready Chore", response_model=JobDetail
)
def ready_chore(context: ApiContextDep, chore_id: str) -> JobDetail:
    context.authored_jobs.move("chore", chore_id, "ready")
    _reconcile_jobs(context, kind="chore")
    entry = _find_chore_or_404(context, chore_id)
    return _chore_detail_item(context, entry)


@router.post(
    "/chores/{chore_id}/archive", summary="Archive Chore", response_model=JobDetail
)
def archive_chore(context: ApiContextDep, chore_id: str) -> JobDetail:
    context.authored_jobs.move("chore", chore_id, "archived")
    _reconcile_jobs(context, kind="chore")
    entry = _find_archived_chore_or_404(context, chore_id)
    return _chore_detail_item(context, entry)


@router.get("/jobs", summary="List Jobs", response_model=list[JobInfo])
def jobs(context: ApiContextDep, kind: JobKind | None = None) -> list[JobInfo]:
    items = _job_collection(context, archived=False)
    if kind is not None:
        items = [item for item in items if item.kind == kind]
    return items


@router.get(
    "/jobs/archived", summary="List Archived Jobs", response_model=list[JobInfo]
)
def archived_jobs(context: ApiContextDep, kind: JobKind | None = None) -> list[JobInfo]:
    items = _job_collection(context, archived=True)
    if kind is not None:
        items = [item for item in items if item.kind == kind]
    return items


@router.get(
    "/jobs/archived/{job_id}",
    summary="Get Archived Job",
    response_model=JobDetail,
)
def archived_job_detail(context: ApiContextDep, job_id: str) -> JobDetail:
    kind, entry = _find_archived_job_or_404(context, job_id)
    return _job_detail_item(context, kind=kind, entry=entry)


@router.get("/jobs/{job_id}", summary="Get Job", response_model=JobDetail)
def job_detail(context: ApiContextDep, job_id: str) -> JobDetail:
    kind, entry = _find_job_or_404(context, job_id)
    return _job_detail_item(context, kind=kind, entry=entry)


@router.get("/tasks", summary="List Tasks", response_model=list[JobInfo])
def tasks(context: ApiContextDep) -> list[JobInfo]:
    return _task_collection(context, archived=False)


@router.get(
    "/tasks/archived", summary="List Archived Tasks", response_model=list[JobInfo]
)
def archived_tasks(context: ApiContextDep) -> list[JobInfo]:
    return _task_collection(context, archived=True)


@router.get(
    "/tasks/archived/{task_id}",
    summary="Get Archived Task",
    response_model=JobDetail,
)
def archived_task_detail(context: ApiContextDep, task_id: str) -> JobDetail:
    entry = _find_archived_task_or_404(context, task_id)
    return _task_detail_item(context, entry)


@router.get("/tasks/{task_id}", summary="Get Task", response_model=JobDetail)
def task_detail(context: ApiContextDep, task_id: str) -> JobDetail:
    entry = _find_task_or_404(context, task_id)
    return _task_detail_item(context, entry)


@router.get("/chores", summary="List Chores", response_model=list[JobInfo])
def chores(context: ApiContextDep) -> list[JobInfo]:
    return _chore_collection(context, archived=False)


@router.get(
    "/chores/archived", summary="List Archived Chores", response_model=list[JobInfo]
)
def archived_chores(context: ApiContextDep) -> list[JobInfo]:
    return _chore_collection(context, archived=True)


@router.get(
    "/chores/archived/{chore_id}",
    summary="Get Archived Chore",
    response_model=JobDetail,
)
def archived_chore_detail(context: ApiContextDep, chore_id: str) -> JobDetail:
    entry = _find_archived_chore_or_404(context, chore_id)
    return _chore_detail_item(context, entry)


@router.get("/chores/{chore_id}", summary="Get Chore", response_model=JobDetail)
def chore_detail(context: ApiContextDep, chore_id: str) -> JobDetail:
    entry = _find_chore_or_404(context, chore_id)
    return _chore_detail_item(context, entry)


def _reconcile_jobs(context, *, kind: JobKind) -> None:
    store = open_job_store(context.root, context.name)
    try:
        store.reconcile(
            jobs=AgentJobs.load(
                context.root, context.name, context.state_watcher.current().program
            ),
            kind=kind,
        )
    finally:
        store.close()


def _job_path(job: JobFile) -> Path:
    if job.path is None:
        raise ValueError("authored job path is required")
    return job.path


def _job_inspection(context) -> JobInspection:
    return JobInspection.load(
        root=context.root,
        agent_name=context.name,
        home=context.home,
        program=context.state_watcher.current().program,
        runs=context.executor.store.list_runs(limit=None),
    )


def _job_collection(context, *, archived: bool) -> list[JobInfo]:
    stage: JobStage = "archived" if archived else "ready"
    return list(_job_inspection(context).list(stage=stage))


def _task_collection(context, *, archived: bool) -> list[JobInfo]:
    stage: JobStage = "archived" if archived else "ready"
    return [item for item in _job_inspection(context).list(kind="task", stage=stage)]


def _chore_collection(context, *, archived: bool) -> list[JobInfo]:
    stage: JobStage = "archived" if archived else "ready"
    return [item for item in _job_inspection(context).list(kind="chore", stage=stage)]


def _task_detail_item(context, entry: JobFile) -> JobDetail:
    return _job_inspection(context).detail(entry)


def _chore_detail_item(context, entry: JobFile) -> JobDetail:
    return _job_inspection(context).detail(entry)


def _job_detail_item(context, *, kind: JobKind, entry: JobFile) -> JobDetail:
    del kind
    return _job_inspection(context).detail(entry)


def _find_job_or_404(context, job_id: str) -> tuple[JobKind, JobFile]:
    catalog = context.authored_jobs
    task = catalog.get("task", job_id)
    if task is not None:
        return "task", task
    chore = catalog.get("chore", job_id)
    if chore is not None:
        return "chore", chore
    raise HTTPException(status_code=404, detail=f"job not found: {job_id}")


def _find_archived_job_or_404(context, job_id: str) -> tuple[JobKind, JobFile]:
    catalog = context.authored_jobs
    task = catalog.get("task", job_id, stage="archived")
    if task is not None:
        return "task", task
    chore = catalog.get("chore", job_id, stage="archived")
    if chore is not None:
        return "chore", chore
    raise HTTPException(status_code=404, detail=f"archived job not found: {job_id}")


def _find_task_or_404(context, task_id: str) -> JobFile:
    entry = context.authored_jobs.get("task", task_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"task not found: {task_id}")
    return entry


def _find_archived_task_or_404(context, task_id: str) -> JobFile:
    entry = context.authored_jobs.get("task", task_id, stage="archived")
    if entry is None:
        raise HTTPException(
            status_code=404, detail=f"archived task not found: {task_id}"
        )
    return entry


def _find_chore_or_404(context, chore_id: str) -> JobFile:
    entry = context.authored_jobs.get("chore", chore_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"chore not found: {chore_id}")
    return entry


def _find_archived_chore_or_404(context, chore_id: str) -> JobFile:
    entry = context.authored_jobs.get("chore", chore_id, stage="archived")
    if entry is None:
        raise HTTPException(
            status_code=404, detail=f"archived chore not found: {chore_id}"
        )
    return entry


def _task_document_from_create(context, payload: TaskCreateRequest) -> JobFile:
    try:
        return new_job_file(
            kind="task",
            job_id=allocate_authored_job_id(context.root, context.name),
            title=payload.title,
            body=payload.body,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _chore_document_from_create(context, payload: ChoreCreateRequest) -> JobFile:
    try:
        return new_job_file(
            kind="chore",
            job_id=allocate_authored_job_id(context.root, context.name),
            title=payload.title,
            body=payload.body,
            schedule=payload.schedule,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _update_task(
    context,
    *,
    entry: JobFile,
    payload: TaskPatchRequest,
) -> JobDetail:
    document = _patch_task_document(entry, payload)
    saved = context.authored_jobs.update(document)
    return _job_inspection(context).detail(saved)


def _update_chore(
    context,
    *,
    entry: JobFile,
    payload: ChorePatchRequest,
) -> JobDetail:
    document = _patch_chore_document(entry, payload)
    saved = context.authored_jobs.update(document)
    return _job_inspection(context).detail(saved)


def _patch_task_document(document: JobFile, payload: TaskPatchRequest) -> JobFile:
    return _patch_document(document, payload, fields=("title", "body"))


def _patch_chore_document(document: JobFile, payload: ChorePatchRequest) -> JobFile:
    return _patch_document(document, payload, fields=("title", "body", "schedule"))


def _patch_document(
    document: JobFile,
    payload: TaskPatchRequest | ChorePatchRequest,
    *,
    fields: tuple[str, ...],
) -> JobFile:
    changes = {
        field: getattr(payload, field)
        for field in fields
        if field in payload.model_fields_set
    }
    try:
        return document.patch(changes)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
