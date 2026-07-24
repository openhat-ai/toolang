"""Executable input expansion for Toolang programs."""

from __future__ import annotations

import re
import shlex

from toolang.base.errors import ToolangError
from toolang.common.template import render_text_template

from .ast import Parameter, Program

_PROMPT_CALL_RE = re.compile(r"^/([A-Za-z_][\w-]*)(?:\s+(.*))?$")


def expand_program_input(program: Program, raw_input: str) -> str:
    """Expand a prompt directive in executable input."""

    if not raw_input:
        return raw_input
    lines = raw_input.splitlines()
    if not lines:
        return raw_input
    match = _PROMPT_CALL_RE.match(lines[0].strip())
    if not match:
        return raw_input

    prompt_name = match.group(1)
    prompt_cap = next(
        (
            item
            for item in program.caps
            if item.kind == "prompt" and item.name == prompt_name
        ),
        None,
    )
    if prompt_cap is None:
        raise ToolangError(f"Prompt not found: {prompt_name}")

    bindings = _parse_prompt_args(
        match.group(2) or "",
        params=prompt_cap.params,
        prompt_name=prompt_name,
    )
    extra_lines = lines[1:]
    if extra_lines and not extra_lines[0].strip():
        extra_lines = extra_lines[1:]
    extra_text = "\n".join(extra_lines).strip("\n")
    return render_text_template(
        prompt_cap.body,
        {"_": extra_text, **bindings},
    ).strip()


def _parse_prompt_args(
    raw_args: str,
    *,
    params: tuple[Parameter, ...],
    prompt_name: str,
) -> dict[str, str]:
    if not raw_args.strip():
        tokens: list[str] = []
    else:
        try:
            tokens = shlex.split(raw_args)
        except ValueError as exc:
            raise ToolangError(f"Invalid prompt argument syntax: {exc}") from exc

    bindings: dict[str, str] = {}
    positionals: list[str] = []
    known = {param.name for param in params}
    for token in tokens:
        if "=" in token:
            candidate, value = token.split("=", 1)
            if candidate in known:
                if candidate in bindings:
                    raise ToolangError(
                        f"Duplicate prompt argument {candidate!r} for /{prompt_name}."
                    )
                bindings[candidate] = value
                continue
        positionals.append(token)

    positional_index = 0
    for param in params:
        if param.name in bindings:
            continue
        if positional_index < len(positionals):
            bindings[param.name] = positionals[positional_index]
            positional_index += 1
            continue
        if param.optional:
            bindings[param.name] = ""
            continue
        raise ToolangError(
            f"Missing required prompt argument {param.name!r} for /{prompt_name}."
        )

    if positional_index < len(positionals):
        raise ToolangError(f"Too many prompt arguments for /{prompt_name}.")
    return bindings
