"""Lower Tree-sitter Toolang CST nodes into the semantic AST."""

from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass
from functools import lru_cache
import re
from typing import Any, cast

import frontmatter
from tree_sitter import Language, Node, Parser
import tree_sitter_toolang

from toolang.base.error import ToolangError

from .ast import (
    CapDecl,
    ContextBlock,
    Directive,
    Flow,
    FlowStage,
    FlowStageKind,
    InstructBlock,
    MessageBlock,
    MessageBlockKind,
    ParamDecl,
    Program,
    SourceSpan,
    StructDecl,
    StructFieldDecl,
    Thunk,
    UseDecl,
    WorkDecl,
)
from .validate import validate_service_meta


PROMPT_FIELDS = frozenset({"params"})
SIGNATURE_PARAM_RE = re.compile(r"^[A-Za-z_][\w-]*\??$")
THUNK_HEADER_RE = re.compile(r"^(?P<indent>[ \t]*)thunk(?P<rest>.*):(?P<suffix>[ \t]*(?:#.*)?)$")
STRUCT_HEADER_RE = re.compile(r"^(?P<indent>[ \t]*)struct(?P<rest>.*):(?P<suffix>[ \t]*(?:#.*)?)$")
FIELD_RE = re.compile(
    r"^(?P<indent>[ \t]+)(?P<name>[a-z][a-z0-9_-]*)(?P<optional>\?)?:"
    r"(?P<space>[ \t]*)(?P<type>[A-Za-z][A-Za-z0-9]*(?:\[\])?)(?P<suffix>[ \t]*(?:#.*)?)$"
)
DIRECTIVE_RE = re.compile(
    r"^(?P<indent>[ \t]*)(?P<key>models|tools|skills|services|"
    r"psyches|hands|handoffs|recall)(?P<space>[ \t]*)(?P<op>=|\+=|-=)"
)
LEGACY_DELEGATES_RE = re.compile(r"^[ \t]*delegates[ \t]*(?:=|\+=|-=)")
TOP_LEVEL_RE = re.compile(
    r"^(use|struct|psyche|skill|service|prompt|task|chore|context|instruct|thunk|flow)\b"
)


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

    pending_doc: list[str] = []
    for child in tree.root_node.named_children:
        node = _item_node(child)
        if node.type in {"blank_line", "comment", "comment_line"}:
            pending_doc.clear()
            continue
        if node.type in {"parent_doc_line", "program_doc_comment"}:
            program.doc = _join_doc(program.doc, _doc_line_text(node))
            continue
        if node.type in {"doc_line", "doc_comment"}:
            pending_doc.append(_doc_line_text(node))
            continue
        doc = "\n".join(item for item in pending_doc if item).strip() or None
        pending_doc.clear()
        if node.type in {"use", "use_statement"}:
            program.uses.append(_use_from_node(node, syntax_source, doc=doc))
            continue
        if node.type in {"fenced_declaration", "psyche", "skill", "service", "prompt"}:
            program.caps.append(_cap_from_node(node, syntax_source, doc=doc))
            continue
        if node.type in {"task", "chore"}:
            work = _work_from_node(node, syntax_source, doc=doc)
            if work.kind == "task":
                program.tasks.append(work)
            else:
                program.chores.append(work)
            continue
        if node.type == "instruct":
            program.instructs.append(_instruct_from_node(node, syntax_source, doc=doc))
            continue
        if node.type == "context":
            program.contexts.append(_context_from_node(node, syntax_source, doc=doc))
            continue
        if node.type in {"struct", "struct_declaration"}:
            program.structs.append(_struct_from_node(node, lines, syntax_source, doc=doc))
            continue
        if node.type == "thunk":
            program.thunks.append(_thunk_from_node(node, lines, syntax_source, doc=doc))
            continue
        if node.type == "flow":
            program.flows.append(_flow_from_node(node, syntax_source, doc=doc))
            continue
        raise ToolangError(
            f"Unsupported statement at line {syntax_source.original_line_number(node.start_point.row)}: "
            f"{_node_text(node)!r}"
        )

    return program


def program_to_ast_data(program: Program, *, include_source_lines: bool = False) -> dict[str, object]:
    """Return one JSON-compatible representation of a parsed program AST."""
    data = _program_to_semantic_ast_data(program)
    if include_source_lines and program._source_lines is not None:
        data["_source_lines"] = list(program._source_lines)
    return data


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


