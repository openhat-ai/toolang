"""Formal job control API routes."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from ..inspect import _shared


def create_router() -> APIRouter:
    """Build the formal job control route group."""

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

    @router.post("/chores", tags=["jobs"], summary="Create Chore", status_code=201)
    async def create_chore(
        request: Request,
        payload: _shared.ChoreCreateRequest,
    ) -> dict[str, object]:
        context = request.app.state.runtime
        document = _shared._chore_document_from_create(context, payload)
        path = _shared.work.chore_path(context.root, context.name, document.chore_id())
        document.save(path)
        _shared._append_job_update(context, kind="chore", item_id=document.chore_id(), action="created", path=path)
        entry = _shared._find_chore_or_404(context, document.chore_id())
        return {"item": _shared._chore_detail_item(context, entry)}

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

    return router
