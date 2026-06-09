"""Toolang program AST and parser."""

from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass
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
THUNK_HEADER_RE = re.compile(r"^(?P<indent>[ \t]*)thunk(?P<rest>.*):(?P<suffix>[ \t]*(?:#.*)?)$")
STRUCT_HEADER_RE = re.compile(r"^(?P<indent>[ \t]*)struct(?P<rest>.*):(?P<suffix>[ \t]*(?:#.*)?)$")
FIELD_RE = re.compile(
    r"^(?P<indent>[ \t]+)(?P<name>[a-z][a-z0-9_-]*)(?P<optional>\?)?:"
    r"(?P<space>[ \t]*)(?P<type>[A-Za-z][A-Za-z0-9]*(?:\[\])?)(?P<suffix>[ \t]*(?:#.*)?)$"
)
DIRECTIVE_RE = re.compile(
    r"^(?P<indent>[ \t]*)(?P<key>model|models|tool|tools|skill|skills|service|services|"
    r"psyche|psyches|hands|handoffs|recall)(?P<space>[ \t]*)(?P<op>=|\+=|-=)"
)
LEGACY_DELEGATES_RE = re.compile(r"^[ \t]*delegates[ \t]*(?:=|\+=|-=)")
TOP_LEVEL_RE = re.compile(
    r"^(use|struct|psyche|skill|service|prompt|context|instruct|thunk|flow)\b"
)
OverlayKind = Literal["model", "tool", "psyche", "skill", "service", "hand", "handoff", "recall"]
OverlayOperator = Literal["set", "add", "remove"]
MessageBlockKind = Literal["context", "instruct", "user", "assistant", "tool"]
FlowStageKind = Literal[
    "bare",
    "do",
    "ask",
    "unfold",
    "keep",
    "drop",
    "rank",
    "each",
    "fold",
    "repeat",
]
TREE_SITTER_TYPE_ALIASES = {
    "string": "Text",
    "text": "Text",
    "number": "Number",
    "boolean": "Boolean",
    "json": "Json",
    "message": "Message",
    "path": "Path",
    "artifact": "Artifact",
}
AST_TYPE_ALIASES = {
    "Text": "string",
    "Number": "number",
    "Boolean": "boolean",
    "Json": "json",
    "Message": "message",
    "Path": "path",
    "Artifact": "artifact",
}


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
class InstructBlock:
    name: str | None
    body: str
    span: SourceSpan
    language: str | None = None


@dataclass(slots=True)
class ContextBlock:
    name: str | None
    body: str
    span: SourceSpan
    language: str | None = None


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
    context: MessageBlock | None = None
    instruct: MessageBlock | None = None
    messages: tuple[MessageBlock, ...] = ()
    span: SourceSpan = field(default_factory=lambda: SourceSpan(0))

    def thunk_name(self) -> str:
        return self.name or "main"

    def is_thread_thunk(self) -> bool:
        return self.thunk_name() in {"chat", "task", "chore"}

    def overlays_for(self, kind: OverlayKind) -> tuple[ThunkOverlay, ...]:
        return tuple(item for item in self.overlays if item.kind == kind)

    def message_blocks(self, kind: MessageBlockKind) -> tuple[MessageBlock, ...]:
        blocks: list[MessageBlock] = []
        if self.context is not None and kind == "context":
            blocks.append(self.context)
        if self.instruct is not None and kind == "instruct":
            blocks.append(self.instruct)
        blocks.extend(item for item in self.messages if item.kind == kind)
        return tuple(blocks)

    @property
    def directives(self) -> tuple[ThunkOverlay, ...]:
        return self.overlays

    def directives_for(self, kind: OverlayKind) -> tuple[ThunkOverlay, ...]:
        return self.overlays_for(kind)

    def messages_text(self) -> str:
        return "\n\n".join(
            block.text
            for block in self.messages
            if block.text.strip()
        ).strip()


@dataclass(frozen=True, slots=True)
class FlowStage:
    kind: FlowStageKind
    span: SourceSpan
    target: str | None = None
    targets: tuple[str, ...] = ()
    body: str | None = None
    doc: str | None = None
    output: str | None = None
    parallelism: int | None = None
    limit: int | None = None
    count: int | None = None
    condition: str | None = None
    stages: tuple["FlowStage", ...] = ()


@dataclass(slots=True)
class Flow:
    name: str | None
    input: ParamDecl | None = None
    params: list[ParamDecl] = field(default_factory=list)
    output: str | None = None
    overlays: tuple[ThunkOverlay, ...] = ()
    stages: tuple[FlowStage, ...] = ()
    span: SourceSpan = field(default_factory=lambda: SourceSpan(0))

    def flow_name(self) -> str:
        return self.name or "main"

    def overlays_for(self, kind: OverlayKind) -> tuple[ThunkOverlay, ...]:
        return tuple(item for item in self.overlays if item.kind == kind)


