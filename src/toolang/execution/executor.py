"""AST-driven agic and flow executor."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
import json
import threading
import time
from typing import TYPE_CHECKING, Any, Literal, cast

from toolang.base.error import ToolangError
from toolang.base.types.message import Message, Part, TextPart, message_text
from toolang.base.types.run import RunResult

from .. import agents
from ..common.ids import RUN_ID_FAMILY, allocate_id
from ..lang.ast import (
    AgicDecl,
    AskStmt,
    DropStmt,
    FlowDecl,
    FlowStmt,
    GatherStmt,
    KeepStmt,
    LetStmt,
    MapStmt,
    RankStmt,
    RepeatStmt,
    RunStmt,
    ScatterStmt,
    SeekStmt,
    SettleStmt,
    StormStmt,
    StructDecl,
)
from ..plugin import load_loop as _default_load_loop
from .assembly import RunInput
from .binding import RunBinding, invoke_params
from .context import RunContext
from .events import (
    RunBegin,
    RunEnd,
    RunStarting,
    StepBegin,
    StepEnd,
    TraceEvent,
    TraceEventHandler,
)
from .model import resolve_model
from .records import (
    CommandKind,
    CommandRecord,
    InputRef,
    OutputRef,
    StepInputItem,
    StepKind,
    StepPath,
    trace_child_path,
    trace_run,
)

if TYPE_CHECKING:
    from ..up import UptimeContext

Shape = Literal["none", "item", "list"]


@dataclass(frozen=True, slots=True)
class Local:
    """One runtime local and its flow shape."""

    value: Any = None
    shape: Shape = "none"


class _RunStopped(asyncio.CancelledError):
    def __init__(self, command: CommandRecord) -> None:
        super().__init__(_command_text(command) or "canceled")
        self.command = command


class Executor:
    """Execute semantic AST declarations against one immutable run binding."""

    def __init__(
        self,
        context: UptimeContext,
        *,
        emit: TraceEventHandler,
        consume_commands: Callable[[str, CommandKind], Sequence[CommandRecord]]
        | None = None,
        load_loop: Callable[[str], Any] = _default_load_loop,
        stream: bool = False,
    ) -> None:
        self._context = context
        self._emit_trace = emit
        self._consume_commands = consume_commands
        self._load_loop = load_loop
        self._stream = stream
        self._run_outputs: dict[str, StepPath] = {}

    async def run(
        self,
        binding: RunBinding,
        executable: AgicDecl | FlowDecl,
        *,
        parent: StepPath | None = None,
        locals: Mapping[str, Local] | None = None,
    ) -> Local:
        """Execute one top-level or child run and emit its complete lifecycle."""

        current = (
            dict(locals) if locals is not None else _initial_locals(binding, executable)
        )
        started_at = _utc_now()
        self._emit(
            RunBegin(
                run=binding.run_id,
                input=InputRef(cmd=0),
                context=_run_context(binding, executable, parent=parent),
                started_at=started_at,
            )
        )
        try:
            if isinstance(executable, AgicDecl):
                result = await self._execute_agic(binding, executable, current)
                current["_"] = result
            else:
                result = await self._execute_flow(binding, executable, current)
        except asyncio.CancelledError as exc:
            command = (
                exc.command
                if isinstance(exc, _RunStopped)
                else next(
                    (
                        item
                        for item in self._pending_commands(binding.run_id, "stop")
                        if item.apply == "now"
                    ),
                    None,
                )
            )
            self._emit(
                RunEnd(
                    run=binding.run_id,
                    status="canceled",
                    input=InputRef(cmd=command.index) if command is not None else None,
                    output=self._run_output(binding.run_id),
                    error=_command_text(command) or "canceled",
                    finished_at=_utc_now(),
                )
            )
            raise
        except Exception as exc:
            self._emit(
                RunEnd(
                    run=binding.run_id,
                    status="failed",
                    output=self._run_output(binding.run_id),
                    error=str(exc) or type(exc).__name__,
                    finished_at=_utc_now(),
                )
            )
            raise
        self._emit(
            RunEnd(
                run=binding.run_id,
                status="finished",
                output=self._run_output(binding.run_id),
                finished_at=_utc_now(),
            )
        )
        return result

    async def _execute_agic(
        self,
        binding: RunBinding,
        agic: AgicDecl,
        locals: Mapping[str, Local],
    ) -> Local:
        invoke = {name: local.value for name, local in locals.items() if name != "_"}
        primary = locals.get("_", Local())
        metadata = {**binding.metadata, "invoke_params": invoke}
        bound = replace(
            binding,
            input_text=_value_text(primary.value) if primary.shape != "none" else "",
            metadata=metadata,
        )
        run_input = RunInput.from_thunk(self._context, bound, agic)
        allowed = run_input.effective_model_selectors(self._context)
        model = resolve_model(
            self._context,
            selector=run_input.model_selector(self._context),
            allowed_selectors=allowed,
        )
        provider = self._context.model_providers[model.provider]
        model = provider.prepare_target(model)
        adapter = self._context.model_adapters.get(model.adapter)
        if adapter is None:
            raise ToolangError(f"unknown model adapter: {model.adapter}")
        loop = self._load_loop(bound.run_loop)
        context = RunContext(
            run_input,
            model,
            adapter,
            on_event=self._emit,
            consume_inputs=lambda run_id: self._pending_commands(run_id, "steer"),
            before_call=lambda: self._raise_if_stopping(
                binding.run_id, call=True
            ),
            stream=self._stream,
        )
        execution = await _run_loop(loop.run, context, run_id=binding.run_id)
        return Local(
            _decode_agic_output(
                execution,
                agic.output,
                structs=_program_structs(binding),
            ),
            "item",
        )

    async def _execute_flow(
        self,
        binding: RunBinding,
        flow: FlowDecl,
        locals: dict[str, Local],
    ) -> Local:
        await self._execute_statements(
            binding,
            flow.stmts,
            locals,
            parent=binding.run_id,
        )
        result = locals.get("_", Local())
        if flow.output is not None:
            if result.shape == "none":
                raise ToolangError(f"flow output is missing; expected {flow.output}")
            _validate_output(
                result.value,
                flow.output,
                structs=_program_structs(binding),
            )
        return result

    async def _execute_statements(
        self,
        binding: RunBinding,
        statements: Sequence[FlowStmt],
        locals: dict[str, Local],
        *,
        parent: StepPath,
        start: int = 0,
        placement: Mapping[str, object] | None = None,
    ) -> int:
        index = start
        for statement in statements:
            self._raise_if_stopping(
                binding.run_id, call=_statement_has_call(statement)
            )
            commands = self._steer_commands(binding.run_id, statement)
            _apply_steer(locals, commands)
            result = await self._execute_stmt(
                binding,
                dict(locals),
                parent=parent,
                index=index,
                statement=statement,
                commands=commands,
                placement=placement,
            )
            _update_locals(locals, statement.binding, result)
            if parent == binding.run_id and statement.binding == "_":
                self._run_outputs[binding.run_id] = trace_child_path(parent, index)
            index += 1
        return index

    async def _execute_stmt(
        self,
        binding: RunBinding,
        locals: Mapping[str, Local],
        *,
        parent: StepPath,
        index: int,
        statement: FlowStmt,
        commands: Sequence[CommandRecord] = (),
        placement: Mapping[str, object] | None = None,
    ) -> Local:
        path = trace_child_path(parent, index)
        kind = _step_kind(statement)
        statement_context = _statement_context(statement)
        started_at = _utc_now()
        inputs = tuple(InputRef(cmd=item.index) for item in commands)
        self._emit(
            StepBegin(
                step=path,
                kind=kind,
                input=cast(tuple[StepInputItem, ...], inputs),
                context={
                    **statement_context,
                    "binding": statement.binding,
                    "reads": [] if isinstance(statement, LetStmt) else sorted(locals),
                    "placement": dict(placement or {}),
                    "source": {"line": statement.span.line},
                },
                started_at=started_at,
            )
        )
        try:
            result = await self._dispatch_stmt(
                binding,
                locals,
                path=path,
                statement=statement,
                placement=placement,
            )
        except asyncio.CancelledError:
            self._emit(
                StepEnd(
                    step=path,
                    kind=kind,
                    status="canceled",
                    started_at=started_at,
                    finished_at=_utc_now(),
                )
            )
            raise
        except Exception as exc:
            self._emit(
                StepEnd(
                    step=path,
                    kind=kind,
                    status="failed",
                    error=str(exc) or type(exc).__name__,
                    started_at=started_at,
                    finished_at=_utc_now(),
                )
            )
            raise
        self._emit(
            StepEnd(
                step=path,
                kind=kind,
                status="finished",
                output=_output_parts(result),
                detail={
                    "shape": result.shape,
                    **(
                        {"reshape": reshape}
                        if (reshape := _statement_reshape(statement)) is not None
                        else {}
                    ),
                    **(
                        {"items": len(result.value)}
                        if result.shape == "list" and isinstance(result.value, list)
                        else {}
                    ),
                },
                started_at=started_at,
                finished_at=_utc_now(),
            )
        )
        return result

    async def _dispatch_stmt(
        self,
        binding: RunBinding,
        locals: Mapping[str, Local],
        *,
        path: StepPath,
        statement: FlowStmt,
        placement: Mapping[str, object] | None,
    ) -> Local:
        if isinstance(statement, RunStmt):
            return await self._run_runnable(
                binding, locals, path, statement.runnable, placement
            )
        if isinstance(statement, SeekStmt):
            raise ToolangError("seek requires an agent execution bridge")
        if isinstance(statement, AskStmt):
            raise ToolangError("ask requires a human input bridge")
        if isinstance(statement, ScatterStmt):
            result = await self._run_runnable(
                binding, locals, path, statement.runnable, placement
            )
            values = _result_list(result, operation="scatter")
            if len(values) != statement.count:
                raise ToolangError(
                    f"scatter expected {statement.count} items, got {len(values)}"
                )
            return Local(values, "list")
        if isinstance(statement, StormStmt):
            basis = _require_item(locals, operation="storm")
            values = await self._parallel_runs(
                binding,
                locals,
                path,
                statement.runnable,
                [basis] * statement.count,
                limit=statement.par,
            )
            return Local(values, "list")
        if isinstance(statement, GatherStmt):
            _require_list(locals, operation="gather")
            result = await self._run_runnable(
                binding, locals, path, statement.runnable, placement
            )
            return Local(result.value, "item")
        if isinstance(statement, SettleStmt):
            return await self._execute_settle(binding, locals, path, statement)
        if isinstance(statement, MapStmt):
            values = _require_list(locals, operation="map")
            mapped = await self._parallel_runs(
                binding,
                locals,
                path,
                statement.runnable,
                values,
                limit=statement.par,
            )
            return Local(mapped, "list")
        if isinstance(statement, KeepStmt | DropStmt):
            return await self._execute_filter(binding, locals, path, statement)
        if isinstance(statement, RankStmt):
            return await self._execute_rank(binding, locals, path, statement)
        if isinstance(statement, RepeatStmt):
            return await self._execute_repeat(binding, locals, path, statement)
        if isinstance(statement, LetStmt):
            return Local(statement.value, "item")
        raise ToolangError(f"unsupported flow statement: {statement.kind}")

    async def _execute_settle(
        self,
        binding: RunBinding,
        locals: Mapping[str, Local],
        path: StepPath,
        statement: SettleStmt,
    ) -> Local:
        items = _require_list(locals, operation="settle")
        accumulator = Local()
        for index, item in enumerate(items):
            child_locals = dict(locals)
            child_locals["_"] = Local(item, "item")
            child_locals["accumulator"] = accumulator
            accumulator = await self._run_runnable(
                binding,
                child_locals,
                path,
                statement.runnable,
                {"item": index, "items": len(items), "loop": index},
            )
        return accumulator

    async def _execute_filter(
        self,
        binding: RunBinding,
        locals: Mapping[str, Local],
        path: StepPath,
        statement: KeepStmt | DropStmt,
    ) -> Local:
        items = _require_list(locals, operation=statement.kind)
        if statement.position is not None:
            count = statement.count or 0
            selected = set(
                range(min(count, len(items)))
                if statement.position == "first"
                else range(max(len(items) - count, 0), len(items))
            )
            matches = [index in selected for index in range(len(items))]
        else:
            if statement.predicate is None:
                raise ToolangError(f"{statement.kind} requires a predicate")
            values = await self._parallel_runs(
                binding,
                locals,
                path,
                statement.predicate,
                items,
                limit=statement.par,
            )
            matches = [_boolean(value, operation=statement.kind) for value in values]
        kept = [
            item
            for item, matched in zip(items, matches, strict=True)
            if matched == isinstance(statement, KeepStmt)
        ]
        return Local(kept, "list")

    async def _execute_rank(
        self,
        binding: RunBinding,
        locals: Mapping[str, Local],
        path: StepPath,
        statement: RankStmt,
    ) -> Local:
        items = _require_list(locals, operation="rank")
        values = await self._parallel_runs(
            binding,
            locals,
            path,
            statement.scorer,
            items,
            limit=statement.par,
        )
        scores = [_number(value, operation="rank") for value in values]
        ranked = [
            item
            for _, item, _ in sorted(
                zip(scores, items, range(len(items)), strict=True),
                key=lambda entry: (-entry[0], entry[2]),
            )
        ]
        if statement.limit == "top":
            ranked = ranked[: statement.count or 0]
        elif statement.limit == "bottom":
            count = statement.count or 0
            ranked = ranked[-count:] if count else []
        return Local(ranked, "list")

    async def _execute_repeat(
        self,
        binding: RunBinding,
        locals: Mapping[str, Local],
        path: StepPath,
        statement: RepeatStmt,
    ) -> Local:
        working = dict(locals)
        child_index = 0
        iteration = 0
        while statement.count is None or iteration < statement.count:
            child_index = await self._execute_statements(
                binding,
                statement.stmts,
                working,
                parent=path,
                start=child_index,
                placement={"loop": iteration},
            )
            iteration += 1
            if statement.until is not None:
                condition = await self._run_runnable(
                    binding,
                    working,
                    path,
                    statement.until,
                    {"loop": iteration - 1, "role": "until"},
                )
                if _boolean(condition.value, operation="until"):
                    break
        return working.get("_", Local())

    async def _parallel_runs(
        self,
        binding: RunBinding,
        locals: Mapping[str, Local],
        parent: StepPath,
        runnable: str,
        inputs: Sequence[Any],
        *,
        limit: int | None,
    ) -> list[Any]:
        semaphore = asyncio.Semaphore(limit or max(len(inputs), 1))
        lanes = limit or max(len(inputs), 1)

        async def execute(index: int, value: Any) -> Any:
            async with semaphore:
                child_locals = dict(locals)
                child_locals["_"] = Local(value, "item")
                result = await self._run_runnable(
                    binding,
                    child_locals,
                    parent,
                    runnable,
                    {
                        "item": index,
                        "items": len(inputs),
                        "lane": index % lanes,
                        "lanes": lanes,
                    },
                )
                return result.value

        tasks = [
            asyncio.create_task(execute(index, value))
            for index, value in enumerate(inputs)
        ]
        try:
            return list(await asyncio.gather(*tasks))
        except BaseException:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    async def _run_runnable(
        self,
        parent: RunBinding,
        locals: Mapping[str, Local],
        step: StepPath,
        name: str,
        placement: Mapping[str, object] | None,
    ) -> Local:
        executable = _resolve_runnable(parent, name)
        binding = _child_binding(self._context, parent, executable, locals, placement)
        context = _run_context(binding, executable, parent=step)
        self._emit(
            RunStarting(
                run=binding.run_id,
                cmd=0,
                parent=step,
                thread=binding.thread_id,
                input=binding.message or Message.user(binding.input_text),
                context=context,
                created_at=binding.created_at,
            )
        )
        return await self.run(binding, executable, parent=step, locals=locals)

    def _pending_commands(
        self, run_id: str, kind: CommandKind
    ) -> tuple[CommandRecord, ...]:
        if self._consume_commands is None:
            return ()
        return tuple(self._consume_commands(run_id, kind))

    def _steer_commands(
        self, run_id: str, statement: FlowStmt
    ) -> tuple[CommandRecord, ...]:
        allowed = {"now", "next_step"}
        if _statement_has_call(statement):
            allowed.add("next_call")
        return tuple(
            command
            for command in self._pending_commands(run_id, "steer")
            if command.apply in allowed
        )

    def _raise_if_stopping(self, run_id: str, *, call: bool) -> None:
        allowed = {"now", "next_step"}
        if call:
            allowed.add("next_call")
        command = next(
            (
                item
                for item in self._pending_commands(run_id, "stop")
                if item.apply in allowed
            ),
            None,
        )
        if command is not None:
            raise _RunStopped(command)

    def _run_output(self, run_id: str) -> OutputRef | None:
        path = self._run_outputs.get(run_id)
        return OutputRef(step=path) if path is not None else None

    def _emit(self, event: TraceEvent) -> None:
        if (
            isinstance(event, StepEnd)
            and event.kind == "model"
            and event.status == "finished"
        ):
            self._run_outputs[trace_run(event.step)] = event.step
        self._emit_trace(event)


def _initial_locals(
    binding: RunBinding, executable: AgicDecl | FlowDecl
) -> dict[str, Local]:
    locals = {
        name: Local(value, "item") for name, value in invoke_params(binding).items()
    }
    if executable.input is not None:
        primary = Local(binding.input_text, "item")
        locals["_"] = primary
        if executable.input.name != "_":
            locals[executable.input.name] = primary
    else:
        locals.setdefault("_", Local())
    return locals


def _update_locals(
    locals: dict[str, Local], binding: str | None, result: Local
) -> None:
    if binding is not None:
        locals[binding] = result


def _apply_steer(locals: dict[str, Local], commands: Sequence[CommandRecord]) -> None:
    for command in commands:
        if command.input is not None:
            locals["_"] = Local(message_text(command.input.parts), "item")


def _step_kind(statement: FlowStmt) -> StepKind:
    if isinstance(statement, SeekStmt):
        return "agent"
    if isinstance(statement, AskStmt):
        return "human"
    if isinstance(statement, StormStmt | MapStmt | KeepStmt | DropStmt | RankStmt):
        return "par"
    if isinstance(statement, SettleStmt | RepeatStmt):
        return "loop"
    if isinstance(statement, LetStmt):
        return "system"
    return "run"


def _statement_has_call(statement: FlowStmt) -> bool:
    if isinstance(
        statement,
        RunStmt
        | SeekStmt
        | AskStmt
        | ScatterStmt
        | StormStmt
        | GatherStmt
        | SettleStmt
        | MapStmt
        | RankStmt,
    ):
        return True
    if isinstance(statement, KeepStmt | DropStmt):
        return statement.predicate is not None
    if isinstance(statement, RepeatStmt):
        return statement.until is not None or any(
            _statement_has_call(child) for child in statement.stmts
        )
    return False


def _statement_reshape(statement: FlowStmt) -> str | None:
    if isinstance(statement, ScatterStmt):
        return "unfold"
    if isinstance(statement, GatherStmt):
        return "fold"
    if isinstance(statement, KeepStmt):
        return "keep"
    if isinstance(statement, DropStmt):
        return "drop"
    if isinstance(statement, RankStmt):
        return "rank"
    if isinstance(statement, StormStmt | MapStmt):
        return "list"
    return None


def _statement_context(statement: FlowStmt) -> dict[str, object]:
    context: dict[str, object] = {"statement": statement.kind}
    for name in (
        "runnable",
        "agent",
        "count",
        "par",
        "position",
        "predicate",
        "scorer",
        "limit",
        "until",
    ):
        value = getattr(statement, name, None)
        if value is not None:
            context[name] = value
    return context


def _resolve_runnable(binding: RunBinding, name: str) -> AgicDecl | FlowDecl:
    program = binding.live.program
    for agic in program.thunks:
        if agic.name == name:
            return agic
    for flow in program.flows:
        if flow.name == name:
            return flow
    raise ToolangError(f"Runnable not found: {name}")


def _child_binding(
    context: UptimeContext,
    parent: RunBinding,
    executable: AgicDecl | FlowDecl,
    locals: Mapping[str, Local],
    placement: Mapping[str, object] | None,
) -> RunBinding:
    primary = locals.get("_", Local())
    metadata = {
        key: value
        for key, value in parent.metadata.items()
        if key not in {"request_id", "invoke_params", "executable_kind"}
    }
    metadata.update(
        {
            "executable_kind": executable.kind,
            "root": parent.metadata.get("root") or parent.run_id,
            "invoke_params": {
                name: local.value for name, local in locals.items() if name != "_"
            },
            "placement": dict(placement or {}),
        }
    )
    text = _value_text(primary.value) if primary.shape != "none" else ""
    return RunBinding(
        run_id=_allocate_run_id(context),
        group=parent.group,
        origin=parent.origin,
        thread_id=parent.thread_id,
        thunk_name=executable.name,
        input_text=text,
        message=Message.user(text),
        model_selector=parent.model_selector,
        model_selectors=parent.model_selectors,
        tool_selectors=parent.tool_selectors,
        cap_selectors=parent.cap_selectors,
        run_loop=parent.run_loop,
        metadata=metadata,
        live=parent.live,
        created_at=_utc_now(),
    )


def _run_context(
    binding: RunBinding,
    executable: AgicDecl | FlowDecl,
    *,
    parent: StepPath | None,
) -> dict[str, object]:
    root = (
        str(binding.metadata.get("root") or trace_run(parent))
        if parent is not None
        else binding.run_id
    )
    return {
        **dict(binding.metadata),
        "origin": binding.origin,
        "root": root,
        "executable": {"kind": executable.kind, "name": executable.name},
        "call": "top" if parent is None else "run",
    }


def _require_item(locals: Mapping[str, Local], *, operation: str) -> Any:
    current = locals.get("_", Local())
    if current.shape != "item":
        raise ToolangError(
            f"{operation} requires current shape item, got {current.shape}"
        )
    return current.value


def _require_list(locals: Mapping[str, Local], *, operation: str) -> list[Any]:
    current = locals.get("_", Local())
    if current.shape != "list" or not isinstance(current.value, list):
        raise ToolangError(
            f"{operation} requires current shape list, got {current.shape}"
        )
    return list(current.value)


def _result_list(result: Local, *, operation: str) -> list[Any]:
    if isinstance(result.value, list | tuple):
        return list(result.value)
    raise ToolangError(f"{operation} requires a list result")


def _boolean(value: Any, *, operation: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in {"true", "false"}:
        return value.strip().lower() == "true"
    raise ToolangError(f"{operation} requires a Boolean result")


def _number(value: Any, *, operation: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ToolangError(f"{operation} requires a Number result")
    return float(value)


def _decode_agic_output(
    execution: RunResult,
    output_type: str | None,
    *,
    structs: Mapping[str, StructDecl] | None = None,
) -> Any:
    text = execution.output_text
    if output_type is None or output_type == "Text":
        return text
    if output_type == "Path":
        return text
    if output_type == "Part":
        parts = execution.message.parts if execution.message is not None else ()
        if len(parts) != 1:
            raise ToolangError(
                f"agic output is not a Part: expected 1 part, got {len(parts)}"
            )
        return parts[0].to_data()
    if output_type == "Part[]":
        return (
            [part.to_data() for part in execution.message.parts]
            if execution.message
            else []
        )
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ToolangError(
            f"agic output is not valid {output_type}: {exc.msg}"
        ) from exc
    _validate_output(value, output_type, structs=structs or {})
    return value


def _program_structs(binding: RunBinding) -> dict[str, StructDecl]:
    parsed = getattr(binding.live.program, "parsed", None)
    return {item.name: item for item in getattr(parsed, "structs", ())}


def _validate_output(
    value: Any,
    type_name: str,
    *,
    structs: Mapping[str, StructDecl],
    path: str = "output",
) -> None:
    if type_name.endswith("[]"):
        if not isinstance(value, list):
            raise ToolangError(f"{path} is not {type_name}")
        item_type = type_name[:-2]
        for index, item in enumerate(value):
            _validate_output(
                item,
                item_type,
                structs=structs,
                path=f"{path}[{index}]",
            )
        return

    if type_name in {"Text", "Path"}:
        valid = isinstance(value, str)
    elif type_name == "Number":
        valid = not isinstance(value, bool) and isinstance(value, int | float)
    elif type_name == "Boolean":
        valid = isinstance(value, bool)
    elif type_name == "Json":
        valid = _is_json_value(value)
    elif type_name == "Part":
        valid = isinstance(value, Mapping) and isinstance(value.get("type"), str)
    elif type_name == "Artifact":
        valid = isinstance(value, Mapping) and _is_json_value(value)
    elif struct := structs.get(type_name):
        if not isinstance(value, Mapping):
            valid = False
        else:
            fields = {field.name: field for field in struct.fields}
            unknown = set(value) - set(fields)
            missing = {
                name
                for name, field in fields.items()
                if not field.optional and name not in value
            }
            if unknown:
                names = ", ".join(sorted(str(name) for name in unknown))
                raise ToolangError(f"{path} has unknown {type_name} fields: {names}")
            if missing:
                names = ", ".join(sorted(missing))
                raise ToolangError(f"{path} is missing {type_name} fields: {names}")
            for name, item in value.items():
                _validate_output(
                    item,
                    fields[str(name)].type_name,
                    structs=structs,
                    path=f"{path}.{name}",
                )
            return
    else:
        raise ToolangError(f"unknown output type: {type_name}")

    if not valid:
        raise ToolangError(f"{path} is not {type_name}")


def _is_json_value(value: Any) -> bool:
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError):
        return False
    return True


def _output_parts(local: Local) -> tuple[Part, ...]:
    if local.shape == "none":
        return ()
    return (TextPart(text=_value_text(local.value)),)


def _value_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, Message):
        return message_text(value.parts)
    if isinstance(value, bool | int | float | list | dict | tuple):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def _command_text(command: CommandRecord | None) -> str:
    if command is None or command.input is None:
        return ""
    return message_text(command.input.parts)


def _allocate_run_id(context: UptimeContext) -> str:
    value = allocate_id(
        agents.agent_id_state_path(context.root, context.name),
        family=RUN_ID_FAMILY,
    ).value
    return f"run_{value}"


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


async def _run_loop(
    run: Callable[[RunContext], RunResult],
    context: RunContext,
    *,
    run_id: str,
) -> RunResult:
    """Run a blocking agent loop without delaying async task cancellation."""

    loop = asyncio.get_running_loop()
    future: asyncio.Future[RunResult] = loop.create_future()

    def worker() -> None:
        try:
            result = run(context)
        except BaseException as exc:
            _notify_loop(loop, _set_exception, future, exc)
            return
        _notify_loop(loop, _set_result, future, result)

    threading.Thread(
        target=worker,
        name=f"toolang-run-{run_id[:12]}",
        daemon=True,
    ).start()
    return await future


def _set_result(future: asyncio.Future[RunResult], result: RunResult) -> None:
    if not future.done():
        future.set_result(result)


def _set_exception(future: asyncio.Future[RunResult], error: BaseException) -> None:
    if not future.done():
        future.set_exception(error)


def _notify_loop(
    loop: asyncio.AbstractEventLoop,
    callback: Callable[..., None],
    *args: object,
) -> None:
    try:
        loop.call_soon_threadsafe(callback, *args)
    except RuntimeError:
        pass
