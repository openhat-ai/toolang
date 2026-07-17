"""Formal job management API routes."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from toolang.catalog.job import JobCatalog
from toolang.work.state import AgentJobs
from toolang.work.store import open_job_store
from toolang.execution.request import RunRequest
from . import _views


def create_router() -> APIRouter:
    """Build the formal job management route group."""

    router = APIRouter(prefix="/api/v1")

    @router.patch(
        "/jobs/archived/{job_id}", tags=["jobs"], summary="Update Archived Job"
    )
    async def update_archived_job(
        request: Request,
        job_id: str,
        payload: _views.JobPatchRequest,
    ) -> dict[str, object]:
        context = request.app.state
        kind, entry = _views._find_archived_job_or_404(context, job_id)
        return _views._update_job(context, kind=kind, entry=entry, payload=payload)

    @router.delete(
        "/jobs/archived/{job_id}", tags=["jobs"], summary="Delete Archived Job"
    )
    async def delete_archived_job(request: Request, job_id: str) -> dict[str, object]:
        context = request.app.state
        kind, entry = _views._find_archived_job_or_404(context, job_id)
        removed = JobCatalog(context.root, context.name).remove(kind, job_id)
        if not removed:
            raise HTTPException(
                status_code=404, detail=f"archived job not found: {job_id}"
            )
        _reconcile_jobs(context, kind=kind)
        _views._append_job_update(
            context, kind=kind, item_id=job_id, action="deleted", path=entry.path
        )
        return {"deleted": True, "id": job_id, "kind": kind}

    @router.patch("/jobs/{job_id}", tags=["jobs"], summary="Update Job")
    async def update_job(
        request: Request,
        job_id: str,
        payload: _views.JobPatchRequest,
    ) -> dict[str, object]:
        context = request.app.state
        kind, entry = _views._find_job_or_404(context, job_id)
        return _views._update_job(context, kind=kind, entry=entry, payload=payload)

    @router.post("/tasks", tags=["jobs"], summary="Create Task", status_code=201)
    async def create_task(
        request: Request,
        payload: _views.TaskCreateRequest,
    ) -> dict[str, object]:
        context = request.app.state
        document = _views._task_document_from_create(context, payload)
        path = JobCatalog(context.root, context.name).create_document(document)
        _reconcile_jobs(context, kind="task")
        _views._append_job_update(
            context,
            kind="task",
            item_id=document.task_id(),
            action="created",
            path=path,
        )
        entry = _views._find_task_or_404(context, document.task_id())
        return {"item": _views._task_detail_item(context, entry)}

    @router.patch(
        "/tasks/archived/{task_id}", tags=["jobs"], summary="Update Archived Task"
    )
    async def update_archived_task(
        request: Request,
        task_id: str,
        payload: _views.TaskPatchRequest,
    ) -> dict[str, object]:
        context = request.app.state
        entry = _views._find_archived_task_or_404(context, task_id)
        return _views._update_task(context, entry=entry, payload=payload)

    @router.delete(
        "/tasks/archived/{task_id}", tags=["jobs"], summary="Delete Archived Task"
    )
    async def delete_archived_task(request: Request, task_id: str) -> dict[str, object]:
        context = request.app.state
        entry = _views._find_archived_task_or_404(context, task_id)
        if not JobCatalog(context.root, context.name).remove("task", task_id):
            raise HTTPException(
                status_code=404, detail=f"archived task not found: {task_id}"
            )
        _reconcile_jobs(context, kind="task")
        _views._append_job_update(
            context, kind="task", item_id=task_id, action="deleted", path=entry.path
        )
        return {"deleted": True, "id": task_id, "kind": "task"}

    @router.patch("/tasks/{task_id}", tags=["jobs"], summary="Update Task")
    async def update_task(
        request: Request,
        task_id: str,
        payload: _views.TaskPatchRequest,
    ) -> dict[str, object]:
        context = request.app.state
        entry = _views._find_task_or_404(context, task_id)
        return _views._update_task(context, entry=entry, payload=payload)

    @router.post("/tasks/{task_id}/draft", tags=["jobs"], summary="Draft Task")
    async def draft_task(request: Request, task_id: str) -> dict[str, object]:
        context = request.app.state
        catalog = JobCatalog(context.root, context.name)
        path = catalog.draft("task", task_id)
        if path is None:
            raise HTTPException(status_code=404, detail=f"task not found: {task_id}")
        _reconcile_jobs(context, kind="task")
        _views._append_job_update(
            context, kind="task", item_id=task_id, action="drafted", path=path
        )
        entry = catalog.get("task", task_id, lifecycle="draft")
        if entry is None:
            raise HTTPException(
                status_code=404, detail=f"task not found after draft: {task_id}"
            )
        return {"item": _views._task_detail_item(context, entry)}

    @router.post("/tasks/{task_id}/ready", tags=["jobs"], summary="Ready Task")
    async def ready_task(request: Request, task_id: str) -> dict[str, object]:
        context = request.app.state
        path = JobCatalog(context.root, context.name).ready("task", task_id)
        if path is None:
            raise HTTPException(status_code=404, detail=f"task not found: {task_id}")
        _reconcile_jobs(context, kind="task")
        _views._append_job_update(
            context, kind="task", item_id=task_id, action="ready", path=path
        )
        entry = _views._find_task_or_404(context, task_id)
        return {"item": _views._task_detail_item(context, entry)}

    @router.post("/tasks/{task_id}/archive", tags=["jobs"], summary="Archive Task")
    async def archive_task(request: Request, task_id: str) -> dict[str, object]:
        context = request.app.state
        path = JobCatalog(context.root, context.name).archive("task", task_id)
        if path is None:
            raise HTTPException(status_code=404, detail=f"task not found: {task_id}")
        _reconcile_jobs(context, kind="task")
        _views._append_job_update(
            context, kind="task", item_id=task_id, action="archived", path=path
        )
        entry = _views._find_archived_task_or_404(context, task_id)
        return {"item": _views._task_detail_item(context, entry)}

    @router.post("/tasks/{task_id}/reopen", tags=["jobs"], summary="Reopen Task")
    async def reopen_task(request: Request, task_id: str) -> dict[str, object]:
        context = request.app.state
        store = open_job_store(context.root, context.name)
        try:
            record = store.reopen_task(
                jobs=AgentJobs.load(
                    context.root, context.name, context.get_agent_state().program
                ),
                task_id=task_id,
            )
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        finally:
            store.close()
        return {"job": record}

    @router.post("/tasks/{task_id}/cancel", tags=["jobs"], summary="Cancel Task")
    async def cancel_task(request: Request, task_id: str) -> dict[str, object]:
        context = request.app.state
        store = open_job_store(context.root, context.name)
        try:
            store.reconcile(
                jobs=AgentJobs.load(
                    context.root, context.name, context.get_agent_state().program
                ),
                kind="task",
            )
            record = store.get(job_id=task_id, kind="task")
            if record is None:
                raise HTTPException(
                    status_code=404, detail=f"task not found: {task_id}"
                )
            if record.status == "todo":
                return {"job": store.cancel_pending_task(task_id=task_id)}
            if record.status == "running" and record.last_run_id is not None:
                await context.executor.stop(run_id=record.last_run_id)
                return {"job": record, "run_id": record.last_run_id}
            raise HTTPException(
                status_code=409,
                detail=f"task cannot be canceled from status: {record.status}",
            )
        finally:
            store.close()

    @router.post("/chores", tags=["jobs"], summary="Create Chore", status_code=201)
    async def create_chore(
        request: Request,
        payload: _views.ChoreCreateRequest,
    ) -> dict[str, object]:
        context = request.app.state
        document = _views._chore_document_from_create(context, payload)
        path = JobCatalog(context.root, context.name).create_document(document)
        _reconcile_jobs(context, kind="chore")
        _views._append_job_update(
            context,
            kind="chore",
            item_id=document.chore_id(),
            action="created",
            path=path,
        )
        entry = _views._find_chore_or_404(context, document.chore_id())
        return {"item": _views._chore_detail_item(context, entry)}

    @router.post("/chores/{chore_id}/run", tags=["jobs"], summary="Run Chore")
    async def run_chore(request: Request, chore_id: str) -> dict[str, object]:
        context = request.app.state
        store = open_job_store(context.root, context.name)
        try:
            claimed = store.claim_chore_manual(
                jobs=AgentJobs.load(
                    context.root, context.name, context.get_agent_state().program
                ),
                chore_id=chore_id,
                run_id=_views.allocate_run_id(context.root, context.name),
            )
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        finally:
            store.close()
        context.executor.start(
            RunRequest(
                group="pulse:chore",
                origin="chore",
                run_id=claimed.run_id,
                thread_id=claimed.job.thread_id,
                input=claimed.definition.input,
                metadata={
                    "job": claimed.definition.run_metadata(),
                    "job_trigger": "manual",
                },
            ),
            context.get_agent_state(),
        )
        return {"run_id": claimed.run_id, "job": claimed.job}

    @router.post("/chores/{chore_id}/cancel", tags=["jobs"], summary="Cancel Chore")
    async def cancel_chore(request: Request, chore_id: str) -> dict[str, object]:
        context = request.app.state
        store = open_job_store(context.root, context.name)
        try:
            store.reconcile(
                jobs=AgentJobs.load(
                    context.root, context.name, context.get_agent_state().program
                ),
                kind="chore",
            )
            record = store.get(job_id=chore_id, kind="chore")
            if record is None:
                raise HTTPException(
                    status_code=404, detail=f"chore not found: {chore_id}"
                )
            if record.status == "running" and record.last_run_id is not None:
                await context.executor.stop(run_id=record.last_run_id)
                return {"job": record, "run_id": record.last_run_id}
            raise HTTPException(
                status_code=409,
                detail=f"chore cannot be canceled from status: {record.status}",
            )
        finally:
            store.close()

    @router.patch(
        "/chores/archived/{chore_id}", tags=["jobs"], summary="Update Archived Chore"
    )
    async def update_archived_chore(
        request: Request,
        chore_id: str,
        payload: _views.ChorePatchRequest,
    ) -> dict[str, object]:
        context = request.app.state
        entry = _views._find_archived_chore_or_404(context, chore_id)
        return _views._update_chore(context, entry=entry, payload=payload)

    @router.delete(
        "/chores/archived/{chore_id}", tags=["jobs"], summary="Delete Archived Chore"
    )
    async def delete_archived_chore(
        request: Request, chore_id: str
    ) -> dict[str, object]:
        context = request.app.state
        entry = _views._find_archived_chore_or_404(context, chore_id)
        if not JobCatalog(context.root, context.name).remove("chore", chore_id):
            raise HTTPException(
                status_code=404, detail=f"archived chore not found: {chore_id}"
            )
        _reconcile_jobs(context, kind="chore")
        _views._append_job_update(
            context, kind="chore", item_id=chore_id, action="deleted", path=entry.path
        )
        return {"deleted": True, "id": chore_id, "kind": "chore"}

    @router.patch("/chores/{chore_id}", tags=["jobs"], summary="Update Chore")
    async def update_chore(
        request: Request,
        chore_id: str,
        payload: _views.ChorePatchRequest,
    ) -> dict[str, object]:
        context = request.app.state
        entry = _views._find_chore_or_404(context, chore_id)
        return _views._update_chore(context, entry=entry, payload=payload)

    @router.post("/chores/{chore_id}/draft", tags=["jobs"], summary="Draft Chore")
    async def draft_chore(request: Request, chore_id: str) -> dict[str, object]:
        context = request.app.state
        catalog = JobCatalog(context.root, context.name)
        path = catalog.draft("chore", chore_id)
        if path is None:
            raise HTTPException(status_code=404, detail=f"chore not found: {chore_id}")
        _reconcile_jobs(context, kind="chore")
        _views._append_job_update(
            context, kind="chore", item_id=chore_id, action="drafted", path=path
        )
        entry = catalog.get("chore", chore_id, lifecycle="draft")
        if entry is None:
            raise HTTPException(
                status_code=404, detail=f"chore not found after draft: {chore_id}"
            )
        return {"item": _views._chore_detail_item(context, entry)}

    @router.post("/chores/{chore_id}/ready", tags=["jobs"], summary="Ready Chore")
    async def ready_chore(request: Request, chore_id: str) -> dict[str, object]:
        context = request.app.state
        path = JobCatalog(context.root, context.name).ready("chore", chore_id)
        if path is None:
            raise HTTPException(status_code=404, detail=f"chore not found: {chore_id}")
        _reconcile_jobs(context, kind="chore")
        _views._append_job_update(
            context, kind="chore", item_id=chore_id, action="ready", path=path
        )
        entry = _views._find_chore_or_404(context, chore_id)
        return {"item": _views._chore_detail_item(context, entry)}

    @router.post("/chores/{chore_id}/archive", tags=["jobs"], summary="Archive Chore")
    async def archive_chore(request: Request, chore_id: str) -> dict[str, object]:
        context = request.app.state
        path = JobCatalog(context.root, context.name).archive("chore", chore_id)
        if path is None:
            raise HTTPException(status_code=404, detail=f"chore not found: {chore_id}")
        _reconcile_jobs(context, kind="chore")
        _views._append_job_update(
            context, kind="chore", item_id=chore_id, action="archived", path=path
        )
        entry = _views._find_archived_chore_or_404(context, chore_id)
        return {"item": _views._chore_detail_item(context, entry)}

    return router


def _reconcile_jobs(context, *, kind: _views.JobKind) -> None:
    store = open_job_store(context.root, context.name)
    try:
        store.reconcile(
            jobs=AgentJobs.load(context.root, context.name, context.get_agent_state().program),
            kind=kind,
        )
    finally:
        store.close()
