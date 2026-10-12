"""Conservative, intraprocedural checks using already determined signatures."""

from collections.abc import Mapping
from dataclasses import dataclass

from toolang.common.template import (
    require_template_inputs,
    require_template_fields,
    template_history_depth,
)

from . import ast
from .contracts import operation_transform
from .errors import ToolangValidationError, source_location
from .types import parse_runnable_ref_parts


@dataclass(frozen=True)
class _Local:
    # Full value type: Text[][] for a list of Text[] elements.
    type_name: str | None = None
    length: int | None = None
    handle: bool = False
    result_type: str | None = None


_Locals = dict[str, _Local]
_Runnables = Mapping[str, ast.AgicDecl | ast.FlowDecl]


def validate_flows(
    program: ast.Program,
    runnables: _Runnables,
    runnable_programs: Mapping[str, ast.Program],
) -> None:
    checker = _FlowChecker(program, runnables, runnable_programs)
    for flow in program.flows:
        locals = {p.name: _Local(p.type_name) for p in flow.params}
        if flow.input is not None:
            locals["_"] = _Local(flow.input.type_name)
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
            a.length if a.length == b.length else None,
            a.handle or b.handle,
            a.result_type if a.result_type == b.result_type else None,
        )
    return result


class _FlowChecker:
    def __init__(
        self,
        program: ast.Program,
        runnables: _Runnables,
        runnable_programs: Mapping[str, ast.Program],
    ):
        self.program = program
        self.runnables = runnables
        self.runnable_programs = runnable_programs

    def content(self, text: str, locals: _Locals, window: int | None) -> None:
        require_template_inputs(text, locals)
        require_template_fields(
            text,
            {
                name: frozenset({"id", "thread", "status"}) if local.handle else None
                for name, local in locals.items()
            },
        )
        if window is not None:
            template_history_depth(text, window)

    def inputs(
        self,
        reference: str,
        locals: _Locals,
    ) -> None:
        runnable = self.runnables[reference]
        inline = parse_runnable_ref_parts(reference).role == "adhoc"
        for parameter in (
            *runnable.params,
            *((runnable.input,) if runnable.input else ()),
        ):
            if not parameter.optional and parameter.name not in locals:
                raise ToolangValidationError(
                    f"Missing input {parameter.name!r} for {'inline agic' if inline else reference!r}"
                )
            if (
                not inline
                and parameter.name in locals
                and locals[parameter.name].handle
            ):
                raise ToolangValidationError(
                    f"Run handle {parameter.name!r} cannot be passed as an input; capture its fields instead"
                )

    def history(
        self,
        reference: str,
        window: int | None,
        settings: Mapping[str, str | None],
    ) -> None:
        runnable = self.runnables[reference]
        # Flow signatures are checked locally. Do not descend through callees.
        if not isinstance(runnable, ast.AgicDecl) or window is None:
            return
        templates = [message.content for message in runnable.messages]
        for kind in ("instruct", "context"):
            own = getattr(runnable, kind)
            selection = own if own is not None else settings.get(kind)
            # Explicit templates belong to the callee; inherited ones to the caller.
            owner = (
                self.runnable_programs[reference] if own is not None else self.program
            )
            declarations = owner.instructs if kind == "instruct" else owner.contexts
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
    ) -> _Locals | None:
        locals = dict(locals)
        for stmt in statements:
            with source_location(stmt.span.line):
                if isinstance(stmt, ast.ExecStmt):
                    self.statement(stmt, locals, window, settings)
                    return None
                if isinstance(stmt, ast.RepeatStmt):
                    result_locals = self.repeat(stmt, locals, settings)
                    if result_locals is None:
                        return None
                    locals = result_locals
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
    ) -> _Locals | None:
        # Runtime preflights the condition window even when the body is skipped.
        if stmt.runnable is not None:
            self.history(stmt.runnable, stmt.window, settings)
        if stmt.count == 0:
            return locals

        index = len(stmt.stmts) if stmt.until_index is None else stmt.until_index

        def body(entry: _Locals) -> tuple[_Locals | None, _Locals | None]:
            prefix = self.statements(
                stmt.stmts[:index], entry, window=stmt.window, settings=settings
            )
            if prefix is None:
                return None, None
            exit_locals = None
            if stmt.runnable is not None:
                self.inputs(stmt.runnable, prefix)
                exit_locals = prefix
            suffix = self.statements(
                stmt.stmts[index:], prefix, window=stmt.window, settings=settings
            )
            return suffix, exit_locals

        def exits(
            completed: _Locals | None, condition: _Locals | None
        ) -> _Locals | None:
            if stmt.count is None or completed is None:
                return condition
            return completed if condition is None else _join(completed, condition)

        completed, condition = body(locals)
        result = exits(completed, condition)
        if completed is None or stmt.count == 1:
            return result
        # Finite widening: names can only be added, and differing facts become
        # unknown. Include the first iteration and possible later iterations.
        entry = _join(locals, completed)
        while True:
            following, condition = body(entry)
            possible_exit = exits(following, condition)
            if possible_exit is not None:
                result = (
                    possible_exit if result is None else _join(result, possible_exit)
                )
            if following is None:
                return result
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
            return _Local("Part[]")

        if isinstance(stmt, ast.AwaitStmt):
            target = locals.get(stmt.handle)
            if target is None or not target.handle:
                raise ToolangValidationError(
                    f"Await requires a retained handle: {stmt.handle!r}"
                )
            return _Local(target.result_type)

        source = locals.get("_")
        collection = isinstance(
            stmt,
            ast.MapStmt | ast.KeepStmt | ast.DropStmt | ast.SortStmt | ast.ReduceStmt,
        )
        if collection:
            if source is None or (
                source.type_name is not None
                and source.type_name != "Json"
                and not source.type_name.endswith("[]")
            ):
                actual = "missing input" if source is None else source.type_name
                raise ToolangValidationError(
                    f"{stmt.kind.capitalize()} requires an outer array, got {actual}"
                )
            if (
                isinstance(stmt, ast.ReduceStmt)
                and stmt.initial is None
                and source.length == 0
            ):
                raise ToolangValidationError("Reduce requires a nonempty array")

        child_name = getattr(stmt, "runnable", None)
        child = self.runnables.get(child_name) if child_name else None
        inline = bool(
            child_name and parse_runnable_ref_parts(child_name).role == "adhoc"
        )
        if isinstance(stmt, ast.SeekStmt) and not inline:
            # Named seek targets belong to the remote agent, even when a local
            # runnable happens to have the same name.
            child = None
        output = child.output if child else None
        child_locals = dict(locals)
        element_type = (
            source.type_name[:-2]
            if source and source.type_name and source.type_name.endswith("[]")
            else "Json"
            if source and source.type_name == "Json"
            else None
        )
        if collection:
            child_locals["_"] = _Local(element_type)
        if isinstance(stmt, ast.ReduceStmt):
            if stmt.initial is None:
                if element_type is not None and element_type != output:
                    raise ToolangValidationError(
                        f"Reduce without an initializer requires {element_type} output, got {output}"
                    )
            else:
                self.content(stmt.initial, locals, window)
        no_calls = (
            isinstance(stmt, ast.GenerateStmt)
            and stmt.count == 0
            or isinstance(
                stmt,
                ast.MapStmt
                | ast.KeepStmt
                | ast.DropStmt
                | ast.SortStmt
                | ast.ReduceStmt,
            )
            and source is not None
            and source.length == 0
            or isinstance(stmt, ast.ReduceStmt)
            and stmt.initial is None
            and source is not None
            and source.length == 1
        )
        if child is not None:
            assert child_name is not None
            # Zero-call operations still preflight inputs, but never render the
            # child's history templates (including implicit-seed singleton reduce).
            self.inputs(child_name, child_locals)
            if isinstance(child, ast.AgicDecl) and inline:
                for message in child.messages:
                    self.content(message.content, child_locals, None)
            if not no_calls:
                self.history(
                    child_name,
                    1 if isinstance(stmt, ast.ReduceStmt) else window,
                    settings,
                )

        if (
            isinstance(stmt, ast.SpawnStmt)
            or isinstance(stmt, ast.RunStmt)
            and stmt.asynchronous
        ):
            result_type = (
                child.output or ("Part[]" if isinstance(child, ast.AgicDecl) else None)
                if child
                else None
            )
            return _Local(handle=True, result_type=result_type)

        transform = operation_transform(stmt.kind)
        if transform in {"filter", "sort"}:
            assert source is not None
            length = source.length
            if isinstance(stmt, ast.KeepStmt | ast.DropStmt):
                if stmt.count is None:
                    length = 0 if length == 0 else None
                elif isinstance(stmt, ast.KeepStmt):
                    length = (
                        min(length, stmt.count)
                        if length is not None
                        else 0
                        if stmt.count == 0
                        else None
                    )
                elif length is not None:
                    length = max(0, length - stmt.count)
            return _Local(source.type_name, length)
        if transform == "list":
            return _Local(
                f"{output}[]" if output else None,
                stmt.count
                if isinstance(stmt, ast.GenerateStmt)
                else source.length
                if source
                else None,
            )
        return _Local(output)
