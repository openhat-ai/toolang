"""Agent-owned task, chore, and cap state toolset plugin."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast

import frontmatter

from toolang.catalog import cap as caps
from toolang.catalog.errors import CatalogError
from toolang.state import state as cap_state
from toolang.common.immutable import mutable_data
from toolang.common.layout import AgentLayout
from toolang.catalog.job import AuthoredJobs, JobFile
from toolang.catalog.types import CapKind, DEFAULT_CHORE_SCHEDULE, JobKind
from toolang.common.errors import ToolangError
from toolang.base.protocols.tool import AgentTool, Toolset
from toolang.base.types.tool import ToolContext
from toolang.base.utils.function_tools import create_function_tool, tool
from toolang.state.state import StateCap, CapScope
from toolang.work.authoring import (
    allocate_authored_job_id,
    assign_missing_authored_job_ids,
    new_job_file,
)
from toolang.work.state import job_thread_id

VisibilityFilter = Literal["all", "home", "root"]


@dataclass(slots=True)
class AgentStateToolset:
    """Tools for managing the current agent's authored state."""

    config: dict[str, Any]
    name: str = "agent_state"
    description: str | None = (
        "Inspect, create, and update this agent's tasks, chores, psyches, skills, "
        "services, and prompts."
    )
    _tools: dict[str, AgentTool] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._tools = self._build_tools()

    def tools(self) -> Mapping[str, AgentTool]:
        return dict(self._tools)

    def _build_tools(self) -> dict[str, AgentTool]:
        @tool(
            name="task_list", description="List task documents for the current agent."
        )
        def task_list(
            include_archived: bool = False,
            context: ToolContext | None = None,
        ) -> dict[str, Any]:
            scope = _scope(context)
            catalog = _jobs(scope)
            entries = list(catalog.list(kind="task"))
            if include_archived:
                entries.extend(catalog.list(kind="task", stage="archived"))
            return {
                "tasks": [
                    _task_payload(entry)
                    for entry in sorted(entries, key=lambda item: str(item.path))
                ]
            }

        @tool(name="task_get", description="Get one task document by id.")
        def task_get(
            task_id: str,
            include_archived: bool = False,
            context: ToolContext | None = None,
        ) -> dict[str, Any]:
            scope = _scope(context)
            entry = _jobs(scope).get(
                "task",
                task_id,
                stage=None if include_archived else "ready",
            )
            if entry is None:
                raise ToolangError(f"task not found: {task_id}")
            return {"task": _task_payload(entry)}

        @tool(
            name="task_create",
            description="Create one task document for the current agent.",
        )
        def task_create(
            body: str,
            title: str | None = None,
            context: ToolContext | None = None,
        ) -> dict[str, Any]:
            scope = _scope(context)
            document = _new_job(
                scope,
                kind="task",
                title=_blank_to_none(title),
                body=body,
            )
            return {"task": _task_payload(_jobs(scope).create(document))}

        @tool(
            name="task_update", description="Update fields on one task document by id."
        )
        def task_update(
            task_id: str,
            title: str | None = None,
            body: str | None = None,
            context: ToolContext | None = None,
        ) -> dict[str, Any]:
            scope = _scope(context)
            catalog = _jobs(scope)
            entry = catalog.get(
                "task",
                task_id,
                stage=None,
            )
            if entry is None:
                raise ToolangError(f"task not found: {task_id}")
            changes: dict[str, str | None] = {}
            if title is not None:
                changes["title"] = _blank_to_none(title)
            if body is not None:
                changes["body"] = body
            return {"task": _task_payload(catalog.update(entry.patch(changes)))}

        @tool(
            name="chore_list", description="List chore documents for the current agent."
        )
        def chore_list(
            include_archived: bool = False,
            context: ToolContext | None = None,
        ) -> dict[str, Any]:
            scope = _scope(context)
            catalog = _jobs(scope)
            entries = list(catalog.list(kind="chore"))
            if include_archived:
                entries.extend(catalog.list(kind="chore", stage="archived"))
            return {
                "chores": [
                    _chore_payload(entry)
                    for entry in sorted(entries, key=lambda item: str(item.path))
                ]
            }

        @tool(name="chore_get", description="Get one chore document by id.")
        def chore_get(
            chore_id: str,
            include_archived: bool = False,
            context: ToolContext | None = None,
        ) -> dict[str, Any]:
            scope = _scope(context)
            entry = _jobs(scope).get(
                "chore",
                chore_id,
                stage=None if include_archived else "ready",
            )
            if entry is None:
                raise ToolangError(f"chore not found: {chore_id}")
            return {"chore": _chore_payload(entry)}

        @tool(
            name="chore_create",
            description="Create one chore document for the current agent.",
        )
        def chore_create(
            body: str,
            schedule: str = DEFAULT_CHORE_SCHEDULE,
            title: str | None = None,
            context: ToolContext | None = None,
        ) -> dict[str, Any]:
            scope = _scope(context)
            document = _new_job(
                scope,
                kind="chore",
                title=_blank_to_none(title),
                body=body,
                schedule=schedule,
            )
            return {"chore": _chore_payload(_jobs(scope).create(document))}

        @tool(
            name="chore_update",
            description="Update fields on one chore document by id.",
        )
        def chore_update(
            chore_id: str,
            title: str | None = None,
            body: str | None = None,
            schedule: str | None = None,
            context: ToolContext | None = None,
        ) -> dict[str, Any]:
            scope = _scope(context)
            catalog = _jobs(scope)
            entry = catalog.get(
                "chore",
                chore_id,
                stage=None,
            )
            if entry is None:
                raise ToolangError(f"chore not found: {chore_id}")
            changes: dict[str, str | None] = {}
            if title is not None:
                changes["title"] = _blank_to_none(title)
            if schedule is not None:
                changes["schedule"] = schedule
            if body is not None:
                changes["body"] = body
            return {"chore": _chore_payload(catalog.update(entry.patch(changes)))}

        @tool(
            name="psyche_list",
            description="List psyche definitions visible to the current agent.",
        )
        def psyche_list(
            visibility: str = "all", context: ToolContext | None = None
        ) -> dict[str, Any]:
            return {
                "psyches": _list_caps("psyche", visibility=visibility, context=context)
            }

        @tool(name="psyche_get", description="Get one psyche definition by name.")
        def psyche_get(
            name: str,
            visibility: str = "home",
            context: ToolContext | None = None,
        ) -> dict[str, Any]:
            return {
                "psyche": _get_cap(
                    "psyche", name, visibility=visibility, context=context
                )
            }

        @tool(name="psyche_create", description="Create one local psyche definition.")
        def psyche_create(
            name: str,
            body: str,
            visibility: str = "home",
            context: ToolContext | None = None,
        ) -> dict[str, Any]:
            return {
                "psyche": _create_cap(
                    "psyche",
                    name,
                    _plain_text(body),
                    visibility=visibility,
                    context=context,
                )
            }

        @tool(name="psyche_update", description="Update one local psyche definition.")
        def psyche_update(
            name: str,
            body: str,
            visibility: str = "home",
            context: ToolContext | None = None,
        ) -> dict[str, Any]:
            return {
                "psyche": _update_cap(
                    "psyche",
                    name,
                    _plain_text(body),
                    visibility=visibility,
                    context=context,
                )
            }

        @tool(name="psyche_delete", description="Delete one local psyche definition.")
        def psyche_delete(
            name: str,
            visibility: str = "home",
            context: ToolContext | None = None,
        ) -> dict[str, Any]:
            return _delete_cap("psyche", name, visibility=visibility, context=context)

        @tool(
            name="skill_list",
            description="List skill definitions visible to the current agent.",
        )
        def skill_list(
            visibility: str = "all", context: ToolContext | None = None
        ) -> dict[str, Any]:
            return {
                "skills": _list_caps("skill", visibility=visibility, context=context)
            }

        @tool(name="skill_get", description="Get one skill definition by name.")
        def skill_get(
            name: str,
            visibility: str = "home",
            context: ToolContext | None = None,
        ) -> dict[str, Any]:
            return {
                "skill": _get_cap("skill", name, visibility=visibility, context=context)
            }

        @tool(name="skill_create", description="Create one local skill definition.")
        def skill_create(
            name: str,
            description: str,
            body: str,
            visibility: str = "home",
            context: ToolContext | None = None,
        ) -> dict[str, Any]:
            text = _markdown_text(body, {"description": description})
            return {
                "skill": _create_cap(
                    "skill", name, text, visibility=visibility, context=context
                )
            }

        @tool(name="skill_update", description="Update one local skill definition.")
        def skill_update(
            name: str,
            description: str | None = None,
            body: str | None = None,
            visibility: str = "home",
            context: ToolContext | None = None,
        ) -> dict[str, Any]:
            scope = _scope(context)
            parsed = _load_local_cap_parts(scope, "skill", name, visibility=visibility)
            meta = dict(parsed.metadata)
            if description is not None:
                meta["description"] = description
            text = _markdown_text(parsed.content if body is None else body, meta)
            return {
                "skill": _update_cap(
                    "skill", name, text, visibility=visibility, context=context
                )
            }

        @tool(name="skill_delete", description="Delete one local skill definition.")
        def skill_delete(
            name: str,
            visibility: str = "home",
            context: ToolContext | None = None,
        ) -> dict[str, Any]:
            return _delete_cap("skill", name, visibility=visibility, context=context)

        @tool(
            name="service_list",
            description="List service definitions visible to the current agent.",
        )
        def service_list(
            visibility: str = "all", context: ToolContext | None = None
        ) -> dict[str, Any]:
            return {
                "services": _list_caps(
                    "service", visibility=visibility, context=context
                )
            }

        @tool(name="service_get", description="Get one service definition by name.")
        def service_get(
            name: str,
            visibility: str = "home",
            context: ToolContext | None = None,
        ) -> dict[str, Any]:
            return {
                "service": _get_cap(
                    "service", name, visibility=visibility, context=context
                )
            }

        @tool(name="service_create", description="Create one local service definition.")
        def service_create(
            name: str,
            description: str,
            transport: str,
            target: str,
            body: str = "",
            headers: dict[str, str] | None = None,
            env: list[str] | None = None,
            visibility: str = "home",
            context: ToolContext | None = None,
        ) -> dict[str, Any]:
            text = _service_text(
                body,
                description=description,
                transport=transport,
                target=target,
                headers=headers,
                env=env,
            )
            return {
                "service": _create_cap(
                    "service", name, text, visibility=visibility, context=context
                )
            }

        @tool(name="service_update", description="Update one local service definition.")
        def service_update(
            name: str,
            description: str | None = None,
            transport: str | None = None,
            target: str | None = None,
            body: str | None = None,
            headers: dict[str, str] | None = None,
            env: list[str] | None = None,
            visibility: str = "home",
            context: ToolContext | None = None,
        ) -> dict[str, Any]:
            scope = _scope(context)
            parsed = _load_local_cap_parts(
                scope, "service", name, visibility=visibility
            )
            meta = dict(parsed.metadata)
            if description is not None:
                meta["description"] = description
            if transport is not None:
                meta["transport"] = transport
            if target is not None:
                meta["target"] = target
            if headers is not None:
                meta["headers"] = headers
            if env is not None:
                meta["env"] = env
            text = _markdown_text(parsed.content if body is None else body, meta)
            return {
                "service": _update_cap(
                    "service", name, text, visibility=visibility, context=context
                )
            }

        @tool(name="service_delete", description="Delete one local service definition.")
        def service_delete(
            name: str,
            visibility: str = "home",
            context: ToolContext | None = None,
        ) -> dict[str, Any]:
            return _delete_cap("service", name, visibility=visibility, context=context)

        @tool(
            name="prompt_list",
            description="List prompt definitions visible to the current agent.",
        )
        def prompt_list(
            visibility: str = "all", context: ToolContext | None = None
        ) -> dict[str, Any]:
            return {
                "prompts": _list_caps("prompt", visibility=visibility, context=context)
            }

        @tool(name="prompt_get", description="Get one prompt definition by name.")
        def prompt_get(
            name: str,
            visibility: str = "home",
            context: ToolContext | None = None,
        ) -> dict[str, Any]:
            return {
                "prompt": _get_cap(
                    "prompt", name, visibility=visibility, context=context
                )
            }

        @tool(name="prompt_create", description="Create one local prompt definition.")
        def prompt_create(
            name: str,
            body: str,
            visibility: str = "home",
            context: ToolContext | None = None,
        ) -> dict[str, Any]:
            return {
                "prompt": _create_cap(
                    "prompt",
                    name,
                    _plain_text(body),
                    visibility=visibility,
                    context=context,
                )
            }

        @tool(name="prompt_update", description="Update one local prompt definition.")
        def prompt_update(
            name: str,
            body: str,
            visibility: str = "home",
            context: ToolContext | None = None,
        ) -> dict[str, Any]:
            return {
                "prompt": _update_cap(
                    "prompt",
                    name,
                    _plain_text(body),
                    visibility=visibility,
                    context=context,
                )
            }

        @tool(name="prompt_delete", description="Delete one local prompt definition.")
        def prompt_delete(
            name: str,
            visibility: str = "home",
            context: ToolContext | None = None,
        ) -> dict[str, Any]:
            return _delete_cap("prompt", name, visibility=visibility, context=context)

        return {
            "task_list": create_function_tool(task_list),
            "task_get": create_function_tool(task_get),
            "task_create": create_function_tool(task_create),
            "task_update": create_function_tool(task_update),
            "chore_list": create_function_tool(chore_list),
            "chore_get": create_function_tool(chore_get),
            "chore_create": create_function_tool(chore_create),
            "chore_update": create_function_tool(chore_update),
            "psyche_list": create_function_tool(psyche_list),
            "psyche_get": create_function_tool(psyche_get),
            "psyche_create": create_function_tool(psyche_create),
            "psyche_update": create_function_tool(psyche_update),
            "psyche_delete": create_function_tool(psyche_delete),
            "skill_list": create_function_tool(skill_list),
            "skill_get": create_function_tool(skill_get),
            "skill_create": create_function_tool(skill_create),
            "skill_update": create_function_tool(skill_update),
            "skill_delete": create_function_tool(skill_delete),
            "service_list": create_function_tool(service_list),
            "service_get": create_function_tool(service_get),
            "service_create": create_function_tool(service_create),
            "service_update": create_function_tool(service_update),
            "service_delete": create_function_tool(service_delete),
            "prompt_list": create_function_tool(prompt_list),
            "prompt_get": create_function_tool(prompt_get),
            "prompt_create": create_function_tool(prompt_create),
            "prompt_update": create_function_tool(prompt_update),
            "prompt_delete": create_function_tool(prompt_delete),
        }


