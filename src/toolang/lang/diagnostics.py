"""Pure syntax diagnosis shared by source parsing, formatting, and inspection."""

from collections.abc import Iterator

from tree_sitter import Node

from .types import SourceDiagnostic, SourceLocation


_PUNCTUATION = {
    "lparen": "(",
    "rparen": ")",
    "lbracket": "[",
    "rbracket": "]",
    "lbrace": "{",
    "rbrace": "}",
    "colon": ":",
    "comma": ",",
}
_CONTEXTS = {
    "params": "parameter list",
    "param": "parameter",
    "field": "field declaration",
    "property": "property assignment",
    "return_type": "return type",
    "flow": "flow block",
    "agic": "agic block",
    "struct": "struct declaration",
    "repeat_statement": "repeat block",
    "settle_statement": "settle block",
    "skill": "skill declaration",
    "service": "service declaration",
    "psyche": "psyche declaration",
    "prompt": "prompt declaration",
}
_INVALID_CONSTRUCTS = {
    "invalid_flow_reserved_statement": "flow statement",
    "invalid_agic_reserved_message": "message header",
}


def error_kind(node: Node) -> str | None:
    if node.is_missing:
        return "missing"
    if node.is_error:
        return "error"
    if node.type.startswith("invalid_"):
        return "invalid"
    return None


def error_nodes(root: Node) -> Iterator[Node]:
    """Include grammar-invalid nodes even when native has_error is false."""
    pending = [root]
    while pending:
        node = pending.pop()
        if error_kind(node) is not None:
            yield node
        pending.extend(reversed(node.children))


def _ancestors(node: Node) -> Iterator[Node]:
    current = node.parent
    while current is not None:
        yield current
        current = current.parent


def primary_error(root: Node) -> Node | None:
    """Localize the first recovery region without skipping to later errors."""
    errors = error_nodes(root)
    first = next(errors, None)
    if first is None:
        return None
    candidate = first
    for child in errors:
        if not candidate.is_error or candidate not in _ancestors(child):
            break
        candidate = child
    # Neither generic recovery nor an unknown grammar node proves a cause.
    supported = candidate.type in _INVALID_CONSTRUCTS or (
        candidate.is_missing
        and (
            candidate.type in _PUNCTUATION
            or any(
                parent.type in {"type", "property_value"}
                for parent in _ancestors(candidate)
            )
        )
    )
    return candidate if supported else first


def source_position(node: Node, source: bytes) -> tuple[int, int]:
    """One-based byte position, clamped to authored EOF after normalization."""
    if node.start_byte <= len(source):
        row, column = node.start_point
        return row + 1, column + 1
    offset = len(source)
    return source.count(b"\n", 0, offset) + 1, offset - source.rfind(b"\n", 0, offset)


def _excerpt(text: str, *, truncated: bool = False) -> str:
    return repr(text[:100] + ("…" if truncated or len(text) > 100 else ""))


def _context(node: Node) -> str:
    for ancestor in (node, *_ancestors(node)):
        if context := _CONTEXTS.get(ancestor.type):
            return context
        if ancestor.is_error:
            # Recovery can erase the owning declaration node. Retain evidence
            # from its keyword, without claiming that a child token caused it.
            for child in ancestor.children:
                keyword = child.type.removesuffix("_keyword")
                if context := _CONTEXTS.get(keyword):
                    return context
    return "source"


