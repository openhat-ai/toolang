"""Formal job management API routes."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from .... import jobs
from ..inspect import _shared


def create_router() -> APIRouter:
    """Build the formal job management route group."""

    router = APIRouter(prefix="/api/v1")

    @router.patch("/jobs/archived/{job_id}", tags=["jobs"], summary="Update Archived Job")
    async def update_archived_job(
        request: Request,
        job_id: str,
        payload: _shared.JobPatchRequest,
    ) -> dict[str, object]:
        context = request.app.state.runtime
        kind, entry = _shared._find_archived_job_or_404(context, job_id)
        return _shared._update_job(context, kind=kind, entry=entry, payload=payload)

    @router.delete("/jobs/archived/{job_id}", tags=["jobs"], summary="Delete Archived Job")
    async def delete_archived_job(request: Request, job_id: str) -> dict[str, object]:
        context = request.app.state.runtime
        kind, entry = _shared._find_archived_job_or_404(context, job_id)
        removed = (
            _shared.work.remove_archived_task(context.root, context.name, job_id)
            if kind == "task"
            else _shared.work.remove_archived_chore(context.root, context.name, job_id)
        )
        if not removed:
            raise HTTPException(status_code=404, detail=f"archived job not found: {job_id}")
        _reconcile_jobs(context, kind=kind)
        _shared._append_job_update(context, kind=kind, item_id=job_id, action="deleted", path=entry.path)
        return {"deleted": True, "id": job_id, "kind": kind}

    @router.patch("/jobs/{job_id}", tags=["jobs"], summary="Update Job")
    async def update_job(
        request: Request,
        job_id: str,
        payload: _shared.JobPatchRequest,
    ) -> dict[str, object]:
        context = request.app.state.runtime
        kind, entry = _shared._find_job_or_404(context, job_id)
        return _shared._update_job(context, kind=kind, entry=entry, payload=payload)

    @router.post("/tasks", tags=["jobs"], summary="Create Task", status_code=201)
    async def create_task(
        request: Request,
        payload: _shared.TaskCreateRequest,
    ) -> dict[str, object]:
        context = request.app.state.runtime
        document = _shared._task_document_from_create(context, payload)
        path = _shared.work.task_path(context.root, context.name, document.task_id())
        document.save(path)
        _reconcile_jobs(context, kind="task")
        _shared._append_job_update(context, kind="task", item_id=document.task_id(), action="created", path=path)
        entry = _shared._find_task_or_404(context, document.task_id())
        return {"item": _shared._task_detail_item(context, entry)}

    @router.patch("/tasks/archived/{task_id}", tags=["jobs"], summary="Update Archived Task")
    async def update_archived_task(
        request: Request,
        task_id: str,
        payload: _shared.TaskPatchRequest,
    ) -> dict[str, object]:
        context = request.app.state.runtime
        entry = _shared._find_archived_task_or_404(context, task_id)
        return _shared._update_task(context, entry=entry, payload=payload)

    @router.delete("/tasks/archived/{task_id}", tags=["jobs"], summary="Delete Archived Task")
    async def delete_archived_task(request: Request, task_id: str) -> dict[str, object]:
        context = request.app.state.runtime
        entry = _shared._find_archived_task_or_404(context, task_id)
        if not _shared.work.remove_archived_task(context.root, context.name, task_id):
            raise HTTPException(status_code=404, detail=f"archived task not found: {task_id}")
        _reconcile_jobs(context, kind="task")
        _shared._append_job_update(context, kind="task", item_id=task_id, action="deleted", path=entry.path)
        return {"deleted": True, "id": task_id, "kind": "task"}

    @router.patch("/tasks/{task_id}", tags=["jobs"], summary="Update Task")
    async def update_task(
        request: Request,
        task_id: str,
        payload: _shared.TaskPatchRequest,
    ) -> dict[str, object]:
        context = request.app.state.runtime
        entry = _shared._find_task_or_404(context, task_id)
        return _shared._update_task(context, entry=entry, payload=payload)

    @router.post("/tasks/{task_id}/draft", tags=["jobs"], summary="Draft Task")
    async def draft_task(request: Request, task_id: str) -> dict[str, object]:
        context = request.app.state.runtime
        path = _shared.work.draft_task(context.root, context.name, task_id)
        if path is None:
            raise HTTPException(status_code=404, detail=f"task not found: {task_id}")
        _reconcile_jobs(context, kind="task")
        _shared._append_job_update(context, kind="task", item_id=task_id, action="drafted", path=path)
        entry = _shared.work.find_task(context.root, context.name, task_id, lifecycle="draft")
        if entry is None:
            raise HTTPException(status_code=404, detail=f"task not found after draft: {task_id}")
        return {"item": _shared._task_detail_item(context, entry)}

    @router.post("/tasks/{task_id}/ready", tags=["jobs"], summary="Ready Task")
    async def ready_task(request: Request, task_id: str) -> dict[str, object]:
        context = request.app.state.runtime
        path = _shared.work.ready_task(context.root, context.name, task_id)
        if path is None:
            raise HTTPException(status_code=404, detail=f"task not found: {task_id}")
        _reconcile_jobs(context, kind="task")
        _shared._append_job_update(context, kind="task", item_id=task_id, action="ready", path=path)
        entry = _shared._find_task_or_404(context, task_id)
        return {"item": _shared._task_detail_item(context, entry)}

    @router.post("/tasks/{task_id}/archive", tags=["jobs"], summary="Archive Task")
    async def archive_task(request: Request, task_id: str) -> dict[str, object]:
        context = request.app.state.runtime
        path = _shared.work.archive_task(context.root, context.name, task_id)
        if path is None:
            raise HTTPException(status_code=404, detail=f"task not found: {task_id}")
        _reconcile_jobs(context, kind="task")
        _shared._append_job_update(context, kind="task", item_id=task_id, action="archived", path=path)
        entry = _shared._find_archived_task_or_404(context, task_id)
        return {"item": _shared._task_detail_item(context, entry)}

    @router.post("/tasks/{task_id}/reopen", tags=["jobs"], summary="Reopen Task")
    async def reopen_task(request: Request, task_id: str) -> dict[str, object]:
        context = request.app.state.runtime
        store = jobs.open_job_store(context.root, context.name)
        try:
            record = store.reopen_task(
                toolang_root=context.root,
                agent_name=context.name,
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
        context = request.app.state.runtime
        store = jobs.open_job_store(context.root, context.name)
        try:
            store.reconcile(toolang_root=context.root, agent_name=context.name, kind="task")
            record = store.get(job_id=task_id, kind="task")
            if record is None:
                raise HTTPException(status_code=404, detail=f"task not found: {task_id}")
            if record.status == "todo":
                return {"job": store.cancel_pending_task(task_id=task_id)}
            if record.status == "running" and record.last_run_id is not None:
                context.store.cancel_run(run_id=record.last_run_id)
                return {"job": record, "run_id": record.last_run_id}
            raise HTTPException(status_code=409, detail=f"task cannot be canceled from status: {record.status}")
        finally:
            store.close()

    @router.post("/chores", tags=["jobs"], summary="Create Chore", status_code=201)
    async def create_chore(
        request: Request,
        payload: _shared.ChoreCreateRequest,
    ) -> dict[str, object]:
        context = request.app.state.runtime
        document = _shared._chore_document_from_create(context, payload)
        path = _shared.work.chore_path(context.root, context.name, document.chore_id())
        document.save(path)
        _reconcile_jobs(context, kind="chore")
        _shared._append_job_update(context, kind="chore", item_id=document.chore_id(), action="created", path=path)
        entry = _shared._find_chore_or_404(context, document.chore_id())
        return {"item": _shared._chore_detail_item(context, entry)}

    @router.post("/chores/{chore_id}/run", tags=["jobs"], summary="Run Chore")
    async def run_chore(request: Request, chore_id: str) -> dict[str, object]:
        context = request.app.state.runtime
        store = jobs.open_job_store(context.root, context.name)
        try:
            claimed = store.claim_chore_manual(
                toolang_root=context.root,
                agent_name=context.name,
                chore_id=chore_id,
                run_id=_shared.allocate_run_id(context),
            )
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        finally:
            store.close()
        context.runner.enqueue(
            _shared.RunRequest(
                group="pulse:chore",
                origin="chore",
                run_id=claimed.run_id,
                thread_id=claimed.job.thread_id,
                thunk=claimed.text,
                metadata={"job_trigger": "manual"},
            )
        )
        return {"run_id": claimed.run_id, "job": claimed.job}

    @router.post("/chores/{chore_id}/cancel", tags=["jobs"], summary="Cancel Chore")
    async def cancel_chore(request: Request, chore_id: str) -> dict[str, object]:
        context = request.app.state.runtime
        store = jobs.open_job_store(context.root, context.name)
        try:
            store.reconcile(toolang_root=context.root, agent_name=context.name, kind="chore")
            record = store.get(job_id=chore_id, kind="chore")
            if record is None:
                raise HTTPException(status_code=404, detail=f"chore not found: {chore_id}")
            if record.status == "running" and record.last_run_id is not None:
                context.store.cancel_run(run_id=record.last_run_id)
                return {"job": record, "run_id": record.last_run_id}
            raise HTTPException(status_code=409, detail=f"chore cannot be canceled from status: {record.status}")
        finally:
            store.close()

    @router.patch("/chores/archived/{chore_id}", tags=["jobs"], summary="Update Archived Chore")
    async def update_archived_chore(
        request: Request,
        chore_id: str,
        payload: _shared.ChorePatchRequest,
    ) -> dict[str, object]:
        context = request.app.state.runtime
        entry = _shared._find_archived_chore_or_404(context, chore_id)
        return _shared._update_chore(context, entry=entry, payload=payload)

    @router.delete("/chores/archived/{chore_id}", tags=["jobs"], summary="Delete Archived Chore")
    async def delete_archived_chore(request: Request, chore_id: str) -> dict[str, object]:
        context = request.app.state.runtime
        entry = _shared._find_archived_chore_or_404(context, chore_id)
        if not _shared.work.remove_archived_chore(context.root, context.name, chore_id):
            raise HTTPException(status_code=404, detail=f"archived chore not found: {chore_id}")
        _reconcile_jobs(context, kind="chore")
        _shared._append_job_update(context, kind="chore", item_id=chore_id, action="deleted", path=entry.path)
        return {"deleted": True, "id": chore_id, "kind": "chore"}

    @router.patch("/chores/{chore_id}", tags=["jobs"], summary="Update Chore")
    async def update_chore(
        request: Request,
        chore_id: str,
        payload: _shared.ChorePatchRequest,
    ) -> dict[str, object]:
        context = request.app.state.runtime
        entry = _shared._find_chore_or_404(context, chore_id)
        return _shared._update_chore(context, entry=entry, payload=payload)

    @router.post("/chores/{chore_id}/draft", tags=["jobs"], summary="Draft Chore")
    async def draft_chore(request: Request, chore_id: str) -> dict[str, object]:
        context = request.app.state.runtime
        path = _shared.work.draft_chore(context.root, context.name, chore_id)
        if path is None:
            raise HTTPException(status_code=404, detail=f"chore not found: {chore_id}")
        _reconcile_jobs(context, kind="chore")
        _shared._append_job_update(context, kind="chore", item_id=chore_id, action="drafted", path=path)
        entry = _shared.work.find_chore(context.root, context.name, chore_id, lifecycle="draft")
        if entry is None:
            raise HTTPException(status_code=404, detail=f"chore not found after draft: {chore_id}")
        return {"item": _shared._chore_detail_item(context, entry)}

    @router.post("/chores/{chore_id}/ready", tags=["jobs"], summary="Ready Chore")
    async def ready_chore(request: Request, chore_id: str) -> dict[str, object]:
        context = request.app.state.runtime
        path = _shared.work.ready_chore(context.root, context.name, chore_id)
        if path is None:
            raise HTTPException(status_code=404, detail=f"chore not found: {chore_id}")
        _reconcile_jobs(context, kind="chore")
        _shared._append_job_update(context, kind="chore", item_id=chore_id, action="ready", path=path)
        entry = _shared._find_chore_or_404(context, chore_id)
        return {"item": _shared._chore_detail_item(context, entry)}

    @router.post("/chores/{chore_id}/archive", tags=["jobs"], summary="Archive Chore")
    async def archive_chore(request: Request, chore_id: str) -> dict[str, object]:
        context = request.app.state.runtime
        path = _shared.work.archive_chore(context.root, context.name, chore_id)
        if path is None:
            raise HTTPException(status_code=404, detail=f"chore not found: {chore_id}")
        _reconcile_jobs(context, kind="chore")
        _shared._append_job_update(context, kind="chore", item_id=chore_id, action="archived", path=path)
        entry = _shared._find_archived_chore_or_404(context, chore_id)
        return {"item": _shared._chore_detail_item(context, entry)}

    return router


def _reconcile_jobs(context, *, kind: _shared.JobKind) -> None:
    store = jobs.open_job_store(context.root, context.name)
    try:
        store.reconcile(toolang_root=context.root, agent_name=context.name, kind=kind)
    finally:
        store.close()
