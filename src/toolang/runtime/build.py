from __future__ import annotations

import json
import re
import shlex
from dataclasses import asdict, dataclass
from pathlib import Path
from re import Match
from typing import Any

from toolang.caps import load_prepared_caps
from toolang.concepts.layout import AgentHome
from toolang.concepts.persisted import (
    TaskMirrorState,
    find_local_task,
    task_id_from_thread_id,
)
from toolang.errors import ToolangError
from toolang.program import Program
from toolang.program.ast import Thunk
from toolang.tools import ToolRuntime

from .messages import Message, context_prompt

MODEL_DIRECTIVE_RE = re.compile(r"^model\s*=\s*(.*)$")
PROMPT_CALL_RE = re.compile(r"^/([A-Za-z_][\w-]*)(?:\s+(.*))?$")
TEMPLATE_VAR_RE = re.compile(r"\{\{\s*([A-Za-z_][\w-]*)\s*\}\}")
DEFAULT_MODEL = "gpt-5"


@dataclass(frozen=True, slots=True)
class PromptBuild:
    model: str
    raw_input: str | None
    expanded_input: str | None
    message_context: dict[str, Any] | None
    runtime_context: dict[str, Any]
    developer_message: str
    messages: list[dict[str, Any]]
    source_text: str
    tool_runtime: ToolRuntime | None = None


def infer_model(thunk: Thunk, override: str | None = None) -> str:
    if override:
        return override
    for directive in thunk.directives:
        match = MODEL_DIRECTIVE_RE.match(directive)
        if not match:
            continue
        raw = match.group(1).strip()
        if not raw or raw == "default":
            return DEFAULT_MODEL
        for candidate in [item.strip() for item in raw.split(",")]:
            if candidate and candidate != "default":
                return candidate
    return DEFAULT_MODEL


def expand_prompt_input(program: Program, raw_input: str) -> str:
    lines = raw_input.splitlines()
    if not lines:
        return raw_input

    first_line = lines[0].strip()
    match = PROMPT_CALL_RE.match(first_line)
    if not match:
        return raw_input

    prompt_name = match.group(1)
    prompt_decl = program.get_decl("prompt", prompt_name)
    if prompt_decl is None:
        raise ToolangError(f"Prompt template not found: {prompt_name}")

    args = _parse_prompt_args(
        match.group(2) or "",
        known={param.name for param in prompt_decl.params},
        prompt_name=prompt_name,
    )
    body_lines = lines[1:]
    if body_lines and not body_lines[0].strip():
        body_lines = body_lines[1:]
    bindings = {"input": "\n".join(body_lines).strip("\n")}

    for param in prompt_decl.params:
        if param.name in args:
            bindings[param.name] = args[param.name]
        elif param.optional:
            bindings[param.name] = ""
        else:
            raise ToolangError(
                f"Missing required prompt argument {param.name!r} for /{prompt_name}."
            )

    return TEMPLATE_VAR_RE.sub(lambda item: _render_template_var(item, bindings), prompt_decl.body)


def build_invoke_prompt(
    prepared,
    thunk: Thunk,
    *,
    user_input: str | None,
    model: str | None,
    origin: str,
    thread_id: str | None,
    sandbox: str,
    input_meta: dict[str, Any] | None = None,
    tool_runtime: ToolRuntime | None = None,
) -> PromptBuild:
    if thunk.input_name and user_input is None:
        raise ToolangError(
            f"Thunk {thunk.name or '<default>'} requires user input. Pass --input or pipe stdin."
        )
    if not thunk.input_name and user_input is not None:
        raise ToolangError(
            f"Thunk {thunk.name or '<default>'} does not accept user input."
        )

    expanded_user_input = expand_prompt_input(prepared.program, user_input) if user_input else ""
    source_text = prepared.ref.source.read_text(encoding="utf-8")
    runtime_context = _build_runtime_context(
        prepared,
        thunk,
        sandbox=sandbox,
        origin=origin,
        thread_id=thread_id,
        raw_input=user_input,
        input_meta=input_meta,
        tool_runtime=tool_runtime,
    )
    developer_message = _build_developer_message(
        prepared.program,
        thunk,
        runtime_context=runtime_context,
        source_text=source_text,
    )
    user_message = (
        expanded_user_input
        if thunk.input_name
        else "Execute the selected thunk with no external user message."
    )
    return PromptBuild(
        model=infer_model(thunk, override=model),
        raw_input=user_input,
        expanded_input=expanded_user_input if thunk.input_name else None,
        message_context=None,
        runtime_context=runtime_context,
        developer_message=developer_message,
        messages=[
            {"role": "developer", "content": developer_message},
            {"role": "user", "content": user_message},
        ],
        source_text=source_text,
        tool_runtime=tool_runtime,
    )


