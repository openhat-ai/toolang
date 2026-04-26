"""Toolang program AST and parser."""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
import re
from typing import Any, Literal, cast

import frontmatter
from tree_sitter import Language, Node, Parser
import tree_sitter_toolang

from toolang.base.error import ToolangError


SERVICE_FIELDS = frozenset({"description", "transport", "target", "headers", "env"})
PROMPT_FIELDS = frozenset({"params"})
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SIGNATURE_PARAM_RE = re.compile(r"^[A-Za-z_][\w-]*\??$")
OverlayKind = Literal["model", "tool", "psyche", "skill", "service"]
OverlayOperator = Literal["set", "add", "remove"]
MessageBlockKind = Literal["system", "user", "assistant", "tool"]


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


@dataclass(frozen=True, slots=True)
class ThunkOverlay:
    kind: OverlayKind
    op: OverlayOperator
    items: tuple[str, ...]
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class MessageBlock:
    kind: MessageBlockKind
    text: str
    span: SourceSpan
    explicit: bool = True


@dataclass(slots=True)
class Thunk:
    name: str | None
    input: ParamDecl | None = None
    params: list[ParamDecl] = field(default_factory=list)
    output: str | None = None
    overlays: tuple[ThunkOverlay, ...] = ()
    messages: tuple[MessageBlock, ...] = ()
    span: SourceSpan = field(default_factory=lambda: SourceSpan(0))

    def thunk_name(self) -> str:
        return self.name or "main"

    def is_thread_thunk(self) -> bool:
        return self.thunk_name() in {"chat", "task", "chore"}

    def overlays_for(self, kind: OverlayKind) -> tuple[ThunkOverlay, ...]:
        return tuple(item for item in self.overlays if item.kind == kind)

    def message_blocks(self, kind: MessageBlockKind) -> tuple[MessageBlock, ...]:
        return tuple(item for item in self.messages if item.kind == kind)

    def messages_text(self) -> str:
        return "\n\n".join(
            block.text
            for block in self.messages
            if block.text.strip()
        ).strip()


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
    signature = _required_child(node, "signature")
    body = _required_child(node, "body")
    params_node = signature.child_by_field_name("params")
    implicit_input = params_node is None
    input_param, params = _params_from_node(params_node)
    overlays: list[ThunkOverlay] = []
    messages: list[MessageBlock] = []
    thunk = Thunk(
        name=_optional_text(signature.child_by_field_name("name")),
        input=input_param,
        params=params,
        output=_optional_text(signature.child_by_field_name("output")),
        span=SourceSpan(node.start_point.row + 1),
    )

    for child in body.named_children:
        if child.type == "overlay_line":
            overlays.append(_overlay_from_node(child))
            continue
        if child.type == "blank_line":
            continue
        if child.type == "message":
            messages.append(_message_from_node(child, thunk_name=thunk.thunk_name()))
            continue
        raise ToolangError(
            f"Unsupported thunk content at line {child.start_point.row + 1}: {child.type!r}"
        )

    thunk.overlays = tuple(overlays)
    thunk.messages = tuple(messages)
    if implicit_input and thunk.is_thread_thunk():
        thunk.input = None
    return thunk


def _params_from_node(node: Node | None) -> tuple[ParamDecl | None, list[ParamDecl]]:
    if node is None:
        return ParamDecl(name="_"), []
    input_node = node.child_by_field_name("input")
    input_param = (
        ParamDecl(
            name=_required_text(input_node, "name"),
            optional=False,
            type_name=None,
        )
        if input_node is not None
        else None
    )
    params: list[ParamDecl] = []
    for parameter in node.children_by_field_name("param"):
        name_node = _required_child(parameter, "name")
        param = ParamDecl(
            name=_node_text(name_node),
            optional=parameter.child_by_field_name("optional") is not None,
            type_name=_optional_text(parameter.child_by_field_name("type")),
        )
        params.append(param)
    return input_param, params


def _overlay_from_node(node: Node) -> ThunkOverlay:
    overlay = _required_child(node, "overlay")
    subject = _required_text(overlay, "subject").strip()
    operator = _required_text(overlay, "operator").strip()
    raw_values = _optional_text(overlay.child_by_field_name("values")) or ""
    kind = _overlay_kind(subject, line_number=node.start_point.row + 1)
    items = tuple(
        item
        for item in (part.strip() for part in raw_values.split(","))
        if item
    )
    return ThunkOverlay(
        kind=kind,
        op=_overlay_operator(operator, line_number=node.start_point.row + 1),
        items=items,
        span=SourceSpan(node.start_point.row + 1),
    )


