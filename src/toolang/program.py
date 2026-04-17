"""Toolang program AST and parser."""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
import re
from typing import Any

import frontmatter
from tree_sitter import Language, Node, Parser
import tree_sitter_toolang

from toolang.base.error import ToolangError


HTTP_SERVICE_FIELDS = frozenset({"transport", "url", "headers"})
STDIO_SERVICE_FIELDS = frozenset({"transport", "command", "args", "env", "cwd"})
PROMPT_FIELDS = frozenset({"params"})
SIGNATURE_PARAM_RE = re.compile(r"^[A-Za-z_][\w-]*\??$")


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
    type_name: str | None = None
    message: bool = False


@dataclass(slots=True)
class DeclBlock:
    kind: str
    name: str
    body: str
    span: SourceSpan
    language: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    params: list[ParamDecl] = field(default_factory=list)


@dataclass(slots=True)
class StructFieldDecl:
    name: str
    type_name: str
    span: SourceSpan


@dataclass(slots=True)
class StructDecl:
    name: str
    fields: list[StructFieldDecl]
    span: SourceSpan


@dataclass(slots=True)
class Thunk:
    name: str | None
    params_omitted: bool
    params: list[ParamDecl] = field(default_factory=list)
    returns: str | None = None
    directives: list[str] = field(default_factory=list)
    body: str = ""
    span: SourceSpan = field(default_factory=lambda: SourceSpan(0))


@dataclass(slots=True)
class Program:
    uses: list[UseDecl] = field(default_factory=list)
    declarations: list[DeclBlock] = field(default_factory=list)
    structs: list[StructDecl] = field(default_factory=list)
    thunks: list[Thunk] = field(default_factory=list)
    _source_lines: list[str] | None = field(default=None, repr=False, compare=False)

    def get_decl(self, kind: str, name: str) -> DeclBlock | None:
        for item in self.declarations:
            if item.kind == kind and item.name == name:
                return item
        return None

    def get_struct(self, name: str) -> StructDecl | None:
        for item in self.structs:
            if item.name == name:
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
        if child.type == "fenced_declaration":
            program.declarations.append(_decl_from_node(child))
            continue
        if child.type == "struct_declaration":
            program.structs.append(_struct_from_node(child))
            continue
        if child.type == "thunk":
            program.thunks.append(_thunk_from_node(child))
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
    body_node = _required_child(node, "body")
    kind = _required_text(header, "kind")
    name = _required_text(header, "name")
    language = _required_text(header, "language")
    line_number = node.start_point.row + 1
    raw_body = _fence_body_from_node(body_node)
    frontmatter_present = body_node.child_by_field_name("frontmatter") is not None
    meta, body, params = _declaration_semantics(
        kind=kind,
        raw_body=raw_body,
        frontmatter_present=frontmatter_present,
        line_number=line_number,
    )
    return DeclBlock(
        kind=kind,
        name=name,
        language=language,
        meta=meta,
        body=body,
        params=params,
        span=SourceSpan(line_number),
    )


def _struct_from_node(node: Node) -> StructDecl:
    header = next(
        (child for child in node.named_children if child.type == "struct_header"),
        None,
    )
    body = _required_child(node, "body")
    if header is None:
        raise ToolangError(f"Missing struct header at line {node.start_point.row + 1}.")
    fields: list[StructFieldDecl] = []
    for line in body.named_children:
        field_node = line.child_by_field_name("field")
        if field_node is None:
            continue
        fields.append(
            StructFieldDecl(
                name=_required_text(field_node, "name"),
                type_name=_required_text(field_node, "type"),
                span=SourceSpan(field_node.start_point.row + 1),
            )
        )
    return StructDecl(
        name=_required_text(header, "name"),
        fields=fields,
        span=SourceSpan(node.start_point.row + 1),
    )


def _thunk_from_node(node: Node) -> Thunk:
    header = _required_child(node, "header")
    body = _required_child(node, "body")
    params_node = header.child_by_field_name("parameters")
    thunk = Thunk(
        name=_optional_text(header.child_by_field_name("name")),
        params_omitted=params_node is None,
        params=_params_from_node(params_node),
        returns=_optional_text(header.child_by_field_name("returns")),
        span=SourceSpan(node.start_point.row + 1),
    )

    body_started = False
    body_lines: list[str] = []
    for child in body.named_children:
        if child.type == "directive_line":
            if body_started:
                raise ToolangError(
                    f"Directive line must appear before thunk body text at line {child.start_point.row + 1}."
                )
            thunk.directives.append(_directive_from_node(child))
            continue
        if child.type == "blank_line":
            if body_started:
                body_lines.append("")
            continue
        if child.type == "body_line":
            body_started = True
            body_lines.append(_required_text(child, "text").rstrip())
            continue
        raise ToolangError(
            f"Unsupported thunk content at line {child.start_point.row + 1}: {child.type!r}"
        )

    thunk.body = _dedent_lines(body_lines).strip()
    return thunk


def _params_from_node(node: Node | None) -> list[ParamDecl]:
    if node is None:
        return []
    params: list[ParamDecl] = []
    for parameter in node.children_by_field_name("parameter"):
        name_node = _required_child(parameter, "name")
        params.append(
            ParamDecl(
                name="_" if name_node.type == "underscore" else _node_text(name_node),
                optional=parameter.child_by_field_name("optional") is not None,
                type_name=_optional_text(parameter.child_by_field_name("type")),
                message=name_node.type == "underscore",
            )
        )
    return params


def _directive_from_node(node: Node) -> str:
    directive = node.named_children[0]
    subject = _required_text(directive, "subject").strip()
    operator = _required_text(directive, "operator").strip()
    values = _required_text(directive, "values").strip()
    return f"{subject} {operator} {values}"