def _program_to_semantic_ast_data(program: Program) -> dict[str, object]:
    data: dict[str, object] = {"node": "program"}
    if program.doc is not None:
        data["doc"] = program.doc
    items: list[dict[str, object]] = []
    items.extend(_use_to_ast_data(item) for item in program.uses)
    items.extend(_cap_to_ast_data(item) for item in program.caps)
    items.extend(_work_to_ast_data(item) for item in program.tasks)
    items.extend(_work_to_ast_data(item) for item in program.chores)
    items.extend(_struct_to_ast_data(item) for item in program.structs)
    items.extend(_context_to_ast_data(item) for item in program.contexts)
    items.extend(_instruct_to_ast_data(item) for item in program.instructs)
    items.extend(_thunk_to_ast_data(item) for item in program.thunks)
    items.extend(_flow_to_ast_data(item) for item in program.flows)
    items.sort(key=lambda item: cast(dict[str, int], item["span"])["line"] if "span" in item else 0)
    data["items"] = items
    return data


def _use_to_ast_data(use: UseDecl) -> dict[str, object]:
    data: dict[str, object] = {
        "node": "use",
        "kind": use.kind,
        "ref": use.reference,
        "span": _span_data(use.span),
    }
    _set_optional(data, "doc", use.doc)
    return data


def _cap_to_ast_data(cap: CapDecl) -> dict[str, object]:
    data: dict[str, object] = {
        "node": "cap",
        "kind": cap.kind,
        "name": cap.name,
        "properties": _properties_to_ast_data(cap.meta),
        "span": _span_data(cap.span),
    }
    _set_optional(data, "doc", cap.doc)
    _set_optional(data, "content", cap.body)
    return data


def _work_to_ast_data(work: WorkDecl) -> dict[str, object]:
    data: dict[str, object] = {
        "node": "job",
        "kind": work.kind,
        "name": work.name,
        "properties": _properties_to_ast_data(work.meta),
        "span": _span_data(work.span),
    }
    _set_optional(data, "doc", work.doc)
    _set_optional(data, "content", work.body)
    return data


def _struct_to_ast_data(struct: StructDecl) -> dict[str, object]:
    data: dict[str, object] = {
        "node": "struct",
        "name": struct.name,
        "fields": [_field_to_ast_data(item) for item in struct.fields],
        "span": _span_data(struct.span),
    }
    _set_optional(data, "doc", struct.doc)
    return data


def _field_to_ast_data(field: StructFieldDecl) -> dict[str, object]:
    data: dict[str, object] = {
        "name": field.name,
        "type": _type_to_ast_data(field.type_name),
        "optional": field.optional,
        "span": _span_data(field.span),
    }
    _set_optional(data, "doc", field.doc)
    return data


def _context_to_ast_data(context: ContextBlock) -> dict[str, object]:
    data: dict[str, object] = {
        "node": "context",
        "content": context.body,
        "span": _span_data(context.span),
    }
    _set_optional(data, "doc", context.doc)
    _set_optional(data, "name", context.name)
    return data


def _instruct_to_ast_data(instruct: InstructBlock) -> dict[str, object]:
    data: dict[str, object] = {
        "node": "instruct",
        "content": instruct.body,
        "span": _span_data(instruct.span),
    }
    _set_optional(data, "doc", instruct.doc)
    _set_optional(data, "name", instruct.name)
    return data


def _thunk_to_ast_data(thunk: Thunk) -> dict[str, object]:
    data: dict[str, object] = {
        "node": "thunk",
        "directives": [_directive_to_ast_data(item) for item in thunk.directives],
        "messages": [_message_to_ast_data(item) for item in thunk.messages],
        "span": _span_data(thunk.span),
    }
    _set_optional(data, "doc", thunk.doc)
    _set_optional(data, "name", thunk.name)
    if thunk.params_explicit:
        data["params"] = _params_to_ast_data(thunk.input, thunk.params)
    if thunk.output is not None:
        data["return"] = _type_to_ast_data(thunk.output)
    if thunk.context is not None:
        data["context"] = _prompt_setting_to_ast_data(thunk.context)
    if thunk.instruct is not None:
        data["instruct"] = _prompt_setting_to_ast_data(thunk.instruct)
    return data


def _flow_to_ast_data(flow: Flow) -> dict[str, object]:
    data: dict[str, object] = {
        "node": "flow",
        "directives": [_directive_to_ast_data(item) for item in flow.directives],
        "statements": [_flow_stage_to_ast_data(item) for item in flow.stages],
        "span": _span_data(flow.span),
    }
    _set_optional(data, "doc", flow.doc)
    _set_optional(data, "name", flow.name)
    if flow.params_explicit:
        data["params"] = _params_to_ast_data(flow.input, flow.params)
    if flow.output is not None:
        data["return"] = _type_to_ast_data(flow.output)
    return data