def syntax_diagnostic(node: Node, source: bytes) -> SourceDiagnostic:
    """Describe evidence, not the parser's arbitrary recovery-token choice."""
    context = _context(node)
    ancestors = {parent.type for parent in _ancestors(node)}
    if node.is_missing:
        if token := _PUNCTUATION.get(node.type):
            reason = f"Expected {token!r}"
            if context != "source":
                reason += f" in {context}"
        elif "type" in ancestors:
            category = (
                "field"
                if "field" in ancestors
                else "parameter"
                if "param" in ancestors
                else "value"
            )
            reason = f"Expected a {category} type"
        elif "property_value" in ancestors:
            reason = "Expected a property value after '='"
        else:
            reason = "Parse error"
            if context != "source":
                reason += f" in {context}"
    elif node.type in _INVALID_CONSTRUCTS:
        keyword_node = node.children[0] if node.children else node
        keyword = source[keyword_node.start_byte : keyword_node.end_byte].decode(
            "utf-8"
        )
        category = _INVALID_CONSTRUCTS[node.type]
        reason = f"Malformed {category} {_excerpt(keyword)}"
    else:
        reason = "Parse error"
        if context != "source":
            reason += f" in {context}"
    line, column = source_position(node, source)
    end_line, end_column = node.end_point
    if node.end_byte > len(source):
        end_line = source.count(b"\n")
        end_column = len(source) - source.rfind(b"\n") - 1
    return SourceDiagnostic(
        reason,
        SourceLocation(
            line,
            column,
            end_line + 1,
            end_column + 1,
            precision="token"
            if node.is_missing
            else "recovery"
            if node.is_error
            else "construct",
        ),
    )


def syntax_message(node: Node, source: bytes) -> str:
    """Return only the factual reason for raw CST consumers."""
    return syntax_diagnostic(node, source).reason


class DiagnosticSource:
    """Index one original source once for rendering any number of diagnostics."""

    def __init__(self, source: str):
        self.source = source.encode("utf-8")
        self.lines = self.source.split(b"\n")
        self.starts = [0]
        for line in self.lines[:-1]:
            self.starts.append(self.starts[-1] + len(line) + 1)

    def _offset(self, line: int, column: int | None) -> int:
        row = min(max(0, line - 1), len(self.lines) - 1)
        return self.starts[row] + min(max(0, (column or 1) - 1), len(self.lines[row]))

    def excerpt(self, location: SourceLocation) -> str | None:
        if location.origin != "authored" or not 1 <= location.line <= len(self.lines):
            return None
        if (
            location.precision == "recovery"
            and location.end_line is not None
            and location.end_column is not None
        ):
            start = self._offset(location.line, location.column)
            end = self._offset(location.end_line, location.end_column)
            # Limit decoding too, including recovery regions spanning the whole file.
            raw = (
                self.source[start : min(end, start + 404)]
                .decode("utf-8", errors="ignore")
                .strip()
            )
            return _excerpt(raw, truncated=end > start + 404)
        raw = self.lines[location.line - 1]
        head = raw[:404].decode("utf-8", errors="ignore")
        if len(raw) <= 404 and len(head) <= 100:
            return _excerpt(head.strip())
        offset = min(max(0, (location.column or 1) - 1), len(raw))
        # Four bytes per character suffice for a bounded UTF-8 window. Ignore
        # only a partial code point at a window boundary, never re-scan the line.
        before = raw[max(0, offset - 200) : offset].decode("utf-8", errors="ignore")[
            -50:
        ]
        after = raw[offset : offset + 404].decode("utf-8", errors="ignore")
        truncated_start = offset > len(before.encode("utf-8"))
        return _excerpt(
            ("…" if truncated_start else "") + (before + after).strip(),
            truncated=offset + 404 < len(raw),
        )


def _location_label(location: SourceLocation | None, label: str | None) -> str:
    if location is None:
        return label or ""
    point = str(location.line)
    if location.column is not None:
        point += f":{location.column}"
    if location.origin == "generated":
        return f"{label + ': ' if label else ''}generated {point}"
    return f"{label}:{point}" if label is not None else f"line {point}"


def render_diagnostic(
    diagnostic: SourceDiagnostic,
    *,
    label: str | None = None,
    source: DiagnosticSource | None = None,
) -> str:
    """Render facts without extracting locations or rewriting upstream messages."""
    prefix = _location_label(diagnostic.location, label)
    message = f"{prefix}: {diagnostic.reason}" if prefix else diagnostic.reason
    if source is not None and diagnostic.location is not None:
        if excerpt := source.excerpt(diagnostic.location):
            message += f": {excerpt}"
    for note in diagnostic.related:
        prefix = _location_label(note.location, label)
        message += f"\n{prefix}: note: {note.reason}"
    return message
