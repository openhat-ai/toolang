"""Prepared and live program views."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
import shlex
from re import Match
from typing import cast

from ..agents import agent_program_path
from ..program import DeclBlock, ParamDecl, Program, Thunk, parse
from .durable import DurableState
from toolang.base.error import ToolangError

DEFAULT_MODEL = "gpt-5"
DEFAULT_THUNK_BODY = "Respond helpfully, clearly, and directly to the user's message."
AGENT_HEADER_RE = re.compile(r"^agent\s+[A-Za-z_][\w-]*\s*$")
PROMPT_CALL_RE = re.compile(r"^/([A-Za-z_][\w-]*)(?:\s+(.*))?$")
TEMPLATE_VAR_RE = re.compile(r"\{\{\s*([A-Za-z_][\w-]*)\s*\}\}")


@dataclass(frozen=True, slots=True)
class ProgramThunk:
    """One prepared thunk definition."""

    name: str
    accepts_message: bool
    params: tuple[ParamDecl, ...]
    returns: str | None
    directives: tuple[str, ...]
    body: str

    def to_data(self) -> dict[str, object]:
        return {
            "name": self.name,
            "accepts_message": self.accepts_message,
            "params": [_param_to_data(item) for item in self.params],
            "returns": self.returns,
            "directives": list(self.directives),
            "body": self.body,
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> "ProgramThunk":
        raw_directives = data.get("directives", [])
        directives = raw_directives if isinstance(raw_directives, list) else []
        raw_params = data.get("params", [])
        params = raw_params if isinstance(raw_params, list) else []
        return cls(
            name=_canonical_thunk_name(
                str(data["name"]) if data.get("name") is not None else None
            ),
            accepts_message=bool(data.get("accepts_message", False)),
            params=tuple(
                _param_from_data(cast(dict[str, object], item))
                for item in params
                if isinstance(item, dict)
            ),
            returns=str(data["returns"]) if data.get("returns") is not None else None,
            directives=tuple(str(item) for item in directives),
            body=str(data["body"]),
        )

    def model_selector(self) -> str | None:
        for directive in self.directives:
            match = re.match(r"^model\s*=\s*(.*)$", directive)
            if not match:
                continue
            raw = match.group(1).strip()
            if not raw or raw == "default":
                return None
            for candidate in [item.strip() for item in raw.split(",")]:
                if candidate and candidate != "default":
                    return candidate
        return None

    def model(self, *, override: str | None = None) -> str:
        if override:
            return override
        selector = self.model_selector()
        if selector is not None:
            return selector
        return DEFAULT_MODEL


@dataclass(frozen=True, slots=True)
class PreparedProgram:
    """Prepared program payload persisted with the agent lock."""

    agent_name: str
    source_path: str
    source_text: str
    body_text: str
    thunks: tuple[ProgramThunk, ...]

    def to_data(self) -> dict[str, object]:
        return {
            "agent_name": self.agent_name,
            "source_path": self.source_path,
            "source_text": self.source_text,
            "body_text": self.body_text,
            "thunks": [item.to_data() for item in self.thunks],
        }

    def to_snapshot(self) -> dict[str, object]:
        return {
            "agent_name": self.agent_name,
            "source_path": self.source_path,
            "thunks": [item.to_data() for item in self.thunks],
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
        raw_thunks = data.get("thunks", [])
        thunks = raw_thunks if isinstance(raw_thunks, list) else []
        return cls(
            agent_name=str(data["agent_name"]),
            source_path=str(data["source_path"]),
            source_text=str(data["source_text"]),
            body_text=str(data["body_text"]),
            thunks=tuple(
                ProgramThunk.from_data(cast(dict[str, object], item))
                for item in thunks
                if isinstance(item, dict)
            ),
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
    def thunks(self) -> tuple[ProgramThunk, ...]:
        return self.prepared.thunks

    def get_thunk(self, name: str | None) -> ProgramThunk:
        if name is not None:
            for thunk in self.thunks:
                if thunk.name == name:
                    return thunk
            raise ToolangError(f"Thunk not found: {name}")
        for thunk in self.thunks:
            if thunk.name == "main":
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
    parsed = _parse_body_text(body_text)
    thunks = tuple(_prepared_thunks(parsed))
    return PreparedProgram(
        agent_name=durable.agent_name,
        source_path=str(path.relative_to(durable.toolang_root)),
        source_text=source_text,
        body_text=body_text,
        thunks=thunks,
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


def _prepared_thunks(program: Program) -> list[ProgramThunk]:
    if not program.thunks:
        return [_default_thunk()]
    return [_prepared_thunk(item) for item in program.thunks]


def _prepared_thunk(thunk: Thunk) -> ProgramThunk:
    accepts_message, named_params = _canonical_thunk_params(thunk)
    return ProgramThunk(
        name=_canonical_thunk_name(thunk.name),
        accepts_message=accepts_message,
        params=tuple(named_params),
        returns=thunk.returns,
        directives=tuple(thunk.directives),
        body=thunk.body,
    )


def _default_thunk() -> ProgramThunk:
    return ProgramThunk(
        name="main",
        accepts_message=True,
        params=(),
        returns=None,
        directives=(),
        body=DEFAULT_THUNK_BODY,
    )


def _canonical_thunk_params(thunk: Thunk) -> tuple[bool, list[ParamDecl]]:
    if thunk.params_omitted:
        return True, []
    if not thunk.params:
        return False, []
    if thunk.params[0].message:
        return True, [item for item in thunk.params[1:]]
    return False, list(thunk.params)


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
        thunk_name = _canonical_thunk_name(thunk.name)
        if thunk_name in seen_thunk_names:
            raise ToolangError(f"Duplicate thunk name {thunk_name!r}.")
        seen_thunk_names.add(thunk_name)
        _validate_thunk_params(thunk, thunk_name=thunk_name)
        if thunk.body.strip():
            continue
        raise ToolangError(f"Thunk {thunk_name!r} is missing body text.")


def _validate_decl_params(decl: DeclBlock) -> None:
    seen: set[str] = set()
    for param in decl.params:
        if param.name in seen:
            raise ToolangError(
                f"Duplicate prompt parameter {param.name!r} in {decl.kind} {decl.name}."
            )
        seen.add(param.name)


def _validate_thunk_params(thunk: Thunk, *, thunk_name: str) -> None:
    seen: set[str] = set()
    for param in thunk.params:
        if param.message:
            continue
        if param.name in seen:
            raise ToolangError(f"Duplicate thunk parameter {param.name!r} in {thunk_name!r}.")
        seen.add(param.name)


def _param_to_data(param: ParamDecl) -> dict[str, object]:
    return {
        "name": param.name,
        "optional": param.optional,
        "type_name": param.type_name,
        "message": param.message,
    }


def _param_from_data(data: dict[str, object]) -> ParamDecl:
    return ParamDecl(
        name=str(data["name"]),
        optional=bool(data.get("optional", False)),
        type_name=str(data["type_name"]) if data.get("type_name") is not None else None,
        message=bool(data.get("message", False)),
    )


def _sha256_text(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_thunk_name(name: str | None) -> str:
    return name or "main"
