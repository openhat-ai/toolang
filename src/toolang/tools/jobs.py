"""Task and chore job tool plugin."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from toolang import work
from toolang.base.error import ToolangError
from toolang.base.protocols.tool import Tool, ToolPlugin
from toolang.base.types.tool import ToolContext
from toolang.base.utils.function_tools import create_function_tool, tool


@dataclass(slots=True)
class JobsPlugin:
    """Tools for managing the current agent's task and chore documents."""

    config: dict[str, Any]
    name: str = "jobs"
    description: str | None = "Inspect, create, and update local task and chore documents."
    _tools: dict[str, Tool] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._tools = self._build_tools()

    def tools(self) -> Mapping[str, Tool]:
        return dict(self._tools)

    def _build_tools(self) -> dict[str, Tool]:
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

        return {
            "task_list": create_function_tool(task_list),
            "task_get": create_function_tool(task_get),
            "task_create": create_function_tool(task_create),
            "task_update": create_function_tool(task_update),
            "chore_list": create_function_tool(chore_list),
            "chore_get": create_function_tool(chore_get),
            "chore_create": create_function_tool(chore_create),
            "chore_update": create_function_tool(chore_update),
        }


@dataclass(frozen=True, slots=True)
class _JobsScope:
    toolang_root: Path
    agent_name: str


def create_tool(config: Mapping[str, Any]) -> ToolPlugin:
    """Create the jobs tool plugin."""

    return JobsPlugin(config=dict(config))


def _scope(context: ToolContext | None) -> _JobsScope:
    if context is None:
        raise ToolangError("jobs tool context is required")
    home = context.home.resolve()
    if home.parent.name != "agents":
        raise ToolangError(f"jobs tool requires an agent home under agents/: {home}")
    return _JobsScope(toolang_root=home.parent.parent, agent_name=home.name)


def _task_payload(entry: work.TaskEntry) -> dict[str, Any]:
    document = entry.document
    return {
        "id": document.task_id(),
        "thread_id": document.thread_id(),
        "path": str(entry.path),
        "title": document.title,
        "remote_ref": document.remote_ref(),
        "remote_status": document.remote_status(),
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


def _blank_to_none(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    return text or None
