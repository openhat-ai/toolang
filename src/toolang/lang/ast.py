"""Static semantic AST nodes for Toolang programs."""

from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass
from collections.abc import Mapping
from typing import Any, ClassVar, Literal

from toolang.common.error import ToolangError
from toolang.common.immutable import freeze_mapping

CapKind = Literal["psyche", "skill", "service", "prompt"]
JobKind = Literal["task", "chore"]
Role = Literal["user", "assistant", "tool"]
Position = Literal["first", "last"]
Limit = Literal["top", "bottom"]


@dataclass(frozen=True, slots=True)
class Span:
    line: int


@dataclass(frozen=True, slots=True, kw_only=True)
class Node:
    span: Span
    doc: str | None = None

    kind: ClassVar[str]


@dataclass(frozen=True, slots=True, kw_only=True)
class WithDecl(Node):
    kind: ClassVar[str] = "with"

    cap_kind: CapKind
    reference: str


@dataclass(frozen=True, slots=True, kw_only=True)
class Parameter(Node):
    kind: ClassVar[str] = "parameter"

    name: str
    optional: bool = False
    type_name: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class CapDecl(Node):
    kind: CapKind
    name: str
    body: str
    language: str | None = None
    meta: Mapping[str, Any] = field(default_factory=dict)
    params: tuple[Parameter, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "meta", freeze_mapping(self.meta))


@dataclass(frozen=True, slots=True, kw_only=True)
class JobDecl(Node):
    kind: JobKind
    name: str
    body: str
    meta: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "meta", freeze_mapping(self.meta))