@dataclass(slots=True)
class Program:
    uses: list[UseDecl] = field(default_factory=list)
    contexts: list[ContextBlock] = field(default_factory=list)
    instructs: list[InstructBlock] = field(default_factory=list)
    declarations: list[DeclBlock] = field(default_factory=list)
    structs: list[StructDecl] = field(default_factory=list)
    thunks: list[Thunk] = field(default_factory=list)
    flows: list[Flow] = field(default_factory=list)
    _source_lines: list[str] | None = field(default=None, repr=False, compare=False)

    def get_decl(self, kind: str, name: str) -> DeclBlock | None:
        for item in self.declarations:
            if item.kind == kind and item.name == name:
                return item
        return None

    def get_instruct(self, name: str | None) -> InstructBlock | None:
        for item in self.instructs:
            if item.name == name:
                return item
        return None

    def get_context(self, name: str | None) -> ContextBlock | None:
        for item in self.contexts:
            if item.name == name:
                return item
        return None

    def get_struct(self, name: str) -> StructDecl | None:
        for item in self.structs:
            if item.name == name:
                return item
        return None

    def get_flow(self, name: str) -> Flow | None:
        for item in self.flows:
            if item.flow_name() == name:
                return item
        return None


@dataclass(frozen=True, slots=True)
class _TreeSitterSource:
    source: str
    line_map: tuple[int | None, ...]
    synthetic_message_rows: frozenset[int]
    original_lines: tuple[str, ...]

    def original_line_index(self, row: int) -> int:
        if 0 <= row < len(self.line_map):
            original = self.line_map[row]
            if original is not None:
                return original
        return row

    def original_line_number(self, row: int) -> int:
        return self.original_line_index(row) + 1

    def original_line(self, row: int) -> str:
        original = self.original_line_index(row)
        if 0 <= original < len(self.original_lines):
            return self.original_lines[original]
        return ""


def parse(source: str) -> Program:
    """Parse one Toolang program source string."""

    normalized_source = _source_without_shebang(source)
    syntax_source = _tree_sitter_source(normalized_source)
    tree = Parser(_toolang_language()).parse(syntax_source.source.encode("utf-8"))
    lines = normalized_source.splitlines()
    program = Program(_source_lines=lines)

    error_node = _first_error_node(tree.root_node)
    if error_node is not None:
        _raise_syntax_error(lines, syntax_source, error_node)

    for child in tree.root_node.named_children:
        node = _item_node(child)
        if node.type in {"blank_line", "comment", "comment_line"}:
            continue
        if node.type in {"use", "use_statement"}:
            program.uses.append(_use_from_node(node, syntax_source))
            continue
        if node.type in {"fenced_declaration", "psyche", "skill", "service", "prompt"}:
            program.declarations.append(_decl_from_node(node, syntax_source))
            continue
        if node.type == "instruct":
            program.instructs.append(_instruct_from_node(node, syntax_source))
            continue
        if node.type == "context":
            program.contexts.append(_context_from_node(node, syntax_source))
            continue
        if node.type in {"struct", "struct_declaration"}:
            program.structs.append(_struct_from_node(node, lines, syntax_source))
            continue
        if node.type == "thunk":
            program.thunks.append(_thunk_from_node(node, lines, syntax_source))
            continue
        if node.type == "flow":
            program.flows.append(_flow_from_node(node, syntax_source))
            continue
        raise ToolangError(
            f"Unsupported statement at line {syntax_source.original_line_number(node.start_point.row)}: "
            f"{_node_text(node)!r}"
        )

    return program


def program_to_ast_data(program: Program, *, include_source_lines: bool = False) -> dict[str, object]:
    """Return one JSON-compatible representation of a parsed program AST."""

    return cast(
        dict[str, object],
        _ast_value_to_data(program, include_source_lines=include_source_lines),
    )


def _ast_value_to_data(value: object, *, include_source_lines: bool) -> object:
    if is_dataclass(value) and not isinstance(value, type):
        data: dict[str, object] = {}
        for item in fields(value):
            if item.name == "_source_lines" and not include_source_lines:
                continue
            data[item.name] = _ast_value_to_data(
                getattr(value, item.name),
                include_source_lines=include_source_lines,
            )
        return data
    if isinstance(value, list | tuple):
        return [_ast_value_to_data(item, include_source_lines=include_source_lines) for item in value]
    if isinstance(value, dict):
        return {
            str(key): _ast_value_to_data(item, include_source_lines=include_source_lines)
            for key, item in value.items()
        }
    return value


def _use_from_node(node: Node, syntax_source: _TreeSitterSource) -> UseDecl:
    return UseDecl(
        kind=_required_text(node, "kind").strip(),
        reference=_required_text(node, "reference").strip(),
        span=SourceSpan(syntax_source.original_line_number(node.start_point.row)),
    )


