"""Current-agent authored-data toolset plugin."""

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

ScopeFilter = Literal["all", "home", "root"]


@dataclass(slots=True)
class AgentStateToolset:
    """Tools for managing the current agent's authored data."""

    config: dict[str, Any]
    name: str = "_me"
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
            name="list_tasks", description="List task documents for the current agent."
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

        @tool(name="get_task", description="Get one task document by id.")
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
            name="create_task",
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
            name="update_task", description="Update fields on one task document by id."
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
            name="list_chores",
            description="List chore documents for the current agent.",
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

        @tool(name="get_chore", description="Get one chore document by id.")
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
            name="create_chore",
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
            name="update_chore",
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
            name="list_psyches",
            description="List psyche definitions visible to the current agent.",
        )
        def psyche_list(
            scope: str = "all", context: ToolContext | None = None
        ) -> dict[str, Any]:
            return {"psyches": _list_caps("psyche", cap_scope=scope, context=context)}

        @tool(name="get_psyche", description="Get one psyche definition by name.")
        def psyche_get(
            name: str,
            scope: str = "home",
            context: ToolContext | None = None,
        ) -> dict[str, Any]:
            return {
                "psyche": _get_cap("psyche", name, cap_scope=scope, context=context)
            }

        @tool(name="create_psyche", description="Create one local psyche definition.")
        def psyche_create(
            name: str,
            body: str,
            scope: str = "home",
            context: ToolContext | None = None,
        ) -> dict[str, Any]:
            return {
                "psyche": _create_cap(
                    "psyche",
                    name,
                    _plain_text(body),
                    cap_scope=scope,
                    context=context,
                )
            }

        @tool(name="update_psyche", description="Update one local psyche definition.")
        def psyche_update(
            name: str,
            body: str,
            scope: str = "home",
            context: ToolContext | None = None,
        ) -> dict[str, Any]:
            return {
                "psyche": _update_cap(
                    "psyche",
                    name,
                    _plain_text(body),
                    cap_scope=scope,
                    context=context,
                )
            }

        @tool(name="delete_psyche", description="Delete one local psyche definition.")
        def psyche_delete(
            name: str,
            scope: str = "home",
            context: ToolContext | None = None,
        ) -> dict[str, Any]:
            return _delete_cap("psyche", name, cap_scope=scope, context=context)

        @tool(
            name="list_skills",
            description="List skill definitions visible to the current agent.",
        )
        def skill_list(
            scope: str = "all", context: ToolContext | None = None
        ) -> dict[str, Any]:
            return {"skills": _list_caps("skill", cap_scope=scope, context=context)}

        @tool(name="get_skill", description="Get one skill definition by name.")
        def skill_get(
            name: str,
            scope: str = "home",
            context: ToolContext | None = None,
        ) -> dict[str, Any]:
            return {"skill": _get_cap("skill", name, cap_scope=scope, context=context)}

        @tool(name="create_skill", description="Create one local skill definition.")
        def skill_create(
            name: str,
            description: str,
            body: str,
            scope: str = "home",
            context: ToolContext | None = None,
        ) -> dict[str, Any]:
            text = _markdown_text(body, {"description": description})
            return {
                "skill": _create_cap(
                    "skill", name, text, cap_scope=scope, context=context
                )
            }

        @tool(name="update_skill", description="Update one local skill definition.")
        def skill_update(
            name: str,
            description: str | None = None,
            body: str | None = None,
            scope: str = "home",
            context: ToolContext | None = None,
        ) -> dict[str, Any]:
            agent = _scope(context)
            parsed = _load_local_cap_parts(agent, "skill", name, cap_scope=scope)
            meta = dict(parsed.metadata)
            if description is not None:
                meta["description"] = description
            text = _markdown_text(parsed.content if body is None else body, meta)
            return {
                "skill": _update_cap(
                    "skill", name, text, cap_scope=scope, context=context
                )
            }

        @tool(name="delete_skill", description="Delete one local skill definition.")
        def skill_delete(
            name: str,
            scope: str = "home",
            context: ToolContext | None = None,
        ) -> dict[str, Any]:
            return _delete_cap("skill", name, cap_scope=scope, context=context)

        @tool(
            name="list_services",
            description="List service definitions visible to the current agent.",
        )
        def service_list(
            scope: str = "all", context: ToolContext | None = None
        ) -> dict[str, Any]:
            return {"services": _list_caps("service", cap_scope=scope, context=context)}

        @tool(name="get_service", description="Get one service definition by name.")
        def service_get(
            name: str,
            scope: str = "home",
            context: ToolContext | None = None,
        ) -> dict[str, Any]:
            return {
                "service": _get_cap("service", name, cap_scope=scope, context=context)
            }

        @tool(name="create_service", description="Create one local service definition.")
        def service_create(
            name: str,
            description: str,
            transport: str,
            target: str,
            body: str = "",
            headers: dict[str, str] | None = None,
            env: list[str] | None = None,
            scope: str = "home",
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
                    "service", name, text, cap_scope=scope, context=context
                )
            }

        @tool(name="update_service", description="Update one local service definition.")
        def service_update(
            name: str,
            description: str | None = None,
            transport: str | None = None,
            target: str | None = None,
            body: str | None = None,
            headers: dict[str, str] | None = None,
            env: list[str] | None = None,
            scope: str = "home",
            context: ToolContext | None = None,
        ) -> dict[str, Any]:
            agent = _scope(context)
            parsed = _load_local_cap_parts(agent, "service", name, cap_scope=scope)
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
                    "service", name, text, cap_scope=scope, context=context
                )
            }

        @tool(name="delete_service", description="Delete one local service definition.")
        def service_delete(
            name: str,
            scope: str = "home",
            context: ToolContext | None = None,
        ) -> dict[str, Any]:
            return _delete_cap("service", name, cap_scope=scope, context=context)

        @tool(
            name="list_prompts",
            description="List prompt definitions visible to the current agent.",
        )
        def prompt_list(
            scope: str = "all", context: ToolContext | None = None
        ) -> dict[str, Any]:
            return {"prompts": _list_caps("prompt", cap_scope=scope, context=context)}

        @tool(name="get_prompt", description="Get one prompt definition by name.")
        def prompt_get(
            name: str,
            scope: str = "home",
            context: ToolContext | None = None,
        ) -> dict[str, Any]:
            return {
                "prompt": _get_cap("prompt", name, cap_scope=scope, context=context)
            }

        @tool(name="create_prompt", description="Create one local prompt definition.")
        def prompt_create(
            name: str,
            body: str,
            scope: str = "home",
            context: ToolContext | None = None,
        ) -> dict[str, Any]:
            return {
                "prompt": _create_cap(
                    "prompt",
                    name,
                    _plain_text(body),
                    cap_scope=scope,
                    context=context,
                )
            }

        @tool(name="update_prompt", description="Update one local prompt definition.")
        def prompt_update(
            name: str,
            body: str,
            scope: str = "home",
            context: ToolContext | None = None,
        ) -> dict[str, Any]:
            return {
                "prompt": _update_cap(
                    "prompt",
                    name,
                    _plain_text(body),
                    cap_scope=scope,
                    context=context,
                )
            }

        @tool(name="delete_prompt", description="Delete one local prompt definition.")
        def prompt_delete(
            name: str,
            scope: str = "home",
            context: ToolContext | None = None,
        ) -> dict[str, Any]:
            return _delete_cap("prompt", name, cap_scope=scope, context=context)

        return {
            "list_tasks": create_function_tool(task_list),
            "get_task": create_function_tool(task_get),
            "create_task": create_function_tool(task_create),
            "update_task": create_function_tool(task_update),
            "list_chores": create_function_tool(chore_list),
            "get_chore": create_function_tool(chore_get),
            "create_chore": create_function_tool(chore_create),
            "update_chore": create_function_tool(chore_update),
            "list_psyches": create_function_tool(psyche_list),
            "get_psyche": create_function_tool(psyche_get),
            "create_psyche": create_function_tool(psyche_create),
            "update_psyche": create_function_tool(psyche_update),
            "delete_psyche": create_function_tool(psyche_delete),
            "list_skills": create_function_tool(skill_list),
            "get_skill": create_function_tool(skill_get),
            "create_skill": create_function_tool(skill_create),
            "update_skill": create_function_tool(skill_update),
            "delete_skill": create_function_tool(skill_delete),
            "list_services": create_function_tool(service_list),
            "get_service": create_function_tool(service_get),
            "create_service": create_function_tool(service_create),
            "update_service": create_function_tool(service_update),
            "delete_service": create_function_tool(service_delete),
            "list_prompts": create_function_tool(prompt_list),
            "get_prompt": create_function_tool(prompt_get),
            "create_prompt": create_function_tool(prompt_create),
            "update_prompt": create_function_tool(prompt_update),
            "delete_prompt": create_function_tool(prompt_delete),
        }


