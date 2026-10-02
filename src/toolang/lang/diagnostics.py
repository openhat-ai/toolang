"""Pure syntax diagnosis shared by source parsing, formatting, and inspection."""

from collections.abc import Iterator

from tree_sitter import Node


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
    for candidate in errors:
        if not first.is_error or first not in _ancestors(candidate):
            break
        first = candidate
    return first


def source_position(node: Node, source: bytes) -> tuple[int, int]:
    """One-based byte position, clamped to authored EOF after normalization."""
    if node.start_byte <= len(source):
        row, column = node.start_point
        return row + 1, column + 1
    offset = len(source)
    return source.count(b"\n", 0, offset) + 1, offset - source.rfind(b"\n", 0, offset)


def _excerpt(text: str) -> str:
    return repr(text[:100] + ("…" if len(text) > 100 else ""))


def _context(node: Node) -> str:
    for ancestor in _ancestors(node):
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


def syntax_message(node: Node, source: bytes) -> str:
    """Describe evidence, not the parser's arbitrary recovery-token choice."""
    context = _context(node)
    ancestors = {parent.type for parent in _ancestors(node)}
    if node.is_missing:
        if token := _PUNCTUATION.get(node.type):
            reason = f"Expected {token!r} in {context}"
        elif "type" in ancestors:
            category = (
                "field"
                if "field" in ancestors
                else "parameter"
                if "param" in ancestors
                else "value"
            )
            reason = f"Expected a {category} type in {context}"
        elif "property_value" in ancestors:
            reason = "Expected a property value after '='"
        else:
            reason = f"Expected syntax in {context}"
    elif node.type in {
        "invalid_flow_reserved_statement",
        "invalid_agic_reserved_message",
    }:
        keyword_node = node.children[0] if node.children else node
        keyword = source[keyword_node.start_byte : keyword_node.end_byte].decode(
            "utf-8"
        )
        category = (
            "flow statement"
            if node.type == "invalid_flow_reserved_statement"
            else "message header"
        )
        reason = f"Malformed {category} {_excerpt(keyword)}"
    else:
        reason = f"Unexpected syntax in {context}"
        if node.start_point.row != node.end_point.row or any(
            parent.is_error and parent.start_point.row < node.start_point.row
            for parent in _ancestors(node)
        ):
            reason += "; check the surrounding block structure"
    line, _ = source_position(node, source)
    start = source.rfind(b"\n", 0, min(node.start_byte, len(source))) + 1
    end = source.find(b"\n", start)
    raw = source[start : end if end >= 0 else len(source)].decode("utf-8")
    character = len(source[start : min(node.start_byte, len(source))].decode("utf-8"))
    window = max(0, character - 50) if len(raw) > 100 else 0
    raw = ("…" if window else "") + raw[window:].strip()
    return f"Syntax error at line {line}: {reason} near {_excerpt(raw)}."