@dataclass(frozen=True, slots=True)
class _AgentStateScope:
    layout: AgentLayout


def create_toolset(config: Mapping[str, Any]) -> Toolset:
    """Create the agent_state toolset plugin."""

    return AgentStateToolset(config=dict(config))


def _scope(context: ToolContext | None) -> _AgentStateScope:
    if context is None:
        raise ToolangError("agent_state tool context is required")
    home = context.home.resolve()
    if home.parent.name != "agents":
        raise ToolangError(
            f"agent_state tool requires an agent home under agents/: {home}"
        )
    return _AgentStateScope(
        AgentLayout(
            root=home.parent.parent,
            name=home.name,
            placement=context.placement,
        )
    )


def _task_payload(document: JobFile) -> dict[str, Any]:
    return {
        "id": document.id,
        "thread_id": job_thread_id(document),
        "path": str(_job_path(document)),
        "title": document.title,
        "stage": document.stage,
        "body": document.body,
    }


def _chore_payload(document: JobFile) -> dict[str, Any]:
    return {
        "id": document.id,
        "thread_id": job_thread_id(document),
        "path": str(_job_path(document)),
        "title": document.title,
        "stage": document.stage,
        "schedule": document.schedule,
        "body": document.body,
    }


def _jobs(scope: _AgentStateScope) -> AuthoredJobs:
    catalog = AuthoredJobs(scope.layout.home)
    try:
        assign_missing_authored_job_ids(
            scope.layout,
            catalog=catalog,
        )
    except (CatalogError, ValueError) as exc:
        raise ToolangError(str(exc)) from exc
    return catalog


