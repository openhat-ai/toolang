from __future__ import annotations

from functools import lru_cache

from tree_sitter import Language, Node, Parser

from toolang.ast import DeclBlock, ParamDecl, Program, SourceSpan, Thunk, UseDecl
from toolang.errors import ToolangError


def parse_program(source: str) -> Program:
    source_bytes = source.encode("utf-8")
    tree = _parse_tree(source_bytes)
    lines = source.splitlines()
    program = Program()

    error_node = _first_error_node(tree.root_node)
    if error_node is not None:
        _raise_syntax_error(lines, error_node)

    for child in tree.root_node.named_children:
        if child.type in {"blank_line", "comment"}:
            continue
        if child.type == "use_statement":
            program.uses.append(_parse_use(child))
            continue
        if child.type == "declaration":
            program.declarations.append(_parse_decl(child))
            continue
        if child.type == "thunk":
            program.thunks.append(_parse_thunk(lines, child))
            continue
        raise ToolangError(
            f"Unsupported statement at line {child.start_point.row + 1}: {_node_text(child)!r}"
        )

    return program


def _parse_use(node: Node) -> UseDecl:
    return UseDecl(
        kind=_required_text(node, "kind"),
        reference=_required_text(node, "reference"),
        span=SourceSpan(node.start_point.row + 1),
    )


def _parse_decl(node: Node) -> DeclBlock:
    header = _required_child(node, "header")
    kind = _required_text(header, "kind")
    name = _required_text(header, "name")
    line_number = node.start_point.row + 1
    params = _parse_params(header.child_by_field_name("parameters"), line_number)

    if params and kind != "prompt":
        raise ToolangError(f"Only prompt declarations may declare parameters at line {line_number}.")
    if kind == "prompt" and any(param.name == "input" for param in params):
        raise ToolangError(
            f"Prompt parameters may not use reserved name 'input' at line {line_number}."
        )

    language_node = header.child_by_field_name("language")
    language = _node_text(language_node) if language_node is not None else None
    body_node = node.child_by_field_name("body")

    return DeclBlock(
        kind=kind,
        name=name,
        language=language,
        body=_parse_fence_body(body_node) if body_node is not None else "",
        header_suffix=f"```{language or ''}" if body_node is not None else "",
        params=params,
        span=SourceSpan(line_number),
    )


def _parse_thunk(lines: list[str], node: Node) -> Thunk:
    header = _required_child(node, "header")
    thunk = Thunk(
        name=_optional_text(header.child_by_field_name("name")),
        input_name=_parse_thunk_input(header.child_by_field_name("input")),
        output=_optional_text(header.child_by_field_name("output")),
        span=SourceSpan(node.start_point.row + 1),
    )
    prompt_started = False
    prompt_lines: list[str] = []

    for child in node.named_children:
        if child.type == "thunk_header":
            continue
        if child.type == "blank_line":
            if prompt_started:
                prompt_lines.append("")
            continue

        raw_line = _line_text(lines, child.start_point.row)
        if not raw_line.startswith((" ", "\t")):
            raise ToolangError(
                f"Thunk body must be indented under line {node.start_point.row + 1}: {raw_line!r}"
            )

        text = _parse_thunk_line_text(child)
        if child.type == "directive_line" and not prompt_started:
            thunk.directives.append(text)
            continue

        prompt_started = True
        prompt_lines.append(text)

    thunk.prompt = "\n".join(prompt_lines).strip()
    if not thunk.prompt:
        raise ToolangError(f"Thunk at line {node.start_point.row + 1} is missing prompt text.")
    return thunk


def _parse_params(node: Node | None, line_number: int) -> list[ParamDecl]:
    if node is None:
        return []

    params: list[ParamDecl] = []
    seen: set[str] = set()
    for parameter in node.children_by_field_name("parameter"):
        name = _required_text(parameter, "name")
        if name in seen:
            raise ToolangError(f"Duplicate parameter {name!r} at line {line_number}.")
        seen.add(name)
        params.append(
            ParamDecl(name=name, optional=parameter.child_by_field_name("optional") is not None)
        )
    return params


def _parse_fence_body(node: Node) -> str:
    lines: list[str] = []
    for child in node.named_children:
        text_node = child.child_by_field_name("text")
        lines.append(_node_text(text_node) if text_node is not None else "")
    return "\n".join(lines).rstrip()


def _parse_thunk_input(node: Node | None) -> str | None:
    if node is None:
        return None
    return _required_text(node, "value")


def _parse_thunk_line_text(node: Node) -> str:
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


def _parse_tree(source: bytes):
    parser = Parser(_toolang_language())
    return parser.parse(source)


@lru_cache(maxsize=1)
def _toolang_language() -> Language:
    try:
        import tree_sitter_toolang
    except ImportError as exc:
        raise ToolangError(
            "The 'tree-sitter-toolang' package is not installed. Install a local wheel "
            "or publishable package before running Toolang parsing commands."
        ) from exc
    return Language(tree_sitter_toolang.language())