def build_chat_prompt(
    prepared,
    thunk: Thunk,
    *,
    history_messages: list[dict[str, Any]],
    message: Message,
    model: str | None,
    sandbox: str,
    tool_runtime: ToolRuntime | None = None,
) -> PromptBuild:
    source_text = prepared.ref.source.read_text(encoding="utf-8")
    runtime_context = _build_runtime_context(
        prepared,
        thunk,
        sandbox=sandbox,
        origin=message.origin,
        thread_id=message.thread_id,
        raw_input=message.text,
        input_meta=message.meta,
        tool_runtime=tool_runtime,
    )
    developer_message = _build_developer_message(
        prepared.program,
        thunk,
        runtime_context=runtime_context,
        source_text=source_text,
        message=message,
    )
    return PromptBuild(
        model=infer_model(thunk, override=model),
        raw_input=message.text,
        expanded_input=None,
        message_context=_message_context(message),
        runtime_context=runtime_context,
        developer_message=developer_message,
        messages=[
            {"role": "developer", "content": developer_message},
            *history_messages,
        ],
        source_text=source_text,
        tool_runtime=tool_runtime,
    )


def build_prompt_error_trace_data(
    prepared,
    thunk: Thunk,
    *,
    origin: str,
    thread_id: str | None,
    sandbox: str,
    model: str | None,
    raw_input: str | None,
    message: Message | None = None,
    input_meta: dict[str, Any] | None = None,
    tool_runtime: ToolRuntime | None = None,
) -> dict[str, Any]:
    runtime_context = _build_runtime_context(
        prepared,
        thunk,
        sandbox=sandbox,
        origin=origin,
        thread_id=thread_id,
        raw_input=raw_input,
        input_meta=input_meta if message is None else message.meta,
        tool_runtime=tool_runtime,
    )
    source_text = prepared.ref.source.read_text(encoding="utf-8")
    return {
        "model": infer_model(thunk, override=model),
        "raw_input": raw_input,
        "expanded_input": None,
        "message_context": _message_context(message) if message is not None else None,
        "runtime_context": runtime_context,
        "developer_message": "",
        "messages": [],
        "source_text": source_text,
    }


def _build_runtime_context(
    prepared,
    thunk: Thunk,
    *,
    sandbox: str,
    origin: str,
    thread_id: str | None,
    raw_input: str | None,
    input_meta: dict[str, Any] | None,
    tool_runtime: ToolRuntime | None,
) -> dict[str, Any]:
    visible_caps = load_prepared_caps(prepared)
    context = {
        "agent": {
            "uri": prepared.ref.uri,
            "id": prepared.ref.id,
            "name": prepared.ref.name,
            "kind": prepared.ref.kind,
        },
        "working_directory": str(prepared.ref.home),
        "sandbox": sandbox,
        "cap_scopes": list(prepared.cap_scopes.labels()),
        "origin": origin,
        "thread_id": thread_id,
        "visible_caps": visible_caps.model_dump(mode="python"),
        "program": _program_context(prepared.program, thunk, prepared.ref.source),
        "tools": tool_runtime.enabled_families() if tool_runtime is not None else [],
    }
    task_context = _task_context(
        prepared,
        origin=origin,
        thread_id=thread_id,
        raw_input=raw_input,
        input_meta=input_meta,
        visible_caps=visible_caps,
    )
    if task_context is not None:
        context["task"] = task_context["task"]
        context["task_services"] = task_context["task_services"]
    return context