def _new_job(
    scope: _AgentStateScope,
    *,
    kind: JobKind,
    title: str | None,
    body: str,
    schedule: str | None = None,
) -> JobFile:
    return new_job_file(
        kind=kind,
        job_id=allocate_authored_job_id(scope.layout),
        title=title,
        body=body,
        schedule=schedule,
    )


def _job_path(job: JobFile) -> Path:
    if job.path is None:
        raise ValueError("authored job path is required")
    return job.path


def _list_caps(
    kind: CapKind, *, visibility: str, context: ToolContext | None
) -> list[dict[str, Any]]:
    scope = _scope(context)
    filter_visibility = _visibility_filter(visibility)
    entries = cap_state.list_entries(
        scope.layout.root,
        scope.layout.name,
        scope=None if filter_visibility == "all" else filter_visibility,
        kinds={kind},
    )
    return [_cap_payload(scope, entry) for entry in entries]


def _get_cap(
    kind: CapKind,
    name: str,
    *,
    visibility: str,
    context: ToolContext | None,
) -> dict[str, Any]:
    scope = _scope(context)
    entry = _find_cap_entry(
        scope, kind, name, visibility=_visibility_filter(visibility)
    )
    return _cap_payload(scope, entry, include_content=True)


def _create_cap(
    kind: CapKind,
    name: str,
    text: str,
    *,
    visibility: str,
    context: ToolContext | None,
) -> dict[str, Any]:
    scope = _scope(context)
    cap_visibility = _visibility(visibility)
    catalog = _authored_caps(scope, cap_visibility)
    if catalog.get(kind, name) is not None:
        raise ToolangError(f"local {kind} already exists: {name}")
    catalog.create(caps.CapFile.parse(text, kind=kind, name=name))
    entry = _find_cap_entry(
        scope, kind, name, visibility=cap_visibility, source_form="authored"
    )
    return _cap_payload(scope, entry, include_content=True)


