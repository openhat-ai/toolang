from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from toolang.errors import ToolangError


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


@dataclass(slots=True)
class DeclBlock:
    kind: str
    name: str
    language: str | None
    body: str
    header_suffix: str
    span: SourceSpan
    params: list[ParamDecl] = field(default_factory=list)


@dataclass(slots=True)
class Thunk:
    name: str | None
    input_name: str | None
    output: str | None
    directives: list[str] = field(default_factory=list)
    prompt: str = ""
    span: SourceSpan = field(default_factory=lambda: SourceSpan(0))


@dataclass(slots=True)
class Program:
    uses: list[UseDecl] = field(default_factory=list)
    declarations: list[DeclBlock] = field(default_factory=list)
    thunks: list[Thunk] = field(default_factory=list)

    def uses_by_kind(self, kind: str) -> list[UseDecl]:
        return [item for item in self.uses if item.kind == kind]

    def has_use(self, kind: str, reference: str) -> bool:
        return any(item.kind == kind and item.reference == reference for item in self.uses)

    def declarations_by_kind(self, kind: str) -> list[DeclBlock]:
        return [item for item in self.declarations if item.kind == kind]

    def get_decl(self, kind: str, name: str) -> DeclBlock | None:
        for item in self.declarations:
            if item.kind == kind and item.name == name:
                return item
        return None

    def default_thunk(self) -> Thunk:
        for thunk in self.thunks:
            if thunk.name is None:
                return thunk
        if self.thunks:
            return self.thunks[0]
        raise ToolangError("No thunk found in source.")

    def get_thunk(self, name: str | None) -> Thunk:
        if name is None:
            return self.default_thunk()
        for thunk in self.thunks:
            if thunk.name == name:
                return thunk
        raise ToolangError(f"Thunk not found: {name}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "uses": [asdict(item) for item in self.uses],
            "declarations": [asdict(item) for item in self.declarations],
            "thunks": [asdict(item) for item in self.thunks],
        }