def _decl_from_node(node: Node, syntax_source: _TreeSitterSource) -> DeclBlock:
    header = node.child_by_field_name("header")
    body_node = _required_child(node, "body")
    kind = _required_text(header, "kind") if header is not None else _required_text(node, "kind")
    name = _required_text(header, "name") if header is not None else _required_text(node, "name")
    language = (
        _required_text(header, "language")
        if header is not None and header.child_by_field_name("language") is not None
        else _cap_body_language(body_node)
    )
    line_number = syntax_source.original_line_number(node.start_point.row)
    raw_body = _cap_body_text(body_node)
    frontmatter_present = _descendant_of_type(body_node, "frontmatter") is not None
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


def _instruct_from_node(node: Node, syntax_source: _TreeSitterSource) -> InstructBlock:
    body_node = _required_child(node, "body")
    return InstructBlock(
        name=_optional_text(node.child_by_field_name("name")),
        body=_block_value_text(body_node),
        language=_cap_body_language(body_node),
        span=SourceSpan(syntax_source.original_line_number(node.start_point.row)),
    )


def _context_from_node(node: Node, syntax_source: _TreeSitterSource) -> ContextBlock:
    body_node = _required_child(node, "body")
    return ContextBlock(
        name=_optional_text(node.child_by_field_name("name")),
        body=_block_value_text(body_node),
        language=_cap_body_language(body_node),
        span=SourceSpan(syntax_source.original_line_number(node.start_point.row)),
    )


def _struct_from_node(
    node: Node,
    lines: list[str],
    syntax_source: _TreeSitterSource,
) -> StructDecl:
    header = next(
        (child for child in node.named_children if child.type == "struct_header"),
        None,
    )
    body = _required_child(node, "body")
    if header is None and node.type == "struct_declaration":
        raise ToolangError(f"Missing struct header at line {node.start_point.row + 1}.")
    fields: list[StructFieldDecl] = []
    for child in body.named_children:
        field_node = child.child_by_field_name("field") or (child if child.type == "field" else None)
        if field_node is None:
            continue
        original_row = syntax_source.original_line_index(field_node.start_point.row)
        parsed_field = _field_from_source_line(_line_text(lines, original_row))
        fields.append(
            StructFieldDecl(
                name=parsed_field[0] if parsed_field is not None else _required_text(field_node, "name"),
                type_name=(
                    parsed_field[1]
                    if parsed_field is not None
                    else _ast_type_name(_required_text(field_node, "type")) or ""
                ),
                span=SourceSpan(original_row + 1),
            )
        )
    return StructDecl(
        name=_required_text(header, "name") if header is not None else _required_text(node, "name"),
        fields=fields,
        span=SourceSpan(syntax_source.original_line_number(node.start_point.row)),
    )


def _thunk_from_node(
    node: Node,
    lines: list[str],
    syntax_source: _TreeSitterSource,
) -> Thunk:
    signature = node.child_by_field_name("signature")
    body = _required_child(node, "body")
    original_row = syntax_source.original_line_index(node.start_point.row)
    header = _parse_thunk_header(_line_text(lines, original_row), line_number=original_row + 1)
    if header is None and signature is None:
        raise ToolangError(f"Missing thunk signature at line {original_row + 1}.")
    params_node = signature.child_by_field_name("params") if signature is not None else node.child_by_field_name("params")
    implicit_input = (header[1] is None) if header is not None else params_node is None
    input_param, params = (
        _params_from_signature(header[1], line_number=original_row + 1)
        if header is not None
        else _params_from_node(params_node)
    )
    overlays: list[ThunkOverlay] = []
    context_block: MessageBlock | None = None
    instruct_block: MessageBlock | None = None
    messages: list[MessageBlock] = []
    if header is None:
        signature_node = signature if signature is not None else _required_child(node, "signature")
        thunk_name = _optional_text(signature_node.child_by_field_name("name"))
        output = _ast_type_name(_optional_text(signature_node.child_by_field_name("output")))
    else:
        thunk_name = header[0]
        output = header[2]
    thunk = Thunk(
        name=thunk_name,
        input=input_param,
        params=params,
        output=output,
        span=SourceSpan(original_row + 1),
    )

    for child in _thunk_content_nodes(body):
        if child.type in {"overlay_line", "directive"}:
            overlays.append(_overlay_from_node(child, syntax_source))
            continue
        if child.type in {"blank_line", "comment_line"}:
            continue
        if child.type in {"message", "block"}:
            block = _message_from_node(
                child,
                thunk_name=thunk.thunk_name(),
                syntax_source=syntax_source,
            )
            if block.kind == "context":
                context_block = block
            elif block.kind == "instruct":
                instruct_block = block
            else:
                messages.append(block)
            continue
        raise ToolangError(
            f"Unsupported thunk content at line {syntax_source.original_line_number(child.start_point.row)}: "
            f"{child.type!r}"
        )

    thunk.overlays = tuple(overlays)
    thunk.context = context_block
    thunk.instruct = instruct_block
    thunk.messages = tuple(messages)
    if implicit_input and thunk.is_thread_thunk():
        thunk.input = None
    return thunk