def _program_context(program: Program, thunk: Thunk, program_path: Path) -> dict[str, Any]:
    structs = {
        decl.name: {"language": decl.language, "body": decl.body}
        for decl in program.declarations_by_kind("struct")
    }
    stashes = {
        decl.name: {"language": decl.language, "body": decl.body}
        for decl in program.declarations_by_kind("stash")
    }
    cap_declarations = [
        {
            "kind": decl.kind,
            "name": decl.name,
            "language": decl.language,
            "body": decl.body,
            "params": [asdict(param) for param in decl.params],
        }
        for decl in program.declarations
        if decl.kind in {"service", "prompt", "psyche"}
    ]
    return {
        "program_path": str(program_path),
        "uses": [{"kind": item.kind, "reference": item.reference} for item in program.uses],
        "cap_declarations": cap_declarations,
        "structs": structs,
        "stashes": stashes,
        "thunk": {
            "name": thunk.name,
            "input_name": thunk.input_name,
            "output": thunk.output,
            "directives": thunk.directives,
            "prompt": thunk.prompt,
        },
    }


def _build_developer_message(
    program: Program,
    thunk: Thunk,
    *,
    runtime_context: dict[str, Any],
    source_text: str,
    message: Message | None = None,
) -> str:
    output_decl = program.get_decl("struct", thunk.output) if thunk.output else None
    developer_sections = [
        "You are the Toolang runtime.",
        "Follow the selected thunk.",
        "Treat the thunk body as system-side instruction.",
        "Respect declared uses, visible capabilities, stashes, and directives.",
    ]
    if message is not None:
        developer_sections.append(context_prompt(message))
    task_prompt = _task_prompt(runtime_context)
    if task_prompt is not None:
        developer_sections.append(task_prompt)
    developer_sections.extend(
        [
            "Runtime context:",
            json.dumps(runtime_context, indent=2, ensure_ascii=False),
            "Source file:",
            source_text,
            "Thunk instruction:",
            thunk.prompt,
        ]
    )
    if output_decl:
        developer_sections.extend(
            [
                f"Expected output declaration: {output_decl.name}",
                output_decl.body or "{}",
            ]
        )
        if output_decl.language == "json":
            developer_sections.append("Return valid JSON only.")
    return "\n\n".join(section for section in developer_sections if section.strip())


def _task_context(
    prepared,
    *,
    origin: str,
    thread_id: str | None,
    raw_input: str | None,
    input_meta: dict[str, Any] | None,
    visible_caps,
) -> dict[str, Any] | None:
    if origin != "task" or thread_id is None:
        return None

    local_task_id = task_id_from_thread_id(thread_id)
    if local_task_id is not None:
        room = AgentHome.resolve(prepared.ref.home).room(prepared.ref.name)
        loaded = find_local_task(room.tasks_dir, local_task_id)
        if loaded is not None:
            path, task = loaded
            mirror_state = (
                TaskMirrorState.load(room.task_mirrors_path)
                if room.task_mirrors_path.exists()
                else TaskMirrorState()
            )
            mirror = mirror_state.find_by_local_task_id(local_task_id)
            provider = "local" if mirror is None else mirror.provider
            service_available = _task_service_available(visible_caps.services, provider)
            return {
                "task": {
                    "provider": provider,
                    "ref": task.thread_id() if mirror is None else mirror.remote_ref,
                    "name": path.stem,
                    "body": task.body,
                    "status": task.status,
                    "requester": task.requester,
                    "thread_id": task.thread_id(),
                    "path": str(path),
                },
                "task_services": {
                    "provider": provider,
                    "read": True if mirror is None else service_available,
                    "write": True if mirror is None else service_available,
                    "comment": True if mirror is None else service_available,
                    "path": str(path),
                },
            }

    provider = _task_provider(thread_id)
    meta = dict(input_meta or {})
    service_available = _task_service_available(visible_caps.services, provider)
    return {
        "task": {
            "provider": provider,
            "ref": _task_text(meta.get("ref")) or thread_id,
            "name": _task_text(meta.get("name")) or _task_text(meta.get("title")),
            "body": _task_text(meta.get("body")) or raw_input or "",
            "status": _task_text(meta.get("status")),
            "requester": _task_text(meta.get("requester")) or "service",
            "thread_id": thread_id,
            "path": None,
        },
        "task_services": {
            "provider": provider,
            "read": service_available,
            "write": service_available,
            "comment": service_available,
            "path": None,
        },
    }


