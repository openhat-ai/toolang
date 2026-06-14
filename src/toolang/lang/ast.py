"""Semantic AST nodes for Toolang programs."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

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


@dataclass(slots=True)
class SourceSpan:
    line: int


@dataclass(slots=True)
class UseDecl:
    kind: str
    reference: str
    span: SourceSpan
    doc: str | None = None


@dataclass(slots=True)
class ParamDecl:
    name: str
    optional: bool = False
    type_name: str | None = None


@dataclass(slots=True)
class CapDecl:
    kind: str
    name: str
    body: str
    span: SourceSpan
    language: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    params: list[ParamDecl] = field(default_factory=list)
    doc: str | None = None


@dataclass(slots=True)
class WorkDecl:
    kind: str
    name: str
    body: str
    span: SourceSpan
    meta: dict[str, Any] = field(default_factory=dict)
    doc: str | None = None


@dataclass(slots=True)
class InstructBlock:
    name: str | None
    body: str
    span: SourceSpan
    language: str | None = None
    doc: str | None = None


@dataclass(slots=True)
class ContextBlock:
    name: str | None
    body: str
    span: SourceSpan
    language: str | None = None
    doc: str | None = None


@dataclass(slots=True)
class StructFieldDecl:
    name: str
    type_name: str
    span: SourceSpan
    optional: bool = False
    doc: str | None = None


@dataclass(slots=True)
class StructDecl:
    name: str
    fields: list[StructFieldDecl]
    span: SourceSpan
    doc: str | None = None


@dataclass(frozen=True, slots=True)
class Directive:
    name: str
    operator: str
    values: tuple[str, ...]
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
    directives: tuple[Directive, ...] = ()
    context: MessageBlock | None = None
    instruct: MessageBlock | None = None
    messages: tuple[MessageBlock, ...] = ()
    span: SourceSpan = field(default_factory=lambda: SourceSpan(0))
    doc: str | None = None
    params_explicit: bool = False

    def thunk_name(self) -> str:
        return self.name or "default"

    def is_thread_thunk(self) -> bool:
        return self.thunk_name() in {"chat", "task", "chore", "file"}

    def directives_for(self, name: str) -> tuple[Directive, ...]:
        return tuple(item for item in self.directives if _directive_family(item.name) == name)

    def message_blocks(self, kind: MessageBlockKind) -> tuple[MessageBlock, ...]:
        blocks: list[MessageBlock] = []
        if self.context is not None and kind == "context":
            blocks.append(self.context)
        if self.instruct is not None and kind == "instruct":
            blocks.append(self.instruct)
        blocks.extend(item for item in self.messages if item.kind == kind)
        return tuple(blocks)

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
    directives: tuple[Directive, ...] = ()
    stages: tuple[FlowStage, ...] = ()
    span: SourceSpan = field(default_factory=lambda: SourceSpan(0))
    doc: str | None = None
    params_explicit: bool = False

    def flow_name(self) -> str:
        return self.name or "main"

    def directives_for(self, name: str) -> tuple[Directive, ...]:
        return tuple(item for item in self.directives if _directive_family(item.name) == name)


@dataclass(slots=True)
class Program:
    doc: str | None = None
    uses: list[UseDecl] = field(default_factory=list)
    caps: list[CapDecl] = field(default_factory=list)
    tasks: list[WorkDecl] = field(default_factory=list)
    chores: list[WorkDecl] = field(default_factory=list)
    structs: list[StructDecl] = field(default_factory=list)
    contexts: list[ContextBlock] = field(default_factory=list)
    instructs: list[InstructBlock] = field(default_factory=list)
    thunks: list[Thunk] = field(default_factory=list)
    flows: list[Flow] = field(default_factory=list)
    _source_lines: list[str] | None = field(default=None, repr=False, compare=False)

    def get_cap(self, kind: str, name: str) -> CapDecl | None:
        for item in self.caps:
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
        return "hands"
    if normalized == "handoffs":
        return "handoffs"
    if normalized == "recall":
        return "recall"
    return normalized