def _thunk_content_nodes(node: Node) -> list[Node]:
    if node.type in {"overlay_line", "directive", "blank_line", "comment_line", "message", "block"}:
        return [node]
    if node.type in {"thunk_body", "thunk_tail", "message_section", "instruction_section", "roled_message", "unroled_message"}:
        items: list[Node] = []
        for child in node.named_children:
            items.extend(_thunk_content_nodes(child))
        return items
    return [node]


def _flow_from_node(node: Node, syntax_source: _TreeSitterSource) -> Flow:
    params_node = node.child_by_field_name("params")
    input_param, params = _flow_params_from_node(params_node)
    body = _required_child(node, "body")
    overlays: list[ThunkOverlay] = []
    stages: list[FlowStage] = []
    for child in body.named_children:
        if child.type in {"overlay_line", "directive"}:
            overlays.append(_overlay_from_node(child, syntax_source))
            continue
        if child.type in {"blank_line", "comment_line", "doc_comment"}:
            continue
        if child.type == "flow_body_tail":
            stages.extend(_flow_stages_from_body_tail(child, syntax_source))
            continue
        raise ToolangError(
            f"Unsupported flow content at line {syntax_source.original_line_number(child.start_point.row)}: "
            f"{child.type!r}"
        )
    return Flow(
        name=_optional_text(node.child_by_field_name("name")),
        input=input_param,
        params=params,
        output=_ast_type_name(_optional_text(node.child_by_field_name("output"))),
        overlays=tuple(overlays),
        stages=tuple(stages),
        span=SourceSpan(syntax_source.original_line_number(node.start_point.row)),
    )


def _flow_params_from_node(node: Node | None) -> tuple[ParamDecl | None, list[ParamDecl]]:
    if node is None:
        return ParamDecl(name="in"), []
    params: list[ParamDecl] = []
    input_param: ParamDecl | None = None
    for parameter in node.children_by_field_name("param"):
        name_node = _required_child(parameter, "name")
        type_name = _ast_type_name(_optional_text(parameter.child_by_field_name("type")))
        param = ParamDecl(
            name=_node_text(name_node),
            optional=parameter.child_by_field_name("optional") is not None,
            type_name=type_name,
        )
        if param.name in {"in", "_"} and input_param is None and not params:
            input_param = param
        else:
            params.append(param)
    return input_param, params


def _flow_stages_from_body_tail(
    node: Node,
    syntax_source: _TreeSitterSource,
) -> list[FlowStage]:
    stages: list[FlowStage] = []
    pending_doc: list[str] = []
    for child in node.named_children:
        if child.type == "doc_comment":
            pending_doc.extend(_doc_comment_lines(_node_text(child)))
            continue
        if child.type in {"blank_line", "comment_line", "pass_statement"}:
            pending_doc.clear()
            continue
        if child.type == "flow_body_statement":
            stages.append(_flow_stage_from_statement(child, syntax_source, doc="\n".join(pending_doc).strip() or None))
            pending_doc.clear()
            continue
        raise ToolangError(
            f"Unsupported flow statement at line {syntax_source.original_line_number(child.start_point.row)}: "
            f"{child.type!r}"
        )
    return stages


def _flow_stage_from_statement(
    node: Node,
    syntax_source: _TreeSitterSource,
    *,
    doc: str | None = None,
) -> FlowStage:
    step = _descendant_of_type(node, "step")
    if step is None:
        raise ToolangError(f"Missing flow step at line {syntax_source.original_line_number(node.start_point.row)}.")
    kind = _flow_stage_kind(step)
    body_node = step.child_by_field_name("body")
    head_node = step.child_by_field_name("head")
    count_node = step.child_by_field_name("count")
    condition_node = step.child_by_field_name("condition")
    repeat_body = step.child_by_field_name("body") if kind == "repeat" else None
    targets = _flow_targets(step)
    return FlowStage(
        kind=kind,
        target=targets[0] if targets else None,
        targets=targets,
        body=_flow_stage_body_text(body_node),
        doc=doc,
        output=_flow_output_type(head_node),
        parallelism=_flow_parallelism(head_node),
        limit=_flow_rank_limit(head_node) if kind == "rank" else None,
        count=_int_node_text(count_node),
        condition=_flow_condition_text(condition_node),
        stages=(
            tuple(_flow_repeat_stages(repeat_body, syntax_source))
            if kind == "repeat" and repeat_body is not None
            else ()
        ),
        span=SourceSpan(syntax_source.original_line_number(step.start_point.row)),
    )


def _flow_repeat_stages(
    node: Node,
    syntax_source: _TreeSitterSource,
) -> list[FlowStage]:
    if node.type != "flow_repeat_block_body":
        return []
    stages: list[FlowStage] = []
    pending_doc: list[str] = []
    for child in node.named_children:
        if child.type == "doc_comment":
            pending_doc.extend(_doc_comment_lines(_node_text(child)))
            continue
        if child.type in {"blank_line", "comment_line", "pass_statement"}:
            pending_doc.clear()
            continue
        if child.type == "flow_body_statement":
            stages.append(_flow_stage_from_statement(child, syntax_source, doc="\n".join(pending_doc).strip() or None))
            pending_doc.clear()
    return stages