def _flow_stage_to_ast_data(stage: FlowStage) -> dict[str, object]:
    if stage.kind == "bare":
        node = "do"
    elif stage.kind == "repeat" and stage.stages:
        node = "repeat_block"
    elif stage.kind == "repeat":
        node = "repeat_above"
    else:
        node = stage.kind
    data: dict[str, object] = {
        "node": node,
        "span": _span_data(stage.span),
    }
    _set_optional(data, "doc", stage.doc)
    if node == "do":
        data["implicit"] = stage.kind == "bare"
    if stage.kind == "ask":
        _set_optional(data, "agent", stage.target)
        return data
    if stage.kind == "repeat":
        _set_optional(data, "times", stage.count)
        if stage.condition is not None:
            data["until"] = {"content": stage.condition}
        if stage.stages:
            data["body"] = {
                "node": "flow",
                "directives": [],
                "statements": [_flow_stage_to_ast_data(item) for item in stage.stages],
            }
        return data
    if stage.kind in {"keep", "drop", "each"}:
        _set_optional(data, "par", stage.parallelism)
        if stage.body is not None:
            data["proc"] = _inline_proc_to_ast_data(stage)
        elif stage.target is not None:
            data["proc"] = {"kind": "ref", "ref": stage.target}
        return data
    if stage.kind == "rank":
        _set_optional(data, "par", stage.parallelism)
        _set_optional(data, "limit", stage.limit)
        data["proc"] = _inline_proc_to_ast_data(stage) if stage.body is not None else {"kind": "ref", "ref": stage.target or ""}
        return data
    if stage.body is not None:
        data["proc"] = _inline_proc_to_ast_data(stage)
    elif stage.targets:
        data["proc"] = (
            {"kind": "pipeline", "pipeline": list(stage.targets)}
            if len(stage.targets) > 1
            else {"kind": "ref", "ref": stage.targets[0]}
        )
    return data


def _inline_proc_to_ast_data(stage: FlowStage) -> dict[str, object]:
    inline: dict[str, object] = {
        "node": "thunk",
        "directives": [],
        "messages": [{"content": stage.body or ""}],
    }
    data: dict[str, object] = {"kind": "inline", "inline": inline}
    if stage.output is not None:
        data["to"] = _type_to_ast_data(stage.output)
    return data


def _params_to_ast_data(input_param: ParamDecl | None, params: list[ParamDecl]) -> list[dict[str, object]]:
    items: list[ParamDecl] = []
    if input_param is not None:
        items.append(input_param)
    items.extend(params)
    return [_param_to_ast_data(item) for item in items]


def _default_input_param() -> ParamDecl:
    return ParamDecl(name="in", type_name="Pack")


def _param_to_ast_data(param: ParamDecl) -> dict[str, object]:
    data: dict[str, object] = {
        "name": param.name,
        "optional": param.optional,
    }
    if param.type_name is not None:
        data["type"] = _type_to_ast_data(param.type_name)
    return data


def _type_to_ast_data(type_name: str) -> dict[str, object]:
    array_depth = 0
    while type_name.endswith("[]"):
        array_depth += 1
        type_name = type_name[:-2]
    return {
        "kind": "builtin" if type_name in {"Text", "Number", "Boolean", "Json", "Part", "Pack"} else "user",
        "name": type_name,
        "array_depth": array_depth,
    }


def _directive_to_ast_data(directive: Directive) -> dict[str, object]:
    return {
        "name": directive.name,
        "op": directive.operator,
        "values": list(directive.values),
        "span": _span_data(directive.span),
    }


def _message_to_ast_data(message: MessageBlock) -> dict[str, object]:
    data: dict[str, object] = {
        "content": message.text,
        "span": _span_data(message.span),
    }
    if message.explicit:
        data["role"] = message.kind
    return data


def _prompt_setting_to_ast_data(block: MessageBlock) -> dict[str, object]:
    text = block.text.strip()
    if text == "default":
        return {"kind": "default", "value": text}
    if text == "none":
        return {"kind": "none", "value": text}
    return {"kind": "ref", "value": text}


def _properties_to_ast_data(properties: dict[str, Any]) -> list[dict[str, object]]:
    return [{"name": key, "value": str(value)} for key, value in properties.items()]


def _span_data(span: SourceSpan) -> dict[str, int]:
    return {"line": span.line}


def _set_optional(data: dict[str, object], key: str, value: object | None) -> None:
    if value is not None:
        data[key] = value


def _use_from_node(node: Node, syntax_source: _TreeSitterSource, *, doc: str | None = None) -> UseDecl:
    return UseDecl(
        kind=_required_text(node, "kind").strip(),
        reference=_required_text(node, "reference").strip(),
        span=SourceSpan(syntax_source.original_line_number(node.start_point.row)),
        doc=doc,
    )


