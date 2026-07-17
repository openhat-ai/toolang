"""Formal capability definition API routes."""

from __future__ import annotations

from typing import Literal, cast

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict

from toolang.catalog import cap as caps
from toolang.state import caps as cap_state
from toolang.state.prepared import PreparedEntry, PreparedVisibility

CapKind = Literal["psyche", "skill", "service", "prompt"]
ApiVisibility = Literal["private", "shared"]

COLLECTION_TO_KIND: dict[str, CapKind] = {
    "psyches": "psyche",
    "skills": "skill",
    "services": "service",
    "prompts": "prompt",
}


class PutCapRequest(BaseModel):
    """One authored cap write request."""

    model_config = ConfigDict(extra="forbid")

    visibility: ApiVisibility = "private"
    content: str | None = None


class WiredCapRequest(BaseModel):
    """One wired cap ref mutation request."""

    model_config = ConfigDict(extra="forbid")

    visibility: ApiVisibility = "private"
    ref: str


def create_router() -> APIRouter:
    """Build the formal capability definition route group."""

    router = APIRouter(prefix="/api/v1", tags=["caps"])

    @router.put("/psyches/{name}/file", summary="Upsert File Psyche")
    @router.put("/skills/{name}/file", summary="Upsert File Skill")
    @router.put("/services/{name}/file", summary="Upsert File Service")
    @router.put("/prompts/{name}/file", summary="Upsert File Prompt")
    async def put_file_cap(
        request: Request,
        name: str,
        payload: PutCapRequest,
    ) -> dict[str, object]:
        context = request.app.state.context
        kind = _collection_kind(_collection_from_path(str(request.url.path)))
        visibility = payload.visibility
        if payload.content is None:
            raise HTTPException(status_code=400, detail="missing cap content")

        catalog = caps.CapCatalog(context.root, context.name, visibility=visibility)
        operation = catalog.update if catalog.get(kind, name) else catalog.create
        _wrap_user_error(operation, kind, name, payload.content or "")
        _append_cap_update(context, kind=kind, name=name, visibility=visibility)
        entry = _find_authored_entry(context, visibility=visibility, kind=kind, name=name)
        return {"item": _cap_detail_item(context, entry)}

    @router.put("/psyches/{name}/wired", summary="Wire Psyche")
    @router.put("/skills/{name}/wired", summary="Wire Skill")
    @router.put("/services/{name}/wired", summary="Wire Service")
    @router.put("/prompts/{name}/wired", summary="Wire Prompt")
    async def put_wired_cap(
        request: Request,
        name: str,
        payload: WiredCapRequest,
    ) -> dict[str, object]:
        context = request.app.state.context
        kind = _collection_kind(_collection_from_path(str(request.url.path)))
        visibility = payload.visibility
        if caps.remote_entry_name(kind, payload.ref) != name:
            raise HTTPException(
                status_code=400,
                detail=f"Wired {kind} ref {payload.ref!r} does not match requested name {name!r}.",
            )
        _wrap_user_error(
            caps.add_remote_entry,
            context.root,
            context.name,
            visibility=visibility,
            kind=kind,
            ref=payload.ref,
        )
        _append_cap_update(context, kind=kind, name=name, visibility=visibility)
        entry = _find_authored_entry(context, visibility=visibility, kind=kind, name=name)
        return {"item": _cap_detail_item(context, entry)}

    @router.delete("/psyches/{name}/file", summary="Delete File Psyche")
    @router.delete("/skills/{name}/file", summary="Delete File Skill")
    @router.delete("/services/{name}/file", summary="Delete File Service")
    @router.delete("/prompts/{name}/file", summary="Delete File Prompt")
    async def delete_file_cap(
        request: Request,
        name: str,
        visibility: ApiVisibility = Query(default="private"),
    ) -> dict[str, object]:
        context = request.app.state.context
        kind = _collection_kind(_collection_from_path(str(request.url.path)))
        requested_visibility = visibility
        removed = _wrap_user_error(
            caps.CapCatalog(
                context.root, context.name, visibility=requested_visibility
            ).remove,
            kind,
            name,
        )
        if not removed:
            raise HTTPException(status_code=404, detail=f"file {kind} not found: {name}")
        _append_cap_update(context, kind=kind, name=name, visibility=requested_visibility)
        return {"ok": True}

    @router.delete("/psyches/{name}/wired", summary="Unwire Psyche")
    @router.delete("/skills/{name}/wired", summary="Unwire Skill")
    @router.delete("/services/{name}/wired", summary="Unwire Service")
    @router.delete("/prompts/{name}/wired", summary="Unwire Prompt")
    async def delete_wired_cap(
        request: Request,
        name: str,
        visibility: ApiVisibility = Query(default="private"),
    ) -> dict[str, object]:
        context = request.app.state.context
        kind = _collection_kind(_collection_from_path(str(request.url.path)))
        requested_visibility = visibility
        removed = _wrap_user_error(
            caps.remove_remote_entry,
            context.root,
            context.name,
            visibility=requested_visibility,
            kind=kind,
            name=name,
        )
        if not removed:
            raise HTTPException(status_code=404, detail=f"wired {kind} not found: {name}")
        _append_cap_update(context, kind=kind, name=name, visibility=requested_visibility)
        return {"ok": True}

    return router


def _collection_kind(collection: str) -> CapKind:
    kind = COLLECTION_TO_KIND.get(collection)
    if kind is None:
        raise HTTPException(status_code=404, detail=f"unsupported cap collection: {collection}")
    return kind


def _collection_from_path(path: str) -> str:
    parts = [part for part in path.split("/") if part]
    if len(parts) < 3:
        raise HTTPException(status_code=404, detail=f"unsupported cap path: {path}")
    return parts[2]


def _find_authored_entry(
    context,
    *,
    visibility: PreparedVisibility,
    kind: CapKind,
    name: str,
) -> PreparedEntry:
    for entry in cap_state.list_entries(
        context.root,
        context.name,
        visibility=visibility,
        kinds={kind},
    ):
        if entry.name == name:
            return entry
    raise HTTPException(status_code=404, detail=f"{kind} not found: {name}")


def _cap_detail_item(context, entry: PreparedEntry) -> dict[str, object]:
    item: dict[str, object] = {
        "kind": entry.kind,
        "name": entry.name,
        "scope": cap_state.entry_scope(entry, agent_name=context.name),
        "origin": cap_state.entry_origin(entry),
        "form": cap_state.entry_form(entry),
        "ref": cap_state.entry_ref(entry, agent_name=context.name),
        "definition_file": cap_state.entry_definition_file(entry),
    }
    line = cap_state.entry_line(entry)
    if line is not None:
        item["line"] = line
    return item


def _append_cap_update(context, *, kind: CapKind, name: str, visibility: PreparedVisibility) -> None:
    event_type = cast(Literal["psyche_changed", "skill_changed", "service_changed", "prompt_changed"], f"{kind}_changed")
    payload = {
        "name": name,
        "visibility": visibility,
    }
    context.store.append_update(
        kind=event_type,
        payload=payload,
    )
    context.store.append_event(
        domain="agent",
        domain_id=context.name,
        type=event_type,
        payload=payload,
    )


def _wrap_user_error(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