def _doc_comment_lines(text: str) -> list[str]:
    lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("##"):
            continue
        lines.append(stripped.lstrip("#").strip())
    return lines


def _flow_stage_kind(step: Node) -> FlowStageKind:
    keyword_types: dict[str, FlowStageKind] = {
        "flow_do_keyword": "do",
        "flow_ask_keyword": "ask",
        "flow_unfold_keyword": "unfold",
        "flow_keep_keyword": "keep",
        "flow_drop_keyword": "drop",
        "flow_rank_keyword": "rank",
        "flow_each_keyword": "each",
        "flow_fold_keyword": "fold",
        "flow_repeat_keyword": "repeat",
    }
    for child in step.named_children:
        if child.type in keyword_types:
            return keyword_types[child.type]
    return "bare"


def _flow_targets(node: Node) -> tuple[str, ...]:
    targets: list[str] = []
    stack = list(node.named_children)
    while stack:
        current = stack.pop(0)
        if current.type == "flow_target":
            targets.append(_node_text(current).strip())
            continue
        stack[0:0] = list(current.named_children)
    return tuple(target for target in targets if target)


def _flow_stage_body_text(node: Node | None) -> str | None:
    if node is None:
        return None
    if node.type == "flow_bare_thunk_body":
        lines: list[tuple[int, str]] = []
        for child in node.named_children:
            if child.type == "flow_bare_content_line":
                content = _required_child(child, "content")
                lines.append((child.start_point.row + 1, _node_text(content).rstrip()))
            elif child.type == "blank_line":
                lines.append((child.start_point.row + 1, ""))
        return "\n".join(text for _, text in _dedent_line_items(lines)).strip()
    if node.type == "flow_inline_step_body":
        value = node.child_by_field_name("value")
        return _flow_stage_body_text(value)
    if node.type in {"flow_inline_body", "flow_inline_text"}:
        return _node_text(node).strip()
    if node.type == "block_indented_implicit":
        return _block_value_text(node)
    if node.type == "flow_repeat_block_body":
        return None
    return _node_text(node).strip()


def _flow_output_type(node: Node | None) -> str | None:
    if node is None:
        return None
    output_node = _descendant_of_type(node, "flow_inline_output_type")
    if output_node is not None:
        return _ast_type_name(_required_text(output_node, "type"))
    if node.type == "flow_inline_output_type":
        return _ast_type_name(_required_text(node, "type"))
    return None


def _flow_parallelism(node: Node | None) -> int | None:
    if node is None:
        return None
    parallel = _descendant_of_type(node, "flow_parallelism")
    if parallel is None and node.type == "flow_parallelism":
        parallel = node
    if parallel is None:
        return None
    return _int_node_text(parallel.child_by_field_name("count"))


def _flow_rank_limit(node: Node | None) -> int | None:
    if node is None:
        return None
    rank = _descendant_of_type(node, "flow_rank_limit")
    if rank is None and node.type == "flow_rank_limit":
        rank = node
    if rank is None:
        return None
    return _int_node_text(rank.child_by_field_name("count"))


def _flow_condition_text(node: Node | None) -> str | None:
    if node is None:
        return None
    text = node.child_by_field_name("text")
    if text is None:
        return _node_text(node).strip() or None
    if text.type == "block_indented_implicit":
        return _block_value_text(text)
    return _node_text(text).strip() or None


def _int_node_text(node: Node | None) -> int | None:
    if node is None:
        return None
    text = _node_text(node).strip()
    return int(text) if text else None


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
        type_name = _ast_type_name(_optional_text(parameter.child_by_field_name("type")))
        param = ParamDecl(
            name=_node_text(name_node),
            optional=parameter.child_by_field_name("optional") is not None,
            type_name=type_name,
        )
        if param.name == "input" and type_name == "message" and input_param is None and not params:
            input_param = ParamDecl(name="_", optional=False, type_name=None)
        else:
            params.append(param)
    return input_param, params


def _overlay_from_node(node: Node, syntax_source: _TreeSitterSource) -> ThunkOverlay:
    overlay = node.child_by_field_name("overlay") or node
    subject = (
        _required_text(overlay, "subject").strip()
        if overlay.child_by_field_name("subject") is not None
        else _required_text(overlay, "key").strip()
    )
    operator = (
        _required_text(overlay, "operator").strip()
        if overlay.child_by_field_name("operator") is not None
        else _required_text(overlay, "operator").strip()
    )
    line_number = syntax_source.original_line_number(node.start_point.row)
    raw_values = (
        _raw_overlay_values_from_line(syntax_source.original_line(node.start_point.row))
        if node.type in {"overlay_line", "directive"}
        else None
    )
    if raw_values is None:
        raw_values = _optional_text(overlay.child_by_field_name("values")) or ""
    kind = _overlay_kind(subject, line_number=line_number)
    items = tuple(
        item
        for item in (part.strip() for part in raw_values.split(","))
        if item
    )
    return ThunkOverlay(
        kind=kind,
        op=_overlay_operator(operator, line_number=line_number),
        items=items,
        span=SourceSpan(line_number),
    )