@dataclass(frozen=True, slots=True, kw_only=True)
class ContextDecl(Node):
    kind: ClassVar[str] = "context"

    name: str
    body: str
    language: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class InstructDecl(Node):
    kind: ClassVar[str] = "instruct"

    name: str
    body: str
    language: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class Field(Node):
    kind: ClassVar[str] = "field"

    name: str
    type_name: str
    optional: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class StructDecl(Node):
    kind: ClassVar[str] = "struct"

    name: str
    fields: tuple[Field, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class Directive(Node):
    kind: ClassVar[str] = "directive"

    name: str
    operator: str
    values: tuple[str, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class Message(Node):
    kind: ClassVar[str] = "message"

    role: Role
    content: str
    explicit: bool = True


@dataclass(frozen=True, slots=True, kw_only=True)
class AgicDecl(Node):
    kind: ClassVar[str] = "agic"

    name: str
    input: Parameter | None = None
    params: tuple[Parameter, ...] = ()
    output: str | None = None
    directives: tuple[Directive, ...] = ()
    context: str | None = None
    instruct: str | None = None
    messages: tuple[Message, ...] = ()
    params_explicit: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class RunStmt(Node):
    kind: ClassVar[str] = "run"

    binding: str | None = "_"
    runnable: str


@dataclass(frozen=True, slots=True, kw_only=True)
class SeekStmt(Node):
    kind: ClassVar[str] = "seek"

    binding: str | None = "_"
    agent: str
    runnable: str


@dataclass(frozen=True, slots=True, kw_only=True)
class AskStmt(Node):
    kind: ClassVar[str] = "ask"

    binding: str | None = "_"
    body: str


@dataclass(frozen=True, slots=True, kw_only=True)
class ScatterStmt(Node):
    kind: ClassVar[str] = "scatter"

    binding: str | None = "_"
    count: int
    runnable: str


@dataclass(frozen=True, slots=True, kw_only=True)
class StormStmt(Node):
    kind: ClassVar[str] = "storm"

    binding: str | None = "_"
    count: int
    runnable: str
    par: int | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class GatherStmt(Node):
    kind: ClassVar[str] = "gather"

    binding: str | None = "_"
    runnable: str


@dataclass(frozen=True, slots=True, kw_only=True)
class SettleStmt(Node):
    kind: ClassVar[str] = "settle"

    binding: str | None = "_"
    runnable: str


@dataclass(frozen=True, slots=True, kw_only=True)
class MapStmt(Node):
    kind: ClassVar[str] = "map"

    binding: str | None = "_"
    runnable: str
    par: int | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class KeepStmt(Node):
    kind: ClassVar[str] = "keep"

    binding: str | None = "_"
    position: Position | None = None
    count: int | None = None
    predicate: str | None = None
    par: int | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class DropStmt(Node):
    kind: ClassVar[str] = "drop"

    binding: str | None = "_"
    position: Position | None = None
    count: int | None = None
    predicate: str | None = None
    par: int | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class RankStmt(Node):
    kind: ClassVar[str] = "rank"

    binding: str | None = "_"
    scorer: str
    limit: Limit | None = None
    count: int | None = None
    par: int | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class RepeatStmt(Node):
    kind: ClassVar[str] = "repeat"

    binding: str | None = "_"
    count: int | None = None
    stmts: tuple[FlowStmt, ...] = ()
    until: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class LetStmt(Node):
    kind: ClassVar[str] = "let"

    binding: str | None = "_"
    value: str


FlowStmt = (
    RunStmt
    | SeekStmt
    | AskStmt
    | ScatterStmt
    | StormStmt
    | GatherStmt
    | SettleStmt
    | MapStmt
    | KeepStmt
    | DropStmt
    | RankStmt
    | RepeatStmt
    | LetStmt
)


@dataclass(frozen=True, slots=True, kw_only=True)
class FlowDecl(Node):
    kind: ClassVar[str] = "flow"

    name: str
    input: Parameter | None = None
    params: tuple[Parameter, ...] = ()
    output: str | None = None
    directives: tuple[Directive, ...] = ()
    stmts: tuple[FlowStmt, ...] = ()
    params_explicit: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class Program(Node):
    kind: ClassVar[str] = "program"

    withs: tuple[WithDecl, ...] = ()
    caps: tuple[CapDecl, ...] = ()
    jobs: tuple[JobDecl, ...] = ()
    structs: tuple[StructDecl, ...] = ()
    contexts: tuple[ContextDecl, ...] = ()
    instructs: tuple[InstructDecl, ...] = ()
    agics: tuple[AgicDecl, ...] = ()
    flows: tuple[FlowDecl, ...] = ()

    @property
    def available_agics(self) -> tuple[AgicDecl, ...]:
        if any(agic.name == "default" for agic in self.agics):
            return self.agics
        return (*self.agics, _default_agic())

    def get_agic(self, name: str | None) -> AgicDecl:
        if name is not None:
            for agic in self.available_agics:
                if agic.name == name:
                    return agic
            raise ToolangError(f"Agic not found: {name}")
        for agic in self.available_agics:
            if agic.name == "default":
                return agic
        if len(self.available_agics) == 1:
            return self.available_agics[0]
        raise ToolangError("No default agic found in program.")

    def get_flow(self, name: str | None) -> FlowDecl:
        if name is not None:
            for flow in self.flows:
                if flow.name == name:
                    return flow
            raise ToolangError(f"Flow not found: {name}")
        for flow in self.flows:
            if flow.name == "main":
                return flow
        raise ToolangError("No default flow found in program.")

    def get_instruct(self, name: str | None) -> InstructDecl | None:
        selected = "default" if name is None else name
        return next((item for item in self.instructs if item.name == selected), None)

    def get_context(self, name: str | None) -> ContextDecl | None:
        selected = "default" if name is None else name
        return next((item for item in self.contexts if item.name == selected), None)

    @classmethod
    def from_source(cls, source: str) -> Program:
        from . import cst
        from .lower import lower
        from .validate import validate

        program = lower(cst.parse(source))
        validate(program)
        return program


def _default_agic() -> AgicDecl:
    return AgicDecl(
        name="default",
        input=Parameter(name="in", type_name="Pack", span=Span(line=1)),
        span=Span(line=1),
    )


def to_data(value: object) -> object:
    """Return a JSON-compatible representation of an AST value."""

    if isinstance(value, Node):
        return {
            "kind": value.kind,
            **{item.name: to_data(getattr(value, item.name)) for item in fields(value)},
        }
    if isinstance(value, Span):
        return {"line": value.line}
    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: to_data(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, tuple | list):
        return [to_data(item) for item in value]
    if isinstance(value, Mapping):
        return {str(key): to_data(item) for key, item in value.items()}
    return value
