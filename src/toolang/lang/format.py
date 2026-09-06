"""Source formatter for `.too` files."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
import re

from tree_sitter import Node, Tree
from toolang.common.query import format_query_text

from . import ast
from .ast import _first_syntax_error, _parse_tree
from .errors import ToolangFormatError
from .text import dedent_text_lines, source_lines, text_indent_width


_RUNNABLE_HEADER_RE = re.compile(
    r"^(?P<kind>agic|flow)(?P<rest>.*):(?P<suffix>[ \t]*(?:#.*)?)$"
)
_STRUCT_HEADER_RE = re.compile(r"^struct(?P<rest>.*):(?P<suffix>[ \t]*(?:#.*)?)$")
_WITH_LINE_RE = re.compile(r"^with[ \t]+(?P<kind>\S+)[ \t]+(?P<reference>.+?)$")
_DECL_HEADER_RE = re.compile(
    r"^(?P<kind>psyche|skill|service|prompt|task|chore)[ \t]+(?P<name>[^:\s]+)[ \t]*:"
    r"(?P<body>[ \t]*.*)$"
)
_NAMED_BLOCK_HEADER_RE = re.compile(
    r"^(?P<kind>context|instruct)(?:[ \t]+(?P<name>[^:\s]+))?[ \t]*:"
    r"(?P<body>[ \t]*.*)$"
)
_MESSAGE_HEADER_RE = re.compile(
    r"^(?P<kind>context|instruct|user|assistant|tool)[ \t]*:(?P<body>[ \t]*.*)$"
)
_COMMENT_SPLIT_KINDS = {
    "directive",
    "control",
    "control_block_header",
    "message_header",
    "message_block_header",
    "message_body",
    "block_body",
}
_FLOW_STATEMENT_TYPES = {
    "let_statement",
    "run_statement",
    "seek_statement",
    "ask_statement",
    "scatter_statement",
    "storm_statement",
    "gather_statement",
    "settle_statement",
    "map_statement",
    "keep_statement",
    "drop_statement",
    "sort_statement",
    "repeat_statement",
    "inline_agic_body",
}
_DECLARATION_TYPES = {
    "with",
    "struct",
    "psyche",
    "skill",
    "service",
    "prompt",
    "task",
    "chore",
    "context",
    "instruct",
    "agic",
    "flow",
}
_COMMENT_TYPES = {"comment_line", "doc_line", "parent_doc_line"}
_TEXT_TYPES = {"text_body", "unroled_message", "implicit_run_statement"}
_CONTROL_TYPES = {"context_setting", "instruct_setting"}


@dataclass(frozen=True, slots=True)
class _Line:
    """Rendered source with the ownership needed by spacing and ordering rules."""

    value: str
    kind: str
    text_owner: int | None = None
    control_owner: int | None = None
    separate: bool = False


def format_source(source: str, *, tab_size: int = 2) -> str:
    """Return a canonical formatting of one Toolang program source string."""

    if tab_size < 1:
        raise ToolangFormatError("tab size must be positive.")
    if not source:
        return ""
    tree = _syntax_tree(source)
    formatted = "\n".join(
        _format_source_lines(
            source_lines(source), root=tree.root_node, tab_size=tab_size
        )
    ).rstrip()
    if formatted:
        formatted = f"{formatted}\n"
    _syntax_tree(formatted)
    return formatted


def format_statement_head(statement: ast.FlowStmt) -> str:
    """Return one compact source-like head for a lowered flow statement."""

    if isinstance(statement, ast.LetStmt):
        return _statement_words("let", statement.binding)
    if isinstance(statement, ast.RunStmt):
        head = _statement_words("run", _authored_runnable(statement.runnable))
    elif isinstance(statement, ast.SeekStmt):
        head = _statement_words(
            "seek",
            statement.name,
            _authored_runnable(statement.runnable),
        )
    elif isinstance(statement, ast.AskStmt):
        head = "ask"
    elif isinstance(statement, ast.ScatterStmt):
        head = _statement_words(
            "scatter",
            str(statement.count),
            _runnable_clause("using", statement.runnable),
        )
    elif isinstance(statement, ast.StormStmt):
        head = _statement_words(
            "storm",
            str(statement.count),
            _parallel_clause(statement.lanes),
            _runnable_clause("using", statement.runnable),
        )
    elif isinstance(statement, ast.GatherStmt):
        head = _statement_words("gather", _runnable_clause("using", statement.runnable))
    elif isinstance(statement, ast.SettleStmt):
        head = _statement_words("settle", _runnable_clause("using", statement.runnable))
    elif isinstance(statement, ast.MapStmt):
        head = _statement_words(
            "map",
            _parallel_clause(statement.lanes),
            _runnable_clause("using", statement.runnable),
        )
    elif isinstance(statement, ast.KeepStmt | ast.DropStmt):
        head = _statement_words(
            statement.kind,
            statement.position,
            str(statement.count) if statement.count is not None else "",
            _parallel_clause(statement.lanes),
            _runnable_clause("if", statement.runnable) if statement.runnable else "",
        )
    elif isinstance(statement, ast.SortStmt):
        head = _statement_words(
            "sort",
            statement.order,
            _parallel_clause(statement.lanes),
            _runnable_clause("by", statement.runnable),
        )
    elif isinstance(statement, ast.RepeatStmt):
        return _statement_words(
            "repeat",
            _count_phrase(statement.count, "time")
            if statement.count is not None
            else "",
        )
    else:
        raise TypeError(f"unsupported flow statement: {type(statement).__name__}")
    if statement.binding == "_":
        return head
    if statement.binding is None:
        return f"let {head}"
    return f"let {statement.binding} = {head}"


def _statement_words(*values: str | None) -> str:
    return " ".join(value for value in values if value)


def _authored_runnable(value: str) -> str:
    return "" if value.startswith("<agic:") else value


def _parallel_clause(value: int | None) -> str:
    return f"in {_count_phrase(value, 'lane')}" if value is not None else ""


def _count_phrase(value: int, noun: str) -> str:
    return f"{value} {noun}{'' if value == 1 else 's'}"


def _runnable_clause(connector: str, runnable: str) -> str:
    return _statement_words(connector, _authored_runnable(runnable))


def _format_source_lines(lines: list[str], *, root: Node, tab_size: int) -> list[str]:
    formatted: list[_Line] = []
    text_bodies: dict[int, dict[int, str]] = {}
    previous_doc_indent: str | None = None

    for row, raw_line in enumerate(lines):
        line = raw_line.rstrip()
        prefix = _leading_whitespace(line)
        if not line.strip():
            formatted.append(_Line("", "blank"))
            previous_doc_indent = None
            continue
        node = root.named_descendant_for_point_range(
            (row, len(prefix)),
            (row, len(prefix) + len(line[len(prefix)].encode("utf-8"))),
        )
        if node is None:
            raise ToolangFormatError(f"Missing syntax node at line {row + 1}.")
        ancestors = tuple(_ancestors(node))
        control = next(
            (item for item in ancestors if item.type in _CONTROL_TYPES), None
        )
        text = next((item for item in ancestors if item.type in _TEXT_TYPES), None)
        kind = _source_line_kind(line, node=node, ancestors=ancestors)
        depth = _indent_depth(node) if prefix else 0
        if node.type in _COMMENT_TYPES and prefix:
            # Trivia can be owned by the block it follows. Its authored level
            # still determines whether documentation attaches to the next entry.
            depth = 1 + sum(
                1
                for item in ancestors
                if item.type == "repeat_statement"
                and text_indent_width(lines[item.start_point.row])
                < text_indent_width(line)
            )
        if node.type == "indented_raw_text" and text is not None:
            if text.id not in text_bodies:
                rows = [
                    child.start_point.row
                    for child in text.named_children
                    if child.type == "text_body_line"
                ]
                text_bodies[text.id] = dict(
                    zip(
                        rows,
                        dedent_text_lines([lines[index] for index in rows]),
                        strict=True,
                    )
                )
            value = text_bodies[text.id][row]
        else:
            value = _format_syntax_line(line.strip(), node=node)
        rendered_prefix = " " * (tab_size * depth)
        formatted.append(
            _Line(
                rendered_prefix + value,
                kind,
                text_owner=text.id if text is not None else None,
                control_owner=control.id if control is not None else None,
                separate=previous_doc_indent is not None
                and previous_doc_indent != prefix
                and _leading_whitespace(formatted[-1].value) == rendered_prefix,
            )
        )
        previous_doc_indent = prefix if node.type == "doc_line" else None

    return _normalize_blank_lines(
        _order_program_comments(_order_control_segments(formatted))
    )


def _source_line_kind(line: str, *, node: Node, ancestors: tuple[Node, ...]) -> str:
    types = {item.type for item in ancestors}
    if not _leading_whitespace(line):
        if line.startswith("#!"):
            return "shebang"
        if node.type == "parent_doc_line":
            return "program_comment"
        if node.type in _COMMENT_TYPES:
            return "top_comment"
        return "agic_header" if "agic" in types else "top_level"
    if "agic_body" not in types:
        return "indented"
    if node.type == "indented_raw_text":
        return "message_body" if "unroled_message" in types else "block_body"
    if node.type in _COMMENT_TYPES:
        return "comment"
    if "directive" in types:
        return "directive"
    owner = next(
        (item for item in ancestors if item.type in _CONTROL_TYPES | {"message"}), None
    )
    if owner is not None:
        text = next(
            (item for item in owner.named_children if item.type == "text_inline"), None
        )
        block = text is not None and any(
            item.type == "text_block" for item in text.named_children
        )
        if owner.type in _CONTROL_TYPES:
            return "control_block_header" if block else "control"
        return "message_block_header" if block else "message_header"
    return "message_body"


def _syntax_tree(source: str) -> Tree:
    syntax = source if source.endswith("\n") else f"{source}\n"
    tree = _parse_tree(syntax.encode("utf-8"))
    error_node = _first_syntax_error(tree.root_node)
    if error_node is not None:
        _raise_syntax_error(source_lines(source), error_node)
    return tree


def _format_syntax_line(stripped_line: str, *, node: Node) -> str:
    if stripped_line.startswith("#"):
        return _format_comment_line(stripped_line)

    ancestors = _ancestor_types(node)
    declaration = next(
        (
            item
            for item in _ancestors(node)
            if item.type in _DECLARATION_TYPES
            and item.start_point.row == node.start_point.row
        ),
        None,
    )
    top_level = declaration.type if declaration is not None else None
    if top_level == "with":
        return _format_with_line(stripped_line)
    if top_level == "struct":
        return _format_struct_header_line(stripped_line)
    if top_level in {"agic", "flow"}:
        return _format_runnable_header_line(stripped_line)
    if top_level in {"context", "instruct"}:
        return _format_named_block_header_line(stripped_line)
    if top_level in {"psyche", "skill", "service", "prompt", "task", "chore"}:
        return _format_decl_header_line(stripped_line)

    if "field" in ancestors:
        return _format_struct_body_line(stripped_line)
    if "property" in ancestors:
        return _format_property_line(stripped_line)
    if "directive" in ancestors:
        return _format_directive_line(stripped_line)
    if ancestors & {
        "context_setting",
        "instruct_setting",
        "message",
    }:
        if match := _MESSAGE_HEADER_RE.match(stripped_line):
            return _format_message_header_line(match)
        return _collapse_syntax_space(stripped_line)
    if ancestors & _FLOW_STATEMENT_TYPES:
        return _format_flow_statement_line(stripped_line, node=node)
    return stripped_line


def _ancestor_types(node: Node) -> set[str]:
    return {item.type for item in _ancestors(node)}


def _indent_depth(node: Node) -> int:
    ancestors = _ancestor_types(node)
    if ancestors & {
        "psyche",
        "skill",
        "service",
        "prompt",
    }:
        return 1
    if ancestors & {"struct_body", "cap_body", "job_body"}:
        return 1
    if "agic_body" in ancestors:
        return 1 + sum(1 for current in _ancestors(node) if current.type == "text_body")
    if "flow_body" in ancestors:
        return 1 + sum(
            1
            for current in _ancestors(node)
            if current.type == "text_body"
            or (
                current.type == "repeat_statement"
                and current.start_point.row < node.start_point.row
            )
        )
    if ancestors & {"context", "instruct"} and "text_body" in ancestors:
        return 1
    return 0


def _ancestors(node: Node) -> Iterator[Node]:
    current: Node | None = node
    while current is not None:
        yield current
        current = current.parent


def _format_with_line(stripped_line: str) -> str:
    body, comment = _split_inline_comment(stripped_line)
    match = _WITH_LINE_RE.match(body)
    if match is None:
        return stripped_line
    return f"with {match.group('kind')} {match.group('reference').strip()}{comment}"


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
    match = _DECL_HEADER_RE.match(stripped_line)
    if match is None:
        return stripped_line
    return f"{match.group('kind')} {match.group('name')}: {match.group('body').strip()}".rstrip()


def _format_named_block_header_line(stripped_line: str) -> str:
    match = _NAMED_BLOCK_HEADER_RE.match(stripped_line)
    if match is None:
        return stripped_line
    name = match.group("name")
    name_text = f" {name}" if name else ""
    return f"{match.group('kind')}{name_text}: {match.group('body').strip()}".rstrip()


def _format_struct_header_line(stripped_line: str) -> str:
    match = _STRUCT_HEADER_RE.match(stripped_line)
    if match is None:
        return stripped_line
    rest = match.group("rest").strip()
    suffix = match.group("suffix").strip()
    return f"struct{f' {rest}' if rest else ''}:{f'  {suffix}' if suffix else ''}"


def _format_struct_body_line(stripped_line: str) -> str:
    body, comment = _split_inline_comment(stripped_line)
    field_match = re.fullmatch(
        r"(?P<name>[a-z][a-z0-9_-]*)[ \t]*(?P<optional>\?)?[ \t]*:[ \t]*"
        r"(?P<type>[A-Za-z][A-Za-z0-9]*(?:\[\])*)",
        body,
    )
    if field_match is None:
        return stripped_line
    return (
        f"{field_match.group('name')}{field_match.group('optional') or ''}: "
        f"{field_match.group('type')}{comment}"
    )


def _format_property_line(stripped_line: str) -> str:
    body, comment = _split_inline_comment(stripped_line)
    match = re.fullmatch(
        r"(?P<key>[a-z][a-z0-9_]*(_[a-z0-9]+)*)[ \t]*=[ \t]*(?P<value>.*)",
        body,
    )
    if match is None:
        return stripped_line
    return f"{match.group('key')} = {match.group('value').strip()}{comment}".rstrip()


def _format_runnable_header_line(stripped_line: str) -> str:
    match = _RUNNABLE_HEADER_RE.match(stripped_line)
    if match is None:
        return stripped_line
    rest = match.group("rest").strip()
    output = ""
    if "->" in rest:
        rest, raw_output = rest.rsplit("->", 1)
        output_type = raw_output.strip()
        output = f" -> {output_type}" if output_type else ""
    name, params = _parse_runnable_rest(rest)
    rendered_name = f" {name}" if name else ""
    rendered_params = "" if params is None else f"({_format_signature_params(params)})"
    suffix = match.group("suffix").strip()
    return (
        f"{match.group('kind')}{rendered_name}{rendered_params}{output}:"
        f"{f'  {suffix}' if suffix else ''}"
    )


def _format_signature_params(raw: str) -> str:
    if not raw.strip():
        return ""
    rendered: list[str] = []
    for item in [part.strip() for part in raw.split(",")]:
        if not item:
            continue
        match = re.fullmatch(
            r"(?P<name>[A-Za-z_][\w-]*)[ \t]*(?P<optional>\?)?"
            r"(?:[ \t]*:[ \t]*(?P<type>[A-Za-z][A-Za-z0-9]*(?:\[\])*))?",
            item,
        )
        if match is None:
            rendered.append(item)
            continue
        type_name = match.group("type")
        raw_name = match.group("name")
        if raw_name == "_":
            type_name = type_name or "Part[]"
        type_text = f": {type_name}" if type_name else ""
        rendered.append(f"{raw_name}{match.group('optional') or ''}{type_text}")
    return ", ".join(rendered)


def _format_directive_line(stripped_line: str) -> str:
    body, comment = _split_inline_comment(stripped_line)
    match = re.fullmatch(
        r"(?P<key>models|tools|skills|services|psyches|prompts|hands|handoffs|recall)"
        r"[ \t]*(?P<op>=|\+=|-=)[ \t]*(?P<values>.*)",
        body,
    )
    if match is None:
        return stripped_line
    values = (
        _format_csv_values(match.group("values"))
        if match.group("key") == "recall"
        else format_query_text(match.group("values"))
    )
    return f"{match.group('key')} {match.group('op')} {values}{comment}".rstrip()


def _format_message_header_line(match: re.Match[str]) -> str:
    body = match.group("body").strip()
    if not body:
        return f"{match.group('kind')}:"
    return f"{match.group('kind')}: {body}"


def _format_flow_statement_line(stripped_line: str, *, node: Node) -> str:
    binding = next(
        (parent for parent in _ancestors(node) if parent.type == "let_statement"),
        None,
    )
    if binding is not None and binding.child_by_field_name("value") is not None:
        before, _, content = stripped_line.partition("=")
        return f"{_collapse_syntax_space(before)} = {content.strip()}".rstrip()
    before, separator, after = stripped_line.partition(":")
    rendered = _collapse_syntax_space(before)
    if not separator:
        return rendered
    body = after.strip()
    return f"{rendered}:" if not body else f"{rendered}: {body}"


def _collapse_syntax_space(value: str) -> str:
    rendered = re.sub(r"[ \t]+", " ", value.strip())
    rendered = re.sub(r"[ \t]*->[ \t]*", " -> ", rendered)
    rendered = re.sub(r"[ \t]*=[ \t]*", " = ", rendered)
    return rendered.rstrip()


def _format_csv_values(raw: str) -> str:
    return ", ".join(item for item in (part.strip() for part in raw.split(",")) if item)


def _order_program_comments(lines: list[_Line]) -> list[_Line]:
    prefix = lines[:1] if lines and lines[0].kind == "shebang" else []
    body = lines[len(prefix) :]
    return [
        *prefix,
        *(line for line in body if line.kind == "program_comment"),
        *(line for line in body if line.kind != "program_comment"),
    ]


def _order_control_segments(lines: list[_Line]) -> list[_Line]:
    ordered: list[_Line] = []
    index = 0
    while index < len(lines):
        if lines[index].kind not in {"control", "control_block_header"}:
            ordered.append(lines[index])
            index += 1
            continue
        segments: list[list[_Line]] = []
        while index < len(lines) and lines[index].kind in {
            "control",
            "control_block_header",
        }:
            header = lines[index]
            segment = [header]
            index += 1
            while index < len(lines):
                line = lines[index]
                if line.value and line.control_owner != header.control_owner:
                    break
                segment.append(line)
                index += 1
            segments.append(segment)
        for segment in sorted(
            segments, key=lambda group: group[0].kind == "control_block_header"
        ):
            ordered.extend(segment)
    return ordered


def _normalize_blank_lines(lines: list[_Line]) -> list[str]:
    normalized: list[str] = []
    previous: _Line | None = None
    previous_significant_kind: str | None = None
    pending_blank = 0

    for line in lines:
        if not line.value:
            pending_blank += 1
            continue
        kind = line.kind
        previous_kind = previous.kind if previous is not None else None
        same_text = (
            previous is not None
            and line.text_owner is not None
            and line.text_owner == previous.text_owner
        )
        if same_text:
            # Whitespace inside one CST text body is content, never a section separator.
            _append_blank_lines(normalized, pending_blank)
        elif line.separate or _needs_blank_line(
            previous_kind, kind, pending_blank=bool(pending_blank)
        ):
            _append_blank_line(normalized)
        elif previous_kind == "comment" and _needs_blank_line_after_comment(
            previous_significant_kind, kind
        ):
            _append_blank_line(normalized)
        elif pending_blank and _preserves_blank_line(previous_kind, kind):
            _append_blank_lines(
                normalized,
                pending_blank
                if kind in {"message_body", "block_body", "indented"}
                else 1,
            )
        normalized.append(line.value)
        pending_blank = 0
        previous = line
        if kind != "comment":
            previous_significant_kind = kind
    return normalized


def _needs_blank_line(
    previous_kind: str | None, current_kind: str, *, pending_blank: bool
) -> bool:
    if previous_kind is None:
        return False
    if current_kind in {"top_level", "agic_header"}:
        return previous_kind not in {"top_comment"} or pending_blank
    if current_kind == "top_comment":
        return previous_kind not in {"shebang", "top_comment"}
    if current_kind == "program_comment":
        return previous_kind not in {"program_comment", "top_comment"}
    if current_kind == "comment":
        return previous_kind in {"block_body", "message_body", "message_header"}
    if current_kind == "directive":
        return previous_kind not in {"agic_header", "directive"}
    if current_kind == "control":
        return previous_kind in {
            "directive",
            "message_header",
            "message_body",
            "block_body",
        }
    if current_kind in {
        "control_block_header",
        "message_header",
        "message_block_header",
    }:
        return previous_kind in {
            "directive",
            "control",
            "message_header",
            "message_body",
            "block_body",
        }
    if current_kind == "block_body":
        return previous_kind not in {
            "control_block_header",
            "message_block_header",
            "block_body",
        }
    if current_kind == "message_body":
        return previous_kind not in {
            "control_block_header",
            "message_block_header",
            "message_header",
            "message_body",
            "agic_header",
        }
    return False


def _needs_blank_line_after_comment(
    previous_significant_kind: str | None, current_kind: str
) -> bool:
    if (
        previous_significant_kind not in _COMMENT_SPLIT_KINDS
        or current_kind not in _COMMENT_SPLIT_KINDS
    ):
        return False
    return previous_significant_kind == current_kind or _needs_blank_line(
        previous_significant_kind,
        current_kind,
        pending_blank=False,
    )


def _preserves_blank_line(previous_kind: str | None, current_kind: str) -> bool:
    if previous_kind is None:
        return False
    if previous_kind == "shebang" and current_kind == "top_comment":
        return True
    if previous_kind == current_kind == "top_comment":
        return True
    if previous_kind == "comment":
        return True
    return previous_kind == current_kind and current_kind in {
        "message_body",
        "block_body",
        "indented",
    }


def _append_blank_line(lines: list[str]) -> None:
    if lines and lines[-1] != "":
        lines.append("")


def _append_blank_lines(lines: list[str], count: int) -> None:
    existing = 0
    for line in reversed(lines):
        if line:
            break
        existing += 1
    lines.extend("" for _ in range(max(0, count - existing)))


def _parse_runnable_rest(rest: str) -> tuple[str | None, str | None]:
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


def _raise_syntax_error(lines: list[str], node: Node) -> None:
    row = node.start_point.row
    line_number = row + 1
    raw_line = lines[row] if 0 <= row < len(lines) else ""
    raise ToolangFormatError(ast._syntax_error_message(line_number, raw_line))


def _split_inline_comment(line: str) -> tuple[str, str]:
    quoted = False
    escaped = False
    comment_start: int | None = None
    for index, char in enumerate(line):
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
            continue
        if char == '"':
            quoted = True
        elif char == "#" and (index == 0 or line[index - 1].isspace()):
            comment_start = index
            break
    if comment_start is None:
        return line.rstrip(), ""
    body = line[:comment_start].rstrip()
    comment = line[comment_start:].strip()
    return body, f"  {comment}" if body else comment


def _leading_whitespace(line: str) -> str:
    return line[: len(line) - len(line.lstrip(" \t"))]