def _raw_overlay_values_from_line(line: str) -> str | None:
    match = DIRECTIVE_RE.match(line)
    if match is None:
        return None
    values, _comment = _split_inline_comment(line[match.end() :])
    return values


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
    if normalized == "hands":
        return "hand"
    if normalized == "handoffs":
        return "handoff"
    if normalized == "recall":
        return "recall"
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


def _message_from_node(
    node: Node,
    *,
    thunk_name: str,
    syntax_source: _TreeSitterSource,
) -> MessageBlock:
    if node.type == "block":
        return _block_message_from_node(node, syntax_source=syntax_source)

    kind_node = node.child_by_field_name("kind")
    if kind_node is None:
        lines: list[tuple[int, str]] = []
        for child in node.named_children:
            if child.type == "message_line":
                lines.append((child.start_point.row + 1, _required_text(child, "text").rstrip()))
                continue
            if child.type == "blank_line":
                lines.append((child.start_point.row + 1, ""))
        implicit_kind: MessageBlockKind = _implicit_message_kind(thunk_name)
        return MessageBlock(
            kind=implicit_kind,
            text="\n".join(text for _, text in lines).strip(),
            span=SourceSpan(syntax_source.original_line_number(node.start_point.row)),
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
        span=SourceSpan(syntax_source.original_line_number(node.start_point.row)),
    )


def _block_message_from_node(node: Node, *, syntax_source: _TreeSitterSource) -> MessageBlock:
    kind_node = _required_child(node, "kind")
    value_node = _required_child(node, "value")
    kind = cast(MessageBlockKind, _node_text(kind_node).strip())
    line_number = syntax_source.original_line_number(node.start_point.row)
    explicit = node.start_point.row not in syntax_source.synthetic_message_rows
    return MessageBlock(
        kind=kind,
        text=_block_value_text(value_node),
        span=SourceSpan(line_number),
        explicit=explicit,
    )


def _block_value_text(node: Node) -> str:
    if node.type in {"block_value", "context_body", "instruct_body"} and node.named_child_count:
        return _block_value_text(node.named_children[0])
    if node.type == "block_inline":
        content = node.child_by_field_name("content") or node.child_by_field_name("name")
        return _node_text(content).strip()
    if node.type in {"block_indented", "block_indented_implicit"}:
        lines: list[tuple[int, str]] = []
        for child in node.named_children:
            if child.type == "block_indented_content_line":
                lines.append((child.start_point.row + 1, _required_text(child, "content").rstrip()))
            elif child.type == "blank_line":
                lines.append((child.start_point.row + 1, ""))
        return "\n".join(text for _, text in _dedent_line_items(lines)).strip()
    if node.type == "block_fenced":
        return _fenced_node_inner_text(node)
    return _node_text(node).strip()


def _cap_body_text(node: Node) -> str:
    if node.type == "cap_body" and node.named_child_count:
        return _cap_body_text(node.named_children[0])
    if node.type == "cap_markdown":
        return _fenced_node_inner_text(node)
    if node.type == "cap_indented":
        lines: list[tuple[int, str]] = []
        for child in node.named_children:
            if child.type == "cap_indented_content_line":
                lines.append((child.start_point.row + 1, _required_text(child, "content").rstrip()))
            elif child.type == "property_eq":
                lines.append((child.start_point.row + 1, _node_text(child).rstrip()))
            elif child.type == "blank_line":
                lines.append((child.start_point.row + 1, ""))
        return "\n".join(text for _, text in _dedent_line_items(lines)).strip()
    return _node_text(node).rstrip("\r\n")


def _cap_body_language(node: Node) -> str | None:
    language = _descendant_field(node, "language")
    return _node_text(language) or None


def _fenced_node_inner_text(node: Node) -> str:
    text = _node_text(node).rstrip("\r\n")
    first_line_end = text.find("\n")
    if first_line_end < 0:
        return ""
    body = text[first_line_end + 1 :]
    close_index = body.rfind("\n```")
    if close_index >= 0:
        body = body[:close_index]
    elif body.startswith("```"):
        body = ""
    return body.rstrip()


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