@dataclass(frozen=True, slots=True)
class _AgentStateScope:
    layout: AgentLayout


def create_toolset(config: Mapping[str, Any]) -> Toolset:
    """Create the _me toolset plugin."""

    return AgentStateToolset(config=dict(config))


def _scope(context: ToolContext | None) -> _AgentStateScope:
    if context is None:
        raise ToolangError("_me tool context is required")
    home = context.home.resolve()
    if home.parent.name != "agents":
        raise ToolangError(f"_me tool requires an agent home under agents/: {home}")
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
    kind: CapKind, *, cap_scope: str, context: ToolContext | None
) -> list[dict[str, Any]]:
    agent = _scope(context)
    selected_scope = _scope_filter(cap_scope)
    entries = cap_state.list_entries(
        agent.layout.root,
        agent.layout.name,
        scope=None if selected_scope == "all" else selected_scope,
        kinds={kind},
    )
    return [_cap_payload(agent, entry) for entry in entries]


def _get_cap(
    kind: CapKind,
    name: str,
    *,
    cap_scope: str,
    context: ToolContext | None,
) -> dict[str, Any]:
    agent = _scope(context)
    entry = _find_cap_entry(agent, kind, name, cap_scope=_scope_filter(cap_scope))
    return _cap_payload(agent, entry, include_content=True)