def _update_cap(
    kind: CapKind,
    name: str,
    text: str,
    *,
    visibility: str,
    context: ToolContext | None,
) -> dict[str, Any]:
    scope = _scope(context)
    cap_visibility = _visibility(visibility)
    _find_cap_entry(
        scope, kind, name, visibility=cap_visibility, source_form="authored"
    )
    _authored_caps(scope, cap_visibility).update(
        caps.CapFile.parse(text, kind=kind, name=name)
    )
    entry = _find_cap_entry(
        scope, kind, name, visibility=cap_visibility, source_form="authored"
    )
    return _cap_payload(scope, entry, include_content=True)


def _delete_cap(
    kind: CapKind,
    name: str,
    *,
    visibility: str,
    context: ToolContext | None,
) -> dict[str, Any]:
    scope = _scope(context)
    cap_visibility = _visibility(visibility)
    entry = _find_cap_entry(
        scope, kind, name, visibility=cap_visibility, source_form="authored"
    )
    deleted_path = scope.layout.root / entry.path
    if entry.shape == "dir":
        deleted_path = deleted_path.parent
    _authored_caps(scope, cap_visibility).remove(kind, name)
    return {
        "kind": kind,
        "name": name,
        "scope": cap_visibility,
        "path": str(deleted_path),
        "deleted": True,
    }


