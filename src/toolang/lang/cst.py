"""Raw Tree-sitter parsing and concrete syntax inspection."""

from __future__ import annotations

from functools import lru_cache
from importlib.metadata import version
from typing import Any

from tree_sitter import Language, Node, Parser, Tree
import tree_sitter_toolang


@lru_cache(maxsize=1)
def language() -> Language:
    return Language(tree_sitter_toolang.language())


def parse(source: bytes) -> Tree:
    """Parse exactly the supplied bytes, including incomplete source."""
    return Parser(language()).parse(source)


def _range(node: Node) -> dict[str, Any]:
    return {
        "start_byte": node.start_byte,
        "end_byte": node.end_byte,
        "start_point": {"row": node.start_point.row, "column": node.start_point.column},
        "end_point": {"row": node.end_point.row, "column": node.end_point.column},
    }


def diagnostics(root: Node) -> list[dict[str, Any]]:
    """Return syntax diagnostics without performing semantic validation."""
    result = []
    pending = [root]
    while pending:
        node = pending.pop()
        kind = (
            "missing"
            if node.is_missing
            else "error"
            if node.is_error
            else "invalid"
            if node.type.startswith("invalid_")
            else None
        )
        if kind is not None:
            result.append(
                {
                    "kind": kind,
                    "node_type": node.type,
                    "message": f"Missing {node.type}"
                    if node.is_missing
                    else f"Syntax error: {node.type}",
                    **_range(node),
                }
            )
        pending.extend(reversed(node.children))
    return sorted(result, key=lambda item: (item["start_byte"], item["end_byte"]))


def to_data(tree: Tree, source: str) -> dict[str, Any]:
    """Project all nodes, including anonymous tokens, with original source."""
    root: dict[str, Any] = {}
    pending = [(tree.root_node, None, root)]
    while pending:
        node, field, data = pending.pop()
        children = node.children
        projected: list[dict[str, Any]] = [{} for _ in children]
        data.update(
            type=node.type,
            field=field,
            is_named=node.is_named,
            is_extra=node.is_extra,
            is_error=node.is_error,
            is_missing=node.is_missing,
            has_error=node.has_error,
            **_range(node),
            children=projected,
        )
        pending.extend(
            (child, node.field_name_for_child(index), projected[index])
            for index, child in enumerate(children)
        )
    return {
        "schema_version": 1,
        "grammar": {"name": "toolang", "version": version("tree-sitter-toolang")},
        "source": source,
        "root": root,
        "diagnostics": diagnostics(tree.root_node),
    }