def _cap_from_node(node: Node, syntax_source: _TreeSitterSource, *, doc: str | None = None) -> CapDecl:
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
    properties = _properties_from_body(body_node)
    raw_body = _cap_body_text(body_node)
    frontmatter_present = _descendant_of_type(body_node, "frontmatter") is not None
    if frontmatter_present:
        meta, body, params = _cap_semantics(
            kind=kind,
            raw_body=raw_body,
            frontmatter_present=frontmatter_present,
            line_number=line_number,
        )
    else:
        meta = dict(properties)
        if kind == "service":
            validate_service_meta(meta, line_number=line_number)
        elif kind == "prompt":
            _require_exact_fields(
                meta=meta,
                allowed=PROMPT_FIELDS,
                kind="prompt",
                line_number=line_number,
            )
        body = _body_content_text(body_node)
        params = (
            _parse_signature_params(str(meta.get("params") or ""), line_number=line_number)
            if kind == "prompt"
            else []
        )
    return CapDecl(
        kind=kind,
        name=name,
        language=language,
        meta=meta,
        body=body,
        params=params,
        span=SourceSpan(line_number),
        doc=doc,
    )


def _work_from_node(node: Node, syntax_source: _TreeSitterSource, *, doc: str | None = None) -> WorkDecl:
    body_node = _required_child(node, "body")
    kind = "task" if node.type == "task" else "chore"
    return WorkDecl(
        kind=kind,
        name=_required_text(node, "name").strip(),
        meta=_properties_from_body(body_node),
        body=_body_content_text(body_node),
        span=SourceSpan(syntax_source.original_line_number(node.start_point.row)),
        doc=doc,
    )


def _instruct_from_node(node: Node, syntax_source: _TreeSitterSource, *, doc: str | None = None) -> InstructBlock:
    body_node = _required_child(node, "body")
    return InstructBlock(
        name=_optional_text(node.child_by_field_name("name")),
        body=_block_value_text(body_node),
        language=_cap_body_language(body_node),
        span=SourceSpan(syntax_source.original_line_number(node.start_point.row)),
        doc=doc,
    )


def _context_from_node(node: Node, syntax_source: _TreeSitterSource, *, doc: str | None = None) -> ContextBlock:
    body_node = _required_child(node, "body")
    return ContextBlock(
        name=_optional_text(node.child_by_field_name("name")),
        body=_block_value_text(body_node),
        language=_cap_body_language(body_node),
        span=SourceSpan(syntax_source.original_line_number(node.start_point.row)),
        doc=doc,
    )


def _struct_from_node(
    node: Node,
    lines: list[str],
    syntax_source: _TreeSitterSource,
    *,
    doc: str | None = None,
) -> StructDecl:
    header = next(
        (child for child in node.named_children if child.type == "struct_header"),
        None,
    )
    body = _required_child(node, "body")
    if header is None and node.type == "struct_declaration":
        raise ToolangError(f"Missing struct header at line {node.start_point.row + 1}.")
    fields: list[StructFieldDecl] = []
    pending_doc: list[str] = []
    for child in body.named_children:
        if child.type in {"doc_line", "doc_comment"}:
            pending_doc.append(_doc_line_text(child))
            continue
        if child.type in {"blank_line", "comment_line"}:
            pending_doc.clear()
            continue
        field_node = child.child_by_field_name("field") or (child if child.type == "field" else None)
        if field_node is None:
            continue
        original_row = syntax_source.original_line_index(field_node.start_point.row)
        parsed_field = _field_from_source_line(_line_text(lines, original_row))
        field_doc = "\n".join(item for item in pending_doc if item).strip() or None
        pending_doc.clear()
        fields.append(
            StructFieldDecl(
                name=parsed_field[0] if parsed_field is not None else _required_text(field_node, "name"),
                type_name=(
                    parsed_field[1]
                    if parsed_field is not None
                    else _ast_type_name(_required_text(field_node, "type")) or ""
                ),
                span=SourceSpan(original_row + 1),
                optional=field_node.child_by_field_name("optional") is not None,
                doc=field_doc,
            )
        )
    return StructDecl(
        name=_required_text(header, "name") if header is not None else _required_text(node, "name"),
        fields=fields,
        span=SourceSpan(syntax_source.original_line_number(node.start_point.row)),
        doc=doc,
    )


