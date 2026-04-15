"""Toolang program AST and parser."""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache

from tree_sitter import Language, Node, Parser
import tree_sitter_toolang

from toolang.base.error import ToolangError


@dataclass(slots=True)
class SourceSpan:
    line: int


@dataclass(slots=True)
class UseDecl:
    kind: str
    reference: str
    span: SourceSpan


@dataclass(slots=True)
class ParamDecl:
    name: str
    optional: bool = False


@dataclass(slots=True)
class DeclBlock:
    kind: str
    name: str
    language: str | None
    body: str
    header_suffix: str
    span: SourceSpan
    params: list[ParamDecl] = field(default_factory=list)


@dataclass(slots=True)
class Thunk:
    name: str | None
    input_name: str | None
    returns: str | None
    directives: list[str] = field(default_factory=list)
    body: str = ""
    span: SourceSpan = field(default_factory=lambda: SourceSpan(0))


@dataclass(slots=True)
class Program:
    uses: list[UseDecl] = field(default_factory=list)
    declarations: list[DeclBlock] = field(default_factory=list)
    thunks: list[Thunk] = field(default_factory=list)
    _source_lines: list[str] | None = field(default=None, repr=False, compare=False)

    def get_decl(self, kind: str, name: str) -> DeclBlock | None:
        for item in self.declarations:
            if item.kind == kind and item.name == name:
                return item
        return None


def parse(source: str) -> Program:
    """Parse one Toolang program source string."""

    normalized_source = _source_without_shebang(source)
    tree = Parser(_toolang_language()).parse(normalized_source.encode("utf-8"))
    lines = normalized_source.splitlines()
    program = Program(_source_lines=lines)

    error_node = _first_error_node(tree.root_node)
    if error_node is not None:
        _raise_syntax_error(lines, error_node)

    for child in tree.root_node.named_children:
        if child.type in {"blank_line", "comment"}:
            continue
        if child.type == "use_statement":
            program.uses.append(_use_from_node(child))
            continue
        if child.type == "declaration":
            program.declarations.append(_decl_from_node(child))
            continue
        if child.type == "thunk":
            program.thunks.append(_thunk_from_node(lines, child))
            continue
        raise ToolangError(
            f"Unsupported statement at line {child.start_point.row + 1}: {_node_text(child)!r}"
        )

    return program


def _use_from_node(node: Node) -> UseDecl:
    return UseDecl(
        kind=_required_text(node, "kind"),
        reference=_required_text(node, "reference"),
        span=SourceSpan(node.start_point.row + 1),
    )


def _decl_from_node(node: Node) -> DeclBlock:
    header = _required_child(node, "header")
    kind = _required_text(header, "kind")
    name = _required_text(header, "name")
    line_number = node.start_point.row + 1
    params = _params_from_node(header.child_by_field_name("parameters"))
    language_node = header.child_by_field_name("language")
    language = _node_text(language_node) if language_node is not None else None
    body_node = node.child_by_field_name("body")
    return DeclBlock(
        kind=kind,
        name=name,
        language=language,
        body=_fence_body_from_node(body_node) if body_node is not None else "",
        header_suffix=f"```{language or ''}" if body_node is not None else "",
        params=params,
        span=SourceSpan(line_number),
    )


def _thunk_from_node(lines: list[str], node: Node) -> Thunk:
    header = _required_child(node, "header")
    thunk = Thunk(
        name=_optional_text(header.child_by_field_name("name")),
        input_name=_thunk_input_from_node(header.child_by_field_name("input")),
        returns=_optional_text(header.child_by_field_name("output")),
        span=SourceSpan(node.start_point.row + 1),
    )
    body_started = False
    body_lines: list[str] = []

    for child in node.named_children:
        if child.type == "thunk_header":
            continue
        if child.type == "blank_line":
            if body_started:
                body_lines.append("")
            continue
        raw_line = _line_text(lines, child.start_point.row)
        if not raw_line.startswith((" ", "\t")):
            raise ToolangError(
                f"Thunk body must be indented under line {node.start_point.row + 1}: {raw_line!r}"
            )
        text = _thunk_line_text_from_node(child)
        if child.type == "directive_line" and not body_started:
            thunk.directives.append(text)
            continue
        body_started = True
        body_lines.append(text)

    thunk.body = "\n".join(body_lines).strip()
    return thunk


def _params_from_node(node: Node | None) -> list[ParamDecl]:
    if node is None:
        return []
    return [
        ParamDecl(
            name=_required_text(parameter, "name"),
            optional=parameter.child_by_field_name("optional") is not None,
        )
        for parameter in node.children_by_field_name("parameter")
    ]


def _fence_body_from_node(node: Node) -> str:
    lines: list[str] = []
    for child in node.named_children:
        text_node = child.child_by_field_name("text")
        lines.append(_node_text(text_node) if text_node is not None else "")
    return "\n".join(lines).rstrip()


def _thunk_input_from_node(node: Node | None) -> str | None:
    if node is None:
        return None
    return _required_text(node, "value")


def _thunk_line_text_from_node(node: Node) -> str:
    if node.type == "directive_line":
        return _node_text(node.named_children[0]).strip()
    if node.type == "prompt_line":
        return _required_text(node, "text")
    raise ToolangError(f"Unsupported thunk content node: {node.type}")


def _first_error_node(node: Node) -> Node | None:
    if node.is_error or node.is_missing:
        return node
    for child in node.children:
        result = _first_error_node(child)
        if result is not None:
            return result
    return None


def _raise_syntax_error(lines: list[str], node: Node) -> None:
    line_number = node.start_point.row + 1
    raw_line = _line_text(lines, node.start_point.row)
    if raw_line.startswith((" ", "\t")) and raw_line.strip():
        raise ToolangError(f"Unexpected indentation at line {line_number}.")
    raise ToolangError(f"Syntax error at line {line_number}.")


def _required_child(node: Node, field_name: str) -> Node:
    child = node.child_by_field_name(field_name)
    if child is None:
        raise ToolangError(f"Missing syntax field {field_name!r} at line {node.start_point.row + 1}.")
    return child


def _required_text(node: Node, field_name: str) -> str:
    return _node_text(_required_child(node, field_name))


def _optional_text(node: Node | None) -> str | None:
    return _node_text(node) if node is not None else None


def _node_text(node: Node | None) -> str:
    if node is None or node.text is None:
        return ""
    return node.text.decode("utf-8")


def _line_text(lines: list[str], row: int) -> str:
    if 0 <= row < len(lines):
        return lines[row]
    return ""


def _source_without_shebang(source: str) -> str:
    if not source.startswith("#!"):
        return source
    first_line, separator, rest = source.partition("\n")
    if not separator:
        return ""
    return f"\n{rest}"


@lru_cache(maxsize=1)
def _toolang_language() -> Language:
    return Language(tree_sitter_toolang.language())