def _overlay_kind(subject: str, *, line_number: int) -> OverlayKind:
    normalized = subject.strip()
    if normalized == "models":
        return "model"
    if normalized in {"tool", "tools"}:
        return "tool"
    if normalized in {"psyche", "psyches"}:
        return "psyche"
    if normalized in {"skill", "skills"}:
        return "skill"
    if normalized in {"service", "services"}:
        return "service"
    raise ToolangError(f"Unsupported thunk directive {subject!r} at line {line_number}.")


def _overlay_operator(operator: str, *, line_number: int) -> OverlayOperator:
    normalized = operator.strip()
    if normalized == "=":
        return "set"
    if normalized == "+=":
        return "add"
    if normalized == "-=":
        return "remove"
    raise ToolangError(f"Unsupported thunk directive operator {operator!r} at line {line_number}.")


def _message_from_node(node: Node, *, thunk_name: str) -> MessageBlock:
    kind_node = node.child_by_field_name("kind")
    if kind_node is None:
        lines: list[tuple[int, str]] = []
        for child in node.named_children:
            if child.type == "message_line":
                lines.append((child.start_point.row + 1, _required_text(child, "text").rstrip()))
                continue
            if child.type == "blank_line":
                lines.append((child.start_point.row + 1, ""))
        implicit_kind: MessageBlockKind = "system" if thunk_name in {"chat", "task", "chore"} else "user"
        return MessageBlock(
            kind=implicit_kind,
            text="\n".join(text for _, text in lines).strip(),
            span=SourceSpan(node.start_point.row + 1),
            explicit=False,
        )

    continuation: list[tuple[int, str]] = []
    for child in node.named_children:
        if child.type == "message_continuation_line":
            continuation.append((child.start_point.row + 1, _required_text(child, "text").rstrip()))
            continue
        if child.type == "blank_line":
            continuation.append((child.start_point.row + 1, ""))
    return MessageBlock(
        kind=cast(MessageBlockKind, _node_text(kind_node).strip()),
        text=_message_block_text(
            inline_text=_optional_text(node.child_by_field_name("inline")) or "",
            continuation=continuation,
        ),
        span=SourceSpan(node.start_point.row + 1),
    )


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
    _require_exact_fields(
        meta=meta,
        allowed=SERVICE_FIELDS,
        kind="service",
        line_number=line_number,
    )
    description = meta.get("description")
    if not isinstance(description, str) or not description:
        raise ToolangError(f"Service declaration at line {line_number} is missing description.")
    transport = meta.get("transport")
    if not isinstance(transport, str) or not transport:
        raise ToolangError(f"Service declaration at line {line_number} is missing transport.")
    if transport not in {"http", "stdio"}:
        raise ToolangError(
            f"Service declaration at line {line_number} uses unsupported transport {transport!r}."
        )
    target = meta.get("target")
    if not isinstance(target, str) or not target:
        raise ToolangError(f"Service declaration at line {line_number} is missing target.")
    headers = meta.get("headers")
    if headers is not None and not _is_string_map(headers):
        raise ToolangError(
            f"Service declaration at line {line_number} must define headers as a string map."
        )
    env = meta.get("env")
    if env is not None and not _is_env_names(env):
        raise ToolangError(
            f"Service declaration at line {line_number} must list environment variable names."
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


def _is_env_names(value: object) -> bool:
    if isinstance(value, str):
        items = [item.strip() for item in value.split(",")]
    elif isinstance(value, list | tuple):
        items = [item.strip() for item in value if isinstance(item, str)]
        if len(items) != len(value):
            return False
    else:
        return False
    return bool(items) and all(ENV_NAME_RE.fullmatch(item) is not None for item in items)


def _dedent_line_items(lines: list[tuple[int, str]]) -> list[tuple[int, str]]:
    non_blank = [text for _, text in lines if text.strip()]
    if not non_blank:
        return list(lines)
    indent = min(len(line) - len(line.lstrip(" \t")) for line in non_blank)
    return [
        (line_number, text[indent:].rstrip() if text.strip() else "")
        for line_number, text in lines
    ]


def _message_block_text(
    *,
    inline_text: str,
    continuation: list[tuple[int, str]],
) -> str:
    if continuation:
        normalized = [text for _, text in _dedent_line_items(continuation)]
        block_text = "\n".join(normalized).strip()
        if inline_text and block_text:
            return f"{inline_text}\n{block_text}".strip()
        if inline_text:
            return inline_text.strip()
        return block_text
    return inline_text.strip()


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
