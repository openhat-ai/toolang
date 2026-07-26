"""Capability inspection and management routes."""

from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import TypeAdapter

from toolang.api.app import AgentCoreDep, CapsManagerDep
from toolang.api.schemas import PutCapRequest, WiredCapRequest
from toolang.up import AgentCore
from toolang.catalog import cap as caps, templates
from toolang.catalog import config as cap_config
from toolang.catalog.types import CapKind
from toolang.state import state as cap_state
from toolang.state.schemas import CapDetail, CapInfo
from toolang.state.state import PreparedCap, PreparedVisibility

ApiVisibility = Literal["private", "shared"]

_CAP_INFOS = TypeAdapter(tuple[CapInfo, ...])

COLLECTION_TO_KIND: dict[str, CapKind] = {
    "psyches": "psyche",
    "skills": "skill",
    "services": "service",
    "prompts": "prompt",
}


router = APIRouter(tags=["caps"])


@router.put(
    "/prompts/{name}/file", summary="Upsert File Prompt", response_model=CapDetail
)
@router.put(
    "/services/{name}/file", summary="Upsert File Service", response_model=CapDetail
)
@router.put(
    "/skills/{name}/file", summary="Upsert File Skill", response_model=CapDetail
)
@router.put(
    "/psyches/{name}/file", summary="Upsert File Psyche", response_model=CapDetail
)
def put_file_cap(
    core: AgentCoreDep,
    manager: CapsManagerDep,
    request: Request,
    name: str,
    payload: PutCapRequest,
) -> CapDetail:
    kind = _collection_kind(_collection_from_path(str(request.url.path)))
    visibility = payload.visibility
    if payload.content is None:
        raise HTTPException(status_code=400, detail="missing cap content")

    catalog = _authored_caps(
        manager.home_authoring, manager.root_authoring, visibility
    )
    cap = _wrap_user_error(
        caps.CapFile.parse,
        payload.content or "",
        kind=kind,
        name=name,
    )
    _wrap_user_error(catalog.upsert, cap)
    entry = _find_authored_entry(
        core, visibility=visibility, kind=kind, name=name
    )
    return CapDetail.from_cap(entry, agent_name=core.layout.name)


@router.put("/prompts/{name}/wired", summary="Wire Prompt", response_model=CapDetail)
@router.put("/services/{name}/wired", summary="Wire Service", response_model=CapDetail)
@router.put("/skills/{name}/wired", summary="Wire Skill", response_model=CapDetail)
@router.put("/psyches/{name}/wired", summary="Wire Psyche", response_model=CapDetail)
def put_wired_cap(
    core: AgentCoreDep,
    manager: CapsManagerDep,
    request: Request,
    name: str,
    payload: WiredCapRequest,
) -> CapDetail:
    kind = _collection_kind(_collection_from_path(str(request.url.path)))
    visibility = payload.visibility
    canonical_ref = _wrap_user_error(cap_state.resolve_remote_ref, kind, payload.ref)
    if cap_state.remote_entry_name(kind, canonical_ref) != name:
        raise HTTPException(
            status_code=400,
            detail=f"Wired {kind} ref {payload.ref!r} does not match requested name {name!r}.",
        )
    catalog = _wired_caps(
        manager.home_wiring, manager.root_wiring, visibility
    )
    cap = cap_config.CapRef(kind=kind, name=name, ref=canonical_ref)
    _wrap_user_error(catalog.upsert, cap)
    entry = _find_authored_entry(
        core, visibility=visibility, kind=kind, name=name
    )
    return CapDetail.from_cap(entry, agent_name=core.layout.name)


@router.delete(
    "/prompts/{name}/file",
    summary="Delete File Prompt",
    status_code=204,
    response_class=Response,
)
@router.delete(
    "/services/{name}/file",
    summary="Delete File Service",
    status_code=204,
    response_class=Response,
)
@router.delete(
    "/skills/{name}/file",
    summary="Delete File Skill",
    status_code=204,
    response_class=Response,
)
@router.delete(
    "/psyches/{name}/file",
    summary="Delete File Psyche",
    status_code=204,
    response_class=Response,
)
def delete_file_cap(
    manager: CapsManagerDep,
    request: Request,
    name: str,
    visibility: ApiVisibility = Query(default="private"),
) -> None:
    kind = _collection_kind(_collection_from_path(str(request.url.path)))
    requested_visibility = visibility
    _wrap_user_error(
        _authored_caps(
            manager.home_authoring,
            manager.root_authoring,
            requested_visibility,
        ).remove,
        kind,
        name,
    )


@router.delete(
    "/prompts/{name}/wired",
    summary="Unwire Prompt",
    status_code=204,
    response_class=Response,
)
@router.delete(
    "/services/{name}/wired",
    summary="Unwire Service",
    status_code=204,
    response_class=Response,
)
@router.delete(
    "/skills/{name}/wired",
    summary="Unwire Skill",
    status_code=204,
    response_class=Response,
)
@router.delete(
    "/psyches/{name}/wired",
    summary="Unwire Psyche",
    status_code=204,
    response_class=Response,
)
def delete_wired_cap(
    manager: CapsManagerDep,
    request: Request,
    name: str,
    visibility: ApiVisibility = Query(default="private"),
) -> None:
    kind = _collection_kind(_collection_from_path(str(request.url.path)))
    requested_visibility = visibility
    _wrap_user_error(
        _wired_caps(
            manager.home_wiring,
            manager.root_wiring,
            requested_visibility,
        ).remove,
        kind,
        name,
    )