def _find_cap_entry(
    scope: _AgentStateScope,
    kind: CapKind,
    name: str,
    *,
    visibility: VisibilityFilter | CapScope,
    source_origin: str | None = None,
    source_form: str | None = None,
) -> StateCap:
    entry_visibility = None if visibility == "all" else visibility
    entries = cap_state.list_entries(
        scope.layout.root,
        scope.layout.name,
        scope=entry_visibility,
        kinds={kind},
    )
    matches = [
        entry
        for entry in entries
        if (
            entry.name == name
            and (source_origin is None or entry.source.origin == source_origin)
            and (source_form is None or entry.source.form == source_form)
        )
    ]
    if not matches:
        qualifier = f"{source_origin} " if source_origin is not None else ""
        raise ToolangError(f"{qualifier}{kind} not found: {name}")
    return sorted(
        matches,
        key=lambda entry: cap_state.entry_ref(entry, agent_name=scope.layout.name),
    )[0]


def _cap_payload(
    scope: _AgentStateScope,
    entry: StateCap,
    *,
    include_content: bool = False,
) -> dict[str, Any]:
    visibility = cap_state.entry_scope(entry, agent_name=scope.layout.name)
    item: dict[str, Any] = {
        "kind": entry.kind,
        "name": entry.name,
        "scope": cap_state.entry_scope(entry, agent_name=scope.layout.name),
        "origin": cap_state.entry_origin(entry),
        "form": cap_state.entry_form(entry),
        "ref": cap_state.entry_ref(entry, agent_name=scope.layout.name),
        "path": str(scope.layout.root / entry.path),
        "definition_file": cap_state.entry_definition_file(entry),
        "meta": mutable_data(entry.meta),
    }
    line = cap_state.entry_line(entry)
    if line is not None:
        item["line"] = line
    if include_content and entry.source.form == "authored":
        cap = _authored_caps(scope, visibility).get(entry.kind, entry.name)
        if cap is None:
            raise ToolangError(f"local {entry.kind} not found: {entry.name}")
        item["content"] = cap.content
    return item