def _thunk_from_node(
    node: Node,
    lines: list[str],
    syntax_source: _TreeSitterSource,
    *,
    doc: str | None = None,
) -> Thunk:
    signature = node.child_by_field_name("signature")
    body = _required_child(node, "body")
    original_row = syntax_source.original_line_index(node.start_point.row)
    header = _parse_thunk_header(_line_text(lines, original_row), line_number=original_row + 1)
    if header is None and signature is None:
        raise ToolangError(f"Missing thunk signature at line {original_row + 1}.")
    params_node = signature.child_by_field_name("params") if signature is not None else node.child_by_field_name("params")
    params_explicit = params_node is not None if header is None else header[1] is not None
    input_param, params = (
        _params_from_signature(header[1], line_number=original_row + 1)
        if header is not None
        else _params_from_node(params_node)
    )
    directives: list[Directive] = []
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
        doc=doc,
        params_explicit=params_explicit,
    )

    for child in _thunk_content_nodes(body):
        if child.type in {"overlay_line", "directive"}:
            directives.append(_directive_from_node(child, syntax_source))
            continue
        if child.type in {"blank_line", "comment_line", "doc_line", "parent_doc_line", "line_end"}:
            continue
        if child.type in {"pass_statement", "pass_keyword"}:
            continue
        if child.type == "context_setting":
            context_block = _setting_from_node(child, kind="context", syntax_source=syntax_source)
            continue
        if child.type == "instruct_setting":
            instruct_block = _setting_from_node(child, kind="instruct", syntax_source=syntax_source)
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

    thunk.directives = tuple(directives)
    thunk.context = context_block
    thunk.instruct = instruct_block
    thunk.messages = tuple(messages)
    return thunk


def _thunk_content_nodes(node: Node) -> list[Node]:
    if node.type in {
        "overlay_line",
        "directive",
        "blank_line",
        "comment_line",
        "doc_line",
        "parent_doc_line",
        "context_setting",
        "instruct_setting",
        "message",
        "block",
    }:
        return [node]
    if node.type in {
        "thunk_body",
        "thunk_tail",
        "message_section",
        "instruction_section",
        "settings",
        "messages",
        "roled_message",
        "unroled_message",
    }:
        items: list[Node] = []
        for child in node.named_children:
            items.extend(_thunk_content_nodes(child))
        return items
    return [node]


def _flow_from_node(node: Node, syntax_source: _TreeSitterSource, *, doc: str | None = None) -> Flow:
    params_node = node.child_by_field_name("params")
    input_param, params = _flow_params_from_node(params_node)
    body = _required_child(node, "body")
    directives: list[Directive] = []
    stages: list[FlowStage] = []
    pending_doc: list[str] = []
    for child in _flow_content_nodes(body):
        if child.type in {"overlay_line", "directive"}:
            directives.append(_directive_from_node(child, syntax_source))
            continue
        if child.type in {"doc_comment", "doc_line"}:
            pending_doc.extend(_doc_comment_lines(_node_text(child)))
            continue
        if child.type in {"blank_line", "comment_line", "parent_doc_line", "pass_statement", "pass_keyword", "line_end"}:
            pending_doc.clear()
            continue
        if child.type in {"flow_body_tail", "statements"}:
            stages.extend(_flow_stages_from_body_tail(child, syntax_source, initial_doc="\n".join(pending_doc).strip() or None))
            pending_doc.clear()
            continue
        if _is_flow_statement_node(child):
            stages.append(_flow_stage_from_statement(child, syntax_source, doc="\n".join(pending_doc).strip() or None))
            pending_doc.clear()
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
        directives=tuple(directives),
        stages=tuple(stages),
        span=SourceSpan(syntax_source.original_line_number(node.start_point.row)),
        doc=doc,
        params_explicit=params_node is not None,
    )


def _flow_content_nodes(node: Node) -> list[Node]:
    if node.type in {"directive", "overlay_line", "statements", "flow_body_tail", "blank_line", "comment_line", "doc_line", "parent_doc_line"}:
        return [node]
    if node.type == "flow_body":
        return list(node.named_children)
    return [node]


def _flow_params_from_node(node: Node | None) -> tuple[ParamDecl | None, list[ParamDecl]]:
    if node is None:
        return _default_input_param(), []
    params: list[ParamDecl] = []
    input_param: ParamDecl | None = None
    for parameter in _param_nodes(node):
        name_node = _required_child(parameter, "name")
        type_name = _ast_type_name(_optional_text(parameter.child_by_field_name("type")))
        param = ParamDecl(
            name=_node_text(name_node),
            optional=_has_optional_marker(parameter),
            type_name=type_name,
        )
        if param.name == "in" and input_param is None:
            input_param = param
        else:
            params.append(param)
    return input_param, params


