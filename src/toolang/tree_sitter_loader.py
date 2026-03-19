from __future__ import annotations

from functools import lru_cache

from tree_sitter import Language, Parser, Tree

from toolang.errors import ToolangError


def parse_tree(source: bytes) -> Tree:
    parser = Parser(load_toolang_language())
    return parser.parse(source)


@lru_cache(maxsize=1)
def load_toolang_language() -> Language:
    try:
        import tree_sitter_toolang
    except ImportError as exc:
        raise ToolangError(
            "The 'tree-sitter-toolang' package is not installed. Install a local wheel "
            "or publishable package before running Toolang parsing commands."
        ) from exc
    return Language(tree_sitter_toolang.language())
