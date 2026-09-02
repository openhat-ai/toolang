"""Source formatter for `.too` files."""

from __future__ import annotations

import re

from tree_sitter import Node, Tree
from toolang.common.query import format_query_text

from . import ast
from .ast import _first_syntax_error, _parse_tree
from .errors import ToolangFormatError


_RUNNABLE_HEADER_RE = re.compile(
    r"^(?P<kind>agic|flow)(?P<rest>.*):(?P<suffix>[ \t]*(?:#.*)?)$"
)
_STRUCT_HEADER_RE = re.compile(r"^struct(?P<rest>.*):(?P<suffix>[ \t]*(?:#.*)?)$")
_DIRECTIVE_RE = re.compile(
    r"^(?P<indent>[ \t]*)(?P<key>models|tools|skills|services|psyches|prompts|hands|handoffs|recall)"
    r"(?P<space>[ \t]*)(?P<op>=|\+=|-=)"
)
_TOP_LEVEL_RE = re.compile(
    r"^(with|struct|psyche|skill|service|prompt|task|chore|context|instruct|agic|flow)\b"
)
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
    "rank_statement",
    "repeat_statement",
    "until_statement",
}
_FLOW_STATEMENT_RE = re.compile(
    r"^(let|run|seek|ask|scatter|storm|gather|settle|map|keep|drop|rank|repeat|until|pass)\b"
)