def _task_prompt(runtime_context: dict[str, Any]) -> str | None:
    task = runtime_context.get("task")
    services = runtime_context.get("task_services")
    if not isinstance(task, dict) or not isinstance(services, dict):
        return None

    provider = _task_text(task.get("provider")) or "unknown"
    can_read = bool(services.get("read"))
    can_write = bool(services.get("write"))
    can_comment = bool(services.get("comment"))
    local_path = _task_text(services.get("path")) or _task_text(task.get("path"))
    lines = [
        "Task execution protocol:",
        "- You are handling one task-driven turn.",
        "- Understand the current task before acting.",
        "- Keep the task itself as the durable record of progress and outcome.",
        f"- Task provider: {provider}.",
        f"- Task read available: {'yes' if can_read else 'no'}.",
        f"- Task write available: {'yes' if can_write else 'no'}.",
        f"- Task comment available: {'yes' if can_comment else 'no'}.",
    ]
    if provider == "local":
        lines.extend(
            [
                "- This task is backed by a local markdown file.",
                f"- Update the task file directly at: {local_path or '<unknown path>'}.",
                "- Keep front matter minimal: id, requester, status, paused.",
                "- Use the markdown body as the durable task input and append progress or outcome notes there.",
            ]
        )
    else:
        lines.extend(
            [
                f"- Use the local mirrored task file as the working copy at: {local_path or '<unknown path>'}.",
                "- Use configured task services for provider-specific updates.",
            ]
        )
    if not can_read and local_path is None:
        lines.append("- If task read is unavailable, do not continue execution. Explain the missing configuration.")
    elif not can_write:
        lines.append("- If task write is unavailable, you may proceed, but you must clearly state that the task could not be updated.")
    else:
        lines.append("- Update the task at important milestones and before finishing.")
    return "\n".join(lines)


def _task_provider(thread_id: str) -> str:
    if thread_id.startswith("task:"):
        remainder = thread_id.removeprefix("task:")
        if ":" in remainder:
            return remainder.split(":", 1)[0]
        if "/" in remainder:
            return remainder.split("/", 1)[0]
        return remainder or "unknown"
    return "unknown"


def _task_service_available(services: list[Any], provider: str) -> bool:
    if provider == "local":
        return True
    for item in services:
        if item.name == provider:
            return True
        front_matter = item.front_matter
        target = getattr(front_matter, "target", None)
        if isinstance(target, str) and target.strip() == provider:
            return True
    return False


def _task_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text


def _message_context(message: Message) -> dict[str, Any]:
    return {
        "origin": message.origin,
        "channel": message.channel,
        "sender": message.sender,
        "thread_id": message.thread_id,
        "text": message.text,
        "meta": dict(message.meta),
    }


def _parse_prompt_args(raw_args: str, *, known: set[str], prompt_name: str) -> dict[str, str]:
    if not raw_args.strip():
        return {}
    try:
        tokens = shlex.split(raw_args)
    except ValueError as exc:
        raise ToolangError(f"Invalid prompt argument syntax: {exc}") from exc

    args: dict[str, str] = {}
    for token in tokens:
        if "=" not in token:
            raise ToolangError(f"Prompt argument must use key=value syntax: {token!r}")
        name, value = token.split("=", 1)
        if name not in known:
            raise ToolangError(f"Unknown prompt argument {name!r} for /{prompt_name}.")
        if name in args:
            raise ToolangError(f"Duplicate prompt argument {name!r} for /{prompt_name}.")
        args[name] = value
    return args


def _render_template_var(match: Match[str], bindings: dict[str, str]) -> str:
    name = match.group(1)
    if name not in bindings:
        raise ToolangError(f"Unknown template variable {name!r}.")
    return bindings[name]
