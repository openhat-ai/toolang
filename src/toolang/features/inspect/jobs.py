"""Formal job inspection routes."""

from __future__ import annotations

from fastapi import APIRouter, Request

from . import _shared


def create_router() -> APIRouter:
    """Build the formal job inspection route group."""

    router = APIRouter(prefix="/api/v1")

    @router.get("/jobs", tags=["jobs"], summary="List Jobs")
    async def jobs(request: Request, kind: _shared.JobKind | None = None) -> dict[str, object]:
        context = request.app.state.runtime
        items = _shared._job_collection(context, archived=False)
        if kind is not None:
            items = [item for item in items if item["kind"] == kind]
        return {"items": items}

    @router.get("/jobs/archived", tags=["jobs"], summary="List Archived Jobs")
    async def archived_jobs(request: Request, kind: _shared.JobKind | None = None) -> dict[str, object]:
        context = request.app.state.runtime
        items = _shared._job_collection(context, archived=True)
        if kind is not None:
            items = [item for item in items if item["kind"] == kind]
        return {"items": items}

    @router.get("/jobs/archived/{job_id}", tags=["jobs"], summary="Get Archived Job")
    async def archived_job_detail(request: Request, job_id: str) -> dict[str, object]:
        context = request.app.state.runtime
        kind, entry = _shared._find_archived_job_or_404(context, job_id)
        return {"item": _shared._job_detail_item(context, kind=kind, entry=entry)}

    @router.get("/jobs/{job_id}", tags=["jobs"], summary="Get Job")
    async def job_detail(request: Request, job_id: str) -> dict[str, object]:
        context = request.app.state.runtime
        kind, entry = _shared._find_job_or_404(context, job_id)
        return {"item": _shared._job_detail_item(context, kind=kind, entry=entry)}

    @router.get("/tasks", tags=["jobs"], summary="List Tasks")
    async def tasks(request: Request) -> dict[str, object]:
        context = request.app.state.runtime
        return {"items": _shared._task_collection(context, archived=False)}

    @router.get("/tasks/archived", tags=["jobs"], summary="List Archived Tasks")
    async def archived_tasks(request: Request) -> dict[str, object]:
        context = request.app.state.runtime
        return {"items": _shared._task_collection(context, archived=True)}

    @router.get("/tasks/archived/{task_id}", tags=["jobs"], summary="Get Archived Task")
    async def archived_task_detail(request: Request, task_id: str) -> dict[str, object]:
        context = request.app.state.runtime
        entry = _shared._find_archived_task_or_404(context, task_id)
        return {"item": _shared._task_detail_item(context, entry)}

    @router.get("/tasks/{task_id}", tags=["jobs"], summary="Get Task")
    async def task_detail(request: Request, task_id: str) -> dict[str, object]:
        context = request.app.state.runtime
        entry = _shared._find_task_or_404(context, task_id)
        return {"item": _shared._task_detail_item(context, entry)}

    @router.get("/chores", tags=["jobs"], summary="List Chores")
    async def chores(request: Request) -> dict[str, object]:
        context = request.app.state.runtime
        return {"items": _shared._chore_collection(context, archived=False)}

    @router.get("/chores/archived", tags=["jobs"], summary="List Archived Chores")
    async def archived_chores(request: Request) -> dict[str, object]:
        context = request.app.state.runtime
        return {"items": _shared._chore_collection(context, archived=True)}

    @router.get("/chores/archived/{chore_id}", tags=["jobs"], summary="Get Archived Chore")
    async def archived_chore_detail(request: Request, chore_id: str) -> dict[str, object]:
        context = request.app.state.runtime
        entry = _shared._find_archived_chore_or_404(context, chore_id)
        return {"item": _shared._chore_detail_item(context, entry)}

    @router.get("/chores/{chore_id}", tags=["jobs"], summary="Get Chore")
    async def chore_detail(request: Request, chore_id: str) -> dict[str, object]:
        context = request.app.state.runtime
        entry = _shared._find_chore_or_404(context, chore_id)
        return {"item": _shared._chore_detail_item(context, entry)}

    @router.get("/will", tags=["jobs"], summary="Get Will")
    async def will() -> dict[str, object]:
        return {"item": None}

    return router
