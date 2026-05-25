"""Agent-owned task, chore, and cap state tool plugin."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast

import frontmatter

from toolang import caps, work
from toolang.base.error import ToolangError
from toolang.base.protocols.tool import AgentTool, AgentToolSet
from toolang.base.types.tool import ToolContext
from toolang.base.utils.function_tools import create_function_tool, tool
from toolang.state.prepared import PreparedEntry, PreparedVisibility

CapKind = Literal["psyche", "skill", "service", "prompt"]
VisibilityFilter = Literal["all", "private", "shared"]


@dataclass(slots=True)
class AgentStatePlugin:
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
        @tool(name="task_list", description="List task documents for the current agent.")
        def task_list(
            include_archived: bool = False,
            context: ToolContext | None = None,
        ) -> dict[str, Any]:
            scope = _scope(context)
            return {
                "tasks": [
                    _task_payload(entry)
                    for entry in work.list_tasks(
                        scope.toolang_root,
                        scope.agent_name,
                        include_archived=include_archived,
                    )
                ]
            }

        @tool(name="task_get", description="Get one task document by id.")
        def task_get(
            task_id: str,
            include_archived: bool = False,
            context: ToolContext | None = None,
        ) -> dict[str, Any]:
            scope = _scope(context)
            entry = work.find_task(
                scope.toolang_root,
                scope.agent_name,
                task_id,
                include_archived=include_archived,
            )
            if entry is None:
                raise ToolangError(f"task not found: {task_id}")
            return {"task": _task_payload(entry)}

        @tool(name="task_create", description="Create one task document for the current agent.")
        def task_create(
            body: str,
            title: str | None = None,
            state: str = "active",
            stage: str = "todo",
            context: ToolContext | None = None,
        ) -> dict[str, Any]:
            scope = _scope(context)
            document = work.TaskFile.model_validate(
                {
                    "id": work.allocate_job_id(scope.toolang_root, scope.agent_name),
                    "title": _blank_to_none(title),
                    "state": state,
                    "stage": stage,
                    "body": body,
                }
            )
            path = work.task_path(scope.toolang_root, scope.agent_name, document.task_id())
            document.save(path)
            return {
                "task": _task_payload(work.TaskEntry(name=path.stem, path=path, document=document))
            }

        @tool(name="task_update", description="Update fields on one task document by id.")
        def task_update(
            task_id: str,
            title: str | None = None,
            body: str | None = None,
            state: str | None = None,
            stage: str | None = None,
            context: ToolContext | None = None,
        ) -> dict[str, Any]:
            scope = _scope(context)
            entry = work.find_task(
                scope.toolang_root,
                scope.agent_name,
                task_id,
                include_archived=True,
            )
            if entry is None:
                raise ToolangError(f"task not found: {task_id}")
            updates: dict[str, object] = {}
            if title is not None:
                updates["title"] = _blank_to_none(title)
            if body is not None:
                updates["body"] = body
            if state is not None:
                updates["state"] = state
            if stage is not None:
                updates["stage"] = stage
            document = work.TaskFile.model_validate(
                {**entry.document.model_dump(mode="python"), **updates}
            )
            path = work.save_task_entry(scope.toolang_root, scope.agent_name, entry, document)
            return {
                "task": _task_payload(work.TaskEntry(name=path.stem, path=path, document=document))
            }

        @tool(name="chore_list", description="List chore documents for the current agent.")
        def chore_list(
            include_archived: bool = False,
            context: ToolContext | None = None,
        ) -> dict[str, Any]:
            scope = _scope(context)
            return {
                "chores": [
                    _chore_payload(entry)
                    for entry in work.list_chores(
                        scope.toolang_root,
                        scope.agent_name,
                        include_archived=include_archived,
                    )
                ]
            }

        @tool(name="chore_get", description="Get one chore document by id.")
        def chore_get(
            chore_id: str,
            include_archived: bool = False,
            context: ToolContext | None = None,
        ) -> dict[str, Any]:
            scope = _scope(context)
            entry = work.find_chore(
                scope.toolang_root,
                scope.agent_name,
                chore_id,
                include_archived=include_archived,
            )
            if entry is None:
                raise ToolangError(f"chore not found: {chore_id}")
            return {"chore": _chore_payload(entry)}

        @tool(name="chore_create", description="Create one chore document for the current agent.")
        def chore_create(
            body: str,
            schedule: str = work.DEFAULT_CHORE_SCHEDULE,
            title: str | None = None,
            state: str = "active",
            context: ToolContext | None = None,
        ) -> dict[str, Any]:
            scope = _scope(context)
            document = work.ChoreFile.model_validate(
                {
                    "id": work.allocate_job_id(scope.toolang_root, scope.agent_name),
                    "title": _blank_to_none(title),
                    "state": state,
                    "schedule": schedule,
                    "body": body,
                }
            )
            path = work.chore_path(scope.toolang_root, scope.agent_name, document.chore_id())
            document.save(path)
            return {
                "chore": _chore_payload(work.ChoreEntry(name=path.stem, path=path, document=document))
            }

        @tool(name="chore_update", description="Update fields on one chore document by id.")
        def chore_update(
            chore_id: str,
            title: str | None = None,
            body: str | None = None,
            state: str | None = None,
            schedule: str | None = None,
            context: ToolContext | None = None,
        ) -> dict[str, Any]:
            scope = _scope(context)
            entry = work.find_chore(
                scope.toolang_root,
                scope.agent_name,
                chore_id,
                include_archived=True,
            )
            if entry is None:
                raise ToolangError(f"chore not found: {chore_id}")
            updates: dict[str, object] = {}
            if title is not None:
                updates["title"] = _blank_to_none(title)
            if body is not None:
                updates["body"] = body
            if state is not None:
                updates["state"] = state
            if schedule is not None:
                updates["schedule"] = schedule
            document = work.ChoreFile.model_validate(
                {**entry.document.model_dump(mode="python"), **updates}
            )
            path = work.save_chore_entry(scope.toolang_root, scope.agent_name, entry, document)
            return {
                "chore": _chore_payload(work.ChoreEntry(name=path.stem, path=path, document=document))
            }

        @tool(name="psyche_list", description="List psyche definitions visible to the current agent.")
        def psyche_list(visibility: str = "all", context: ToolContext | None = None) -> dict[str, Any]:
            return {"psyches": _list_caps("psyche", visibility=visibility, context=context)}

        @tool(name="psyche_get", description="Get one psyche definition by name.")
        def psyche_get(
            name: str,
            visibility: str = "private",
            context: ToolContext | None = None,
        ) -> dict[str, Any]:
            return {"psyche": _get_cap("psyche", name, visibility=visibility, context=context)}

        @tool(name="psyche_create", description="Create one local psyche definition.")
        def psyche_create(
            name: str,
            body: str,
            visibility: str = "private",
            context: ToolContext | None = None,
        ) -> dict[str, Any]:
            return {"psyche": _create_cap("psyche", name, _plain_text(body), visibility=visibility, context=context)}

        @tool(name="psyche_update", description="Update one local psyche definition.")
        def psyche_update(
            name: str,
            body: str,
            visibility: str = "private",
            context: ToolContext | None = None,
        ) -> dict[str, Any]:
            return {"psyche": _update_cap("psyche", name, _plain_text(body), visibility=visibility, context=context)}

        @tool(name="psyche_delete", description="Delete one local psyche definition.")
        def psyche_delete(
            name: str,
            visibility: str = "private",
            context: ToolContext | None = None,
        ) -> dict[str, Any]:
            return _delete_cap("psyche", name, visibility=visibility, context=context)

        @tool(name="skill_list", description="List skill definitions visible to the current agent.")
        def skill_list(visibility: str = "all", context: ToolContext | None = None) -> dict[str, Any]:
            return {"skills": _list_caps("skill", visibility=visibility, context=context)}

        @tool(name="skill_get", description="Get one skill definition by name.")
        def skill_get(
            name: str,
            visibility: str = "private",
            context: ToolContext | None = None,
        ) -> dict[str, Any]:
            return {"skill": _get_cap("skill", name, visibility=visibility, context=context)}

        @tool(name="skill_create", description="Create one local skill definition.")
        def skill_create(
            name: str,
            description: str,
            body: str,
            visibility: str = "private",
            context: ToolContext | None = None,
        ) -> dict[str, Any]:
            text = _markdown_text(body, {"description": description})
            return {"skill": _create_cap("skill", name, text, visibility=visibility, context=context)}

        @tool(name="skill_update", description="Update one local skill definition.")
        def skill_update(
            name: str,
            description: str | None = None,
            body: str | None = None,
            visibility: str = "private",
            context: ToolContext | None = None,
        ) -> dict[str, Any]:
            scope = _scope(context)
            parsed = _load_local_cap_parts(scope, "skill", name, visibility=visibility)
            meta = dict(parsed.metadata)
            if description is not None:
                meta["description"] = description
            text = _markdown_text(parsed.content if body is None else body, meta)
            return {"skill": _update_cap("skill", name, text, visibility=visibility, context=context)}

        @tool(name="skill_delete", description="Delete one local skill definition.")
        def skill_delete(
            name: str,
            visibility: str = "private",
            context: ToolContext | None = None,
        ) -> dict[str, Any]:
            return _delete_cap("skill", name, visibility=visibility, context=context)

        @tool(name="service_list", description="List service definitions visible to the current agent.")
        def service_list(visibility: str = "all", context: ToolContext | None = None) -> dict[str, Any]:
            return {"services": _list_caps("service", visibility=visibility, context=context)}

        @tool(name="service_get", description="Get one service definition by name.")
        def service_get(
            name: str,
            visibility: str = "private",
            context: ToolContext | None = None,
        ) -> dict[str, Any]:
            return {"service": _get_cap("service", name, visibility=visibility, context=context)}

        @tool(name="service_create", description="Create one local service definition.")
        def service_create(
            name: str,
            description: str,
            transport: str,
            target: str,
            body: str = "",
            headers: dict[str, str] | None = None,
            env: list[str] | None = None,
            visibility: str = "private",
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
            return {"service": _create_cap("service", name, text, visibility=visibility, context=context)}

        @tool(name="service_update", description="Update one local service definition.")
        def service_update(
            name: str,
            description: str | None = None,
            transport: str | None = None,
            target: str | None = None,
            body: str | None = None,
            headers: dict[str, str] | None = None,
            env: list[str] | None = None,
            visibility: str = "private",
            context: ToolContext | None = None,
        ) -> dict[str, Any]:
            scope = _scope(context)
            parsed = _load_local_cap_parts(scope, "service", name, visibility=visibility)
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
            return {"service": _update_cap("service", name, text, visibility=visibility, context=context)}

        @tool(name="service_delete", description="Delete one local service definition.")
        def service_delete(
            name: str,
            visibility: str = "private",
            context: ToolContext | None = None,
        ) -> dict[str, Any]:
            return _delete_cap("service", name, visibility=visibility, context=context)

        @tool(name="prompt_list", description="List prompt definitions visible to the current agent.")
        def prompt_list(visibility: str = "all", context: ToolContext | None = None) -> dict[str, Any]:
            return {"prompts": _list_caps("prompt", visibility=visibility, context=context)}

        @tool(name="prompt_get", description="Get one prompt definition by name.")
        def prompt_get(
            name: str,
            visibility: str = "private",
            context: ToolContext | None = None,
        ) -> dict[str, Any]:
            return {"prompt": _get_cap("prompt", name, visibility=visibility, context=context)}

        @tool(name="prompt_create", description="Create one local prompt definition.")
        def prompt_create(
            name: str,
            body: str,
            visibility: str = "private",
            context: ToolContext | None = None,
        ) -> dict[str, Any]:
            return {"prompt": _create_cap("prompt", name, _plain_text(body), visibility=visibility, context=context)}

        @tool(name="prompt_update", description="Update one local prompt definition.")
        def prompt_update(
            name: str,
            body: str,
            visibility: str = "private",
            context: ToolContext | None = None,
        ) -> dict[str, Any]:
            return {"prompt": _update_cap("prompt", name, _plain_text(body), visibility=visibility, context=context)}

        @tool(name="prompt_delete", description="Delete one local prompt definition.")
        def prompt_delete(
            name: str,
            visibility: str = "private",
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
    toolang_root: Path
    agent_name: str


def create_tool_set(config: Mapping[str, Any]) -> AgentToolSet:
    """Create the agent_state tool plugin."""

    return AgentStatePlugin(config=dict(config))


def _scope(context: ToolContext | None) -> _AgentStateScope:
    if context is None:
        raise ToolangError("agent_state tool context is required")
    home = context.home.resolve()
    if home.parent.name != "agents":
        raise ToolangError(f"agent_state tool requires an agent home under agents/: {home}")
    return _AgentStateScope(toolang_root=home.parent.parent, agent_name=home.name)


def _task_payload(entry: work.TaskEntry) -> dict[str, Any]:
    document = entry.document
    return {
        "id": document.task_id(),
        "thread_id": document.thread_id(),
        "path": str(entry.path),
        "title": document.title,
        "state": document.state,
        "stage": document.stage,
        "body": document.body,
    }


def _chore_payload(entry: work.ChoreEntry) -> dict[str, Any]:
    document = entry.document
    return {
        "id": document.chore_id(),
        "thread_id": document.thread_id(),
        "path": str(entry.path),
        "title": document.title,
        "state": document.state,
        "schedule": document.schedule,
        "body": document.body,
    }


def _list_caps(kind: CapKind, *, visibility: str, context: ToolContext | None) -> list[dict[str, Any]]:
    scope = _scope(context)
    filter_visibility = _visibility_filter(visibility)
    entries = caps.list_entries(
        scope.toolang_root,
        scope.agent_name,
        visibility=None if filter_visibility == "all" else filter_visibility,
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
    entry = _find_cap_entry(scope, kind, name, visibility=_visibility_filter(visibility))
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
    if _local_cap_exists(scope, kind, name, visibility=cap_visibility):
        raise ToolangError(f"local {kind} already exists: {name}")
    caps.put_local_entry_text(
        scope.toolang_root,
        scope.agent_name,
        visibility=cap_visibility,
        kind=kind,
        name=name,
        text=text,
    )
    entry = _find_cap_entry(scope, kind, name, visibility=cap_visibility, source_form="file")
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
    _find_cap_entry(scope, kind, name, visibility=cap_visibility, source_form="file")
    caps.put_local_entry_text(
        scope.toolang_root,
        scope.agent_name,
        visibility=cap_visibility,
        kind=kind,
        name=name,
        text=text,
    )
    entry = _find_cap_entry(scope, kind, name, visibility=cap_visibility, source_form="file")
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
    entry = _find_cap_entry(scope, kind, name, visibility=cap_visibility, source_form="file")
    deleted_path = scope.toolang_root / entry.path
    if entry.shape == "dir":
        deleted_path = deleted_path.parent
    removed = caps.remove_local_entry(
        scope.toolang_root,
        scope.agent_name,
        visibility=cap_visibility,
        kind=kind,
        name=name,
    )
    if not removed:
        raise ToolangError(f"local {kind} not found: {name}")
    return {
        "kind": kind,
        "name": name,
        "visibility": cap_visibility,
        "path": str(deleted_path),
        "deleted": True,
    }


def _find_cap_entry(
    scope: _AgentStateScope,
    kind: CapKind,
    name: str,
    *,
    visibility: VisibilityFilter | PreparedVisibility,
    source_origin: str | None = None,
    source_form: str | None = None,
) -> PreparedEntry:
    entry_visibility = None if visibility == "all" else visibility
    entries = caps.list_entries(
        scope.toolang_root,
        scope.agent_name,
        visibility=entry_visibility,
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
    return sorted(matches, key=lambda entry: caps.entry_ref(entry, agent_name=scope.agent_name))[0]


def _local_cap_exists(
    scope: _AgentStateScope,
    kind: CapKind,
    name: str,
    *,
    visibility: PreparedVisibility,
) -> bool:
    try:
        _find_cap_entry(scope, kind, name, visibility=visibility, source_form="file")
    except ToolangError:
        return False
    return True


def _cap_payload(
    scope: _AgentStateScope,
    entry: PreparedEntry,
    *,
    include_content: bool = False,
) -> dict[str, Any]:
    visibility = caps.entry_visibility(entry, agent_name=scope.agent_name)
    item: dict[str, Any] = {
        "kind": entry.kind,
        "name": entry.name,
        "scope": caps.entry_scope(entry, agent_name=scope.agent_name),
        "origin": caps.entry_origin(entry),
        "form": caps.entry_form(entry),
        "ref": caps.entry_ref(entry, agent_name=scope.agent_name),
        "path": str(scope.toolang_root / entry.path),
        "definition_file": caps.entry_definition_file(entry),
        "meta": dict(entry.meta),
    }
    line = caps.entry_line(entry)
    if line is not None:
        item["line"] = line
    if include_content and entry.source.form == "file":
        item["content"] = caps.load_local_entry_text(
            scope.toolang_root,
            scope.agent_name,
            visibility=visibility,
            kind=entry.kind,
            name=entry.name,
        )
    return item


def _load_local_cap_parts(
    scope: _AgentStateScope,
    kind: CapKind,
    name: str,
    *,
    visibility: str,
) -> frontmatter.Post:
    cap_visibility = _visibility(visibility)
    text = caps.load_local_entry_text(
        scope.toolang_root,
        scope.agent_name,
        visibility=cap_visibility,
        kind=kind,
        name=name,
    )
    return frontmatter.loads(text)


def _visibility(value: str) -> PreparedVisibility:
    text = value.strip().lower()
    if text not in {"private", "shared"}:
        raise ToolangError(f"visibility must be private or shared: {value}")
    return cast(PreparedVisibility, text)


def _visibility_filter(value: str) -> VisibilityFilter:
    text = value.strip().lower()
    if text not in {"all", "private", "shared"}:
        raise ToolangError(f"visibility must be all, private, or shared: {value}")
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
