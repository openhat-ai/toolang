"""Prepared and live program views."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
import shlex
from re import Match
from typing import cast

from ..agents import agent_program_path
from ..base.error import ToolangError
from ..program import DeclBlock, Program, Thunk, parse
from .durable import DurableState

DEFAULT_MODEL = "gpt-5"
DEFAULT_THUNK_PROMPT = "Respond helpfully, clearly, and directly to the user's message."
AGENT_HEADER_RE = re.compile(r"^agent\s+[A-Za-z_][\w-]*\s*$")
PROMPT_CALL_RE = re.compile(r"^/([A-Za-z_][\w-]*)(?:\s+(.*))?$")
TEMPLATE_VAR_RE = re.compile(r"\{\{\s*([A-Za-z_][\w-]*)\s*\}\}")


@dataclass(frozen=True, slots=True)
class ProgramThunk:
    """One prepared thunk definition."""

    name: str | None
    input_name: str | None
    output: str | None
    directives: tuple[str, ...]
    prompt: str

    def to_data(self) -> dict[str, object]:
        return {
            "name": self.name,
            "input_name": self.input_name,
            "output": self.output,
            "directives": list(self.directives),
            "prompt": self.prompt,
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> "ProgramThunk":
        raw_directives = data.get("directives", [])
        directives = raw_directives if isinstance(raw_directives, list) else []
        return cls(
            name=str(data["name"]) if data.get("name") is not None else None,
            input_name=(
                str(data["input_name"]) if data.get("input_name") is not None else None
            ),
            output=str(data["output"]) if data.get("output") is not None else None,
            directives=tuple(str(item) for item in directives),
            prompt=str(data["prompt"]),
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
            if thunk.name is None:
                return thunk
        if self.thunks:
            return self.thunks[0]
        raise ToolangError("No thunk found in prepared program.")

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
            raise ToolangError(f"Prompt template not found: {prompt_name}")

        args = _parse_prompt_args(
            match.group(2) or "",
            known={param.name for param in prompt_decl.params},
            prompt_name=prompt_name,
        )
        body_lines = lines[1:]
        if body_lines and not body_lines[0].strip():
            body_lines = body_lines[1:]
        bindings = {"input": "\n".join(body_lines).strip("\n")}

        for param in prompt_decl.params:
            if param.name in args:
                bindings[param.name] = args[param.name]
            elif param.optional:
                bindings[param.name] = ""
            else:
                raise ToolangError(
                    f"Missing required prompt argument {param.name!r} for /{prompt_name}."
                )
        return TEMPLATE_VAR_RE.sub(lambda item: _render_template_var(item, bindings), prompt_decl.body)

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
    if lines and AGENT_HEADER_RE.match(lines[0].strip()):
        return "\n".join(lines[1:]).lstrip("\n")
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
    return ProgramThunk(
        name=thunk.name,
        input_name=thunk.input_name,
        output=thunk.output,
        directives=tuple(thunk.directives),
        prompt=thunk.prompt,
    )


def _default_thunk() -> ProgramThunk:
    return ProgramThunk(
        name=None,
        input_name="user",
        output=None,
        directives=(),
        prompt=DEFAULT_THUNK_PROMPT,
    )


def _parse_prompt_args(raw_args: str, *, known: set[str], prompt_name: str) -> dict[str, str]:
    if not raw_args.strip():
        return {}
    try:
        tokens = shlex.split(raw_args)
    except ValueError as exc:
        raise ToolangError(f"Invalid prompt argument syntax: {exc}") from exc

    args: dict[str, str] = {}
    for token in tokens:
        if "=" not in token:
            raise ToolangError(f"Prompt argument must use key=value syntax: {token!r}")
        name, value = token.split("=", 1)
        if name not in known:
            raise ToolangError(f"Unknown prompt argument {name!r} for /{prompt_name}.")
        if name in args:
            raise ToolangError(f"Duplicate prompt argument {name!r} for /{prompt_name}.")
        args[name] = value
    return args


def _render_template_var(match: Match[str], bindings: dict[str, str]) -> str:
    name = match.group(1)
    if name not in bindings:
        raise ToolangError(f"Unknown template variable {name!r}.")
    return bindings[name]


def _validate_program(program: Program) -> None:
    for decl in program.declarations:
        _validate_decl_params(decl)
    for thunk in program.thunks:
        if thunk.prompt.strip():
            continue
        thunk_name = thunk.name or "<default>"
        raise ToolangError(f"Thunk {thunk_name!r} is missing prompt text.")


def _validate_decl_params(decl: DeclBlock) -> None:
    seen: set[str] = set()
    for param in decl.params:
        if param.name == "input":
            raise ToolangError(
                f"Prompt parameter name 'input' is reserved ({decl.kind} {decl.name})."
            )
        if param.name in seen:
            raise ToolangError(
                f"Duplicate prompt parameter {param.name!r} in {decl.kind} {decl.name}."
            )
        seen.add(param.name)


def _sha256_text(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode("utf-8")).hexdigest()
