from __future__ import annotations

import json
import re
import shlex
from dataclasses import asdict
from pathlib import Path
from re import Match
from typing import Any

from toolang.ast import Program, Thunk
from toolang.errors import ToolangError

MODEL_DIRECTIVE_RE = re.compile(r"^model\s*=\s*(.*)$")
PROMPT_CALL_RE = re.compile(r"^/([A-Za-z_][\w-]*)(?:\s+(.*))?$")
TEMPLATE_VAR_RE = re.compile(r"\{\{\s*([A-Za-z_][\w-]*)\s*\}\}")
DEFAULT_MODEL = "gpt-5"


def build_runtime_context(program: Program, thunk: Thunk, program_path: Path) -> dict[str, Any]:
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


def execute_thunk(
    program: Program,
    thunk: Thunk,
    program_path: Path,
    *,
    user_input: str | None,
    model: str | None = None,
) -> str:
    openai_client = _create_openai_client()

    if thunk.input_name and user_input is None:
        raise ToolangError(
            f"Thunk {thunk.name or '<default>'} requires user input. Pass --input or pipe stdin."
        )
    if not thunk.input_name and user_input is not None:
        raise ToolangError(
            f"Thunk {thunk.name or '<default>'} does not accept user input."
        )

    output_decl = program.get_decl("struct", thunk.output) if thunk.output else None
    runtime_context = build_runtime_context(program, thunk, program_path)
    expanded_user_input = expand_prompt_input(program, user_input) if user_input else ""

    developer_sections = [
        "You are the Toolang runtime.",
        "Follow the selected thunk.",
        "Treat the thunk body as system-side instruction.",
        "Respect declared uses, inline capabilities, stashes, and directives.",
        "Runtime context:",
        json.dumps(runtime_context, indent=2, ensure_ascii=False),
        "Source file:",
        program_path.read_text(encoding="utf-8"),
        "Thunk instruction:",
        thunk.prompt,
    ]
    if output_decl:
        developer_sections.extend(
            [
                f"Expected output declaration: {output_decl.name}",
                output_decl.body or "{}",
            ]
        )
        if output_decl.language == "json":
            developer_sections.append("Return valid JSON only.")

    developer_message = "\n\n".join(section for section in developer_sections if section.strip())
    user_message = (
        expanded_user_input
        if thunk.input_name
        else "Execute the selected thunk with no external user message."
    )

    response = openai_client.responses.create(
        model=infer_model(thunk, override=model),
        input=[
            {"role": "developer", "content": developer_message},
            {"role": "user", "content": user_message},
        ],
    )
    return _coerce_response_text(response)


def _create_openai_client() -> Any:
    try:
        from openai import OpenAI
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise ToolangError(
            "The 'openai' package is not installed. Run 'uv add openai' to enable toolang invoke."
        ) from exc
    return OpenAI()


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


def _coerce_response_text(response: Any) -> str:
    text = getattr(response, "output_text", None)
    if isinstance(text, str) and text.strip():
        return text

    collected: list[str] = []
    for item in getattr(response, "output", []):
        if getattr(item, "type", None) != "message":
            continue
        for content in getattr(item, "content", []):
            content_type = getattr(content, "type", None)
            if content_type in {"output_text", "text"} and getattr(content, "text", None):
                collected.append(content.text)

    if collected:
        return "".join(collected)
    raise ToolangError("Model response did not contain text output.")
