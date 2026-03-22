from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, cast

from toolang.concepts.caps import CapContent, CapKind, CapParam
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
    _source_lines: list[str] | None = field(default=None, repr=False, compare=False)

    @classmethod
    def load(cls, path: Path) -> "Program":
        """Load one authored Toolang program from disk."""

        if not path.exists():
            return cls(_source_lines=[])

        from .parser import parse

        return parse(path.read_text(encoding="utf-8"))

    def save(self, path: Path) -> None:
        """Write this program back to disk."""

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_source(), encoding="utf-8")

    def uses_by_kind(self, kind: str) -> list[UseDecl]:
        return [item for item in self.uses if item.kind == kind]

    def has_use(self, kind: str, reference: str) -> bool:
        return any(item.kind == kind and item.reference == reference for item in self.uses)

    def declarations_by_kind(self, kind: str) -> list[DeclBlock]:
        return [item for item in self.declarations if item.kind == kind]

    def declared_caps(self) -> list[CapContent]:
        """Return authored capability declarations defined in this program."""

        caps: list[CapContent] = []
        for declaration in self.declarations:
            if declaration.kind not in {"service", "prompt", "psyche"}:
                continue
            kind = cast(CapKind, declaration.kind)
            caps.append(
                CapContent(
                    kind=kind,
                    name=declaration.name,
                    language=declaration.language,
                    raw_text=declaration.body,
                    params=[
                        CapParam(name=param.name, optional=param.optional)
                        for param in declaration.params
                    ],
                )
            )
        return caps

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

    def add_cap_ref(self, kind: CapKind, ref: str) -> bool:
        """Add one `use <kind> <ref>` statement to this program."""

        name = _cap_name_from_ref(ref)
        if self.has_use(kind, ref):
            return False

        for use in self.uses_by_kind(kind):
            if _cap_name_from_ref(use.reference) == name:
                raise ToolangError(
                    f"{kind.title()} {name!r} is already referenced as {use.reference!r}."
                )

        lines = self._editable_lines()
        use_line = f"use {kind} {ref}"

        if not lines:
            self._replace_from_source_lines([use_line])
            return True

        use_indexes = [index for index, line in enumerate(lines) if _is_cap_use_line(line)]
        insert_at = use_indexes[-1] + 1 if use_indexes else _leading_header_length(lines)

        updated = list(lines)
        updated.insert(insert_at, use_line)
        if insert_at == 0 and len(updated) > 1 and updated[1].strip():
            updated.insert(1, "")

        self._replace_from_source_lines(updated)
        return True

    def remove_cap_ref(
        self,
        kind: CapKind,
        name: str,
        *,
        delete_when_empty: bool = False,
    ) -> bool:
        """Remove `use` statements for one capability name."""

        lines = self._editable_lines()
        if not lines:
            return False

        remove_indexes = {
            use.span.line - 1
            for use in self.uses_by_kind(kind)
            if _cap_name_from_ref(use.reference) == name
        }
        if not remove_indexes:
            return False

        updated = [line for index, line in enumerate(lines) if index not in remove_indexes]
        while updated and not updated[0].strip():
            updated.pop(0)
        while len(updated) >= 2 and not updated[0].strip() and not updated[1].strip():
            updated.pop(0)
        while len(updated) >= 2 and not updated[-1].strip() and not updated[-2].strip():
            updated.pop()

        if delete_when_empty and not updated:
            self.uses = []
            self.declarations = []
            self.thunks = []
            self._source_lines = []
            return True

        self._replace_from_source_lines(updated)
        return True

    def validate(self) -> None:
        self._validate_declarations()
        self._validate_thunks()

    def to_dict(self) -> dict[str, Any]:
        return {
            "uses": [asdict(item) for item in self.uses],
            "declarations": [asdict(item) for item in self.declarations],
            "thunks": [asdict(item) for item in self.thunks],
        }

    def to_source(self) -> str:
        """Render this program back to Toolang source text."""

        lines = self._render_source_lines()
        if not lines:
            return ""
        return "\n".join(lines).rstrip() + "\n"

    def _editable_lines(self) -> list[str]:
        if self._source_lines is not None:
            return list(self._source_lines)
        return _render_program_lines(self)

    def _render_source_lines(self) -> list[str]:
        if self._source_lines is None:
            return _render_program_lines(self)

        from .parser import parse

        source_text = "\n".join(self._source_lines)
        reparsed = parse(source_text)
        if reparsed.to_dict() == self.to_dict():
            return list(self._source_lines)
        return _render_program_lines(self)

    def _replace_from_source_lines(self, lines: list[str]) -> None:
        if not lines:
            self.uses = []
            self.declarations = []
            self.thunks = []
            self._source_lines = []
            return

        from .parser import parse

        reparsed = parse("\n".join(lines))
        self.uses = reparsed.uses
        self.declarations = reparsed.declarations
        self.thunks = reparsed.thunks
        self._source_lines = list(lines)

    def _validate_declarations(self) -> None:
        seen: set[tuple[str, str]] = set()
        for declaration in self.declarations:
            key = (declaration.kind, declaration.name)
            if key in seen:
                raise ToolangError(
                    f"Duplicate {declaration.kind} declaration {declaration.name!r} at line {declaration.span.line}."
                )
            seen.add(key)

    def _validate_thunks(self) -> None:
        if not self.thunks:
            raise ToolangError("No thunk found in source.")

        seen_named: set[str] = set()
        default_seen = False
        structs = {declaration.name for declaration in self.declarations_by_kind("struct")}

        for thunk in self.thunks:
            if thunk.name is None:
                if default_seen:
                    raise ToolangError(f"Duplicate default thunk at line {thunk.span.line}.")
                default_seen = True
            else:
                if thunk.name in seen_named:
                    raise ToolangError(f"Duplicate thunk {thunk.name!r} at line {thunk.span.line}.")
                seen_named.add(thunk.name)

            if thunk.output and thunk.output not in structs:
                raise ToolangError(
                    f"Thunk {thunk.name or '<default>'} refers to unknown output struct {thunk.output!r} at line {thunk.span.line}."
                )