@router.get("/caps", summary="Get Caps Summary")
def caps_summary(core: AgentCoreDep) -> dict[str, object]:
    entries = core.state.current().caps
    collections = {
        "psyches": _CAP_INFOS.dump_python(
            _cap_infos(entries, agent_name=core.layout.name, kind="psyche"),
            mode="json",
        ),
        "skills": _CAP_INFOS.dump_python(
            _cap_infos(entries, agent_name=core.layout.name, kind="skill"),
            mode="json",
        ),
        "services": _CAP_INFOS.dump_python(
            _cap_infos(entries, agent_name=core.layout.name, kind="service"),
            mode="json",
        ),
        "prompts": _CAP_INFOS.dump_python(
            _cap_infos(entries, agent_name=core.layout.name, kind="prompt"),
            mode="json",
        ),
    }
    return {
        "agent": core.layout.name,
        **collections,
        "counts": {key: len(value) for key, value in collections.items()},
    }


@router.get("/prompts", summary="List Prompts", response_model=list[CapInfo])
@router.get("/services", summary="List Services", response_model=list[CapInfo])
@router.get("/skills", summary="List Skills", response_model=list[CapInfo])
@router.get("/psyches", summary="List Psyches", response_model=list[CapInfo])
def cap_list(core: AgentCoreDep, request: Request) -> list[CapInfo]:
    kind = _collection_kind(str(request.url.path).rsplit("/", 1)[-1])
    return list(
        _cap_infos(
            core.state.current().caps,
            agent_name=core.layout.name,
            kind=kind,
        )
    )


@router.get("/prompts/templates", summary="List Prompt Templates")
@router.get("/services/templates", summary="List Service Templates")
@router.get("/skills/templates", summary="List Skill Templates")
@router.get("/psyches/templates", summary="List Psyche Templates")
def cap_template_list(request: Request) -> list[dict[str, object]]:
    collection = str(request.url.path).split("/")[3]
    kind = _collection_kind(collection)
    return [_template_summary(item) for item in templates.list_templates(kind)]


@router.get("/prompts/templates/{template_name}", summary="Get Prompt Template")
@router.get("/services/templates/{template_name}", summary="Get Service Template")
@router.get("/skills/templates/{template_name}", summary="Get Skill Template")
@router.get("/psyches/templates/{template_name}", summary="Get Psyche Template")
def cap_template_detail(
    request: Request, template_name: str
) -> dict[str, object]:
    collection = str(request.url.path).split("/")[3]
    kind = _collection_kind(collection)
    try:
        template = templates.load_template(kind, template_name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _template_detail(template)


@router.get("/prompts/{name}", summary="Get Prompt", response_model=CapDetail)
@router.get("/services/{name}", summary="Get Service", response_model=CapDetail)
@router.get("/skills/{name}", summary="Get Skill", response_model=CapDetail)
@router.get("/psyches/{name}", summary="Get Psyche", response_model=CapDetail)
def cap_detail(core: AgentCoreDep, request: Request, name: str) -> CapDetail:
    collection = str(request.url.path).split("/")[3]
    kind = _collection_kind(collection)
    entry = next(
        (
            entry
            for entry in core.state.current().caps
            if entry.kind == kind and entry.name == name
        ),
        None,
    )
    if entry is None:
        raise HTTPException(status_code=404, detail=f"{kind} not found: {name}")
    return CapDetail.from_cap(entry, agent_name=core.layout.name)


def _collection_kind(collection: str) -> CapKind:
    kind = COLLECTION_TO_KIND.get(collection)
    if kind is None:
        raise HTTPException(
            status_code=404, detail=f"unsupported cap collection: {collection}"
        )
    return kind


def _authored_caps(
    private: caps.AuthoredCaps,
    shared: caps.AuthoredCaps,
    visibility: ApiVisibility,
) -> caps.AuthoredCaps:
    if visibility == "shared":
        return shared
    return private


def _wired_caps(
    private: cap_config.WiredCaps,
    shared: cap_config.WiredCaps,
    visibility: ApiVisibility,
) -> cap_config.WiredCaps:
    if visibility == "shared":
        return shared
    return private


def _collection_from_path(path: str) -> str:
    parts = [part for part in path.split("/") if part]
    if len(parts) < 3:
        raise HTTPException(status_code=404, detail=f"unsupported cap path: {path}")
    return parts[2]


def _find_authored_entry(
    core: AgentCore,
    *,
    visibility: PreparedVisibility,
    kind: CapKind,
    name: str,
) -> PreparedCap:
    for entry in cap_state.list_entries(
        core.layout.root,
        core.layout.name,
        visibility=visibility,
        kinds={kind},
    ):
        if entry.name == name:
            return entry
    raise HTTPException(status_code=404, detail=f"{kind} not found: {name}")


def _cap_infos(
    entries: tuple[PreparedCap, ...], *, agent_name: str, kind: CapKind
) -> tuple[CapInfo, ...]:
    return tuple(
        CapInfo.from_cap(entry, agent_name=agent_name)
        for entry in entries
        if entry.kind == kind
    )


def _template_summary(template: templates.TemplateSpec) -> dict[str, object]:
    return {
        "kind": template.kind,
        "name": template.name,
        "title": template.title,
        "description": template.description,
        "path": template.path,
    }


def _template_detail(template: templates.TemplateSpec) -> dict[str, object]:
    return {**_template_summary(template), "content": template.raw_text}


def _wrap_user_error(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