def format_source(source: str, *, tab_size: int = 2) -> str:
    """Return a canonical formatting of one Toolang program source string."""

    if tab_size < 1:
        raise ToolangFormatError("tab size must be positive.")
    if not source:
        return ""
    tree = _syntax_tree(source)
    formatted = "\n".join(
        _format_source_lines(
            source.splitlines(), root=tree.root_node, tab_size=tab_size
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
            _authored_runnable(statement.runnable),
        )
    elif isinstance(statement, ast.StormStmt):
        head = _statement_words(
            "storm",
            str(statement.count),
            _authored_runnable(statement.runnable),
            _parallel_clause(statement.lanes),
        )
    elif isinstance(statement, ast.GatherStmt):
        head = _statement_words("gather", _authored_runnable(statement.runnable))
    elif isinstance(statement, ast.SettleStmt):
        head = _statement_words("settle", _authored_runnable(statement.runnable))
    elif isinstance(statement, ast.MapStmt):
        head = _statement_words(
            "map",
            _authored_runnable(statement.runnable),
            _parallel_clause(statement.lanes),
        )
    elif isinstance(statement, ast.KeepStmt | ast.DropStmt):
        head = _statement_words(
            statement.kind,
            statement.position,
            str(statement.count) if statement.count is not None else "",
            _authored_runnable(statement.runnable or ""),
            _parallel_clause(statement.lanes),
        )
    elif isinstance(statement, ast.RankStmt):
        head = _statement_words(
            "rank",
            _authored_runnable(statement.runnable),
            statement.selection,
            str(statement.limit) if statement.limit is not None else "",
            _parallel_clause(statement.lanes),
        )
    elif isinstance(statement, ast.RepeatStmt):
        return _statement_words(
            "repeat",
            str(statement.count) if statement.count is not None else "",
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
    return f"par {value}" if value is not None else ""


def _format_source_lines(lines: list[str], *, root: Node, tab_size: int) -> list[str]:
    formatted: list[str] = []
    indent = " " * tab_size
    current_top: str | None = None
    agic_block_indent: int | None = None
    flow_repeat_indents: list[int] = []
    flow_content_block: tuple[int, int] | None = None

    for index, raw_line in enumerate(lines):
        line = raw_line.rstrip()
        stripped = line.strip()

        if not stripped:
            formatted.append("")
            continue

        column = len(_leading_whitespace(line))
        node = root.named_descendant_for_point_range((index, column), (index, column))
        if node is None:
            formatted.append(line)
            continue
        if column == 0:
            current_top = _top_level_kind(stripped)
            agic_block_indent = None
            flow_repeat_indents.clear()
            flow_content_block = None
        if line.startswith("#!"):
            formatted.append(line)
            continue

        if current_top == "agic" and column > 0:
            if _DIRECTIVE_RE.match(line):
                formatted.append(f"{indent}{_format_directive_line(stripped)}")
                agic_block_indent = None
                continue
            if match := _MESSAGE_HEADER_RE.match(stripped):
                formatted.append(f"{indent}{_format_message_header_line(match)}")
                agic_block_indent = column if not match.group("body").strip() else None
                continue
            if stripped.startswith("#"):
                formatted.append(f"{indent}{_format_comment_line(stripped)}")
                continue
            if agic_block_indent is not None and column > agic_block_indent:
                formatted.append(f"{indent}{indent}{stripped}")
                continue
            agic_block_indent = None
            formatted.append(f"{indent}{stripped}")
            continue

        if current_top == "flow" and column > 0:
            if flow_content_block is not None:
                block_indent, block_depth = flow_content_block
                if column > block_indent:
                    formatted.append(f"{indent * (block_depth + 1)}{stripped}")
                    continue
                flow_content_block = None

            if _DIRECTIVE_RE.match(line):
                formatted.append(f"{indent}{_format_directive_line(stripped)}")
                flow_content_block = None
                continue

            if _FLOW_STATEMENT_RE.match(stripped):
                while flow_repeat_indents and column <= flow_repeat_indents[-1]:
                    flow_repeat_indents.pop()
                depth = 1 + len(flow_repeat_indents)
                formatted.append(
                    f"{indent * depth}{_format_flow_statement_line(stripped)}"
                )
                empty_content_assignment = (
                    stripped.startswith("let ")
                    and "=" in stripped
                    and not stripped.partition("=")[2].strip()
                )
                flow_content_block = (
                    (column, depth)
                    if empty_content_assignment
                    or (
                        ":" in stripped
                        and not stripped.partition(":")[2].strip()
                        and not stripped.startswith("repeat")
                    )
                    else None
                )
                if stripped.startswith("repeat"):
                    flow_repeat_indents.append(column)
                continue

            if stripped.startswith("#"):
                depth = 1 + sum(
                    1 for repeat_indent in flow_repeat_indents if repeat_indent < column
                )
                formatted.append(f"{indent * depth}{_format_comment_line(stripped)}")
                continue

            depth = 1 + sum(
                1 for repeat_indent in flow_repeat_indents if repeat_indent < column
            )
            formatted.append(f"{indent * depth}{stripped}")
            continue

        depth = 0 if column == 0 else _indent_depth(node)
        rendered_indent = indent * depth
        if node.type == "indented_raw_text":
            extra = _relative_content_indent(lines, node, tab_size=tab_size)
            content = line.lstrip(" \t")
            formatted.append(f"{rendered_indent}{' ' * extra}{content}")
            continue

        formatted.append(f"{rendered_indent}{_format_syntax_line(stripped, node=node)}")

    return _collapse_blank_edges(
        _normalize_blank_lines(
            _order_program_comments(
                _order_control_segments(formatted, tab_size=tab_size)
            ),
            tab_size=tab_size,
        )
    )


def _syntax_tree(source: str) -> Tree:
    syntax = source if source.endswith("\n") else f"{source}\n"
    tree = _parse_tree(syntax.encode("utf-8"))
    error_node = _first_syntax_error(tree.root_node)
    if error_node is not None:
        _raise_syntax_error(source.splitlines(), error_node)
    return tree


def _format_syntax_line(stripped_line: str, *, node: Node) -> str:
    if stripped_line.startswith("#"):
        return _format_comment_line(stripped_line)

    ancestors = _ancestor_types(node)
    top_level = _top_level_kind(stripped_line)
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
        return _format_flow_statement_line(stripped_line)
    return stripped_line


def _ancestor_types(node: Node) -> set[str]:
    result: set[str] = set()
    current: Node | None = node
    while current is not None:
        result.add(current.type)
        current = current.parent
    return result


def _indent_depth(node: Node) -> int:
    ancestors = _ancestor_types(node)
    if "property" in ancestors and ancestors & {
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
            if current.type in {"repeat_body", "repeat_until_body", "text_body"}
        )
    if ancestors & {"context", "instruct"} and "text_body" in ancestors:
        return 1
    return 0


def _ancestors(node: Node):
    current: Node | None = node
    while current is not None:
        yield current
        current = current.parent


def _relative_content_indent(lines: list[str], node: Node, *, tab_size: int) -> int:
    container = next(
        (
            current
            for current in _ancestors(node)
            if current.type
            in {"text_body", "unroled_message", "implicit_run_statement"}
        ),
        node,
    )
    content_rows = [
        child.start_point.row
        for child in container.named_children
        if child.type == "text_body_line"
    ]
    if not content_rows:
        content_rows = [node.start_point.row]
    widths = [
        len(_leading_whitespace(lines[row]).expandtabs(tab_size))
        for row in content_rows
        if row < len(lines) and lines[row].strip()
    ]
    base = min(widths, default=0)
    current = len(_leading_whitespace(lines[node.start_point.row]).expandtabs(tab_size))
    return max(0, current - base)


def _top_level_kind(stripped_line: str) -> str | None:
    match = _TOP_LEVEL_RE.match(stripped_line)
    if match is None:
        return None
    return match.group(1)


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


def _format_flow_statement_line(stripped_line: str) -> str:
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
    in_agic = False
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

        top_level = (
            _top_level_kind(stripped)
            if stripped and not _leading_whitespace(line)
            else None
        )
        if top_level is not None:
            in_agic = top_level == "agic"

        if in_agic and _control_header_kind(line, tab_size=tab_size) is not None:
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
        if (
            index >= len(lines)
            or _control_header_kind(lines[index], tab_size=tab_size) is None
        ):
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
        and len(_leading_whitespace(line).expandtabs(tab_size)) > tab_size
    )


def _normalize_blank_lines(lines: list[str], *, tab_size: int) -> list[str]:
    normalized: list[str] = []
    in_fence = False
    in_agic = False
    previous_kind: str | None = None
    previous_significant_kind: str | None = None
    pending_blank = 0

    for line in lines:
        stripped = line.strip()
        if in_fence:
            normalized.append(line)
            if stripped.startswith("```"):
                in_fence = False
            previous_kind = "fence"
            continue
        if not stripped:
            pending_blank += 1
            continue

        kind = _formatted_line_kind(line, in_agic=in_agic, tab_size=tab_size)
        if _needs_blank_line(previous_kind, kind, pending_blank=bool(pending_blank)):
            _append_blank_line(normalized)
        elif previous_kind == "comment" and _needs_blank_line_after_comment(
            previous_significant_kind, kind
        ):
            _append_blank_line(normalized)
        elif pending_blank and _preserves_blank_line(previous_kind, kind):
            _append_blank_lines(
                normalized,
                min(
                    pending_blank,
                    2 if kind in {"message_body", "block_body", "indented"} else 1,
                ),
            )
        normalized.append(line)
        pending_blank = 0

        top_level = _top_level_kind(stripped) if not _leading_whitespace(line) else None
        if top_level is not None:
            in_agic = top_level == "agic"
        if _opens_fence(line):
            in_fence = True
        previous_kind = kind
        if kind != "comment":
            previous_significant_kind = kind

    return normalized


def _formatted_line_kind(line: str, *, in_agic: bool, tab_size: int) -> str:
    stripped = line.strip()
    if line.startswith("#!"):
        return "shebang"
    if not _leading_whitespace(line):
        if stripped.startswith("##!"):
            return "program_comment"
        if stripped.startswith("#"):
            return "top_comment"
        top_level = _top_level_kind(stripped)
        if top_level == "agic":
            return "agic_header"
        if top_level is not None:
            return "top_level"
        return "other_top_level"
    if not in_agic:
        return "indented"
    if _DIRECTIVE_RE.match(line):
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
    if len(_leading_whitespace(line).expandtabs(tab_size)) > tab_size:
        return "block_body"
    if stripped.startswith("#"):
        return "comment"
    return "message_body"


def _formatted_message_header_match(
    line: str, *, tab_size: int
) -> re.Match[str] | None:
    indent = re.escape(" " * tab_size)
    return re.match(rf"^{indent}(?P<kind>context|instruct|user|assistant|tool):", line)


def _needs_blank_line(
    previous_kind: str | None, current_kind: str, *, pending_blank: bool
) -> bool:
    if previous_kind is None:
        return False
    if current_kind in {"top_level", "agic_header", "other_top_level"}:
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
    if current_kind == "control_block_header":
        return previous_kind in {
            "directive",
            "control",
            "message_header",
            "message_body",
            "block_body",
        }
    if current_kind == "message_header":
        return previous_kind in {
            "directive",
            "control",
            "message_header",
            "message_body",
            "block_body",
        }
    if current_kind == "message_block_header":
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
    if raw_line.startswith((" ", "\t")) and raw_line.strip():
        raise ToolangFormatError(f"Unexpected indentation at line {line_number}.")
    raise ToolangFormatError(f"Syntax error at line {line_number}.")


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