def _tree_sitter_source(source: str) -> _TreeSitterSource:
    original_lines = source.splitlines()
    transformed: list[str] = []
    line_map: list[int | None] = []
    synthetic_message_rows: set[int] = set()
    index = 0

    while index < len(original_lines):
        line = original_lines[index]
        thunk_match = THUNK_HEADER_RE.match(line)
        if thunk_match is None:
            transformed.append(_transform_non_thunk_line(line))
            line_map.append(index)
            index += 1
            continue

        transformed.append(_transform_thunk_header(line))
        line_map.append(index)
        thunk_name = _thunk_name_from_header(line)
        index += 1

        while index < len(original_lines):
            body_line = original_lines[index]
            if TOP_LEVEL_RE.match(body_line):
                break
            explicit_block_match = re.match(
                r"^(?P<indent>[ \t]*)(context|instruct|user|assistant|tool):",
                body_line,
            )
            if explicit_block_match is not None:
                block_indent = len(explicit_block_match.group("indent"))
                transformed.append(body_line)
                line_map.append(index)
                index += 1
                while index < len(original_lines):
                    continuation = original_lines[index]
                    if TOP_LEVEL_RE.match(continuation):
                        break
                    if continuation.strip() and len(_leading_whitespace(continuation)) <= block_indent:
                        break
                    transformed.append(continuation)
                    line_map.append(index)
                    index += 1
                continue
            if _is_implicit_message_line(body_line):
                synthetic_indent = _leading_whitespace(body_line)
                synthetic_row = len(transformed)
                transformed.append(f"{synthetic_indent}{_implicit_message_kind(thunk_name)}:")
                line_map.append(None)
                synthetic_message_rows.add(synthetic_row)
                while index < len(original_lines):
                    message_line = original_lines[index]
                    if TOP_LEVEL_RE.match(message_line) or not _is_implicit_message_line(message_line):
                        break
                    transformed.append(_indent_message_line(message_line))
                    line_map.append(index)
                    index += 1
                continue

            transformed.append(_transform_non_thunk_line(body_line))
            line_map.append(index)
            index += 1

    if source:
        tree_source = "\n".join(transformed) + "\n"
    else:
        tree_source = ""
    return _TreeSitterSource(
        source=tree_source,
        line_map=tuple(line_map),
        synthetic_message_rows=frozenset(synthetic_message_rows),
        original_lines=tuple(original_lines),
    )


def _transform_non_thunk_line(line: str) -> str:
    struct_match = STRUCT_HEADER_RE.match(line)
    if struct_match is not None:
        return line

    field_match = FIELD_RE.match(line)
    if field_match is not None:
        return (
            f"{field_match.group('indent')}{field_match.group('name')}"
            f"{field_match.group('optional') or ''}:"
            f"{field_match.group('space')}{_tree_sitter_type_name(field_match.group('type'))}"
            f"{field_match.group('suffix')}"
        )

    return _transform_directive_line(line)


def _transform_thunk_header(line: str) -> str:
    match = THUNK_HEADER_RE.match(line)
    if match is None:
        return line
    rest = match.group("rest")
    output = ""
    if "->" in rest:
        rest, raw_output = rest.rsplit("->", 1)
        output = f" -> {_tree_sitter_type_name(raw_output.strip())}"
    name, params = _parse_thunk_rest(rest)
    rendered_name = f" {name}" if name else ""
    rendered_params = "" if params is None else f"({_tree_sitter_params(params)})"
    return f"{match.group('indent')}thunk{rendered_name}{rendered_params}{output}:{match.group('suffix')}"


def _transform_directive_line(line: str) -> str:
    match = DIRECTIVE_RE.match(line)
    if match is None:
        return line
    key = match.group("key")
    normalized_key = "models" if key == "model" else key
    values = line[match.end() :]
    rendered_values = " selector" if values.strip() else values
    return (
        f"{match.group('indent')}{normalized_key}"
        f"{match.group('space')}{match.group('op')}{rendered_values}"
    )


def _split_inline_comment(line: str) -> tuple[str, str]:
    match = re.search(r"(?<!\S)#", line)
    if match is None:
        return line.rstrip(), ""
    body = line[: match.start()].rstrip()
    comment = line[match.start() :].strip()
    return body, f"  {comment}" if body else comment


def _tree_sitter_params(raw: str) -> str:
    if not raw.strip():
        return ""
    rendered: list[str] = []
    for item in [part.strip() for part in raw.split(",")]:
        if item == "_":
            rendered.append("input: Message")
            continue
        match = re.fullmatch(
            r"(?P<name>[A-Za-z_][\w-]*)(?P<optional>\?)?(?::\s*(?P<type>[A-Za-z][A-Za-z0-9]*(?:\[\])?))?",
            item,
        )
        if match is None:
            rendered.append(item)
            continue
        name = "input" if match.group("name") == "_" else match.group("name")
        optional = match.group("optional") or ""
        type_name = _tree_sitter_type_name(match.group("type") or "string")
        rendered.append(f"{name}{optional}: {type_name}")
    return ", ".join(rendered)


def _tree_sitter_type_name(type_name: str | None) -> str:
    if not type_name:
        return ""
    suffix = "[]" if type_name.endswith("[]") else ""
    base = type_name[:-2] if suffix else type_name
    return f"{TREE_SITTER_TYPE_ALIASES.get(base, base)}{suffix}"


def _ast_type_name(type_name: str | None) -> str | None:
    if not type_name:
        return None
    suffix = "[]" if type_name.endswith("[]") else ""
    base = type_name[:-2] if suffix else type_name
    return f"{AST_TYPE_ALIASES.get(base, base)}{suffix}"