def _fence_body_from_node(node: Node) -> str:
    return _node_text(node).rstrip("\r\n")


def _declaration_semantics(
    *,
    kind: str,
    raw_body: str,
    frontmatter_present: bool,
    line_number: int,
) -> tuple[dict[str, Any], str, list[ParamDecl]]:
    if kind == "psyche":
        if frontmatter_present:
            raise ToolangError(f"Psyche {line_number} must not declare frontmatter.")
        return {}, raw_body.rstrip(), []
    if kind == "service":
        if not frontmatter_present:
            raise ToolangError(f"Service declaration at line {line_number} is missing frontmatter.")
        return _service_declaration(raw_body=raw_body, line_number=line_number)
    if kind == "prompt":
        return _prompt_declaration(
            raw_body=raw_body,
            frontmatter_present=frontmatter_present,
            line_number=line_number,
        )
    raise ToolangError(f"Unsupported declaration kind {kind!r} at line {line_number}.")


def _service_declaration(*, raw_body: str, line_number: int) -> tuple[dict[str, Any], str, list[ParamDecl]]:
    post = frontmatter.loads(raw_body)
    meta = dict(post.metadata)
    transport = meta.get("transport")
    if not isinstance(transport, str) or not transport:
        raise ToolangError(f"Service declaration at line {line_number} is missing transport.")

    if transport == "http":
        _require_exact_fields(
            meta=meta,
            allowed=HTTP_SERVICE_FIELDS,
            kind="service",
            line_number=line_number,
        )
        url = meta.get("url")
        headers = meta.get("headers")
        if not isinstance(url, str) or not url:
            raise ToolangError(f"HTTP service declaration at line {line_number} is missing url.")
        if headers is not None and not _is_string_map(headers):
            raise ToolangError(
                f"HTTP service declaration at line {line_number} must define headers as a string map."
            )
    elif transport == "stdio":
        _require_exact_fields(
            meta=meta,
            allowed=STDIO_SERVICE_FIELDS,
            kind="service",
            line_number=line_number,
        )
        command = meta.get("command")
        args = meta.get("args")
        env = meta.get("env")
        cwd = meta.get("cwd")
        if not isinstance(command, str) or not command:
            raise ToolangError(
                f"Stdio service declaration at line {line_number} is missing command."
            )
        if args is not None and not _is_string_list(args):
            raise ToolangError(
                f"Stdio service declaration at line {line_number} must define args as a string list."
            )
        if env is not None and not isinstance(env, str):
            raise ToolangError(
                f"Stdio service declaration at line {line_number} must define env as a string."
            )
        if cwd is not None and not isinstance(cwd, str):
            raise ToolangError(
                f"Stdio service declaration at line {line_number} must define cwd as a string."
            )
    else:
        raise ToolangError(
            f"Service declaration at line {line_number} uses unsupported transport {transport!r}."
        )
    return meta, post.content.rstrip(), []


def _prompt_declaration(
    *,
    raw_body: str,
    frontmatter_present: bool,
    line_number: int,
) -> tuple[dict[str, Any], str, list[ParamDecl]]:
    if not frontmatter_present:
        return {}, raw_body.rstrip(), []

    post = frontmatter.loads(raw_body)
    meta = dict(post.metadata)
    _require_exact_fields(
        meta=meta,
        allowed=PROMPT_FIELDS,
        kind="prompt",
        line_number=line_number,
    )
    params_value = meta.get("params")
    if params_value is not None and not isinstance(params_value, str):
        raise ToolangError(
            f"Prompt declaration at line {line_number} must define params as a string."
        )
    params = _parse_signature_params(params_value or "", line_number=line_number)
    return meta, post.content.rstrip(), params


def _parse_signature_params(raw: str, *, line_number: int) -> list[ParamDecl]:
    if not raw.strip():
        return []

    seen: set[str] = set()
    params: list[ParamDecl] = []
    for item in [part.strip() for part in raw.split(",")]:
        if not item:
            raise ToolangError(
                f"Parameter signature at line {line_number} contains an empty parameter."
            )
        if not SIGNATURE_PARAM_RE.fullmatch(item):
            raise ToolangError(
                f"Parameter signature at line {line_number} contains invalid parameter {item!r}."
            )
        optional = item.endswith("?")
        name = item[:-1] if optional else item
        if name in seen:
            raise ToolangError(
                f"Parameter signature at line {line_number} repeats parameter {name!r}."
            )
        seen.add(name)
        params.append(ParamDecl(name=name, optional=optional))
    return params


def _require_exact_fields(
    *,
    meta: dict[str, Any],
    allowed: frozenset[str],
    kind: str,
    line_number: int,
) -> None:
    unknown = sorted(set(meta) - set(allowed))
    if unknown:
        joined = ", ".join(repr(item) for item in unknown)
        raise ToolangError(
            f"{kind.capitalize()} declaration at line {line_number} has unsupported frontmatter fields: {joined}."
        )


def _is_string_map(value: object) -> bool:
    return isinstance(value, dict) and all(
        isinstance(key, str) and isinstance(item, str) for key, item in value.items()
    )


def _is_string_list(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _dedent_lines(lines: list[str]) -> str:
    non_blank = [line for line in lines if line.strip()]
    if not non_blank:
        return "\n".join(lines)
    indent = min(len(line) - len(line.lstrip(" \t")) for line in non_blank)
    normalized = [
        line[indent:].rstrip() if line.strip() else ""
        for line in lines
    ]
    return "\n".join(normalized)


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
        raise ToolangError(
            f"Missing syntax field {field_name!r} at line {node.start_point.row + 1}."
        )
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
