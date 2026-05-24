"""Prepared and live program views."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
import shlex
from re import Match

from ..agents import agent_program_path
from ..program import (
    DeclBlock,
    MessageBlock,
    ParamDecl,
    Program,
    SourceSpan,
    StructDecl,
    Thunk,
    ThunkOverlay,
    UseDecl,
    parse,
)
from .durable import DurableState
from toolang.base.error import ToolangError

DEFAULT_THUNK_BODY = "Respond helpfully, clearly, and directly to the user's message."
AGENT_HEADER_RE = re.compile(r"^agent\s+[A-Za-z_][\w-]*\s*$")
PROMPT_CALL_RE = re.compile(r"^/([A-Za-z_][\w-]*)(?:\s+(.*))?$")
TEMPLATE_VAR_RE = re.compile(r"\{\{\s*([A-Za-z_][\w-]*)\s*\}\}")


@dataclass(frozen=True, slots=True)
class PreparedProgram:
    """Prepared program payload persisted with the agent lock."""

    agent_name: str
    source_path: str
    source_text: str
    body_text: str

    def to_data(self) -> dict[str, object]:
        return {
            "agent_name": self.agent_name,
            "source_path": self.source_path,
            "source_text": self.source_text,
            "body_text": self.body_text,
        }

    def to_snapshot(self) -> dict[str, object]:
        program = _parse_body_text(self.body_text)
        return {
            "agent_name": self.agent_name,
            "source_path": self.source_path,
            "thunks": [_thunk_to_data(item) for item in _program_thunks(program)],
        }

    def to_lock_data(self) -> dict[str, object]:
        program = _parse_body_text(self.body_text)
        line_offset = _body_line_offset(source_text=self.source_text, body_text=self.body_text)
        return {
            "source": "program",
            "source_text": self.source_text,
            "body_text": self.body_text,
            "uses": [_use_to_lock_data(item, line_offset=line_offset) for item in program.uses],
            "structs": [_struct_to_lock_data(item, line_offset=line_offset) for item in program.structs],
            "instructs": [],
            "caps": [_decl_to_lock_data(item, line_offset=line_offset) for item in program.declarations],
            "thunks": [_thunk_to_lock_data(item, line_offset=line_offset) for item in _program_thunks(program)],
        }

    def fingerprint(self) -> str:
        payload = json.dumps(
            self.to_data(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return _sha256_text(payload)

    @classmethod
    def from_data(cls, data: dict[str, object]) -> "PreparedProgram":
        return cls(
            agent_name=str(data.get("agent_name", "")),
            source_path=str(data.get("source_path", "")),
            source_text=str(data["source_text"]),
            body_text=str(data["body_text"]),
        )


@dataclass(frozen=True, slots=True)
class LiveProgram:
    """In-memory program view used by prompt assembly."""

    prepared: PreparedProgram
    parsed: Program

    @property
    def source_path(self) -> str:
        return self.prepared.source_path

    @property
    def source_text(self) -> str:
        return self.prepared.source_text

    @property
    def body_text(self) -> str:
        return self.prepared.body_text

    @property
    def thunks(self) -> tuple[Thunk, ...]:
        return _program_thunks(self.parsed)

    def get_thunk(self, name: str | None) -> Thunk:
        if name is not None:
            for thunk in self.thunks:
                if _thunk_name(thunk) == name:
                    return thunk
            raise ToolangError(f"Thunk not found: {name}")
        for thunk in self.thunks:
            if _thunk_name(thunk) == "main":
                return thunk
        if len(self.thunks) == 1:
            return self.thunks[0]
        raise ToolangError("No default thunk found in prepared program.")

    def expand_input(self, raw_input: str) -> str:
        if not raw_input:
            return raw_input
        lines = raw_input.splitlines()
        if not lines:
            return raw_input
        first_line = lines[0].strip()
        match = PROMPT_CALL_RE.match(first_line)
        if not match:
            return raw_input

        prompt_name = match.group(1)
        prompt_decl = self.parsed.get_decl("prompt", prompt_name)
        if prompt_decl is None:
            raise ToolangError(f"Prompt not found: {prompt_name}")

        bindings = _parse_prompt_args(
            match.group(2) or "",
            params=prompt_decl.params,
            prompt_name=prompt_name,
        )
        rendered = TEMPLATE_VAR_RE.sub(
            lambda item: _render_template_var(item, bindings),
            prompt_decl.body,
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

    def to_snapshot(self) -> dict[str, object]:
        return self.prepared.to_snapshot()


def build_prepared_program(durable: DurableState) -> PreparedProgram:
    """Build one prepared program payload from durable source."""

    path = agent_program_path(durable.toolang_root, durable.agent_name)
    source_text = (
        path.read_text(encoding="utf-8")
        if path.is_file()
        else f"agent {durable.agent_name}\n"
    )
    body_text = _body_text(source_text)
    _parse_body_text(body_text)
    return PreparedProgram(
        agent_name=durable.agent_name,
        source_path=str(path.relative_to(durable.toolang_root)),
        source_text=source_text,
        body_text=body_text,
    )


def load_live_program(prepared: PreparedProgram) -> LiveProgram:
    """Load one live program view from prepared data."""

    return LiveProgram(prepared=prepared, parsed=_parse_body_text(prepared.body_text))


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
        return Program(_source_lines=[])
    program = parse(body_text)
    _validate_program(program)
    return program


def _program_thunks(program: Program) -> tuple[Thunk, ...]:
    if program.thunks:
        return tuple(program.thunks)
    return (_default_thunk(),)


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


def _default_thunk() -> Thunk:
    return Thunk(
        name="main",
        input=ParamDecl(name="_"),
        messages=(
            MessageBlock(
                kind="user",
                text=DEFAULT_THUNK_BODY,
                span=_default_span(),
                explicit=False,
            ),
        ),
    )


def _parse_prompt_args(
    raw_args: str,
    *,
    params: list[ParamDecl],
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


def _validate_program(program: Program) -> None:
    seen_decl_names: set[tuple[str, str]] = set()
    seen_struct_names: set[str] = set()
    seen_thunk_names: set[str] = set()

    for decl in program.declarations:
        decl_key = (decl.kind, decl.name)
        if decl_key in seen_decl_names:
            raise ToolangError(f"Duplicate {decl.kind} name {decl.name!r}.")
        seen_decl_names.add(decl_key)
        _validate_decl_params(decl)

    for struct in program.structs:
        if struct.name in seen_struct_names:
            raise ToolangError(f"Duplicate struct name {struct.name!r}.")
        seen_struct_names.add(struct.name)

    for thunk in program.thunks:
        thunk_name = _thunk_name(thunk)
        if thunk_name in seen_thunk_names:
            raise ToolangError(f"Duplicate thunk name {thunk_name!r}.")
        seen_thunk_names.add(thunk_name)
        _validate_thunk_params(thunk, thunk_name=thunk_name)
        _validate_thunk_overlays(thunk, thunk_name=thunk_name)
        _validate_thunk_messages(thunk, thunk_name=thunk_name)


def _validate_decl_params(decl: DeclBlock) -> None:
    seen: set[str] = set()
    for param in decl.params:
        if param.name in seen:
            raise ToolangError(
                f"Duplicate prompt parameter {param.name!r} in {decl.kind} {decl.name}."
            )
        seen.add(param.name)


def _validate_thunk_params(thunk: Thunk, *, thunk_name: str) -> None:
    if thunk.input is not None and thunk.input.name == "runtime":
        raise ToolangError(f"Thunk {thunk_name!r} must not use reserved parameter name 'runtime'.")
    seen: set[str] = set()
    for param in thunk.params:
        if param.name == "runtime":
            raise ToolangError(f"Thunk {thunk_name!r} must not use reserved parameter name 'runtime'.")
        if param.name in seen:
            raise ToolangError(f"Duplicate thunk parameter {param.name!r} in {thunk_name!r}.")
        seen.add(param.name)


def _validate_thunk_overlays(thunk: Thunk, *, thunk_name: str) -> None:
    model_overlays = [overlay for overlay in thunk.overlays if overlay.kind == "model"]
    if len(model_overlays) > 1:
        raise ToolangError(f"Thunk {thunk_name!r} may declare at most one models directive.")
    if not model_overlays:
        return
    overlay = model_overlays[0]
    if overlay.op != "set":
        raise ToolangError(f"Thunk {thunk_name!r} must use '=' for its models directive.")
    if not overlay.items:
        raise ToolangError(f"Thunk {thunk_name!r} must declare at least one model selector.")
    routed = [selector for selector in overlay.items if "@" in selector]
    if routed:
        joined = ", ".join(routed)
        raise ToolangError(
            f"Thunk {thunk_name!r} must declare route-neutral model refs, not routed selectors: {joined}"
        )


def _validate_thunk_messages(thunk: Thunk, *, thunk_name: str) -> None:
    if not thunk.messages or not any(block.text.strip() for block in thunk.messages):
        raise ToolangError(f"Thunk {thunk_name!r} is missing body text.")
    if thunk.is_thread_thunk():
        if thunk.input is not None:
            raise ToolangError(f"Thread thunk {thunk_name!r} must not declare an input parameter.")
        invalid = [block.kind for block in thunk.messages if block.kind != "system"]
        if invalid:
            joined = ", ".join(invalid)
            raise ToolangError(
                f"Thread thunk {thunk_name!r} may only declare system message blocks, not: {joined}."
            )
        if len(thunk.message_blocks("system")) > 1:
            raise ToolangError(f"Thread thunk {thunk_name!r} may declare at most one system block.")
        return

    system_count = len(thunk.message_blocks("system"))
    if system_count > 1:
        raise ToolangError(f"Thunk {thunk_name!r} may declare at most one system block.")
    user_count = len(thunk.message_blocks("user"))
    if user_count != 1:
        raise ToolangError(f"Thunk {thunk_name!r} must declare exactly one user block.")
    unsupported = [block.kind for block in thunk.messages if block.kind not in {"system", "user"}]
    if unsupported:
        joined = ", ".join(unsupported)
        raise ToolangError(
            f"Thunk {thunk_name!r} does not yet support message blocks: {joined}."
        )


def _param_to_data(param: ParamDecl) -> dict[str, object]:
    return {
        "name": param.name,
        "optional": param.optional,
        "type_name": param.type_name,
    }


def _sha256_text(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _thunk_name(thunk: Thunk) -> str:
    return thunk.name or "main"


def _thunk_to_data(thunk: Thunk) -> dict[str, object]:
    return {
        "name": _thunk_name(thunk),
        "input": _param_to_data(thunk.input) if thunk.input is not None else None,
        "params": [_param_to_data(item) for item in thunk.params],
        "output": thunk.output,
        "overlays": [_overlay_to_data(item) for item in thunk.overlays],
        "messages": [_message_block_to_data(item) for item in thunk.messages],
    }


def _overlay_to_data(overlay: ThunkOverlay) -> dict[str, object]:
    return {
        "kind": overlay.kind,
        "op": overlay.op,
        "items": list(overlay.items),
        "line": overlay.span.line,
    }


def _message_block_to_data(block: MessageBlock) -> dict[str, object]:
    return {
        "kind": block.kind,
        "text": block.text,
        "line": block.span.line,
        "explicit": block.explicit,
    }


def _use_to_lock_data(use: UseDecl, *, line_offset: int) -> dict[str, object]:
    return {
        "kind": use.kind,
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


def _decl_to_lock_data(decl: DeclBlock, *, line_offset: int) -> dict[str, object]:
    return {
        "kind": decl.kind,
        "name": decl.name,
        "line": decl.span.line + line_offset,
    }


def _thunk_to_lock_data(thunk: Thunk, *, line_offset: int) -> dict[str, object]:
    data: dict[str, object] = {
        "name": _thunk_name(thunk),
        "line": thunk.span.line + line_offset,
        "params": _thunk_params_to_lock_data(thunk),
        "directives": [_directive_to_lock_data(item, line_offset=line_offset) for item in thunk.overlays],
        "blocks": [_block_to_lock_data(item, line_offset=line_offset) for item in thunk.messages],
    }
    if thunk.output is not None:
        data["output"] = _source_type_name(thunk.output)
    return data


def _thunk_params_to_lock_data(thunk: Thunk) -> list[dict[str, object]]:
    params: list[dict[str, object]] = []
    if thunk.input is not None:
        params.append(
            {
                "name": "input" if thunk.input.name == "_" else thunk.input.name,
                "type": "Message",
                "optional": False,
            }
        )
    params.extend(_param_to_lock_data(item) for item in thunk.params)
    return params


def _param_to_lock_data(param: ParamDecl) -> dict[str, object]:
    return {
        "name": param.name,
        "type": _source_type_name(param.type_name or "string"),
        "optional": param.optional,
    }


def _directive_to_lock_data(overlay: ThunkOverlay, *, line_offset: int) -> dict[str, object]:
    return {
        "key": _directive_key(overlay.kind),
        "op": _directive_op(overlay.op),
        "values": list(overlay.items),
        "line": overlay.span.line + line_offset,
    }


def _block_to_lock_data(block: MessageBlock, *, line_offset: int) -> dict[str, object]:
    data: dict[str, object] = {
        "kind": block.kind,
        "line": block.span.line + line_offset,
    }
    if block.text in {"default", "none"}:
        data["value"] = block.text
    else:
        data["content"] = block.text
    if not block.explicit:
        data["explicit"] = False
    return data


def _directive_key(kind: str) -> str:
    if kind == "model":
        return "models"
    return f"{kind}s"


def _directive_op(op: str) -> str:
    return {"set": "=", "add": "+=", "remove": "-="}[op]


def _source_type_name(type_name: str | None) -> str:
    if not type_name:
        return "Text"
    suffix = "[]" if type_name.endswith("[]") else ""
    base = type_name[:-2] if suffix else type_name
    aliases = {
        "string": "Text",
        "text": "Text",
        "number": "Number",
        "boolean": "Boolean",
        "json": "Json",
        "message": "Message",
        "path": "Path",
        "artifact": "Artifact",
    }
    return f"{aliases.get(base, base)}{suffix}"


def _default_span() -> SourceSpan:
    return SourceSpan(0)