def _parse_thunk_header(line: str, *, line_number: int) -> tuple[str | None, str | None, str | None] | None:
    match = THUNK_HEADER_RE.match(line)
    if match is None:
        return None
    rest = match.group("rest").strip()
    output: str | None = None
    if "->" in rest:
        rest, raw_output = rest.rsplit("->", 1)
        output = _ast_type_name(raw_output.strip()) or None
    name, params = _parse_thunk_rest(rest)
    if name == "main":
        name = "main"
    if params is not None:
        _validate_signature_params(params, line_number=line_number)
    return name, params, output


def _parse_thunk_rest(rest: str) -> tuple[str | None, str | None]:
    rest = rest.strip()
    if not rest:
        return None, None
    params_start = rest.find("(")
    if params_start < 0:
        return rest.strip() or None, None
    params_end = rest.rfind(")")
    if params_end < params_start:
        return rest.strip() or None, None
    name = rest[:params_start].strip() or None
    return name, rest[params_start + 1 : params_end]


def _thunk_name_from_header(line: str) -> str:
    parsed = _parse_thunk_header(line, line_number=1)
    if parsed is None:
        return "main"
    return parsed[0] or "main"


def _validate_signature_params(raw: str, *, line_number: int) -> None:
    _params_from_signature(raw, line_number=line_number)


def _params_from_signature(raw: str | None, *, line_number: int) -> tuple[ParamDecl | None, list[ParamDecl]]:
    if raw is None:
        return ParamDecl(name="_"), []
    if not raw.strip():
        return None, []

    input_param: ParamDecl | None = None
    params: list[ParamDecl] = []
    for item in [part.strip() for part in raw.split(",")]:
        if item == "_":
            if input_param is not None:
                raise ToolangError(f"Parameter signature at line {line_number} repeats input parameter.")
            input_param = ParamDecl(name="_")
            continue
        match = re.fullmatch(
            r"(?P<name>[A-Za-z_][\w-]*)(?P<optional>\?)?(?::\s*(?P<type>[A-Za-z][A-Za-z0-9]*(?:\[\])?))?",
            item,
        )
        if match is None:
            raise ToolangError(
                f"Parameter signature at line {line_number} contains invalid parameter {item!r}."
            )
        name = match.group("name")
        optional = match.group("optional") is not None
        type_name = _ast_type_name(match.group("type"))
        if name == "input":
            if input_param is not None:
                raise ToolangError(f"Parameter signature at line {line_number} repeats input parameter.")
            input_param = ParamDecl(name="_", optional=False, type_name=None)
            continue
        params.append(ParamDecl(name=name, optional=optional, type_name=type_name))
    return input_param, params


def _field_from_source_line(line: str) -> tuple[str, str] | None:
    match = FIELD_RE.match(line)
    if match is None:
        return None
    return match.group("name"), match.group("type")


def _is_implicit_message_line(line: str) -> bool:
    if not line.strip():
        return False
    stripped = line.lstrip(" \t")
    if stripped.startswith("#"):
        return False
    if LEGACY_DELEGATES_RE.match(line):
        return False
    if DIRECTIVE_RE.match(line):
        return False
    if re.match(r"^[ \t]*(context|instruct|system|user|assistant|tool):", line):
        return False
    return line.startswith((" ", "\t"))


def _implicit_message_kind(thunk_name: str) -> MessageBlockKind:
    return "instruct" if thunk_name in {"chat", "task", "chore"} else "user"


def _leading_whitespace(line: str) -> str:
    return line[: len(line) - len(line.lstrip(" \t"))]


def _indent_message_line(line: str) -> str:
    indent = _leading_whitespace(line)
    return f"{indent}  {line[len(indent):]}"


def _item_node(node: Node) -> Node:
    if node.type == "item" and node.named_children:
        return node.named_children[0]
    return node


def _descendant_of_type(node: Node, node_type: str) -> Node | None:
    if node.type == node_type:
        return node
    for child in node.named_children:
        found = _descendant_of_type(child, node_type)
        if found is not None:
            return found
    return None


def _descendant_field(node: Node, field_name: str) -> Node | None:
    child = node.child_by_field_name(field_name)
    if child is not None:
        return child
    for descendant in node.named_children:
        found = _descendant_field(descendant, field_name)
        if found is not None:
            return found
    return None


def _first_error_node(node: Node) -> Node | None:
    if node.is_error or node.is_missing:
        if _is_ignored_error_node(node):
            return None
        return node
    for child in node.children:
        result = _first_error_node(child)
        if result is not None:
            return result
    return None


def _is_ignored_error_node(node: Node) -> bool:
    parent = node.parent
    while parent is not None:
        if parent.type == "frontmatter":
            return True
        parent = parent.parent
    return False


def _raise_syntax_error(lines: list[str], syntax_source: _TreeSitterSource, node: Node) -> None:
    original_row = syntax_source.original_line_index(node.start_point.row)
    line_number = original_row + 1
    raw_line = _line_text(lines, original_row)
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
