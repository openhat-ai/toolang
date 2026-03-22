"""Persisted synced-program representation for one agent source."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from toolang.syntax import DeclBlock, ParamDecl, Program, SourceSpan, Thunk, UseDecl


class ProgramUse(BaseModel):
    """Serialized form of one `use` declaration."""

    kind: str
    reference: str
    line: int

    def to_ast(self) -> UseDecl:
        """Rebuild the syntax node for this use declaration."""

        return UseDecl(kind=self.kind, reference=self.reference, span=SourceSpan(self.line))


class ProgramParam(BaseModel):
    """Serialized form of one declaration parameter."""

    name: str
    optional: bool = False

    def to_ast(self) -> ParamDecl:
        """Rebuild the syntax node for this parameter."""

        return ParamDecl(name=self.name, optional=self.optional)


class ProgramDecl(BaseModel):
    """Serialized form of one inline declaration block."""

    kind: str
    name: str
    language: str | None = None
    body: str = ""
    header_suffix: str = ""
    line: int
    params: list[ProgramParam] = Field(default_factory=list)

    def to_ast(self) -> DeclBlock:
        """Rebuild the syntax node for this declaration block."""

        return DeclBlock(
            kind=self.kind,
            name=self.name,
            language=self.language,
            body=self.body,
            header_suffix=self.header_suffix,
            span=SourceSpan(self.line),
            params=[param.to_ast() for param in self.params],
        )


class ProgramThunk(BaseModel):
    """Serialized form of one thunk definition."""

    name: str | None = None
    input_name: str | None = None
    output: str | None = None
    directives: list[str] = Field(default_factory=list)
    prompt: str = ""
    line: int

    def to_ast(self) -> Thunk:
        """Rebuild the syntax node for this thunk."""

        return Thunk(
            name=self.name,
            input_name=self.input_name,
            output=self.output,
            directives=list(self.directives),
            prompt=self.prompt,
            span=SourceSpan(self.line),
        )


class SyncedProgram(BaseModel):
    """Persisted program document used by synced runtime state."""

    uses: list[ProgramUse] = Field(default_factory=list)
    declarations: list[ProgramDecl] = Field(default_factory=list)
    thunks: list[ProgramThunk] = Field(default_factory=list)

    @classmethod
    def from_program(cls, program: Program) -> "SyncedProgram":
        """Build a persisted program document from parsed syntax objects."""

        return cls(
            uses=[
                ProgramUse(kind=item.kind, reference=item.reference, line=item.span.line)
                for item in program.uses
            ],
            declarations=[
                ProgramDecl(
                    kind=item.kind,
                    name=item.name,
                    language=item.language,
                    body=item.body,
                    header_suffix=item.header_suffix,
                    line=item.span.line,
                    params=[
                        ProgramParam(name=param.name, optional=param.optional)
                        for param in item.params
                    ],
                )
                for item in program.declarations
            ],
            thunks=[
                ProgramThunk(
                    name=item.name,
                    input_name=item.input_name,
                    output=item.output,
                    directives=list(item.directives),
                    prompt=item.prompt,
                    line=item.span.line,
                )
                for item in program.thunks
            ],
        )

    @classmethod
    def load(cls, path: Path) -> "SyncedProgram":
        """Load a synced program document from disk."""

        return cls.model_validate_json(path.read_text(encoding="utf-8"))

    def save(self, path: Path) -> None:
        """Write this synced program document to disk."""

        path.write_text(
            self.model_dump_json(indent=2, exclude_none=True),
            encoding="utf-8",
        )

    def to_program(self) -> Program:
        """Rebuild syntax objects from the persisted program document."""

        return Program(
            uses=[item.to_ast() for item in self.uses],
            declarations=[item.to_ast() for item in self.declarations],
            thunks=[item.to_ast() for item in self.thunks],
        )
