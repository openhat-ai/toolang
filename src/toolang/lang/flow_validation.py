"""Conservative, intraprocedural checks using already determined signatures."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from toolang.common.template import require_template_inputs, template_history_depth

from . import ast
from .contracts import operation_transform
from .errors import ToolangValidationError, source_location


@dataclass(frozen=True)
class _Local:
    # Full value type: Text[][] for a list of Text[] elements.
    type_name: str | None = None
    shape: Literal["item", "list"] | None = None
    empty: bool | None = None


_Locals = dict[str, _Local]
_Runnables = Mapping[str, ast.AgicDecl | ast.FlowDecl]


def validate_flows(program: ast.Program, runnables: _Runnables) -> None:
    checker = _FlowChecker(program, runnables)
    for flow in program.flows:
        locals = {p.name: _Local(p.type_name, "item") for p in flow.params}
        if flow.input is not None:
            locals["_"] = _Local(flow.input.type_name, "item")
        checker.statements(
            flow.stmts,
            locals,
            window=None,
            settings={
                "instruct": flow.instruct,
                "context": flow.context,
            },
        )


def _join(left: _Locals, right: _Locals) -> _Locals:
    """Widen differing or conditionally present values to unknown, never absent."""
    result: _Locals = {}
    for name in left.keys() | right.keys():
        a, b = left.get(name, _Local()), right.get(name, _Local())
        result[name] = _Local(
            a.type_name if a.type_name == b.type_name else None,
            a.shape if a.shape == b.shape else None,
            a.empty if a.empty == b.empty else None,
        )
    return result


class _FlowChecker:
    def __init__(self, program: ast.Program, runnables: _Runnables):
        self.program = program
        self.runnables = runnables

    def content(self, text: str, locals: _Locals, window: int | None) -> None:
        require_template_inputs(text, locals)
        if window is not None:
            template_history_depth(text, window)

    def call(
        self,
        runnable: ast.AgicDecl | ast.FlowDecl,
        locals: _Locals,
        window: int | None,
        settings: Mapping[str, str | None],
    ) -> None:
        for parameter in (
            *runnable.params,
            *((runnable.input,) if runnable.input else ()),
        ):
            if not parameter.optional and parameter.name not in locals:
                raise ToolangValidationError(
                    f"missing input {parameter.name!r} for {runnable.name or 'inline agic'}"
                )
        # Flow signatures are checked locally. Do not descend through callees.
        if not isinstance(runnable, ast.AgicDecl) or window is None:
            return
        templates = [message.content for message in runnable.messages]
        for kind, declarations in (
            ("instruct", self.program.instructs),
            ("context", self.program.contexts),
        ):
            own = getattr(runnable, kind)
            selection = own if own is not None else settings.get(kind)
            # An omitted inherited selection may belong to another module.
            if selection is not None and selection != "none":
                templates.extend(d.body for d in declarations if d.name == selection)
        for template in templates:
            template_history_depth(template, window)

    def statements(
        self,
        statements: tuple[ast.FlowStmt, ...],
        locals: _Locals,
        *,
        window: int | None,
        settings: Mapping[str, str | None],
    ) -> _Locals:
        locals = dict(locals)
        for stmt in statements:
            with source_location(stmt.span.line):
                if isinstance(stmt, ast.RepeatStmt):
                    locals = self.repeat(stmt, locals, settings)
                    continue
                result = self.statement(stmt, locals, window, settings)
                if stmt.binding is not None:
                    locals[stmt.binding] = result
        return locals

    def repeat(
        self,
        stmt: ast.RepeatStmt,
        locals: _Locals,
        settings: Mapping[str, str | None],
    ) -> _Locals:
        if stmt.count == 0:
            return locals

        def body(entry: _Locals) -> _Locals:
            result = self.statements(
                stmt.stmts, entry, window=stmt.window, settings=settings
            )
            if stmt.runnable is not None:
                self.call(self.runnables[stmt.runnable], result, stmt.window, settings)
            return result

        result = body(locals)
        if stmt.count == 1:
            return result
        # Finite widening: names can only be added, and differing facts become
        # unknown. Include the first iteration and possible later iterations.
        entry = _join(locals, result)
        while True:
            following = body(entry)
            result = _join(result, following)
            widened = _join(entry, following)
            if widened == entry:
                return result
            entry = widened

    def statement(
        self,
        stmt: ast.FlowStmt,
        locals: _Locals,
        window: int | None,
        settings: Mapping[str, str | None],
    ) -> _Local:
        if isinstance(stmt, ast.LetStmt | ast.AskStmt):
            self.content(
                stmt.value if isinstance(stmt, ast.LetStmt) else stmt.request,
                locals,
                window,
            )
            return _Local("Part[]", "item")

        source = locals.get("_")
        collection = isinstance(
            stmt,
            ast.MapStmt
            | ast.KeepStmt
            | ast.DropStmt
            | ast.SortStmt
            | ast.GatherStmt
            | ast.SettleStmt,
        )
        if collection:
            if source is None or source.shape not in {None, "list"}:
                actual = "none" if source is None else source.shape
                raise ToolangValidationError(
                    f"{stmt.kind} requires current shape list, got {actual}"
                )
            if (
                isinstance(stmt, ast.GatherStmt | ast.SettleStmt)
                and source.empty is True
            ):
                raise ToolangValidationError(f"{stmt.kind} requires a nonempty list")

        child_name = getattr(stmt, "runnable", None)
        child = self.runnables.get(child_name) if child_name else None
        if (
            isinstance(stmt, ast.SeekStmt)
            and child is not None
            and child.name is not None
        ):
            # Named seek targets belong to the remote agent, even when a local
            # runnable happens to have the same name.
            child = None
        output = child.output if child else None
        child_locals = dict(locals)
        element_type = (
            source.type_name[:-2]
            if source
            and source.shape == "list"
            and source.type_name
            and source.type_name.endswith("[]")
            else None
        )
        if collection and not isinstance(stmt, ast.GatherStmt):
            child_locals["_"] = _Local(element_type, "item")
        if isinstance(stmt, ast.SettleStmt):
            if stmt.initial is None:
                if element_type is not None and element_type != output:
                    raise ToolangValidationError(
                        f"settle without from requires {element_type} output, got {output}"
                    )
            else:
                self.content(stmt.initial, locals, window)
        no_calls = (
            isinstance(stmt, ast.StormStmt)
            and stmt.count == 0
            or isinstance(
                stmt, ast.MapStmt | ast.KeepStmt | ast.DropStmt | ast.SortStmt
            )
            and source is not None
            and source.empty is True
        )
        if child is not None and not no_calls:
            self.call(
                child,
                child_locals,
                1 if isinstance(stmt, ast.SettleStmt) else window,
                settings,
            )

        transform = operation_transform(stmt.kind)
        if transform in {"filter", "sort"}:
            assert source is not None
            return _Local(
                source.type_name,
                "list",
                True
                if isinstance(stmt, ast.KeepStmt) and stmt.count == 0
                else source.empty,
            )
        if transform == "list":
            if isinstance(stmt, ast.ScatterStmt):
                return _Local(output, "list")
            return _Local(
                f"{output}[]" if output else None,
                "list",
                stmt.count == 0
                if isinstance(stmt, ast.StormStmt)
                else source.empty
                if source
                else None,
            )
        return _Local(output, "item")
