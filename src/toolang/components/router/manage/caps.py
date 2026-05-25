"""Formal capability definition API routes."""

from __future__ import annotations

from typing import Literal, cast

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict

from .... import caps
from ....state.prepared import EntryKind, PreparedEntry, PreparedVisibility

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


class RemoteCapRequest(BaseModel):
    """One remote cap ref mutation request."""

    model_config = ConfigDict(extra="forbid")

    visibility: ApiVisibility = "private"
    ref: str


def create_router() -> APIRouter:
    """Build the formal capability definition route group."""

    router = APIRouter(prefix="/api/v1", tags=["caps"])

    @router.put("/psyches/{name}/local", summary="Upsert Local Psyche")
    @router.put("/skills/{name}/local", summary="Upsert Local Skill")
    @router.put("/services/{name}/local", summary="Upsert Local Service")
    @router.put("/prompts/{name}/local", summary="Upsert Local Prompt")
    async def put_local_cap(
        request: Request,
        name: str,
        payload: PutCapRequest,
    ) -> dict[str, object]:
        context = request.app.state.runtime
        kind = _collection_kind(_collection_from_path(str(request.url.path)))
        visibility = payload.visibility
        if payload.content is None:
            raise HTTPException(status_code=400, detail="missing cap content")

        _wrap_user_error(
            caps.put_local_entry_text,
            context.root,
            context.name,
            visibility=visibility,
            kind=cast(EntryKind, kind),
            name=name,
            text=payload.content or "",
        )
        _append_cap_update(context, kind=kind, name=name, visibility=visibility)
        entry = _find_authored_entry(context, visibility=visibility, kind=kind, name=name)
        return {"item": _cap_detail_item(context, entry)}

    @router.put("/psyches/{name}/remote", summary="Add Remote Psyche")
    @router.put("/skills/{name}/remote", summary="Add Remote Skill")
    @router.put("/services/{name}/remote", summary="Add Remote Service")
    @router.put("/prompts/{name}/remote", summary="Add Remote Prompt")
    async def put_remote_cap(
        request: Request,
        name: str,
        payload: RemoteCapRequest,
    ) -> dict[str, object]:
        context = request.app.state.runtime
        kind = _collection_kind(_collection_from_path(str(request.url.path)))
        visibility = payload.visibility
        if caps.remote_entry_name(cast(EntryKind, kind), payload.ref) != name:
            raise HTTPException(
                status_code=400,
                detail=f"Remote {kind} ref {payload.ref!r} does not match requested name {name!r}.",
            )
        _wrap_user_error(
            caps.add_remote_entry,
            context.root,
            context.name,
            visibility=visibility,
            kind=cast(EntryKind, kind),
            ref=payload.ref,
        )
        _append_cap_update(context, kind=kind, name=name, visibility=visibility)
        entry = _find_authored_entry(context, visibility=visibility, kind=kind, name=name)
        return {"item": _cap_detail_item(context, entry)}

    @router.delete("/psyches/{name}/local", summary="Delete Local Psyche")
    @router.delete("/skills/{name}/local", summary="Delete Local Skill")
    @router.delete("/services/{name}/local", summary="Delete Local Service")
    @router.delete("/prompts/{name}/local", summary="Delete Local Prompt")
    async def delete_local_cap(
        request: Request,
        name: str,
        visibility: ApiVisibility = Query(default="private"),
    ) -> dict[str, object]:
        context = request.app.state.runtime
        kind = _collection_kind(_collection_from_path(str(request.url.path)))
        requested_visibility = visibility
        removed = _wrap_user_error(
            caps.remove_local_entry,
            context.root,
            context.name,
            visibility=requested_visibility,
            kind=cast(EntryKind, kind),
            name=name,
        )
        if not removed:
            raise HTTPException(status_code=404, detail=f"local {kind} not found: {name}")
        _append_cap_update(context, kind=kind, name=name, visibility=requested_visibility)
        return {"ok": True}

    @router.delete("/psyches/{name}/remote", summary="Remove Remote Psyche")
    @router.delete("/skills/{name}/remote", summary="Remove Remote Skill")
    @router.delete("/services/{name}/remote", summary="Remove Remote Service")
    @router.delete("/prompts/{name}/remote", summary="Remove Remote Prompt")
    async def delete_remote_cap(
        request: Request,
        name: str,
        visibility: ApiVisibility = Query(default="private"),
    ) -> dict[str, object]:
        context = request.app.state.runtime
        kind = _collection_kind(_collection_from_path(str(request.url.path)))
        requested_visibility = visibility
        removed = _wrap_user_error(
            caps.remove_remote_entry,
            context.root,
            context.name,
            visibility=requested_visibility,
            kind=cast(EntryKind, kind),
            name=name,
        )
        if not removed:
            raise HTTPException(status_code=404, detail=f"remote {kind} not found: {name}")
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
    for entry in caps.list_entries(
        context.root,
        context.name,
        visibility=visibility,
        kinds={cast(EntryKind, kind)},
    ):
        if entry.name == name:
            return entry
    raise HTTPException(status_code=404, detail=f"{kind} not found: {name}")


def _cap_detail_item(context, entry: PreparedEntry) -> dict[str, object]:
    item: dict[str, object] = {
        "kind": entry.kind,
        "name": entry.name,
        "scope": caps.entry_scope(entry, agent_name=context.name),
        "origin": caps.entry_origin(entry),
        "form": caps.entry_form(entry),
        "ref": caps.entry_ref(entry, agent_name=context.name),
        "definition_file": caps.entry_definition_file(entry),
    }
    line = caps.entry_line(entry)
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
    context.events.publish(
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
