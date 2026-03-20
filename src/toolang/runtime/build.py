from __future__ import annotations

import json
import re
import shlex
from dataclasses import asdict, dataclass
from pathlib import Path
from re import Match
from typing import Any

from toolang.caps_view import load_prepared_caps
from toolang.errors import ToolangError
from toolang.syntax import Program, Thunk

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
    source_text = prepared.source_path.read_text(encoding="utf-8")
    runtime_context = _build_runtime_context(
        prepared,
        thunk,
        sandbox=sandbox,
        origin=origin,
        thread_id=thread_id,
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
    )


def build_chat_prompt(
    prepared,
    thunk: Thunk,
    *,
    history_messages: list[dict[str, Any]],
    message: Message,
    model: str | None,
    sandbox: str,
) -> PromptBuild:
    source_text = prepared.source_path.read_text(encoding="utf-8")
    runtime_context = _build_runtime_context(
        prepared,
        thunk,
        sandbox=sandbox,
        origin=message.origin,
        thread_id=message.thread_id,
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
) -> dict[str, Any]:
    source_text = prepared.source_path.read_text(encoding="utf-8")
    return {
        "model": infer_model(thunk, override=model),
        "raw_input": raw_input,
        "expanded_input": None,
        "message_context": _message_context(message) if message is not None else None,
        "runtime_context": {
            "agent": {
                "uri": prepared.ref.agent_uri,
                "id": prepared.ref.agent_id,
                "name": prepared.ref.agent_name,
                "kind": prepared.ref.agent_kind,
            },
            "working_directory": str(prepared.ref.agent_home),
            "sandbox": sandbox,
            "cap_scopes": list(prepared.cap_scopes.labels()),
            "origin": origin,
            "thread_id": thread_id,
        },
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
) -> dict[str, Any]:
    caps = load_prepared_caps(prepared).model_dump(mode="python")
    return {
        "agent": {
            "uri": prepared.ref.agent_uri,
            "id": prepared.ref.agent_id,
            "name": prepared.ref.agent_name,
            "kind": prepared.ref.agent_kind,
        },
        "working_directory": str(prepared.ref.agent_home),
        "sandbox": sandbox,
        "cap_scopes": list(prepared.cap_scopes.labels()),
        "origin": origin,
        "thread_id": thread_id,
        "visible_caps": caps,
        "program": _program_context(prepared.program, thunk, prepared.source_path),
    }


def _program_context(program: Program, thunk: Thunk, program_path: Path) -> dict[str, Any]:
    structs = {
        decl.name: {"language": decl.language, "body": decl.body}
        for decl in program.declarations_by_kind("struct")
    }
    stashes = {
        decl.name: {"language": decl.language, "body": decl.body}
        for decl in program.declarations_by_kind("stash")
    }
    inline_caps = [
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
        "inline_caps": inline_caps,
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
