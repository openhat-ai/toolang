"""Authored program source persisted in prepared locks."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import shlex
from re import Match
from typing import cast

from .ast import (
    AgicDecl,
    CapDecl,
    ContextDecl,
    FlowDecl,
    FlowStmt,
    InstructDecl,
    Message,
    Parameter,
    Program,
    Span,
    StructDecl,
    Directive,
    WithDecl,
    to_data,
)
from toolang.common.error import ToolangError

AGENT_HEADER_RE = re.compile(r"^agent\s+[A-Za-z_][\w-]*\s*$")
PROMPT_CALL_RE = re.compile(r"^/([A-Za-z_][\w-]*)(?:\s+(.*))?$")
TEMPLATE_VAR_RE = re.compile(r"\{\{\s*([A-Za-z_][\w-]*)\s*\}\}")


@dataclass(frozen=True, slots=True)
class ProgramSource:
    """Authored program text persisted with the agent lock."""

    agent_name: str
    source_path: str
    source_text: str
    body_text: str

    def parse(self) -> Program:
        return _parse_body_text(self.body_text)

    @classmethod
    def load(
        cls,
        path: Path,
        *,
        agent_name: str,
        source_path: str,
    ) -> "ProgramSource":
        """Load and validate one explicitly located program source."""

        source_text = (
            path.read_text(encoding="utf-8")
            if path.is_file()
            else f"agent {agent_name}\n"
        )
        body_text = _body_text(source_text)
        _parse_body_text(body_text)
        return cls(
            agent_name=agent_name,
            source_path=source_path,
            source_text=source_text,
            body_text=body_text,
        )

    def to_data(self) -> dict[str, object]:
        return {
            "agent_name": self.agent_name,
            "source_path": self.source_path,
            "source_text": self.source_text,
            "body_text": self.body_text,
        }

    def to_snapshot(self) -> dict[str, object]:
        program = self.parse()
        data: dict[str, object] = {
            "agent_name": self.agent_name,
            "source_path": self.source_path,
            "agics": [_agic_to_data(item) for item in program.available_agics],
        }
        if program.flows:
            data["flows"] = [_flow_to_data(item) for item in program.flows]
        return data

    def to_lock_data(self) -> dict[str, object]:
        program = self.parse()
        line_offset = _body_line_offset(
            source_text=self.source_text, body_text=self.body_text
        )
        data: dict[str, object] = {
            "source": "program",
            "source_text": self.source_text,
            "body_text": self.body_text,
            "uses": [
                _with_to_lock_data(item, line_offset=line_offset)
                for item in program.withs
            ],
            "structs": [
                _struct_to_lock_data(item, line_offset=line_offset)
                for item in program.structs
            ],
            "contexts": [
                _context_to_lock_data(item, line_offset=line_offset)
                for item in program.contexts
            ],
            "instructs": [
                _instruct_to_lock_data(item, line_offset=line_offset)
                for item in program.instructs
            ],
            "caps": [
                _cap_to_lock_data(item, line_offset=line_offset)
                for item in program.caps
            ],
            "agics": [
                _agic_to_lock_data(item, line_offset=line_offset)
                for item in program.available_agics
            ],
        }
        if program.flows:
            data["flows"] = [
                _flow_to_lock_data(item, line_offset=line_offset)
                for item in program.flows
            ]
        return data

    def fingerprint(self) -> str:
        payload = json.dumps(
            self.to_data(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return _sha256_text(payload)

    @classmethod
    def from_data(cls, data: dict[str, object]) -> "ProgramSource":
        return cls(
            agent_name=str(data.get("agent_name", "")),
            source_path=str(data.get("source_path", "")),
            source_text=str(data["source_text"]),
            body_text=str(data["body_text"]),
        )


def expand_program_input(program: Program, raw_input: str) -> str:
    """Expand a prompt directive in executable input."""

    if not raw_input:
        return raw_input
    lines = raw_input.splitlines()
    if not lines:
        return raw_input
    match = PROMPT_CALL_RE.match(lines[0].strip())
    if not match:
        return raw_input

    prompt_name = match.group(1)
    prompt_cap = next(
        (
            item
            for item in program.caps
            if item.kind == "prompt" and item.name == prompt_name
        ),
        None,
    )
    if prompt_cap is None:
        raise ToolangError(f"Prompt not found: {prompt_name}")

    bindings = _parse_prompt_args(
        match.group(2) or "",
        params=prompt_cap.params,
        prompt_name=prompt_name,
    )
    rendered = TEMPLATE_VAR_RE.sub(
        lambda item: _render_template_var(item, bindings),
        prompt_cap.body,
    ).strip()
    extra_lines = lines[1:]
    if extra_lines and not extra_lines[0].strip():
        extra_lines = extra_lines[1:]
    extra_text = "\n".join(extra_lines).strip("\n")
    if not extra_text:
        return rendered
    if not rendered:
        return extra_text
    return f"{rendered}\n\n{extra_text}"


def _body_text(source_text: str) -> str:
    lines = source_text.splitlines()
    if lines and lines[0].startswith("#!"):
        lines = lines[1:]
        while lines and not lines[0].strip():
            lines = lines[1:]
    if lines and AGENT_HEADER_RE.match(lines[0].strip()):
        return "\n".join(lines[1:]).lstrip("\n")
    if source_text.startswith("#!"):
        return "\n".join(lines).lstrip("\n")
    return source_text


def _parse_body_text(body_text: str) -> Program:
    if not body_text.strip():
        return Program(span=Span(line=1))
    return Program.from_source(body_text)


def _body_line_offset(*, source_text: str, body_text: str) -> int:
    body_lines = body_text.splitlines()
    if not body_lines:
        return 0
    source_lines = source_text.splitlines()
    body_len = len(body_lines)
    for index in range(0, len(source_lines) - body_len + 1):
        if source_lines[index : index + body_len] == body_lines:
            return index
    return 0


def _parse_prompt_args(
    raw_args: str,
    *,
    params: tuple[Parameter, ...],
    prompt_name: str,
) -> dict[str, str]:
    if not raw_args.strip():
        tokens: list[str] = []
    else:
        try:
            tokens = shlex.split(raw_args)
        except ValueError as exc:
            raise ToolangError(f"Invalid prompt argument syntax: {exc}") from exc

    bindings: dict[str, str] = {}
    positionals: list[str] = []
    known = {param.name for param in params}
    for token in tokens:
        if "=" in token:
            candidate, value = token.split("=", 1)
            if candidate in known:
                if candidate in bindings:
                    raise ToolangError(
                        f"Duplicate prompt argument {candidate!r} for /{prompt_name}."
                    )
                bindings[candidate] = value
                continue
        positionals.append(token)

    positional_index = 0
    for param in params:
        if param.name in bindings:
            continue
        if positional_index < len(positionals):
            bindings[param.name] = positionals[positional_index]
            positional_index += 1
            continue
        if param.optional:
            bindings[param.name] = ""
            continue
        raise ToolangError(
            f"Missing required prompt argument {param.name!r} for /{prompt_name}."
        )

    if positional_index < len(positionals):
        raise ToolangError(f"Too many prompt arguments for /{prompt_name}.")
    return bindings


def _render_template_var(match: Match[str], bindings: dict[str, str]) -> str:
    name = match.group(1)
    if name not in bindings:
        raise ToolangError(f"Unknown template variable {name!r}.")
    return bindings[name]


def _param_to_data(param: Parameter) -> dict[str, object]:
    return {
        "name": param.name,
        "optional": param.optional,
        "type_name": param.type_name,
    }


def _sha256_text(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _agic_to_data(agic: AgicDecl) -> dict[str, object]:
    return {
        "name": agic.name,
        "input": _param_to_data(agic.input) if agic.input is not None else None,
        "params": [_param_to_data(item) for item in agic.params],
        "output": agic.output,
        "directives": [_directive_to_data(item) for item in agic.directives],
        "context": agic.context,
        "instruct": agic.instruct,
        "messages": [_message_to_data(item) for item in agic.messages],
    }


def _flow_to_data(flow: FlowDecl) -> dict[str, object]:
    return {
        "name": flow.name,
        "input": _param_to_data(flow.input) if flow.input is not None else None,
        "params": [_param_to_data(item) for item in flow.params],
        "output": flow.output,
        "directives": [_directive_to_data(item) for item in flow.directives],
        "stmts": [_flow_stmt_to_data(item) for item in flow.stmts],
    }


def _flow_stmt_to_data(stmt: FlowStmt) -> dict[str, object]:
    return cast(dict[str, object], to_data(stmt))


def _directive_to_data(directive: Directive) -> dict[str, object]:
    return {
        "name": directive.name,
        "operator": directive.operator,
        "values": list(directive.values),
        "line": directive.span.line,
    }


def _message_to_data(message: Message) -> dict[str, object]:
    return {
        "role": message.role,
        "content": message.content,
        "explicit": message.explicit,
        "line": message.span.line,
    }


def _with_to_lock_data(use: WithDecl, *, line_offset: int) -> dict[str, object]:
    return {
        "kind": use.cap_kind,
        "ref": use.reference,
        "line": use.span.line + line_offset,
    }


def _struct_to_lock_data(struct: StructDecl, *, line_offset: int) -> dict[str, object]:
    return {
        "name": struct.name,
        "line": struct.span.line + line_offset,
        "fields": [
            {
                "name": field.name,
                "type": _source_type_name(field.type_name),
                "optional": False,
                "line": field.span.line + line_offset,
            }
            for field in struct.fields
        ],
    }


def _instruct_to_lock_data(
    instruct: InstructDecl, *, line_offset: int
) -> dict[str, object]:
    return {
        "name": instruct.name,
        "line": instruct.span.line + line_offset,
        "content": instruct.body,
    }


def _context_to_lock_data(
    context: ContextDecl, *, line_offset: int
) -> dict[str, object]:
    return {
        "name": context.name,
        "line": context.span.line + line_offset,
        "content": context.body,
    }


def _cap_to_lock_data(cap: CapDecl, *, line_offset: int) -> dict[str, object]:
    return {
        "kind": cap.kind,
        "name": cap.name,
        "line": cap.span.line + line_offset,
    }


def _agic_to_lock_data(agic: AgicDecl, *, line_offset: int) -> dict[str, object]:
    data: dict[str, object] = {
        "name": agic.name,
        "line": agic.span.line + line_offset,
        "params": _agic_params_to_lock_data(agic),
        "directives": [
            _directive_to_lock_data(item, line_offset=line_offset)
            for item in agic.directives
        ],
        "context": agic.context,
        "instruct": agic.instruct,
        "messages": [
            _message_to_lock_data(item, line_offset=line_offset)
            for item in agic.messages
        ],
    }
    if agic.output is not None:
        data["output"] = _source_type_name(agic.output)
    return data


def _flow_to_lock_data(flow: FlowDecl, *, line_offset: int) -> dict[str, object]:
    data: dict[str, object] = {
        "name": flow.name,
        "line": flow.span.line + line_offset,
        "params": _flow_params_to_lock_data(flow),
        "directives": [
            _directive_to_lock_data(item, line_offset=line_offset)
            for item in flow.directives
        ],
        "stmts": [
            _flow_stmt_to_lock_data(item, line_offset=line_offset)
            for item in flow.stmts
        ],
    }
    if flow.output is not None:
        data["output"] = _source_type_name(flow.output)
    return data


def _flow_params_to_lock_data(flow: FlowDecl) -> list[dict[str, object]]:
    params: list[dict[str, object]] = []
    if flow.input is not None:
        params.append(_param_to_lock_data(flow.input))
    params.extend(_param_to_lock_data(item) for item in flow.params)
    return params


def _flow_stmt_to_lock_data(stmt: FlowStmt, *, line_offset: int) -> dict[str, object]:
    data = _flow_stmt_to_data(stmt)
    data["span"] = {"line": stmt.span.line + line_offset}
    return data


def _agic_params_to_lock_data(agic: AgicDecl) -> list[dict[str, object]]:
    params: list[dict[str, object]] = []
    if agic.input is not None:
        params.append(_param_to_lock_data(agic.input))
    params.extend(_param_to_lock_data(item) for item in agic.params)
    return params


def _param_to_lock_data(param: Parameter) -> dict[str, object]:
    return {
        "name": param.name,
        "type": _source_type_name(param.type_name or "Text"),
        "optional": param.optional,
    }


def _directive_to_lock_data(
    directive: Directive, *, line_offset: int
) -> dict[str, object]:
    return {
        "key": directive.name,
        "op": directive.operator,
        "values": list(directive.values),
        "line": directive.span.line + line_offset,
    }


def _message_to_lock_data(message: Message, *, line_offset: int) -> dict[str, object]:
    return {
        "role": message.role,
        "content": message.content,
        "explicit": message.explicit,
        "line": message.span.line + line_offset,
    }


def _source_type_name(type_name: str | None) -> str:
    if not type_name:
        return "Text"
    return type_name


def _default_span() -> Span:
    return Span(0)
