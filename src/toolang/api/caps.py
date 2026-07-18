"""Formal cap inspection routes."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from . import _views


def create_router() -> APIRouter:
    """Build the formal cap inspection route group."""

    router = APIRouter(prefix="/api/v1")

    @router.get("/caps", tags=["caps"], summary="Get Caps Summary")
    async def caps_summary(request: Request) -> dict[str, object]:
        context = request.app.state.context
        collections = {
            "psyches": _views._cap_collection(context, kind="psyche"),
            "skills": _views._cap_collection(context, kind="skill"),
            "services": _views._cap_collection(context, kind="service"),
            "prompts": _views._cap_collection(context, kind="prompt"),
        }
        return {
            "agent": context.name,
            **collections,
            "counts": {key: len(value) for key, value in collections.items()},
        }

    @router.get("/psyches", tags=["caps"], summary="List Psyches")
    @router.get("/skills", tags=["caps"], summary="List Skills")
    @router.get("/services", tags=["caps"], summary="List Services")
    @router.get("/prompts", tags=["caps"], summary="List Prompts")
    async def cap_list(request: Request) -> dict[str, object]:
        context = request.app.state.context
        kind = _views._collection_kind(str(request.url.path).rsplit("/", 1)[-1])
        return {"items": _views._cap_collection(context, kind=kind)}

    @router.get("/psyches/templates", tags=["caps"], summary="List Psyche Templates")
    @router.get("/skills/templates", tags=["caps"], summary="List Skill Templates")
    @router.get("/services/templates", tags=["caps"], summary="List Service Templates")
    @router.get("/prompts/templates", tags=["caps"], summary="List Prompt Templates")
    async def cap_template_list(request: Request) -> dict[str, object]:
        collection = str(request.url.path).split("/")[3]
        kind = _views._collection_kind(collection)
        return {"items": [_views._template_summary(item) for item in _views.templates.list_templates(kind)]}

    @router.get("/psyches/templates/{template_name}", tags=["caps"], summary="Get Psyche Template")
    @router.get("/skills/templates/{template_name}", tags=["caps"], summary="Get Skill Template")
    @router.get("/services/templates/{template_name}", tags=["caps"], summary="Get Service Template")
    @router.get("/prompts/templates/{template_name}", tags=["caps"], summary="Get Prompt Template")
    async def cap_template_detail(request: Request, template_name: str) -> dict[str, object]:
        collection = str(request.url.path).split("/")[3]
        kind = _views._collection_kind(collection)
        try:
            template = _views.templates.load_template(kind, template_name)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"item": _views._template_detail(template)}

    @router.get("/psyches/{name}", tags=["caps"], summary="Get Psyche")
    @router.get("/skills/{name}", tags=["caps"], summary="Get Skill")
    @router.get("/services/{name}", tags=["caps"], summary="Get Service")
    @router.get("/prompts/{name}", tags=["caps"], summary="Get Prompt")
    async def cap_detail(request: Request, name: str) -> dict[str, object]:
        context = request.app.state.context
        collection = str(request.url.path).split("/")[3]
        kind = _views._collection_kind(collection)
        entry = _views._state_entry_by_name(context, kind=kind, name=name)
        return {"item": _views._cap_detail_item(context, entry)}

    return router
