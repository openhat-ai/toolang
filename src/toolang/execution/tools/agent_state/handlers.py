"""Operation-kind dispatch for compact current-agent authoring tools."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, NoReturn, cast

import frontmatter
from yaml import YAMLError

from toolang.base.errors import ToolFailure
from toolang.base.types.tool import ToolContext
from toolang.catalog import cap as caps
from toolang.catalog.errors import (
    CatalogConflictError,
    CatalogNotFoundError,
)
from toolang.catalog.job import AuthoredJobs, JobFile
from toolang.catalog.types import CapKind, DEFAULT_CHORE_SCHEDULE, JobKind
from toolang.common.immutable import mutable_data
from toolang.common.layout import AgentLayout
from toolang.state.errors import StatePreparationError
from toolang.state.state import flow_module_name
from toolang.work.authoring import (
    allocate_authored_job_id,
    assign_missing_authored_job_ids,
    new_job_file,
)
from toolang.work.state import job_thread_id

from .flows import AuthoredFlow, AuthoredFlows, DigestMismatchError
from .schemas import (
    JOB_KINDS,
    NAMED_KINDS,
    ResourceKind,
    ResourceRequest,
    fail,
    issue,
)
from .storage import (
    UnsafeAuthoringPathError,
    validate_cap_storage,
    validate_job_storage,
)


@dataclass(frozen=True, slots=True)
class AgentStateScope:
    """Resolved current-agent authoring scope."""

    layout: AgentLayout


def execute(request: ResourceRequest, context: ToolContext) -> dict[str, Any]:
    """Execute one validated compact current-agent request."""

    try:
        scope = _scope(context, request)
        _validate_resource_key(request)
        if request.operation == "list":
            return _list(scope, request.kind)
        if request.operation == "get":
            return _get(scope, request.kind, _key(request))
        if request.operation == "create":
            return _create(scope, request)
        if request.operation == "update":
            return _update(scope, request)
        return _delete(scope, request.kind, _key(request), request.if_digest)
    except ToolFailure:
        raise
    except DigestMismatchError as exc:
        fail(
            "digest_mismatch",
            str(exc),
            operation=request.operation,
            kind=request.kind,
            key=request.key,
            issues=(
                issue(
                    "digest-mismatch",
                    "if_digest",
                    f"expected {exc.expected}, found {exc.actual}",
                ),
            ),
        )
    except CatalogNotFoundError as exc:
        fail(
            "not_found",
            str(exc),
            operation=request.operation,
            kind=request.kind,
            key=request.key,
        )
    except CatalogConflictError:
        fail(
            "conflict",
            f"authored {request.kind} conflicts with existing content",
            operation=request.operation,
            kind=request.kind,
            key=request.key,
        )
    except StatePreparationError as exc:
        _fail_flow(request, exc)
    except UnsafeAuthoringPathError as exc:
        path = "key" if request.key is not None else "kind"
        fail(
            "storage_error",
            str(exc),
            operation=request.operation,
            kind=request.kind,
            key=request.key,
            issues=(issue("unsafe-storage", path, str(exc)),),
        )
    except YAMLError:
        path = "key" if request.key is not None else "kind"
        fail(
            "invalid_content",
            f"authored {request.kind} content is invalid",
            operation=request.operation,
            kind=request.kind,
            key=request.key,
            issues=(
                issue(
                    "invalid-frontmatter",
                    path,
                    "authored front matter is invalid",
                ),
            ),
        )
    except (TypeError, ValueError) as exc:
        if request.operation in {"create", "update"}:
            code = "invalid_flow" if request.kind == "flow" else "invalid_content"
            path = "content.source" if request.kind == "flow" else "content"
        else:
            code = "invalid_flow" if request.kind == "flow" else "invalid_content"
            path = "key" if request.key is not None else "kind"
        fail(
            code,
            str(exc) or type(exc).__name__,
            operation=request.operation,
            kind=request.kind,
            key=request.key,
            issues=(issue("invalid-value", path, str(exc) or type(exc).__name__),),
        )
    except OSError as exc:
        path = "key" if request.key is not None else "kind"
        fail(
            "storage_error",
            f"could not {request.operation} {request.kind}",
            operation=request.operation,
            kind=request.kind,
            key=request.key,
            issues=(issue("storage-error", path, type(exc).__name__),),
        )


def _list(scope: AgentStateScope, kind: ResourceKind) -> dict[str, Any]:
    if kind in JOB_KINDS:
        entries = _jobs(scope, assign_missing=True).list(kind=cast(JobKind, kind))
        items = [_job_item(scope, item, include_content=False) for item in entries]
    elif kind in NAMED_KINDS and kind != "flow":
        cap_kind = cast(CapKind, kind)
        catalog = _caps(scope, cap_kind)
        with catalog.write_lock():
            entries = catalog.list(kinds={cap_kind})
        items = [_cap_item(scope, item, include_content=False) for item in entries]
    else:
        items = [
            _flow_item(scope, item, include_content=False)
            for item in _flows(scope).list()
        ]
    return {"kind": kind, "items": items}


def _get(scope: AgentStateScope, kind: ResourceKind, key: str) -> dict[str, Any]:
    if kind in JOB_KINDS:
        item = _jobs(scope).get(cast(JobKind, kind), key)
        if item is None:
            raise CatalogNotFoundError(f"authored {kind} not found: {key}")
        payload = _job_item(scope, item, include_content=True)
    elif kind in NAMED_KINDS and kind != "flow":
        cap_kind = cast(CapKind, kind)
        catalog = _caps(scope, cap_kind, key=key)
        with catalog.write_lock():
            item = catalog.get(cap_kind, key)
        if item is None:
            raise CatalogNotFoundError(f"authored {kind} not found: {key}")
        payload = _cap_item(scope, item, include_content=True)
    else:
        item = _flows(scope).get(key)
        if item is None:
            raise CatalogNotFoundError(f"authored flow not found: {key}")
        payload = _flow_item(scope, item, include_content=True)
    return {"kind": kind, "item": payload}


def _create(scope: AgentStateScope, request: ResourceRequest) -> dict[str, Any]:
    content = _content(request)
    if request.kind in JOB_KINDS:
        kind = cast(JobKind, request.kind)
        candidate = new_job_file(
            kind=kind,
            job_id="pending",
            title=_blank_to_none(cast(str | None, content.get("title"))),
            body=cast(str, content["body"]),
            schedule=(
                cast(str, content.get("schedule", DEFAULT_CHORE_SCHEDULE))
                if request.kind == "chore"
                else None
            ),
        )
        catalog = _jobs(scope, allocator=True)
        job_id = allocate_authored_job_id(scope.layout, catalog=catalog)
        document = candidate.with_meta({**candidate.meta, "id": job_id})
        item = catalog.create(document)
        payload = _job_item(scope, item, include_content=True)
    elif request.kind in NAMED_KINDS and request.kind != "flow":
        kind = cast(CapKind, request.kind)
        key = _key(request)
        item = caps.CapFile.parse(
            _cap_text(kind, content),
            kind=kind,
            name=key,
        )
        item = _caps(scope, kind, key=key).create(item)
        payload = _cap_item(scope, item, include_content=True)
    else:
        item = _flows(scope).create(_key(request), cast(str, content["source"]))
        payload = _flow_item(scope, item, include_content=True)
    return {"kind": request.kind, "item": payload, "created": True}


def _update(scope: AgentStateScope, request: ResourceRequest) -> dict[str, Any]:
    content = _content(request)
    key = _key(request)
    if request.kind in JOB_KINDS:
        catalog = _jobs(scope)
        with catalog.write_lock():
            current = catalog.get(cast(JobKind, request.kind), key)
            if current is None:
                raise CatalogNotFoundError(f"authored {request.kind} not found: {key}")
            _check_digest(current, request.if_digest)
            changes: dict[str, str | None] = {}
            for name in ("title", "body", "schedule"):
                if name in content:
                    value = cast(str, content[name])
                    changes[name] = _blank_to_none(value) if name == "title" else value
            candidate = current.patch(changes)
            if candidate.content == current.content:
                saved, changed = current, False
            else:
                saved, changed = catalog.update(candidate), True
        payload = _job_item(scope, saved, include_content=True)
    elif request.kind in NAMED_KINDS and request.kind != "flow":
        kind = cast(CapKind, request.kind)
        catalog = _caps(scope, kind, key=key)
        with catalog.write_lock():
            current = catalog.get(kind, key)
            if current is None:
                raise CatalogNotFoundError(f"authored {kind} not found: {key}")
            _check_digest(current, request.if_digest)
            candidate = caps.CapFile.parse(
                _updated_cap_text(current, content),
                kind=kind,
                name=key,
            )
            if candidate.content == current.content:
                saved, changed = current, False
            else:
                saved, changed = catalog.update(candidate), True
        payload = _cap_item(scope, saved, include_content=True)
    else:
        saved, changed = _flows(scope).update(
            key,
            cast(str, content["source"]),
            if_digest=request.if_digest,
        )
        payload = _flow_item(scope, saved, include_content=True)
    return {"kind": request.kind, "item": payload, "changed": changed}


def _delete(
    scope: AgentStateScope,
    kind: ResourceKind,
    key: str,
    if_digest: str | None,
) -> dict[str, Any]:
    if kind == "flow":
        _flows(scope).delete(key, if_digest=if_digest)
    else:
        cap_kind = cast(CapKind, kind)
        catalog = _caps(
            scope,
            cap_kind,
            key=key,
            recursive=cap_kind == "skill",
        )
        with catalog.write_lock():
            current = catalog.get(cap_kind, key)
            if current is None:
                raise CatalogNotFoundError(f"authored {kind} not found: {key}")
            _check_digest(current, if_digest)
            catalog.remove(cap_kind, key)
    return {"kind": kind, "key": key, "deleted": True}


def _scope(context: ToolContext, request: ResourceRequest) -> AgentStateScope:
    authored_home = context.home.expanduser()
    unsafe_context = authored_home.is_symlink() or authored_home.parent.is_symlink()
    home = authored_home.resolve()
    if unsafe_context or home.parent.name != "agents" or not home.is_dir():
        fail(
            "invalid_request",
            "_me requires a current agent home",
            operation=request.operation,
            kind=request.kind,
            key=request.key,
            issues=(issue("invalid-context", "kind", "agent home required"),),
        )
    return AgentStateScope(
        AgentLayout(
            root=home.parent.parent,
            name=home.name,
            placement=context.placement,
        )
    )


def _jobs(
    scope: AgentStateScope,
    *,
    allocator: bool = False,
    assign_missing: bool = False,
) -> AuthoredJobs:
    validate_job_storage(
        scope.layout.home,
        allocator=allocator or assign_missing,
    )
    catalog = AuthoredJobs(scope.layout.home)
    if assign_missing:
        try:
            assign_missing_authored_job_ids(scope.layout, catalog=catalog)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
    return catalog


def _caps(
    scope: AgentStateScope,
    kind: CapKind,
    *,
    key: str | None = None,
    recursive: bool = False,
) -> caps.AuthoredCaps:
    validate_cap_storage(
        scope.layout.home,
        kind,
        key=key,
        recursive=recursive,
    )
    return caps.AuthoredCaps(scope.layout.home)


def _flows(scope: AgentStateScope) -> AuthoredFlows:
    return AuthoredFlows(scope.layout)


def _validate_resource_key(request: ResourceRequest) -> None:
    if request.kind != "flow" or request.key is None:
        return
    try:
        flow_module_name(f"flows/{request.key}.too")
    except ValueError as exc:
        fail(
            "invalid_request",
            str(exc),
            operation=request.operation,
            kind=request.kind,
            key=request.key,
            issues=(issue("invalid-key", "key", str(exc)),),
        )


def _job_item(
    scope: AgentStateScope,
    document: JobFile,
    *,
    include_content: bool,
) -> dict[str, Any]:
    path = _required_path(document.path)
    item: dict[str, Any] = {
        "key": document.id,
        "path": _home_path(scope, path),
        "digest": _content_digest(document.content),
        "title": document.title,
        "thread_id": job_thread_id(document),
    }
    if document.kind == "chore":
        item["schedule"] = document.schedule
    if include_content:
        content: dict[str, Any] = {"body": document.body}
        if document.title is not None:
            content["title"] = document.title
        if document.kind == "chore":
            content["schedule"] = document.schedule
        item["content"] = content
    return item


def _cap_item(
    scope: AgentStateScope,
    cap: caps.CapFile,
    *,
    include_content: bool,
) -> dict[str, Any]:
    path = _required_path(cap.path)
    item: dict[str, Any] = {
        "key": cap.name,
        "path": _home_path(scope, path),
        "digest": _content_digest(cap.content),
        "meta": mutable_data(cap.meta),
    }
    if include_content:
        item["content"] = _cap_content(cap)
    return item


def _flow_item(
    scope: AgentStateScope,
    flow: AuthoredFlow,
    *,
    include_content: bool,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "key": flow.key,
        "path": _home_path(scope, flow.path),
        "digest": flow.digest,
        "bytes": flow.size,
    }
    if include_content:
        item["content"] = {"source": flow.source}
    return item


def _cap_content(cap: caps.CapFile) -> dict[str, Any]:
    if cap.kind in {"psyche", "prompt"}:
        return {"body": cap.body}
    if cap.kind == "skill":
        return {
            "description": cast(str, cap.meta["description"]),
            "body": cap.body,
        }
    content: dict[str, Any] = {
        "description": cast(str, cap.meta["description"]),
        "transport": cap.meta.get("transport") or cap.meta.get("protocol"),
        "target": cast(str, cap.meta["target"]),
        "body": cap.body,
    }
    for name in ("headers", "env"):
        if name in cap.meta:
            content[name] = mutable_data(cap.meta[name])
    return content


def _cap_text(kind: CapKind, content: Mapping[str, Any]) -> str:
    body = cast(str, content.get("body", ""))
    if kind in {"psyche", "prompt"}:
        return _plain_text(body)
    if kind == "skill":
        return _markdown_text(body, {"description": content["description"]})
    meta = {
        name: content[name]
        for name in ("description", "transport", "target", "headers", "env")
        if name in content
    }
    return _markdown_text(body, meta)


def _updated_cap_text(cap: caps.CapFile, changes: Mapping[str, Any]) -> str:
    if cap.kind in {"psyche", "prompt"}:
        return _plain_text(cast(str, changes["body"]))
    meta = {name: value for name, value in cap.meta.items() if name != "name"}
    for name in ("description", "transport", "target", "headers", "env"):
        if name in changes:
            meta[name] = changes[name]
    if cap.kind == "service" and "transport" in changes:
        meta.pop("protocol", None)
    body = cast(str, changes.get("body", cap.body))
    return _markdown_text(body, meta)


def _markdown_text(body: str, meta: Mapping[str, object]) -> str:
    return frontmatter.dumps(frontmatter.Post(body, None, **dict(meta)))


def _plain_text(body: str) -> str:
    return body if body.endswith("\n") else f"{body}\n"


def _blank_to_none(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    return text or None


def _check_digest(
    resource: JobFile | caps.CapFile,
    expected: str | None,
) -> None:
    _required_path(resource.path)
    actual = _content_digest(resource.content)
    if expected is not None and actual != expected:
        raise DigestMismatchError(
            kind=resource.kind,
            key=resource.id if isinstance(resource, JobFile) else resource.name,
            expected=expected,
            actual=actual,
        )


def _content_digest(content: str) -> str:
    return sha256(content.encode("utf-8")).hexdigest()


def _required_path(path: Path | None) -> Path:
    if path is None:
        raise ValueError("authored resource path is required")
    return path


def _home_path(scope: AgentStateScope, path: Path) -> str:
    return path.relative_to(scope.layout.home).as_posix()


def _content(request: ResourceRequest) -> dict[str, Any]:
    if request.content is None:
        raise ValueError("resource content is required")
    return request.content


def _key(request: ResourceRequest) -> str:
    if request.key is None:
        raise ValueError("resource key is required")
    return request.key


def _fail_flow(request: ResourceRequest, error: StatePreparationError) -> NoReturn:
    fail(
        "invalid_flow",
        "flow source is invalid",
        operation=request.operation,
        kind=request.kind,
        key=request.key,
        issues=tuple(
            issue(
                diagnostic.code,
                "content.source",
                diagnostic.message,
                line=diagnostic.line,
            )
            for diagnostic in error.diagnostics
        ),
    )
