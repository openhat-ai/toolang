from __future__ import annotations

import re

from toolang.ast import DeclBlock, ParamDecl, Program, SourceSpan, Thunk, UseDecl
from toolang.errors import ToolangError

USE_RE = re.compile(r"^use\s+(skill|service|prompt|psyche)\s+(.+)$")
DECL_RE = re.compile(
    r"^(service|prompt|psyche|struct|stash)\s+([A-Za-z_][\w-]*)"
    r"(?:\(([^)]*)\))?(?:\s*:\s*(.*))?$"
)
THUNK_RE = re.compile(
    r"^thunk(?:\s+([A-Za-z_][\w-]*))?(?:\s*\(\s*([A-Za-z_][\w-]*)\s*\))?"
    r"(?:\s*=>\s*([A-Za-z_][\w-]*))?\s*:\s*$"
)
FENCE_START_RE = re.compile(r"^(`{3,})([A-Za-z0-9_-]+)?\s*$")
COLLECTION_DIRECTIVE_RE = re.compile(r"^(skills|services|tools|thunks)\s*(=|-)\s*(.*)$")
MODEL_DIRECTIVE_RE = re.compile(r"^model\s*=\s*(.*)$")


def parse_program(source: str) -> Program:
    lines = source.splitlines()
    program = Program()
    index = 0

    while index < len(lines):
        raw = lines[index]
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            index += 1
            continue
        if raw.startswith((" ", "\t")):
            raise ToolangError(f"Unexpected indentation at line {index + 1}.")

        use_match = USE_RE.match(_strip_comment(raw))
        if use_match:
            program.uses.append(
                UseDecl(
                    kind=use_match.group(1),
                    reference=use_match.group(2).strip(),
                    span=SourceSpan(index + 1),
                )
            )
            index += 1
            continue

        thunk_match = THUNK_RE.match(_strip_comment(raw))
        if thunk_match:
            thunk, index = _parse_thunk(lines, index, thunk_match)
            program.thunks.append(thunk)
            continue

        decl_match = DECL_RE.match(_strip_comment(raw))
        if decl_match:
            declaration, index = _parse_decl(lines, index, decl_match)
            program.declarations.append(declaration)
            continue

        raise ToolangError(f"Unsupported statement at line {index + 1}: {raw!r}")

    return program


def _parse_decl(lines: list[str], start: int, match: re.Match[str]) -> tuple[DeclBlock, int]:
    kind = match.group(1)
    name = match.group(2)
    raw_params = match.group(3)
    suffix = (match.group(4) or "").strip()
    line_number = start + 1
    index = start + 1
    params = _parse_params(raw_params, line_number) if raw_params is not None else []

    if raw_params is not None and kind != "prompt":
        raise ToolangError(f"Only prompt declarations may declare parameters at line {line_number}.")
    if kind == "prompt" and any(param.name == "input" for param in params):
        raise ToolangError(
            f"Prompt parameters may not use reserved name 'input' at line {line_number}."
        )

    if not suffix:
        return (
            DeclBlock(
                kind=kind,
                name=name,
                language=None,
                body="",
                header_suffix="",
                params=params,
                span=SourceSpan(line_number),
            ),
            index,
        )

    fence_match = FENCE_START_RE.match(suffix)
    if not fence_match:
        raise ToolangError(f"Expected fenced block after {kind} {name} at line {line_number}.")

    language = fence_match.group(2)
    opening_ticks = fence_match.group(1)
    body_lines: list[str] = []
    while index < len(lines):
        raw = lines[index]
        if _is_fence_close(raw.strip(), opening_ticks):
            return (
                DeclBlock(
                    kind=kind,
                    name=name,
                    language=language,
                    body="\n".join(body_lines).rstrip(),
                    header_suffix=suffix,
                    params=params,
                    span=SourceSpan(line_number),
                ),
                index + 1,
            )
        body_lines.append(raw)
        index += 1

    raise ToolangError(
        f"Unterminated fenced block for {kind} {name} starting at line {line_number}."
    )


def _parse_thunk(
    lines: list[str], start: int, match: re.Match[str]
) -> tuple[Thunk, int]:
    thunk = Thunk(
        name=match.group(1),
        input_name=match.group(2),
        output=match.group(3),
        span=SourceSpan(start + 1),
    )
    index = start + 1
    block: list[str] = []

    while index < len(lines):
        raw = lines[index]
        if raw.strip() and not raw.startswith((" ", "\t")):
            break
        block.append(raw)
        index += 1

    prompt_started = False
    prompt_lines: list[str] = []
    for raw in block:
        if not raw.strip():
            if prompt_started:
                prompt_lines.append("")
            continue

        if not raw.startswith((" ", "\t")):
            raise ToolangError(f"Thunk body must be indented under line {start + 1}: {raw!r}")

        content = raw.lstrip()
        stripped_content = _strip_comment(content).strip()
        if not stripped_content:
            if prompt_started:
                prompt_lines.append("")
            continue

        if not prompt_started and _looks_like_directive(content):
            thunk.directives.append(_strip_comment(content).strip())
            continue

        prompt_started = True
        prompt_lines.append(content)

    thunk.prompt = "\n".join(prompt_lines).strip()
    if not thunk.prompt:
        raise ToolangError(f"Thunk at line {start + 1} is missing prompt text.")
    return thunk, index


def _parse_params(raw_params: str, line_number: int) -> list[ParamDecl]:
    params: list[ParamDecl] = []
    seen: set[str] = set()
    for token in raw_params.split(","):
        item = token.strip()
        if not item:
            raise ToolangError(f"Empty parameter in declaration at line {line_number}.")
        optional = item.endswith("?")
        name = item[:-1] if optional else item
        if not re.fullmatch(r"[A-Za-z_][\w-]*", name):
            raise ToolangError(f"Invalid parameter name {item!r} at line {line_number}.")
        if name in seen:
            raise ToolangError(f"Duplicate parameter {name!r} at line {line_number}.")
        seen.add(name)
        params.append(ParamDecl(name=name, optional=optional))
    return params


def _looks_like_directive(content: str) -> bool:
    stripped = _strip_comment(content).strip()
    return bool(stripped) and bool(
        COLLECTION_DIRECTIVE_RE.match(stripped) or MODEL_DIRECTIVE_RE.match(stripped)
    )


def _is_fence_close(stripped: str, opening_ticks: str) -> bool:
    return bool(stripped) and set(stripped) == {"`"} and len(stripped) >= len(opening_ticks)


def _strip_comment(line: str) -> str:
    if "#" not in line:
        return line.rstrip()
    before_hash, _, _ = line.partition("#")
    return before_hash.rstrip()
