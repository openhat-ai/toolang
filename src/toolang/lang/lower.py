"""Lower a Toolang concrete syntax tree into the static semantic AST."""

from __future__ import annotations

from dataclasses import replace
import re
from typing import TypeVar, cast

from tree_sitter import Node as CstNode

from . import ast
from .cst import Cst

_PROMPT_PARAM_RE = re.compile(r"^(?P<name>[A-Za-z_][\w-]*)(?P<optional>\?)?$")
_DECL_REF_RE = re.compile(r"^[A-Za-z_][\w-]*$")
_TRIVIA = {"blank_line", "comment_line", "line_end"}
NodeT = TypeVar("NodeT", bound=ast.Node)


def _node_line(node: NodeT) -> int:
    return node.span.line


def lower(cst: Cst) -> ast.Program:
    """Lower a checked CST without applying semantic validation."""

    return _Lowerer(cst).lower()


class _Lowerer:
    def __init__(self, cst: Cst) -> None:
        self.cst = cst
        self.withs: list[ast.WithDecl] = []
        self.caps: list[ast.CapDecl] = []
        self.jobs: list[ast.JobDecl] = []
        self.structs: list[ast.StructDecl] = []
        self.contexts: list[ast.ContextDecl] = []
        self.instructs: list[ast.InstructDecl] = []
        self.agics: list[ast.AgicDecl] = []
        self.flows: list[ast.FlowDecl] = []

    def lower(self) -> ast.Program:
        program_doc: list[str] = []
        pending_doc: list[str] = []
        for child in self.cst.tree.root_node.named_children:
            node = self._item(child)
            if node.type == "parent_doc_line":
                program_doc.append(self._doc_text(node))
                continue
            if node.type == "doc_line":
                pending_doc.append(self._doc_text(node))
                continue
            if node.type in _TRIVIA:
                pending_doc.clear()
                continue
            doc = self._pending_doc(pending_doc)
            pending_doc.clear()
            self._lower_item(node, doc=doc)

        return ast.Program(
            span=ast.Span(line=1),
            doc="\n".join(filter(None, program_doc)).strip() or None,
            withs=tuple(sorted(self.withs, key=_node_line)),
            caps=tuple(sorted(self.caps, key=_node_line)),
            jobs=tuple(sorted(self.jobs, key=_node_line)),
            structs=tuple(sorted(self.structs, key=_node_line)),
            contexts=tuple(sorted(self.contexts, key=_node_line)),
            instructs=tuple(sorted(self.instructs, key=_node_line)),
            agics=tuple(sorted(self.agics, key=_node_line)),
            flows=tuple(sorted(self.flows, key=_node_line)),
        )

    def _lower_item(self, node: CstNode, *, doc: str | None) -> None:
        if node.type == "with":
            self.withs.append(
                ast.WithDecl(
                    cap_kind=cast(ast.CapKind, self._required_text(node, "kind").strip()),
                    reference=self._required_text(node, "reference").strip(),
                    span=self._span(node),
                    doc=doc,
                )
            )
            return
        if node.type in {"psyche", "skill", "service", "prompt"}:
            self.caps.append(self._lower_cap(node, doc=doc))
            return
        if node.type in {"task", "chore"}:
            self.jobs.append(self._lower_job(node, doc=doc))
            return
        if node.type == "struct":
            self.structs.append(self._lower_struct(node, doc=doc))
            return
        if node.type == "context":
            self.contexts.append(self._lower_context(node, doc=doc, generated=False))
            return
        if node.type == "instruct":
            self.instructs.append(self._lower_instruct(node, doc=doc, generated=False))
            return
        if node.type == "agic":
            self.agics.append(self._lower_agic(node, doc=doc))
            return
        if node.type == "flow":
            self.flows.append(self._lower_flow(node, doc=doc))
            return
        raise RuntimeError(f"Unsupported Toolang CST item {node.type!r} at line {self._line(node)}.")

    def _lower_cap(self, node: CstNode, *, doc: str | None) -> ast.CapDecl:
        body = self._required(node, "body")
        meta = self._properties(body)
        params = (
            self._prompt_parameters(str(meta.get("params") or ""), span=self._span(node))
            if node.type == "prompt"
            else ()
        )
        return ast.CapDecl(
            kind=cast(ast.CapKind, node.type),
            name=self._required_text(node, "name").strip(),
            body=self._content_text(body),
            meta=meta,
            params=params,
            span=self._span(node),
            doc=doc,
        )

    def _lower_job(self, node: CstNode, *, doc: str | None) -> ast.JobDecl:
        body = self._required(node, "body")
        return ast.JobDecl(
            kind=cast(ast.JobKind, node.type),
            name=self._required_text(node, "name").strip(),
            body=self._content_text(body),
            meta=self._properties(body),
            span=self._span(node),
            doc=doc,
        )

    def _lower_struct(self, node: CstNode, *, doc: str | None) -> ast.StructDecl:
        body = self._required(node, "body")
        fields: list[ast.Field] = []
        pending_doc: list[str] = []
        for child in body.named_children:
            if child.type == "doc_line":
                pending_doc.append(self._doc_text(child))
                continue
            if child.type in _TRIVIA:
                pending_doc.clear()
                continue
            if child.type != "field":
                raise RuntimeError(f"Unsupported struct CST node {child.type!r} at line {self._line(child)}.")
            fields.append(
                ast.Field(
                    name=self._required_text(child, "name").strip(),
                    type_name=self._required_text(child, "type").strip(),
                    optional=child.child_by_field_name("optional") is not None,
                    span=self._span(child),
                    doc=self._pending_doc(pending_doc),
                )
            )
            pending_doc.clear()
        return ast.StructDecl(
            name=self._required_text(node, "name").strip(),
            fields=tuple(fields),
            span=self._span(node),
            doc=doc,
        )

    def _lower_context(
        self,
        node: CstNode,
        *,
        doc: str | None,
        generated: bool,
    ) -> ast.ContextDecl:
        return ast.ContextDecl(
            name=(
                self._generated_name("context", node)
                if generated
                else self._optional_text(node.child_by_field_name("name")) or "default"
            ),
            body=self._block_text(self._required(node, "body")),
            span=self._span(node),
            doc=doc,
        )

    def _lower_instruct(
        self,
        node: CstNode,
        *,
        doc: str | None,
        generated: bool,
    ) -> ast.InstructDecl:
        return ast.InstructDecl(
            name=(
                self._generated_name("instruct", node)
                if generated
                else self._optional_text(node.child_by_field_name("name")) or "default"
            ),
            body=self._block_text(self._required(node, "body")),
            span=self._span(node),
            doc=doc,
        )

    def _lower_agic(self, node: CstNode, *, doc: str | None) -> ast.AgicDecl:
        input_param, params, explicit = self._parameters(node.child_by_field_name("params"), owner=node)
        directives: list[ast.Directive] = []
        messages: list[ast.Message] = []
        context: str | None = None
        instruct: str | None = None
        body = self._required(node, "body")
        pending_doc: list[str] = []

        for child in body.named_children:
            if child.type == "doc_line":
                pending_doc.append(self._doc_text(child))
                continue
            if child.type in _TRIVIA or child.type == "parent_doc_line":
                pending_doc.clear()
                continue
            if child.type == "directive":
                directives.append(self._lower_directive(child))
                continue
            if child.type == "settings":
                for setting in child.named_children:
                    if setting.type == "context_setting":
                        context = self._lower_setting(setting, target="context")
                    elif setting.type == "instruct_setting":
                        instruct = self._lower_setting(setting, target="instruct")
                continue
            if child.type == "context_setting":
                context = self._lower_setting(child, target="context")
                continue
            if child.type == "instruct_setting":
                instruct = self._lower_setting(child, target="instruct")
                continue
            if child.type == "messages":
                messages.extend(self._lower_messages(child, initial_doc=self._pending_doc(pending_doc)))
                pending_doc.clear()
                continue
            if child.type == "message":
                messages.append(self._lower_message(child, doc=self._pending_doc(pending_doc)))
                pending_doc.clear()
                continue
            if child.type in {"pass_keyword", "pass_statement"}:
                continue
            raise RuntimeError(f"Unsupported agic CST node {child.type!r} at line {self._line(child)}.")

        return ast.AgicDecl(
            name=self._optional_text(node.child_by_field_name("name")) or "default",
            input=input_param,
            params=params,
            output=self._optional_text(node.child_by_field_name("return")),
            directives=tuple(directives),
            context=context,
            instruct=instruct,
            messages=tuple(messages),
            params_explicit=explicit,
            span=self._span(node),
            doc=doc,
        )

    def _lower_setting(self, node: CstNode, *, target: str) -> str:
        if ref := self._child_of_type(node, "text_ref"):
            return self._text(ref).strip()
        body = self._child_of_type(node, "text_inline")
        if body is None:
            raise RuntimeError(f"Missing inline {target} body at line {self._line(node)}.")
        text = self._block_text(body)
        if self._child_of_type(body, "text_line") and _DECL_REF_RE.fullmatch(text):
            return text
        if target == "context":
            decl = ast.ContextDecl(
                name=self._generated_name("context", node),
                body=text,
                span=self._span(node),
            )
            self.contexts.append(decl)
            return decl.name
        decl = ast.InstructDecl(
            name=self._generated_name("instruct", node),
            body=text,
            span=self._span(node),
        )
        self.instructs.append(decl)
        return decl.name

    def _lower_messages(self, node: CstNode, *, initial_doc: str | None = None) -> list[ast.Message]:
        messages: list[ast.Message] = []
        pending_doc = [initial_doc] if initial_doc else []
        for child in node.named_children:
            if child.type == "doc_line":
                pending_doc.append(self._doc_text(child))
                continue
            if child.type in _TRIVIA or child.type == "parent_doc_line":
                pending_doc.clear()
                continue
            if child.type != "message":
                raise RuntimeError(f"Unsupported message CST node {child.type!r} at line {self._line(child)}.")
            messages.append(self._lower_message(child, doc=self._pending_doc(pending_doc)))
            pending_doc.clear()
        return messages

    def _lower_message(self, node: CstNode, *, doc: str | None) -> ast.Message:
        role = self._child_of_type(node, "role")
        content = self._child_of_type(node, "text_inline") or self._child_of_type(node, "unroled_message")
        if content is None:
            raise RuntimeError(f"Missing message content at line {self._line(node)}.")
        return ast.Message(
            role=cast(ast.Role, self._text(role).strip() if role is not None else "user"),
            content=self._block_text(content),
            explicit=role is not None,
            span=self._span(node),
            doc=doc,
        )

    def _lower_flow(self, node: CstNode, *, doc: str | None) -> ast.FlowDecl:
        input_param, params, explicit = self._parameters(node.child_by_field_name("params"), owner=node)
        directives: list[ast.Directive] = []
        stmts: list[ast.FlowStmt] = []
        pending_doc: list[str] = []
        body = self._required(node, "body")
        for child in body.named_children:
            if child.type == "doc_line":
                pending_doc.append(self._doc_text(child))
                continue
            if child.type in _TRIVIA or child.type == "parent_doc_line":
                pending_doc.clear()
                continue
            if child.type == "directive":
                directives.append(self._lower_directive(child))
                continue
            if child.type == "statements":
                stmts.extend(self._lower_statements(child, initial_doc=self._pending_doc(pending_doc)))
                pending_doc.clear()
                continue
            if child.type in {"pass_keyword", "pass_statement"}:
                continue
            raise RuntimeError(f"Unsupported flow CST node {child.type!r} at line {self._line(child)}.")
        return ast.FlowDecl(
            name=self._optional_text(node.child_by_field_name("name")) or "main",
            input=input_param,
            params=params,
            output=self._optional_text(node.child_by_field_name("return")),
            directives=tuple(directives),
            stmts=tuple(stmts),
            params_explicit=explicit,
            span=self._span(node),
            doc=doc,
        )

    def _lower_statements(self, node: CstNode, *, initial_doc: str | None = None) -> list[ast.FlowStmt]:
        stmts: list[ast.FlowStmt] = []
        pending_doc = [initial_doc] if initial_doc else []
        for child in node.named_children:
            if child.type == "doc_line":
                pending_doc.append(self._doc_text(child))
                continue
            if child.type in _TRIVIA or child.type == "parent_doc_line":
                pending_doc.clear()
                continue
            stmts.append(self._lower_stmt(child, doc=self._pending_doc(pending_doc)))
            pending_doc.clear()
        return stmts

    def _lower_stmt(self, node: CstNode, *, doc: str | None) -> ast.FlowStmt:
        if node.type == "let_statement":
            if value := node.child_by_field_name("value"):
                return ast.LetStmt(
                    binding=self._required_text(node, "name").strip(),
                    value=self._block_text(value),
                    span=self._span(node),
                    doc=doc,
                )
            nested = self._required(node, "statement")
            stmt = self._lower_stmt(nested, doc=doc)
            binding = self._optional_text(node.child_by_field_name("name"))
            return replace(stmt, binding=binding)

        span = self._span(node)
        if node.type == "implicit_run_statement":
            runnable = self._generated_agic(node, body=self._block_text(node), output=None)
            return ast.RunStmt(runnable=runnable, span=span, doc=doc)
        if node.type == "run_statement":
            return ast.RunStmt(runnable=self._runnable(node), span=span, doc=doc)
        if node.type == "seek_statement":
            return ast.SeekStmt(
                agent=self._required_text(node, "agent").strip(),
                runnable=self._runnable(node),
                span=span,
                doc=doc,
            )
        if node.type == "ask_statement":
            return ast.AskStmt(body=self._block_text(self._required(node, "body")), span=span, doc=doc)
        if node.type == "scatter_statement":
            return ast.ScatterStmt(
                count=self._required_int(node, "count"),
                runnable=self._runnable(node),
                span=span,
                doc=doc,
            )
        if node.type == "storm_statement":
            return ast.StormStmt(
                count=self._required_int(node, "count"),
                runnable=self._runnable(node),
                par=self._par(node),
                span=span,
                doc=doc,
            )
        if node.type == "gather_statement":
            return ast.GatherStmt(runnable=self._runnable(node), span=span, doc=doc)
        if node.type == "settle_statement":
            return ast.SettleStmt(runnable=self._runnable(node), span=span, doc=doc)
        if node.type == "map_statement":
            return ast.MapStmt(runnable=self._runnable(node), par=self._par(node), span=span, doc=doc)
        if node.type in {"keep_statement", "drop_statement"}:
            position = self._child_of_type(node, "position_clause")
            statement = ast.KeepStmt if node.type == "keep_statement" else ast.DropStmt
            return statement(
                position=(
                    cast(ast.Position, self._required_text(position, "position").strip())
                    if position
                    else None
                ),
                count=self._required_int(position, "count") if position else None,
                predicate=None if position else self._runnable(node, output="Boolean"),
                par=self._par(node),
                span=span,
                doc=doc,
            )
        if node.type == "rank_statement":
            selection = self._child_of_type(node, "rank_selection_clause")
            return ast.RankStmt(
                scorer=self._runnable(node, output="Number"),
                limit=cast(ast.Limit, self._required_text(selection, "selection").strip()) if selection else None,
                count=self._required_int(selection, "count") if selection else None,
                par=self._par(node),
                span=span,
                doc=doc,
            )
        if node.type == "repeat_statement":
            body = self._required(node, "body")
            statements = self._child_of_type(body, "statements")
            if statements is None:
                raise RuntimeError(f"Missing repeat statements at line {self._line(node)}.")
            until_node = self._child_of_type(body, "until_statement")
            until = None
            if until_node is not None:
                agic = self._required(until_node, "agic")
                until = self._generated_agic(
                    agic,
                    body=self._block_text(self._required(agic, "body")),
                    output="Boolean",
                )
            return ast.RepeatStmt(
                count=self._optional_int(node.child_by_field_name("count")),
                stmts=tuple(self._lower_statements(statements)),
                until=until,
                span=span,
                doc=doc,
            )
        raise RuntimeError(f"Unsupported flow statement {node.type!r} at line {self._line(node)}.")

    def _runnable(self, node: CstNode, *, output: str | None = None) -> str:
        if runnable := node.child_by_field_name("runnable"):
            return self._text(runnable).strip()
        agic = node.child_by_field_name("agic")
        if agic is None:
            raise RuntimeError(f"Missing runnable at line {self._line(node)}.")
        declared_output = self._optional_text(agic.child_by_field_name("return"))
        return self._generated_agic(
            agic,
            body=self._block_text(self._required(agic, "body")),
            output=output or declared_output,
        )

    def _generated_agic(self, node: CstNode, *, body: str, output: str | None) -> str:
        name = self._generated_name("agic", node)
        self.agics.append(
            ast.AgicDecl(
                name=name,
                input=self._default_input(node),
                output=output,
                messages=(
                    ast.Message(role="user", content=body, span=self._span(node)),
                ),
                span=self._span(node),
            )
        )
        return name

    def _parameters(
        self,
        node: CstNode | None,
        *,
        owner: CstNode,
    ) -> tuple[ast.Parameter | None, tuple[ast.Parameter, ...], bool]:
        if node is None:
            return self._default_input(owner), (), False
        input_param: ast.Parameter | None = None
        params: list[ast.Parameter] = []
        for child in node.children_by_field_name("param"):
            param = ast.Parameter(
                name=self._required_text(child, "name").strip(),
                optional=child.child_by_field_name("optional") is not None,
                type_name=self._optional_text(child.child_by_field_name("type")),
                span=self._span(child),
            )
            if param.name == "in" and input_param is None:
                input_param = param
            else:
                params.append(param)
        return input_param, tuple(params), True

    def _default_input(self, owner: CstNode) -> ast.Parameter:
        return ast.Parameter(name="in", type_name="Pack", span=self._span(owner))

    def _prompt_parameters(self, raw: str, *, span: ast.Span) -> tuple[ast.Parameter, ...]:
        if not raw.strip():
            return ()
        params: list[ast.Parameter] = []
        for value in raw.split(","):
            item = value.strip()
            match = _PROMPT_PARAM_RE.fullmatch(item)
            params.append(
                ast.Parameter(
                    name=match.group("name") if match is not None else item,
                    optional=match is not None and match.group("optional") is not None,
                    span=span,
                )
            )
        return tuple(params)

    def _lower_directive(self, node: CstNode) -> ast.Directive:
        raw = self._required_text(node, "value")
        return ast.Directive(
            name=self._required_text(node, "key").strip(),
            operator=self._required_text(node, "operator").strip(),
            values=tuple(item for item in (part.strip() for part in raw.split(",")) if item),
            span=self._span(node),
        )

    def _properties(self, node: CstNode) -> dict[str, str]:
        return {
            self._required_text(child, "key").strip(): self._required_text(child, "value").strip()
            for child in node.named_children
            if child.type == "property"
        }

    def _content_text(self, node: CstNode) -> str:
        body = self._child_of_type(node, "text_body")
        return self._block_text(body) if body is not None else ""

    def _block_text(self, node: CstNode) -> str:
        if node.type in {"context_body", "instruct_body", "text_inline", "text_block"}:
            child = next(
                (
                    item
                    for item in node.named_children
                    if item.type in {"text_inline", "text_line", "text_block", "text_body"}
                ),
                None,
            )
            return self._block_text(child) if child is not None else ""
        if node.type == "text_line":
            return self._text(node).strip()
        if node.type in {"text_body", "unroled_message", "implicit_run_statement"}:
            lines: list[str] = []
            for child in node.named_children:
                if child.type == "text_body_line":
                    content = child.child_by_field_name("content") or self._child_of_type(child, "indented_raw_text")
                    lines.append(self._text(content).rstrip())
                elif child.type == "blank_line":
                    lines.append("")
            return self._dedent(lines)
        if node.type == "text_body_line":
            content = node.child_by_field_name("content") or self._child_of_type(node, "indented_raw_text")
            return self._text(content).strip()
        return self._text(node).strip()

    @staticmethod
    def _dedent(lines: list[str]) -> str:
        non_blank = [line for line in lines if line.strip()]
        if not non_blank:
            return ""
        indent = min(len(line) - len(line.lstrip(" \t")) for line in non_blank)
        return "\n".join(line[indent:].rstrip() if line.strip() else "" for line in lines).strip()

    def _par(self, node: CstNode) -> int | None:
        clause = self._child_of_type(node, "par_clause")
        return self._required_int(clause, "limit") if clause is not None else None

    def _required_int(self, node: CstNode, field: str) -> int:
        return int(self._required_text(node, field).strip())

    def _optional_int(self, node: CstNode | None) -> int | None:
        return int(self._text(node).strip()) if node is not None else None

    def _generated_name(self, kind: str, node: CstNode) -> str:
        return f"<{kind}:{self._line(node)}>"

    def _span(self, node: CstNode) -> ast.Span:
        return ast.Span(line=self._line(node))

    @staticmethod
    def _line(node: CstNode) -> int:
        return node.start_point.row + 1

    def _text(self, node: CstNode | None) -> str:
        if node is None:
            return ""
        return self.cst.source[node.start_byte : node.end_byte].decode("utf-8")

    def _required_text(self, node: CstNode, field: str) -> str:
        return self._text(self._required(node, field))

    def _optional_text(self, node: CstNode | None) -> str | None:
        text = self._text(node).strip()
        return text or None

    @staticmethod
    def _required(node: CstNode, field: str) -> CstNode:
        child = node.child_by_field_name(field)
        if child is None:
            raise RuntimeError(f"Missing CST field {field!r} at line {node.start_point.row + 1}.")
        return child

    @staticmethod
    def _child_of_type(node: CstNode, node_type: str) -> CstNode | None:
        for child in node.named_children:
            if child.type == node_type:
                return child
        return None

    @staticmethod
    def _item(node: CstNode) -> CstNode:
        return node.named_children[0] if node.type == "item" else node

    def _doc_text(self, node: CstNode) -> str:
        text = self._text(node).strip()
        return text.removeprefix("##!").removeprefix("##").strip()

    @staticmethod
    def _pending_doc(items: list[str]) -> str | None:
        return "\n".join(item for item in items if item).strip() or None