def _cap_name_from_ref(ref: str) -> str:
    owner, sep, name = ref.partition("/")
    if not owner or not sep or not name:
        raise ToolangError(f"Capability ref must look like owner/name: {ref}")
    return name


def _is_cap_use_line(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith("use ") and len(stripped.split()) == 3


def _leading_header_length(lines: list[str]) -> int:
    index = 0
    while index < len(lines):
        stripped = lines[index].strip()
        if not stripped or stripped.startswith("#"):
            index += 1
            continue
        break
    return index


def _render_program_lines(program: Program) -> list[str]:
    lines: list[str] = []

    for use in program.uses:
        lines.append(f"use {use.kind} {use.reference}")

    if program.uses and (program.declarations or program.thunks):
        lines.append("")

    declaration_blocks = [_render_declaration(item) for item in program.declarations]
    thunk_blocks = [_render_thunk(item) for item in program.thunks]
    blocks = declaration_blocks + thunk_blocks

    for index, block in enumerate(blocks):
        if index > 0:
            lines.append("")
        lines.extend(block)

    return lines


def _render_declaration(declaration: DeclBlock) -> list[str]:
    params = ""
    if declaration.params:
        rendered_params = []
        for param in declaration.params:
            rendered_params.append(f"{param.name}{'?' if param.optional else ''}")
        params = "(" + ", ".join(rendered_params) + ")"

    if declaration.language is None:
        return [f"{declaration.kind} {declaration.name}{params}:"]

    header = f"{declaration.kind} {declaration.name}{params}: ```{declaration.language}"
    lines = [header]
    body_lines = declaration.body.splitlines() if declaration.body else []
    lines.extend(body_lines)
    lines.append("```")
    return lines


def _render_thunk(thunk: Thunk) -> list[str]:
    header = "thunk"
    if thunk.name is not None:
        header += f" {thunk.name}"

    if thunk.input_name is not None:
        header += f"({thunk.input_name})"

    if thunk.output is not None:
        header += f" -> {thunk.output}"

    header += ":"
    lines = [header]
    lines.extend(f"    {directive}" for directive in thunk.directives)

    prompt_lines = thunk.prompt.splitlines() or [""]
    lines.extend(f"    {line}" if line else "    " for line in prompt_lines)
    return lines
