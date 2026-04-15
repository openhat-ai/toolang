"""Formal capability definition API routes."""

from __future__ import annotations

from typing import Literal, cast

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from .. import caps
from ..state.prepared import EntryKind, PreparedEntry, PreparedScope

CapKind = Literal["psyche", "skill", "service", "prompt"]
ApiScope = Literal["global", "agent", "shared"]

COLLECTION_TO_KIND: dict[str, CapKind] = {
    "psyches": "psyche",
    "skills": "skill",
    "services": "service",
    "prompts": "prompt",
}


class PutCapRequest(BaseModel):
    """One authored cap write request."""

    scope: ApiScope = "agent"
    source: Literal["local", "remote"] | None = None
    ref: str | None = None
    content: str | None = None


def create_router() -> APIRouter:
    """Build the formal capability definition route group."""

    router = APIRouter(prefix="/api/v1", tags=["caps"])

    @router.put("/psyches/{name}", summary="Upsert Psyche")
    @router.put("/skills/{name}", summary="Upsert Skill")
    @router.put("/services/{name}", summary="Upsert Service")
    @router.put("/prompts/{name}", summary="Upsert Prompt")
    async def put_cap(
        request: Request,
        name: str,
        payload: PutCapRequest,
    ) -> dict[str, object]:
        context = request.app.state.runtime
        kind = _collection_kind(_collection_from_path(str(request.url.path)))
        scope = _scope(payload.scope)
        if payload.ref and payload.content:
            raise HTTPException(status_code=400, detail="provide either ref or content, not both")
        if not payload.ref and not payload.content:
            raise HTTPException(status_code=400, detail="missing cap payload")

        if payload.ref is not None:
            if caps.remote_entry_name(cast(EntryKind, kind), payload.ref) != name:
                raise HTTPException(
                    status_code=400,
                    detail=f"Remote {kind} ref {payload.ref!r} does not match requested name {name!r}.",
                )
            _wrap_user_error(
                caps.add_remote_entry,
                context.root,
                context.name,
                scope=scope,
                kind=cast(EntryKind, kind),
                ref=payload.ref,
            )
            _append_cap_update(context, kind=kind, name=name, scope=scope)
            item = _written_item(
                kind=kind,
                name=name,
                scope=scope,
                source="remote",
                ref=payload.ref,
                path=str(_config_path(context.root, context.name, scope)),
            )
            return {"item": item}

        _wrap_user_error(
            caps.put_local_entry_text,
            context.root,
            context.name,
            scope=scope,
            kind=cast(EntryKind, kind),
            name=name,
            text=payload.content or "",
        )
        _append_cap_update(context, kind=kind, name=name, scope=scope)
        entry = _find_authored_entry(context, scope=scope, kind=kind, name=name)
        return {"item": _cap_detail_item(context, entry)}

    @router.delete("/psyches/{name}", summary="Delete Psyche")
    @router.delete("/skills/{name}", summary="Delete Skill")
    @router.delete("/services/{name}", summary="Delete Service")
    @router.delete("/prompts/{name}", summary="Delete Prompt")
    async def delete_cap(
        request: Request,
        name: str,
        scope: ApiScope = Query(default="agent"),
        source: Literal["local", "remote"] | None = Query(default=None),
    ) -> dict[str, object]:
        del source
        context = request.app.state.runtime
        kind = _collection_kind(_collection_from_path(str(request.url.path)))
        removed = _wrap_user_error(
            caps.remove_entry,
            context.root,
            context.name,
            scope=_scope(scope),
            kind=cast(EntryKind, kind),
            name=name,
        )
        if not removed:
            raise HTTPException(status_code=404, detail=f"{kind} not found: {name}")
        _append_cap_update(context, kind=kind, name=name, scope=_scope(scope))
        return {"ok": True}

    return router


def _scope(scope: ApiScope) -> PreparedScope:
    return "agent" if scope == "shared" else scope


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
    scope: PreparedScope,
    kind: CapKind,
    name: str,
) -> PreparedEntry:
    for entry in caps.list_entries(
        context.root,
        context.name,
        scope=scope,
        kinds={cast(EntryKind, kind)},
    ):
        if entry.name == name:
            return entry
    raise HTTPException(status_code=404, detail=f"{kind} not found: {name}")


def _cap_detail_item(context, entry: PreparedEntry) -> dict[str, object]:
    item: dict[str, object] = {
        "kind": entry.kind,
        "name": entry.name,
        "scope": _entry_scope(context, entry),
        "source": entry.source.form,
        "ref": entry.ref if entry.source.form == "remote" else None,
        "path": entry.path,
    }
    return item


def _written_item(
    *,
    kind: CapKind,
    name: str,
    scope: PreparedScope,
    source: str,
    ref: str,
    path: str,
) -> dict[str, object]:
    return {
        "kind": kind,
        "name": name,
        "scope": scope,
        "source": source,
        "path": path,
        "ref": ref if source == "remote" else None,
    }


def _append_cap_update(context, *, kind: CapKind, name: str, scope: PreparedScope) -> None:
    context.store.append_update(
        kind=cast(Literal["psyche_changed", "skill_changed", "service_changed", "prompt_changed"], f"{kind}_changed"),
        payload={
            "name": name,
            "scope": scope,
        },
    )


def _entry_scope(context, entry: PreparedEntry) -> PreparedScope:
    agent_prefix = f"agents/{context.name}/"
    return "agent" if entry.path.startswith(agent_prefix) else "global"


def _config_path(toolang_root, agent_name: str, scope: PreparedScope):
    if scope == "global":
        return toolang_root / "config.toml"
    return toolang_root / "agents" / agent_name / "config.toml"


def _wrap_user_error(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