def _load_local_cap_parts(
    scope: _AgentStateScope,
    kind: CapKind,
    name: str,
    *,
    visibility: str,
) -> frontmatter.Post:
    cap_visibility = _visibility(visibility)
    cap = _authored_caps(scope, cap_visibility).get(kind, name)
    if cap is None:
        raise ToolangError(f"local {kind} not found: {name}")
    return frontmatter.loads(cap.content)


def _authored_caps(
    scope: _AgentStateScope,
    visibility: CapScope,
) -> caps.AuthoredCaps:
    directory = scope.layout.root if visibility == "root" else scope.layout.home
    return caps.AuthoredCaps(directory)


def _visibility(value: str) -> CapScope:
    text = value.strip().lower()
    if text not in {"home", "root"}:
        raise ToolangError(f"visibility must be home or root: {value}")
    return cast(CapScope, text)


def _visibility_filter(value: str) -> VisibilityFilter:
    text = value.strip().lower()
    if text not in {"all", "home", "root"}:
        raise ToolangError(f"visibility must be all, home, or root: {value}")
    return cast(VisibilityFilter, text)


def _service_text(
    body: str,
    *,
    description: str,
    transport: str,
    target: str,
    headers: dict[str, str] | None,
    env: list[str] | None,
) -> str:
    meta: dict[str, object] = {
        "description": description,
        "transport": transport,
        "target": target,
    }
    if headers is not None:
        meta["headers"] = headers
    if env is not None:
        meta["env"] = env
    return _markdown_text(body, meta)


def _markdown_text(body: str, meta: Mapping[str, object]) -> str:
    post = frontmatter.Post(body, None, **dict(meta))
    return frontmatter.dumps(post)


def _plain_text(body: str) -> str:
    return body if body.endswith("\n") else f"{body}\n"


def _blank_to_none(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    return text or None
