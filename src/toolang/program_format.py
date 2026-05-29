"""Source formatter for `.too` files."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import re

from tree_sitter import Language, Node, Parser
import tree_sitter_toolang


class ToolangFormatError(ValueError):
    """Raised when a source file cannot be formatted safely."""


TREE_SITTER_TYPE_ALIASES = {
    "string": "Text",
    "text": "Text",
    "number": "Number",
    "boolean": "Boolean",
    "json": "Json",
    "message": "Message",
    "path": "Path",
    "artifact": "Artifact",
}
DIRECTIVE_KEY_ALIASES = {
    "model": "models",
    "models": "models",
    "tool": "tools",
    "tools": "tools",
    "skill": "skills",
    "skills": "skills",
    "service": "services",
    "services": "services",
    "psyche": "psyches",
    "psyches": "psyches",
    "hands": "hands",
    "handoffs": "handoffs",
    "recall": "recall",
}
THUNK_HEADER_RE = re.compile(r"^(?P<indent>[ \t]*)thunk(?P<rest>.*):(?P<suffix>[ \t]*(?:#.*)?)$")
STRUCT_HEADER_RE = re.compile(r"^(?P<indent>[ \t]*)struct(?P<rest>.*):(?P<suffix>[ \t]*(?:#.*)?)$")
FIELD_RE = re.compile(
    r"^(?P<indent>[ \t]+)(?P<name>[a-z][a-z0-9_-]*)(?P<optional>\?)?:"
    r"(?P<space>[ \t]*)(?P<type>[A-Za-z][A-Za-z0-9]*(?:\[\])?)(?P<suffix>[ \t]*(?:#.*)?)$"
)
DIRECTIVE_RE = re.compile(
    r"^(?P<indent>[ \t]*)(?P<key>model|models|tool|tools|skill|skills|service|services|"
    r"psyche|psyches|hands|handoffs|recall)(?P<space>[ \t]*)(?P<op>=|\+=|-=)"
)
LEGACY_DELEGATES_RE = re.compile(r"^[ \t]*delegates[ \t]*(?:=|\+=|-=)")
TOP_LEVEL_RE = re.compile(r"^(use|struct|psyche|skill|service|prompt|context|instruct|thunk)\b")
USE_LINE_RE = re.compile(r"^use[ \t]+(?P<kind>\S+)[ \t]+(?P<reference>.+?)$")
DECL_HEADER_RE = re.compile(
    r"^(?P<kind>psyche|skill|service|prompt)[ \t]+(?P<name>[^:\s]+)[ \t]*:"
    r"(?P<body>[ \t]*.*)$"
)
NAMED_BLOCK_HEADER_RE = re.compile(
    r"^(?P<kind>context|instruct)(?:[ \t]+(?P<name>[^:\s]+))?[ \t]*:"
    r"(?P<body>[ \t]*.*)$"
)
MESSAGE_HEADER_RE = re.compile(
    r"^(?P<kind>context|instruct|user|assistant|tool)[ \t]*:(?P<body>[ \t]*.*)$"
)
@dataclass(frozen=True, slots=True)
class _TreeSitterSource:
    source: str
    line_map: tuple[int | None, ...]
    synthetic_message_rows: frozenset[int]

    def original_line_index(self, row: int) -> int:
        if 0 <= row < len(self.line_map):
            original = self.line_map[row]
            if original is not None:
                return original
        return row


def format_source(source: str, *, tab_size: int = 2) -> str:
    """Return a canonical formatting of one Toolang program source string."""

    if tab_size < 1:
        raise ToolangFormatError("tab size must be positive.")
    if not source:
        return ""
    formatted = "\n".join(_format_source_lines(source.splitlines(), tab_size=tab_size)).rstrip()
    if formatted:
        formatted = f"{formatted}\n"
    _validate_syntax(formatted)
    return formatted


def _format_source_lines(lines: list[str], *, tab_size: int) -> list[str]:
    formatted: list[str] = []
    current_top: str | None = None
    in_fence = False
    thunk_block_indent: int | None = None
    indent = " " * tab_size

    for index, raw_line in enumerate(lines):
        line = raw_line.rstrip()
        stripped = line.strip()
        leading = _leading_whitespace(line)

        if index == 0 and line.startswith("#!"):
            formatted.append(line)
            continue

        if in_fence:
            formatted.append(line)
            if stripped.startswith("```"):
                in_fence = False
            continue

        if not stripped:
            if current_top == "thunk":
                thunk_block_indent = None
            formatted.append("")
            continue

        if not leading:
            current_top = _top_level_kind(stripped)
            thunk_block_indent = None
            if stripped.startswith("#"):
                formatted.append(_format_comment_line(stripped))
                continue
            if current_top == "use":
                formatted.append(_format_use_line(stripped))
                continue
            if current_top == "struct":
                formatted.append(_format_struct_header_line(stripped))
                continue
            if current_top == "thunk":
                formatted.append(_format_thunk_header_line(stripped))
                continue
            if current_top in {"context", "instruct"}:
                rendered = _format_named_block_header_line(stripped)
                formatted.append(rendered)
                in_fence = _opens_fence(rendered)
                continue
            if current_top in {"psyche", "skill", "service", "prompt"}:
                rendered = _format_decl_header_line(stripped)
                formatted.append(rendered)
                in_fence = _opens_fence(rendered)
                continue
            formatted.append(line)
            continue

        if current_top == "struct":
            formatted.append(_format_struct_body_line(stripped, indent=indent))
            continue

        if current_top == "thunk":
            rendered, thunk_block_indent = _format_thunk_body_line(
                line,
                block_indent=thunk_block_indent,
                indent=indent,
            )
            formatted.append(rendered)
            in_fence = _opens_fence(rendered)
            continue

        formatted.append(line)

    return _collapse_blank_edges(
        _normalize_blank_lines(
            _order_program_comments(
                _order_control_segments(formatted, tab_size=tab_size)
            ),
            tab_size=tab_size,
        )
    )


def _validate_syntax(source: str) -> None:
    normalized_source = _source_without_shebang(source)
    syntax_source = _tree_sitter_source(normalized_source)
    tree = Parser(_toolang_language()).parse(syntax_source.source.encode("utf-8"))
    error_node = _first_error_node(tree.root_node)
    if error_node is not None:
        _raise_syntax_error(normalized_source.splitlines(), syntax_source, error_node)


def _top_level_kind(stripped_line: str) -> str | None:
    match = TOP_LEVEL_RE.match(stripped_line)
    if match is None:
        return None
    return match.group(1)


def _format_use_line(stripped_line: str) -> str:
    body, comment = _split_inline_comment(stripped_line)
    match = USE_LINE_RE.match(body)
    if match is None:
        return stripped_line
    return f"use {match.group('kind')} {match.group('reference').strip()}{comment}"


def _format_comment_line(stripped_line: str) -> str:
    if not stripped_line.startswith("#") or stripped_line.startswith("#!"):
        return stripped_line
    if stripped_line.startswith("##!"):
        body = stripped_line[3:].strip()
        return "##!" if not body else f"##! {body}"
    if stripped_line.startswith("##"):
        body = stripped_line[2:].strip()
        return "##" if not body else f"## {body}"
    body = stripped_line[1:].strip()
    return "#" if not body else f"# {body}"


def _format_decl_header_line(stripped_line: str) -> str:
    match = DECL_HEADER_RE.match(stripped_line)
    if match is None:
        return stripped_line
    return f"{match.group('kind')} {match.group('name')}: {match.group('body').strip()}".rstrip()


def _format_named_block_header_line(stripped_line: str) -> str:
    match = NAMED_BLOCK_HEADER_RE.match(stripped_line)
    if match is None:
        return stripped_line
    name = match.group("name")
    name_text = f" {name}" if name else ""
    return f"{match.group('kind')}{name_text}: {match.group('body').strip()}".rstrip()


def _format_struct_header_line(stripped_line: str) -> str:
    match = STRUCT_HEADER_RE.match(stripped_line)
    if match is None:
        return stripped_line
    rest = match.group("rest").strip()
    return f"struct{f' {rest}' if rest else ''}:"


def _format_struct_body_line(stripped_line: str, *, indent: str) -> str:
    if stripped_line.startswith("#"):
        return f"{indent}{stripped_line}"
    body, comment = _split_inline_comment(stripped_line)
    field_match = re.fullmatch(
        r"(?P<name>[a-z][a-z0-9_-]*)(?P<optional>\?)?[ \t]*:[ \t]*(?P<type>[A-Za-z][A-Za-z0-9]*(?:\[\])?)",
        body,
    )
    if field_match is None:
        return f"{indent}{stripped_line}"
    return (
        f"{indent}{field_match.group('name')}{field_match.group('optional') or ''}: "
        f"{field_match.group('type')}{comment}"
    )


def _format_thunk_header_line(stripped_line: str) -> str:
    match = THUNK_HEADER_RE.match(stripped_line)
    if match is None:
        return stripped_line
    rest = match.group("rest").strip()
    output = ""
    if "->" in rest:
        rest, raw_output = rest.rsplit("->", 1)
        output_type = raw_output.strip()
        output = f" -> {output_type}" if output_type else ""
    name, params = _parse_thunk_rest(rest)
    rendered_name = f" {name}" if name else ""
    rendered_params = "" if params is None else f"({_format_signature_params(params)})"
    return f"thunk{rendered_name}{rendered_params}{output}:{match.group('suffix')}"


def _format_signature_params(raw: str) -> str:
    if not raw.strip():
        return ""
    rendered: list[str] = []
    for item in [part.strip() for part in raw.split(",")]:
        if not item:
            continue
        match = re.fullmatch(
            r"(?P<name>[A-Za-z_][\w-]*)(?P<optional>\?)?(?::[ \t]*(?P<type>[A-Za-z][A-Za-z0-9]*(?:\[\])?))?",
            item,
        )
        if match is None:
            rendered.append(item)
            continue
        type_name = match.group("type")
        type_text = f": {type_name}" if type_name else ""
        rendered.append(f"{match.group('name')}{match.group('optional') or ''}{type_text}")
    return ", ".join(rendered)


def _format_thunk_body_line(line: str, *, block_indent: int | None, indent: str) -> tuple[str, int | None]:
    stripped = line.strip()
    leading_width = len(_leading_whitespace(line).expandtabs(2))
    directive_match = DIRECTIVE_RE.match(line)
    if directive_match is not None:
        return _format_directive_line(stripped, indent=indent), None

    message_match = MESSAGE_HEADER_RE.match(stripped)
    if message_match is not None:
        return _format_message_header_line(message_match, indent=indent), leading_width

    if block_indent is not None and leading_width > block_indent:
        return f"{indent}{indent}{stripped}", block_indent

    if stripped.startswith("#"):
        return f"{indent}{stripped}", block_indent

    return f"{indent}{stripped}", None


def _format_directive_line(stripped_line: str, *, indent: str) -> str:
    body, comment = _split_inline_comment(stripped_line)
    match = re.fullmatch(
        r"(?P<key>model|models|tool|tools|skill|skills|service|services|"
        r"psyche|psyches|hands|handoffs|recall)[ \t]*(?P<op>=|\+=|-=)[ \t]*(?P<values>.*)",
        body,
    )
    if match is None:
        return f"{indent}{stripped_line}"
    key = DIRECTIVE_KEY_ALIASES.get(match.group("key"), match.group("key"))
    values = _format_csv_values(match.group("values"))
    return f"{indent}{key} {match.group('op')} {values}{comment}".rstrip()


def _format_message_header_line(match: re.Match[str], *, indent: str) -> str:
    body = match.group("body").strip()
    if not body:
        return f"{indent}{match.group('kind')}:"
    return f"{indent}{match.group('kind')}: {body}"


def _format_csv_values(raw: str) -> str:
    return ", ".join(item for item in (part.strip() for part in raw.split(",")) if item)


def _order_program_comments(lines: list[str]) -> list[str]:
    shebang: str | None = None
    program_comments: list[str] = []
    body: list[str] = []
    in_fence = False
    index = 0

    if lines and lines[0].startswith("#!"):
        shebang = lines[0]
        index = 1

    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if in_fence:
            body.append(line)
            if stripped.startswith("```"):
                in_fence = False
            index += 1
            continue
        if stripped.startswith("##!"):
            program_comments.append(line)
            index += 1
            continue
        body.append(line)
        if _opens_fence(line):
            in_fence = True
        index += 1

    ordered: list[str] = []
    if shebang is not None:
        ordered.append(shebang)
    ordered.extend(program_comments)
    ordered.extend(body)
    return ordered


def _order_control_segments(lines: list[str], *, tab_size: int) -> list[str]:
    ordered: list[str] = []
    in_fence = False
    in_thunk = False
    index = 0

    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if in_fence:
            ordered.append(line)
            if stripped.startswith("```"):
                in_fence = False
            index += 1
            continue

        top_level = _top_level_kind(stripped) if stripped and not _leading_whitespace(line) else None
        if top_level is not None:
            in_thunk = top_level == "thunk"

        if in_thunk and _control_header_kind(line, tab_size=tab_size) is not None:
            segments, index = _collect_control_segments(lines, index, tab_size=tab_size)
            for segment in [*segments[0], *segments[1]]:
                ordered.extend(segment)
            continue

        ordered.append(line)
        if _opens_fence(line):
            in_fence = True
        index += 1

    return ordered


def _collect_control_segments(
    lines: list[str],
    start: int,
    *,
    tab_size: int,
) -> tuple[tuple[list[list[str]], list[list[str]]], int]:
    inline_segments: list[list[str]] = []
    block_segments: list[list[str]] = []
    index = start

    while index < len(lines):
        while index < len(lines) and not lines[index].strip():
            index += 1
        if index >= len(lines) or _control_header_kind(lines[index], tab_size=tab_size) is None:
            break

        segment = [lines[index]]
        is_block = lines[index].rstrip().endswith(":")
        index += 1
        if is_block:
            while index < len(lines):
                line = lines[index]
                if not line.strip():
                    break
                if _is_block_continuation(line, tab_size=tab_size):
                    segment.append(line)
                    index += 1
                    continue
                break
            block_segments.append(segment)
        else:
            inline_segments.append(segment)

    return (inline_segments, block_segments), index


def _control_header_kind(line: str, *, tab_size: int) -> str | None:
    match = _formatted_message_header_match(line, tab_size=tab_size)
    if match is None:
        return None
    kind = match.group("kind")
    return kind if kind in {"context", "instruct"} else None


def _is_block_continuation(line: str, *, tab_size: int) -> bool:
    return (
        bool(line.strip())
        and bool(_leading_whitespace(line))
        and len(_leading_whitespace(line).expandtabs(2)) > tab_size
    )


def _normalize_blank_lines(lines: list[str], *, tab_size: int) -> list[str]:
    normalized: list[str] = []
    in_fence = False
    in_thunk = False
    previous_kind: str | None = None
    pending_blank = False

    for line in lines:
        stripped = line.strip()
        if in_fence:
            normalized.append(line)
            if stripped.startswith("```"):
                in_fence = False
            previous_kind = "fence"
            continue
        if not stripped:
            pending_blank = True
            continue

        kind = _formatted_line_kind(line, in_thunk=in_thunk, tab_size=tab_size)
        if _needs_blank_line(previous_kind, kind, pending_blank=pending_blank):
            _append_blank_line(normalized)
        elif pending_blank and _preserves_blank_line(previous_kind, kind):
            _append_blank_line(normalized)
        normalized.append(line)
        pending_blank = False

        top_level = _top_level_kind(stripped) if not _leading_whitespace(line) else None
        if top_level is not None:
            in_thunk = top_level == "thunk"
        if _opens_fence(line):
            in_fence = True
        previous_kind = kind

    return normalized


def _formatted_line_kind(line: str, *, in_thunk: bool, tab_size: int) -> str:
    stripped = line.strip()
    if line.startswith("#!"):
        return "shebang"
    if not _leading_whitespace(line):
        if stripped.startswith("##!"):
            return "program_comment"
        if stripped.startswith("#"):
            return "top_comment"
        top_level = _top_level_kind(stripped)
        if top_level == "thunk":
            return "thunk_header"
        if top_level is not None:
            return "top_level"
        return "other_top_level"
    if not in_thunk:
        return "indented"
    if DIRECTIVE_RE.match(line):
        return "directive"
    message_match = _formatted_message_header_match(line, tab_size=tab_size)
    if message_match is not None:
        if line.rstrip().endswith(":"):
            if message_match.group("kind") in {"context", "instruct"}:
                return "control_block_header"
            return "message_block_header"
        if message_match.group("kind") in {"context", "instruct"}:
            return "control"
        return "message_header"
    if len(_leading_whitespace(line).expandtabs(2)) > tab_size:
        return "block_body"
    if stripped.startswith("#"):
        return "comment"
    return "message_body"


def _formatted_message_header_match(line: str, *, tab_size: int) -> re.Match[str] | None:
    indent = re.escape(" " * tab_size)
    return re.match(rf"^{indent}(?P<kind>context|instruct|user|assistant|tool):", line)


def _needs_blank_line(previous_kind: str | None, current_kind: str, *, pending_blank: bool) -> bool:
    if previous_kind is None:
        return False
    if current_kind in {"top_level", "thunk_header", "other_top_level"}:
        return previous_kind not in {"top_comment"} or pending_blank
    if current_kind == "top_comment":
        return previous_kind not in {"shebang", "top_comment"}
    if current_kind == "program_comment":
        return previous_kind not in {"program_comment", "top_comment"}
    if current_kind == "directive":
        return previous_kind not in {"thunk_header", "directive"}
    if current_kind == "control":
        return previous_kind in {"directive", "message_header", "message_body", "block_body"}
    if current_kind == "control_block_header":
        return previous_kind in {"directive", "control", "message_header", "message_body", "block_body"}
    if current_kind == "message_header":
        return previous_kind in {"directive", "control", "message_header", "message_body", "block_body"}
    if current_kind == "message_block_header":
        return previous_kind in {"directive", "control", "message_header", "message_body", "block_body"}
    if current_kind == "block_body":
        return previous_kind not in {"control_block_header", "message_block_header", "block_body"}
    if current_kind == "message_body":
        return previous_kind not in {
            "control_block_header",
            "message_block_header",
            "message_header",
            "message_body",
            "thunk_header",
        }
    return False


def _preserves_blank_line(previous_kind: str | None, current_kind: str) -> bool:
    if previous_kind is None:
        return False
    if previous_kind == "shebang" and current_kind == "top_comment":
        return True
    if previous_kind == current_kind == "top_comment":
        return True
    return previous_kind == current_kind and current_kind in {"message_body", "block_body"}


def _append_blank_line(lines: list[str]) -> None:
    if lines and lines[-1] != "":
        lines.append("")


def _tree_sitter_source(source: str) -> _TreeSitterSource:
    original_lines = source.splitlines()
    transformed: list[str] = []
    line_map: list[int | None] = []
    synthetic_message_rows: set[int] = set()
    index = 0

    while index < len(original_lines):
        line = original_lines[index]
        thunk_match = THUNK_HEADER_RE.match(line)
        if thunk_match is None:
            transformed.append(_transform_non_thunk_line(line))
            line_map.append(index)
            index += 1
            continue

        transformed.append(_transform_thunk_header(line))
        line_map.append(index)
        thunk_name = _thunk_name_from_header(line)
        index += 1

        while index < len(original_lines):
            body_line = original_lines[index]
            if TOP_LEVEL_RE.match(body_line):
                break
            explicit_block_match = re.match(
                r"^(?P<indent>[ \t]*)(context|instruct|user|assistant|tool):",
                body_line,
            )
            if explicit_block_match is not None:
                block_indent = len(explicit_block_match.group("indent"))
                transformed.append(body_line)
                line_map.append(index)
                index += 1
                while index < len(original_lines):
                    continuation = original_lines[index]
                    if TOP_LEVEL_RE.match(continuation):
                        break
                    if continuation.strip() and len(_leading_whitespace(continuation)) <= block_indent:
                        break
                    transformed.append(continuation)
                    line_map.append(index)
                    index += 1
                continue
            if _is_implicit_message_line(body_line):
                synthetic_indent = _leading_whitespace(body_line)
                synthetic_row = len(transformed)
                transformed.append(f"{synthetic_indent}{_implicit_message_kind(thunk_name)}:")
                line_map.append(None)
                synthetic_message_rows.add(synthetic_row)
                while index < len(original_lines):
                    message_line = original_lines[index]
                    if TOP_LEVEL_RE.match(message_line) or not _is_implicit_message_line(message_line):
                        break
                    transformed.append(_indent_message_line(message_line))
                    line_map.append(index)
                    index += 1
                continue

            transformed.append(_transform_non_thunk_line(body_line))
            line_map.append(index)
            index += 1

    if source:
        tree_source = "\n".join(transformed) + "\n"
    else:
        tree_source = ""
    return _TreeSitterSource(
        source=tree_source,
        line_map=tuple(line_map),
        synthetic_message_rows=frozenset(synthetic_message_rows),
    )


def _transform_non_thunk_line(line: str) -> str:
    if STRUCT_HEADER_RE.match(line) is not None:
        return line

    field_match = FIELD_RE.match(line)
    if field_match is not None:
        return (
            f"{field_match.group('indent')}{field_match.group('name')}"
            f"{field_match.group('optional') or ''}:"
            f"{field_match.group('space')}{_tree_sitter_type_name(field_match.group('type'))}"
            f"{field_match.group('suffix')}"
        )

    return _transform_directive_line(line)


def _transform_thunk_header(line: str) -> str:
    match = THUNK_HEADER_RE.match(line)
    if match is None:
        return line
    rest = match.group("rest")
    output = ""
    if "->" in rest:
        rest, raw_output = rest.rsplit("->", 1)
        output = f" -> {_tree_sitter_type_name(raw_output.strip())}"
    name, params = _parse_thunk_rest(rest)
    rendered_name = f" {name}" if name else ""
    rendered_params = "" if params is None else f"({_tree_sitter_params(params)})"
    return f"{match.group('indent')}thunk{rendered_name}{rendered_params}{output}:{match.group('suffix')}"


def _transform_directive_line(line: str) -> str:
    match = DIRECTIVE_RE.match(line)
    if match is None:
        return line
    key = match.group("key")
    normalized_key = "models" if key == "model" else key
    values = line[match.end() :]
    rendered_values = " selector" if values.strip() else values
    return (
        f"{match.group('indent')}{normalized_key}"
        f"{match.group('space')}{match.group('op')}{rendered_values}"
    )


def _tree_sitter_params(raw: str) -> str:
    if not raw.strip():
        return ""
    rendered: list[str] = []
    for item in [part.strip() for part in raw.split(",")]:
        if item == "_":
            rendered.append("input: Message")
            continue
        match = re.fullmatch(
            r"(?P<name>[A-Za-z_][\w-]*)(?P<optional>\?)?(?::\s*(?P<type>[A-Za-z][A-Za-z0-9]*(?:\[\])?))?",
            item,
        )
        if match is None:
            rendered.append(item)
            continue
        name = "input" if match.group("name") == "_" else match.group("name")
        optional = match.group("optional") or ""
        type_name = _tree_sitter_type_name(match.group("type") or "string")
        rendered.append(f"{name}{optional}: {type_name}")
    return ", ".join(rendered)


def _tree_sitter_type_name(type_name: str | None) -> str:
    if not type_name:
        return ""
    suffix = "[]" if type_name.endswith("[]") else ""
    base = type_name[:-2] if suffix else type_name
    return f"{TREE_SITTER_TYPE_ALIASES.get(base, base)}{suffix}"


def _parse_thunk_rest(rest: str) -> tuple[str | None, str | None]:
    rest = rest.strip()
    if not rest:
        return None, None
    params_start = rest.find("(")
    if params_start < 0:
        return rest.strip() or None, None
    params_end = rest.rfind(")")
    if params_end < params_start:
        return rest.strip() or None, None
    name = rest[:params_start].strip() or None
    return name, rest[params_start + 1 : params_end]


def _thunk_name_from_header(line: str) -> str:
    match = THUNK_HEADER_RE.match(line)
    if match is None:
        return "main"
    name, _params = _parse_thunk_rest(match.group("rest").strip())
    return name or "main"


def _is_implicit_message_line(line: str) -> bool:
    if not line.strip():
        return False
    stripped = line.lstrip(" \t")
    if stripped.startswith("#"):
        return False
    if LEGACY_DELEGATES_RE.match(line):
        return False
    if DIRECTIVE_RE.match(line):
        return False
    if re.match(r"^[ \t]*(context|instruct|system|user|assistant|tool):", line):
        return False
    return line.startswith((" ", "\t"))


def _implicit_message_kind(thunk_name: str) -> str:
    return "instruct" if thunk_name in {"chat", "task", "chore"} else "user"


def _indent_message_line(line: str) -> str:
    indent = _leading_whitespace(line)
    return f"{indent}  {line[len(indent):]}"


def _first_error_node(node: Node) -> Node | None:
    if node.is_error or node.is_missing:
        if _is_ignored_error_node(node):
            return None
        return node
    for child in node.children:
        result = _first_error_node(child)
        if result is not None:
            return result
    return None


def _is_ignored_error_node(node: Node) -> bool:
    parent = node.parent
    while parent is not None:
        if parent.type == "frontmatter":
            return True
        parent = parent.parent
    return False


def _raise_syntax_error(lines: list[str], syntax_source: _TreeSitterSource, node: Node) -> None:
    original_row = syntax_source.original_line_index(node.start_point.row)
    line_number = original_row + 1
    raw_line = _line_text(lines, original_row)
    if raw_line.startswith((" ", "\t")) and raw_line.strip():
        raise ToolangFormatError(f"Unexpected indentation at line {line_number}.")
    raise ToolangFormatError(f"Syntax error at line {line_number}.")


def _split_inline_comment(line: str) -> tuple[str, str]:
    match = re.search(r"(?<!\S)#", line)
    if match is None:
        return line.rstrip(), ""
    body = line[: match.start()].rstrip()
    comment = line[match.start() :].strip()
    return body, f"  {comment}" if body else comment


def _opens_fence(line: str) -> bool:
    stripped = line.strip()
    return "```" in stripped and not stripped.startswith("```")


def _collapse_blank_edges(lines: list[str]) -> list[str]:
    start = 0
    end = len(lines)
    while start < end and not lines[start].strip():
        start += 1
    while end > start and not lines[end - 1].strip():
        end -= 1
    return lines[start:end]


def _leading_whitespace(line: str) -> str:
    return line[: len(line) - len(line.lstrip(" \t"))]


def _line_text(lines: list[str], row: int) -> str:
    if 0 <= row < len(lines):
        return lines[row]
    return ""


def _source_without_shebang(source: str) -> str:
    if not source.startswith("#!"):
        return source
    _first_line, separator, rest = source.partition("\n")
    if not separator:
        return ""
    return f"\n{rest}"


@lru_cache(maxsize=1)
def _toolang_language() -> Language:
    return Language(tree_sitter_toolang.language())