def _flow_stages_from_body_tail(
    node: Node,
    syntax_source: _TreeSitterSource,
    *,
    initial_doc: str | None = None,
) -> list[FlowStage]:
    stages: list[FlowStage] = []
    pending_doc: list[str] = [initial_doc] if initial_doc else []
    for child in node.named_children:
        if child.type in {"doc_comment", "doc_line"}:
            pending_doc.extend(_doc_comment_lines(_node_text(child)))
            continue
        if child.type in {"blank_line", "comment_line", "pass_statement"}:
            pending_doc.clear()
            continue
        if child.type == "flow_body_statement" or _is_flow_statement_node(child):
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
    if step is None and _is_flow_statement_node(node):
        step = node
    if step is None:
        raise ToolangError(f"Missing flow step at line {syntax_source.original_line_number(node.start_point.row)}.")
    kind = _flow_stage_kind(step)
    body_node = _flow_statement_body(step)
    head_node = step.child_by_field_name("head") or step
    count_node = step.child_by_field_name("count") or _descendant_of_type(step, "times_clause")
    condition_node = step.child_by_field_name("condition") or _descendant_of_type(step, "condition")
    repeat_body = _descendant_of_type(step, "repeat_body") if kind == "repeat" else None
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


def _is_flow_statement_node(node: Node) -> bool:
    return node.type in {
        "do_statement",
        "ask_statement",
        "unfold_statement",
        "keep_statement",
        "drop_statement",
        "rank_statement",
        "each_statement",
        "fold_statement",
        "repeat_above_statement",
        "repeat_block_statement",
        "implicit_do_statement",
    }


def _flow_statement_body(node: Node) -> Node | None:
    if node.type == "implicit_do_statement":
        return node
    body = node.child_by_field_name("body")
    if body is not None:
        return body
    return _descendant_of_type(node, "text_inline")


def _flow_repeat_stages(
    node: Node,
    syntax_source: _TreeSitterSource,
) -> list[FlowStage]:
    if node.type not in {"flow_repeat_block_body", "repeat_body"}:
        return []
    stages: list[FlowStage] = []
    pending_doc: list[str] = []
    for child in node.named_children:
        if child.type in {"doc_comment", "doc_line"}:
            pending_doc.extend(_doc_comment_lines(_node_text(child)))
            continue
        if child.type in {"blank_line", "comment_line", "pass_statement"}:
            pending_doc.clear()
            continue
        if child.type == "flow_body_statement" or _is_flow_statement_node(child):
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


def _doc_line_text(node: Node) -> str:
    text = _node_text(node).strip()
    if text.startswith("##!"):
        return text[3:].strip()
    if text.startswith("##"):
        return text[2:].strip()
    return text


def _join_doc(existing: str | None, new: str) -> str:
    if not new:
        return existing or ""
    if not existing:
        return new
    return f"{existing}\n{new}"


def _flow_stage_kind(step: Node) -> FlowStageKind:
    if step.type == "implicit_do_statement":
        return "bare"
    if step.type.endswith("_statement"):
        prefix = step.type.removesuffix("_statement")
        if prefix == "repeat_above" or prefix == "repeat_block":
            return "repeat"
        if prefix in {"do", "ask", "unfold", "keep", "drop", "rank", "each", "fold"}:
            return cast(FlowStageKind, prefix)
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
        if current.type in {"flow_target", "callee", "agent"}:
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
    if node.type in {"text_inline", "text_block", "text_body", "condition", "implicit_do_statement"}:
        return _block_value_text(node)
    return _node_text(node).strip()


def _flow_output_type(node: Node | None) -> str | None:
    if node is None:
        return None
    output_node = _descendant_of_type(node, "flow_inline_output_type")
    if output_node is not None:
        return _ast_type_name(_required_text(output_node, "type"))
    if node.type == "flow_inline_output_type":
        return _ast_type_name(_required_text(node, "type"))
    to_clause = _descendant_of_type(node, "to_clause")
    if to_clause is not None:
        type_node = to_clause.child_by_field_name("type") or _descendant_of_type(to_clause, "type")
        return _ast_type_name(_node_text(type_node))
    return None


def _flow_parallelism(node: Node | None) -> int | None:
    if node is None:
        return None
    parallel = _descendant_of_type(node, "flow_parallelism")
    if parallel is None and node.type == "flow_parallelism":
        parallel = node
    if parallel is None:
        parallel = _descendant_of_type(node, "par_clause")
    if parallel is None:
        return None
    return _int_node_text(parallel.child_by_field_name("count") or _descendant_of_type(parallel, "integer_literal"))


def _flow_rank_limit(node: Node | None) -> int | None:
    if node is None:
        return None
    rank = _descendant_of_type(node, "flow_rank_limit")
    if rank is None and node.type == "flow_rank_limit":
        rank = node
    if rank is None:
        rank = _descendant_of_type(node, "limit_clause")
    if rank is None:
        return None
    return _int_node_text(rank.child_by_field_name("count") or _descendant_of_type(rank, "integer_literal"))


