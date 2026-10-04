"""Lossless top-level declaration indexing for authored source edits."""

from __future__ import annotations

from dataclasses import dataclass
import re

from .ast import _parse_source
from .errors import ToolangSourceError

DECLARATION_KINDS = (
    "agic",
    "flow",
    "instruct",
    "context",
    "struct",
    "psyche",
    "skill",
    "service",
    "prompt",
    "task",
    "chore",
)
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*\Z")
_ATTACHED_COMMENTS = {"plain_comment", "item_doc_comment"}


@dataclass(frozen=True)
class SourceDeclaration:
    key: str
    line: int
    start: int
    end: int
    source: str


def validate_declaration_key(key: str) -> None:
    """Validate a declaration address, not a filesystem path."""
    kind, separator, name = key.partition(":")
    if not separator or kind not in DECLARATION_KINDS or not _NAME.fullmatch(name):
        raise ValueError("program key must be a declaration kind:name")
    if name == "_" and kind not in {"agic", "flow"}:
        raise ValueError("only unnamed agic and flow declarations use the '_' key")


def source_declarations(source: str) -> tuple[SourceDeclaration, ...]:
    """Index direct declarations; offsets count UTF-8 bytes, including comments.

    Only syntax is required here, so callers can inspect and repair semantic
    errors or fragments that refer to declarations elsewhere in the program.
    """
    parsed = _parse_source(source)
    encoded = source.encode("utf-8")
    declarations = []
    pending_start: int | None = None
    keys: set[str] = set()
    for node in parsed.tree.root_node.named_children:
        if node.type in _ATTACHED_COMMENTS:
            if pending_start is None:
                pending_start = node.start_byte
            continue
        if node.type != "item":
            pending_start = None
            continue
        declaration = node.named_children[0]
        start = node.start_byte if pending_start is None else pending_start
        pending_start = None
        kind = declaration.type
        if kind not in DECLARATION_KINDS:
            continue
        name_node = declaration.child_by_field_name("name")
        name = (
            parsed.source[name_node.start_byte : name_node.end_byte].decode("utf-8")
            if name_node is not None
            else "_"
            if kind in {"agic", "flow"}
            else "default"
        )
        key = f"{kind}:{name}"
        if key in keys:
            raise ToolangSourceError(
                f"duplicate program declaration: {key}",
                line=node.start_point.row + 1,
            )
        keys.add(key)
        end = min(node.end_byte, len(encoded))
        # CST bodies include separator blank lines; those belong to the file.
        lines = encoded[start:end].splitlines(keepends=True)
        while lines and not lines[-1].strip():
            end -= len(lines.pop())
        declarations.append(
            SourceDeclaration(
                key=key,
                line=node.start_point.row + 1,
                start=start,
                end=end,
                source=encoded[start:end].decode("utf-8"),
            )
        )
    return tuple(declarations)


def declaration_fragment(source: str, key: str) -> str:
    """Require exactly one matching declaration and its attached comments."""
    declarations = source_declarations(source)
    if len(declarations) != 1 or declarations[0].key != key:
        raise ValueError(
            "content.source must contain exactly the declaration named by key"
        )
    declaration = declarations[0]
    encoded = source.encode("utf-8")
    if encoded[: declaration.start].strip() or encoded[declaration.end :].strip():
        raise ValueError("file-level content requires a whole-program update")
    return declaration.source


def replace_declaration(
    source: str, declaration: SourceDeclaration, replacement: str
) -> str:
    """Replace one indexed declaration without formatting neighboring source."""
    encoded = source.encode("utf-8")
    if (
        replacement
        and not replacement.endswith("\n")
        and declaration.end < len(encoded)
    ):
        replacement += "\n"
    return (
        encoded[: declaration.start]
        + replacement.encode("utf-8")
        + encoded[declaration.end :]
    ).decode("utf-8")