def _create_cap(
    kind: CapKind,
    name: str,
    text: str,
    *,
    cap_scope: str,
    context: ToolContext | None,
) -> dict[str, Any]:
    agent = _scope(context)
    selected_scope = _cap_scope(cap_scope)
    catalog = _authored_caps(agent, selected_scope)
    if catalog.get(kind, name) is not None:
        raise ToolangError(f"local {kind} already exists: {name}")
    catalog.create(caps.CapFile.parse(text, kind=kind, name=name))
    entry = _find_cap_entry(
        agent, kind, name, cap_scope=selected_scope, source_form="authored"
    )
    return _cap_payload(agent, entry, include_content=True)


def _update_cap(
    kind: CapKind,
    name: str,
    text: str,
    *,
    cap_scope: str,
    context: ToolContext | None,
) -> dict[str, Any]:
    agent = _scope(context)
    selected_scope = _cap_scope(cap_scope)
    _find_cap_entry(agent, kind, name, cap_scope=selected_scope, source_form="authored")
    _authored_caps(agent, selected_scope).update(
        caps.CapFile.parse(text, kind=kind, name=name)
    )
    entry = _find_cap_entry(
        agent, kind, name, cap_scope=selected_scope, source_form="authored"
    )
    return _cap_payload(agent, entry, include_content=True)


def _delete_cap(
    kind: CapKind,
    name: str,
    *,
    cap_scope: str,
    context: ToolContext | None,
) -> dict[str, Any]:
    agent = _scope(context)
    selected_scope = _cap_scope(cap_scope)
    entry = _find_cap_entry(
        agent, kind, name, cap_scope=selected_scope, source_form="authored"
    )
    deleted_path = agent.layout.root / entry.path
    if entry.shape == "dir":
        deleted_path = deleted_path.parent
    _authored_caps(agent, selected_scope).remove(kind, name)
    return {
        "kind": kind,
        "name": name,
        "scope": selected_scope,
        "path": str(deleted_path),
        "deleted": True,
    }


def _find_cap_entry(
    agent: _AgentStateScope,
    kind: CapKind,
    name: str,
    *,
    cap_scope: ScopeFilter | CapScope,
    source_origin: str | None = None,
    source_form: str | None = None,
) -> StateCap:
    entry_scope = None if cap_scope == "all" else cap_scope
    entries = cap_state.list_entries(
        agent.layout.root,
        agent.layout.name,
        scope=entry_scope,
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
        key=lambda entry: cap_state.entry_ref(entry, agent_name=agent.layout.name),
    )[0]


def _cap_payload(
    agent: _AgentStateScope,
    entry: StateCap,
    *,
    include_content: bool = False,
) -> dict[str, Any]:
    selected_scope = cap_state.entry_scope(entry, agent_name=agent.layout.name)
    item: dict[str, Any] = {
        "kind": entry.kind,
        "name": entry.name,
        "scope": selected_scope,
        "origin": cap_state.entry_origin(entry),
        "form": cap_state.entry_form(entry),
        "ref": cap_state.entry_ref(entry, agent_name=agent.layout.name),
        "path": str(agent.layout.root / entry.path),
        "definition_file": cap_state.entry_definition_file(entry),
        "meta": mutable_data(entry.meta),
    }
    line = cap_state.entry_line(entry)
    if line is not None:
        item["line"] = line
    if include_content and entry.source.form == "authored":
        cap = _authored_caps(agent, selected_scope).get(entry.kind, entry.name)
        if cap is None:
            raise ToolangError(f"local {entry.kind} not found: {entry.name}")
        item["content"] = cap.content
    return item


def _load_local_cap_parts(
    agent: _AgentStateScope,
    kind: CapKind,
    name: str,
    *,
    cap_scope: str,
) -> frontmatter.Post:
    selected_scope = _cap_scope(cap_scope)
    cap = _authored_caps(agent, selected_scope).get(kind, name)
    if cap is None:
        raise ToolangError(f"local {kind} not found: {name}")
    return frontmatter.loads(cap.content)


def _authored_caps(
    agent: _AgentStateScope,
    cap_scope: CapScope,
) -> caps.AuthoredCaps:
    directory = agent.layout.root if cap_scope == "root" else agent.layout.home
    return caps.AuthoredCaps(directory)


def _cap_scope(value: str) -> CapScope:
    text = value.strip().lower()
    if text not in {"home", "root"}:
        raise ToolangError(f"scope must be home or root: {value}")
    return cast(CapScope, text)


def _scope_filter(value: str) -> ScopeFilter:
    text = value.strip().lower()
    if text not in {"all", "home", "root"}:
        raise ToolangError(f"scope must be all, home, or root: {value}")
    return cast(ScopeFilter, text)


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