def _flow_condition_text(node: Node | None) -> str | None:
    if node is None:
        return None
    text = node.child_by_field_name("text")
    if text is None:
        return _node_text(node).strip() or None
    if text.type == "block_indented_implicit":
        return _block_value_text(text)
    if text.type in {"text_inline", "text_block", "text_body"}:
        return _block_value_text(text)
    return _node_text(text).strip() or None


def _int_node_text(node: Node | None) -> int | None:
    if node is None:
        return None
    if node.type == "times_clause":
        node = _descendant_of_type(node, "integer_literal") or node
    text = _node_text(node).strip()
    return int(text) if text else None


def _params_from_node(node: Node | None) -> tuple[ParamDecl | None, list[ParamDecl]]:
    if node is None:
        return _default_input_param(), []
    input_node = node.child_by_field_name("input")
    input_param = (
        ParamDecl(
            name=_required_text(input_node, "name"),
            optional=False,
            type_name=_ast_type_name(_optional_text(input_node.child_by_field_name("type"))) or "Pack",
        )
        if input_node is not None and _required_text(input_node, "name") == "in"
        else None
    )
    params: list[ParamDecl] = []
    for parameter in _param_nodes(node):
        name_node = _required_child(parameter, "name")
        type_name = _ast_type_name(_optional_text(parameter.child_by_field_name("type")))
        param = ParamDecl(
            name=_node_text(name_node),
            optional=_has_optional_marker(parameter),
            type_name=type_name,
        )
        if param.name == "in" and input_param is None:
            input_param = param
        else:
            params.append(param)
    return input_param, params


def _param_nodes(node: Node) -> list[Node]:
    fields = list(node.children_by_field_name("param"))
    if fields:
        return fields
    return [child for child in node.named_children if child.type == "param"]


def _has_optional_marker(node: Node) -> bool:
    return (
        node.child_by_field_name("optional") is not None
        or any(child.type in {"optional", "optional_marker"} for child in node.named_children)
    )


def _directive_from_node(node: Node, syntax_source: _TreeSitterSource) -> Directive:
    directive = node.child_by_field_name("directive") or node
    subject = (
        _required_text(directive, "subject").strip()
        if directive.child_by_field_name("subject") is not None
        else _required_text(directive, "key").strip()
    )
    operator = (
        _required_text(directive, "operator").strip()
        if directive.child_by_field_name("operator") is not None
        else _required_text(directive, "operator").strip()
    )
    line_number = syntax_source.original_line_number(node.start_point.row)
    raw_values = (
        _raw_directive_values_from_line(syntax_source.original_line(node.start_point.row))
        if node.type in {"overlay_line", "directive"}
        else None
    )
    if raw_values is None:
        raw_values = (
            _optional_text(directive.child_by_field_name("values"))
            or _optional_text(directive.child_by_field_name("value"))
            or ""
        )
    items = tuple(
        item
        for item in (part.strip() for part in raw_values.split(","))
        if item
    )
    _validate_directive_name(subject, line_number=line_number)
    _validate_directive_operator(operator, line_number=line_number)
    return Directive(
        name=subject,
        operator=operator,
        values=items,
        span=SourceSpan(line_number),
    )


def _raw_directive_values_from_line(line: str) -> str | None:
    match = DIRECTIVE_RE.match(line)
    if match is None:
        return None
    values, _comment = _split_inline_comment(line[match.end() :])
    return values


def _directive_family(subject: str) -> str:
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
    return normalized


def _validate_directive_name(subject: str, *, line_number: int) -> None:
    normalized = subject.strip()
    if normalized in {
        "models",
        "tools",
        "psyches",
        "skills",
        "services",
        "hands",
        "handoffs",
        "recall",
    }:
        return
    raise ToolangError(f"Unsupported thunk directive {subject!r} at line {line_number}.")


def _validate_directive_operator(operator: str, *, line_number: int) -> None:
    normalized = operator.strip()
    if normalized in {"=", "+=", "-="}:
        return
    raise ToolangError(f"Unsupported thunk directive operator {operator!r} at line {line_number}.")


def _message_from_node(
    node: Node,
    *,
    thunk_name: str,
    syntax_source: _TreeSitterSource,
) -> MessageBlock:
    message = node.child_by_field_name("message") or node
    if message.type == "message":
        role_node = message.child_by_field_name("role") or _descendant_of_type(message, "role")
        content_node = (
            message.child_by_field_name("content")
            or _descendant_of_type(message, "text_inline")
            or _descendant_of_type(message, "unroled_message")
        )
        explicit = role_node is not None
        return MessageBlock(
            kind=cast(MessageBlockKind, _node_text(role_node).strip() if role_node is not None else "user"),
            text=_block_value_text(content_node) if content_node is not None else "",
            span=SourceSpan(syntax_source.original_line_number(message.start_point.row)),
            explicit=explicit,
        )
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


