"""Tree presentation and byte-preserving source rendering for CLI commands."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import fields
from enum import StrEnum
from io import StringIO
import json
from typing import Annotated, TextIO

from rich.console import Console
from rich.segment import Segment, Segments
from rich.style import Style
from tree_sitter import Node
import typer


class ColorMode(StrEnum):
    auto = "auto"
    always = "always"
    never = "never"


ColorOption = Annotated[
    ColorMode | None,
    typer.Option("--color", help="Terminal color policy", show_default="auto"),
]
HtmlOption = Annotated[
    bool, typer.Option("--html", help="Export standalone highlighted HTML")
]

_PALETTE = {
    "comment": "dim",
    "punctuation": "dim",
    "keyword": "magenta",
    "operator": "magenta",
    "type": "cyan",
    "function": "blue",
    "property": "yellow",
    "variable.parameter": "yellow",
    "constant": "bright_cyan",
    "string": "green",
}


def color_enabled(
    mode: ColorMode | None, *, environ: Mapping[str, str], terminal: bool
) -> bool:
    if mode == ColorMode.always:
        return True
    if mode == ColorMode.never:
        return False
    if environ.get("NO_COLOR"):
        return False
    if environ.get("FORCE_COLOR", "") not in {"", "0"}:
        return True
    return terminal and environ.get("TERM") != "dumb"


def _style(name: str) -> Style | None:
    while name:
        if name in _PALETTE:
            return Style.parse(_PALETTE[name])
        name = name.rpartition(".")[0]
    return None


def render_source(source: str, *, color: bool, html: bool) -> str:
    """Render without normalizing whitespace or interpreting source markup."""
    if not color and not html:
        return source
    from toolang.lang.highlight import captures, resolve

    encoded = source.encode("utf-8")
    segments: list[Segment] = []
    previous = 0
    for capture in resolve(captures(encoded)):
        segments.append(Segment(encoded[previous : capture.start_byte].decode("utf-8")))
        segments.append(
            Segment(
                encoded[capture.start_byte : capture.end_byte].decode("utf-8"),
                _style(capture.name),
            )
        )
        previous = capture.end_byte
    segments.append(Segment(encoded[previous:].decode("utf-8")))
    output = StringIO(newline="")
    console = Console(
        file=output,
        force_terminal=True,
        color_system="standard",
        no_color=False,
        legacy_windows=False,
        record=html,
        markup=False,
        highlight=False,
    )
    console.print(Segments(segments), end="", soft_wrap=True)
    return console.export_html(inline_styles=True) if html else output.getvalue()


def write_source(source: str, stream: TextIO) -> None:
    """Avoid platform newline translation, while supporting text test streams."""
    buffer = getattr(stream, "buffer", None)
    if buffer is not None:
        stream.flush()
        buffer.write(source.encode("utf-8"))
        buffer.flush()
    else:
        stream.write(source)


def ast_sexp(value: object) -> str:
    """Render typed AST values; metadata mappings never masquerade as nodes."""
    from toolang.lang.ast import Node as AstNode, Span

    def render(value: object, depth: int) -> str:
        prefix = "  " * (depth + 1)
        if isinstance(value, AstNode):
            entries = [
                f"{item.name}: {render(getattr(value, item.name), depth + 1)}"
                for item in fields(value)
            ]
            return container(value.kind, entries, prefix)
        if isinstance(value, Span):
            return f"(span line: {value.line})"
        if isinstance(value, Mapping):
            entries = [
                f"({json.dumps(key, ensure_ascii=False)} {render(item, depth + 1)})"
                for key, item in value.items()
            ]
            return container("map", entries, prefix)
        if isinstance(value, (list, tuple)):
            return container(
                "list", [render(item, depth + 1) for item in value], prefix
            )
        return json.dumps(value, ensure_ascii=False)

    def container(kind: str, entries: list[str], prefix: str) -> str:
        return f"({kind}" + "".join(f"\n{prefix}{item}" for item in entries) + ")"

    return render(value, 0) + "\n"


def cst_sexp(root: Node) -> str:
    """Indent the named tree, including native missing/error markers."""
    parts: list[str] = []
    pending: list[tuple[Node | None, str | None, int]] = [(root, None, 0)]
    while pending:
        node, field, depth = pending.pop()
        if node is None:
            parts.append(")")
            continue
        if parts:
            parts.append("\n" + "  " * depth)
        if field is not None:
            parts.append(f"{field}: ")
        if node.is_missing:
            parts.append(str(node))
            continue
        parts.append(f"({node.type}")
        pending.append((None, None, depth))
        pending.extend(
            (child, node.field_name_for_child(index), depth + 1)
            for index, child in reversed(list(enumerate(node.children)))
            if child.is_named or child.is_missing
        )
    return "".join(parts) + "\n"
