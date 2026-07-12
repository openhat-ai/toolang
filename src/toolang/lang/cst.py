"""Tree-sitter parsing for Toolang source."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from tree_sitter import Language, Node, Parser, Tree
import tree_sitter_toolang

from .diagnostics import ToolangSyntaxError


@dataclass(frozen=True, slots=True)
class Cst:
    tree: Tree
    source: bytes
    lines: tuple[str, ...]


def parse(source: str) -> Cst:
    """Parse source into a checked concrete syntax tree."""

    normalized = _without_shebang(source)
    syntax = normalized if not normalized or normalized.endswith("\n") else f"{normalized}\n"
    encoded = syntax.encode("utf-8")
    tree = Parser(_language()).parse(encoded)
    lines = tuple(normalized.splitlines())
    if error := _first_error(tree.root_node):
        line = error.start_point.row + 1
        raw = lines[line - 1] if line <= len(lines) else ""
        if raw.startswith((" ", "\t")) and raw.strip():
            raise ToolangSyntaxError(f"Unexpected indentation at line {line}.")
        raise ToolangSyntaxError(f"Syntax error at line {line}.")
    return Cst(tree=tree, source=encoded, lines=lines)


def _first_error(node: Node) -> Node | None:
    if node.is_error or node.is_missing or node.type.startswith("invalid_"):
        return node
    for child in node.children:
        if error := _first_error(child):
            return error
    return None


def _without_shebang(source: str) -> str:
    if not source.startswith("#!"):
        return source
    _first, separator, rest = source.partition("\n")
    return f"\n{rest}" if separator else ""


@lru_cache(maxsize=1)
def _language() -> Language:
    return Language(tree_sitter_toolang.language())