def _setting_from_node(
    node: Node,
    *,
    kind: MessageBlockKind,
    syntax_source: _TreeSitterSource,
) -> MessageBlock:
    text_ref = _descendant_of_type(node, "text_ref")
    if text_ref is not None:
        text = _node_text(text_ref).strip()
    else:
        text_node = node.child_by_field_name("text") or _descendant_of_type(node, "text_inline")
        text = _block_value_text(text_node) if text_node is not None else ""
    return MessageBlock(
        kind=kind,
        text=text,
        span=SourceSpan(syntax_source.original_line_number(node.start_point.row)),
        explicit=True,
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
    if node.type in {"text_inline", "condition"} and node.named_child_count:
        return _block_value_text(node.named_children[0])
    if node.type == "text_line":
        return _node_text(node).strip()
    if node.type == "text_block" and node.named_child_count:
        return _block_value_text(node.named_children[-1])
    if node.type in {"text_body", "unroled_message", "implicit_do_statement"}:
        return _text_body_node_text(node)
    if node.type == "text_body_line":
        content = node.child_by_field_name("content") or _descendant_of_type(node, "indented_raw_text")
        return _node_text(content).strip()
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


def _text_body_node_text(node: Node) -> str:
    lines: list[tuple[int, str]] = []
    for child in node.named_children:
        if child.type == "text_body_line":
            content = child.child_by_field_name("content") or _descendant_of_type(child, "indented_raw_text")
            lines.append((child.start_point.row + 1, _node_text(content).rstrip()))
        elif child.type == "blank_line":
            lines.append((child.start_point.row + 1, ""))
    return "\n".join(text for _, text in _dedent_line_items(lines)).strip()


def _properties_from_body(node: Node) -> dict[str, str]:
    properties: dict[str, str] = {}
    for child in node.named_children:
        if child.type != "property":
            continue
        key = _required_text(child, "key").strip()
        value = _required_text(child, "value").strip()
        properties[key] = value
    return properties


def _body_content_text(node: Node) -> str:
    for child in node.named_children:
        if child.type == "text_body":
            return _text_body_node_text(child)
    return ""


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


def _cap_semantics(
    *,
    kind: str,
    raw_body: str,
    frontmatter_present: bool,
    line_number: int,
) -> tuple[dict[str, Any], str, list[ParamDecl]]:
    if kind == "psyche":
        if frontmatter_present:
            raise ToolangError(f"Psyche cap at line {line_number} must not declare frontmatter.")
        return {}, raw_body.rstrip(), []
    if kind == "service":
        if not frontmatter_present:
            raise ToolangError(f"Service cap at line {line_number} is missing frontmatter.")
        return _service_cap(raw_body=raw_body, line_number=line_number)
    if kind == "prompt":
        return _prompt_cap(
            raw_body=raw_body,
            frontmatter_present=frontmatter_present,
            line_number=line_number,
        )
    raise ToolangError(f"Unsupported cap kind {kind!r} at line {line_number}.")


def _service_cap(*, raw_body: str, line_number: int) -> tuple[dict[str, Any], str, list[ParamDecl]]:
    post = frontmatter.loads(raw_body)
    meta = dict(post.metadata)
    validate_service_meta(meta, line_number=line_number)
    return meta, post.content.rstrip(), []


def _prompt_cap(
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
            f"Prompt cap at line {line_number} must define params as a string."
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
            f"{kind.capitalize()} cap at line {line_number} has unsupported frontmatter fields: {joined}."
        )


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
    tree_source = source if not source or source.endswith("\n") else f"{source}\n"
    return _TreeSitterSource(
        source=tree_source,
        line_map=tuple(range(len(original_lines) + 1)),
        synthetic_message_rows=frozenset(),
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
            rendered.append("in: Pack")
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
        type_name = _tree_sitter_type_name(match.group("type") or "Text")
        rendered.append(f"{name}{optional}: {type_name}")
    return ", ".join(rendered)


def _tree_sitter_type_name(type_name: str | None) -> str:
    if not type_name:
        return ""
    return type_name


def _ast_type_name(type_name: str | None) -> str | None:
    if not type_name:
        return None
    return type_name


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
        return _default_input_param(), []
    if not raw.strip():
        return None, []

    input_param: ParamDecl | None = None
    params: list[ParamDecl] = []
    for item in [part.strip() for part in raw.split(",")]:
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
        if name == "in":
            if input_param is not None:
                raise ToolangError(f"Parameter signature at line {line_number} repeats input parameter.")
            input_param = ParamDecl(name=name, optional=optional, type_name=type_name)
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
