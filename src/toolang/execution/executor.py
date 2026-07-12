"""Core run executor for thunk and flow executions."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
import time
from typing import TYPE_CHECKING, Any, Literal

from toolang.base.error import ToolangError
from toolang.base.types.message import Message, TextPart, message_text
from toolang.base.types.run import RunResult

from ..common.ids import RUN_ID_FAMILY, allocate_id
from .. import agents
from ..lang.ast import AgicDecl, FlowDecl, Parameter
from ..plugin import load_loop as _default_load_loop
from .assembly import RunInput
from .binding import RunBinding
from .context import RunContext
from .events import RunEnd, RunBegin, StepEnd, StepBegin, TraceEventHandler
from .model import resolve_model
from .records import (
    InputRef,
    RunStatus,
    StepStatus,
    trace_child_path,
)

if TYPE_CHECKING:
    from ..up import UptimeContext

Value = Any
CallKind = Literal["top", "hand", "handoff", "stage"]

_UNSET = object()
_DEFAULT_PARENT_CURRENT = object()


@dataclass(slots=True)
class Frame:
    """Per-run variable environment."""

    vars: dict[str, Value] = field(default_factory=dict)
    current: Value = _UNSET

    @classmethod
    def from_invocation(
        cls,
        *,
        input_param: Parameter | None,
        input_value: Value,
        params: Mapping[str, Value],
    ) -> "Frame":
        variables = dict(params)
        current: Value = _UNSET
        if input_param is not None:
            variables[input_param.name] = input_value
            current = input_value
        return cls(vars=variables, current=current)

    def require_current(self) -> Value:
        if self.current is _UNSET:
            raise ToolangError("current value '_' is not set")
        return self.current

    def set_current(self, value: Value) -> None:
        self.current = value


@dataclass(slots=True)
class RunCtx:
    """Mutable execution state for one run."""

    binding: RunBinding
    root: str
    parent: str | None
    parent_step: int | None
    thread: str
    call: CallKind
    frame: Frame
    step: int = 0
    messages: list[Message] = field(default_factory=list)
    model_state: dict[str, Any] | None = None

    @property
    def id(self) -> str:
        return self.binding.run_id

    def next_step(self) -> int:
        self.step += 1
        return self.step


@dataclass(frozen=True, slots=True)
class ChildResult:
    run_id: str
    value: Value


class Executor:
    """Execute bound thunk and flow runs."""

    def __init__(
        self,
        context: UptimeContext,
        *,
        on_event: TraceEventHandler,
        consume_inputs: Callable[[str], Sequence[object]] | None = None,
        load_loop_func: Callable[[str], Any] = _default_load_loop,
        stream: bool = False,
    ) -> None:
        self._context = context
        self._on_event = on_event
        self._consume_inputs = consume_inputs
        self._load_loop = load_loop_func
        self._stream = stream

    async def execute_thunk(self, ctx: RunCtx, thunk: AgicDecl) -> Value:
        run_input = RunInput.from_thunk(self._context, ctx.binding, thunk)
        allowed_model_selectors = run_input.effective_model_selectors(self._context)
        model = resolve_model(
            self._context,
            selector=run_input.model_selector(self._context),
            allowed_selectors=allowed_model_selectors,
        )
        provider = self._context.model_providers[model.provider]
        model = provider.prepare_target(model)
        adapter = self._context.model_adapters.get(model.adapter)
        if adapter is None:
            raise ToolangError(f"unknown model adapter: {model.adapter}")
        loop = self._load_loop(ctx.binding.run_loop)
        run_context = RunContext(
            run_input,
            model,
            adapter,
            on_event=self._on_event,
            consume_inputs=self._pending_inputs_consumer(),
            stream=self._stream,
        )
        if ctx.binding.origin == "script":
            execution = await _run_script_loop(loop.run, run_context, run_id=ctx.id)
        else:
            execution = await asyncio.to_thread(loop.run, run_context)
        ctx.frame.set_current(execution.output_text)
        return execution.output_text

    async def execute_flow(self, ctx: RunCtx, flow: FlowDecl) -> Value:
        del ctx, flow
        raise ToolangError("Flow execution requires the new step executor.")

    async def execute_child_thunk(
        self,
        parent: RunCtx,
        thunk: AgicDecl,
        *,
        input_value: Value = _DEFAULT_PARENT_CURRENT,
        params: Mapping[str, Value] | None = None,
        call: CallKind,
        meta: Mapping[str, object] | None = None,
    ) -> ChildResult:
        step_index = parent.next_step()
        started_at = _utc_now()
        actual_input = parent.frame.require_current() if input_value is _DEFAULT_PARENT_CURRENT else input_value
        child_meta = _child_meta(thunk, meta or {}, live_program=parent.binding.live.program)
        child = self._create_child_ctx(
            parent,
            thunk,
            input_value=actual_input,
            params=params or {},
            call=call,
            parent_step_index=step_index,
            meta=child_meta,
        )
        self._emit_child_step_begin(
            parent,
            step_index=step_index,
            started_at=started_at,
            metadata={
                **child_meta,
                "target_kind": "thunk",
                "target": thunk.name,
                "call": call,
                "child_run_ids": (child.id,),
            },
        )
        self._emit_run_begin(child, executable_name=thunk.name, executable_kind="thunk")
        try:
            value = await self.execute_thunk(child, thunk)
        except Exception as exc:
            error = str(exc)
            self._emit_run_end(child, status="failed", error=error)
            self._emit_child_step_end(
                parent,
                step_index=step_index,
                started_at=started_at,
                child=child,
                target_kind="thunk",
                target=thunk.name,
                call=call,
                output=None,
                status="failed",
                error=error,
                meta=child_meta,
            )
            raise
        else:
            self._emit_run_end(child, status="finished")
            self._emit_child_step_end(
                parent,
                step_index=step_index,
                started_at=started_at,
                child=child,
                target_kind="thunk",
                target=thunk.name,
                call=call,
                output=value,
                meta=child_meta,
            )
        return ChildResult(run_id=child.id, value=value)

    def _pending_inputs_consumer(self):
        consume_inputs = self._consume_inputs
        if consume_inputs is None:
            return None
        return lambda run_id: consume_inputs(run_id)

    def _create_child_ctx(
        self,
        parent: RunCtx,
        executable: AgicDecl | FlowDecl,
        *,
        input_value: Value,
        params: Mapping[str, Value],
        call: CallKind,
        parent_step_index: int,
        meta: Mapping[str, object],
    ) -> RunCtx:
        run_id = _allocate_run_id(self._context)
        input_param = executable.input
        frame = Frame.from_invocation(
            input_param=input_param,
            input_value=input_value,
            params=params,
        )
        binding = RunBinding(
            run_id=run_id,
            group=parent.binding.group,
            origin=parent.binding.origin,
            thread_id=parent.thread,
            thunk_name=executable.name,
            input_text=_value_to_text(input_value),
            message=None,
            model_selector=parent.binding.model_selector,
            model_selectors=parent.binding.model_selectors,
            tool_selectors=parent.binding.tool_selectors,
            cap_selectors=parent.binding.cap_selectors,
            run_loop=parent.binding.run_loop,
            metadata={"invoke_params": dict(params), "child": dict(meta)},
            live=parent.binding.live,
            created_at=_utc_now(),
        )
        return RunCtx(
            binding=binding,
            root=parent.root,
            parent=parent.id,
            parent_step=parent_step_index,
            thread=parent.thread,
            call=call,
            frame=frame,
        )

    def _emit_run_begin(self, ctx: RunCtx, *, executable_name: str | None, executable_kind: str) -> None:
        self._on_event(
            RunBegin(
                run=ctx.id,
                parent=_child_parent_path(ctx),
                thread=ctx.thread,
                input=Message.user(ctx.binding.input_text),
                created_at=ctx.binding.created_at,
                started_at=ctx.binding.created_at,
                context={
                    **dict(ctx.binding.metadata),
                    "origin": ctx.binding.origin,
                    "root": ctx.root,
                    "executable": {"kind": executable_kind, "name": executable_name},
                    "call": ctx.call,
                },
            )
        )

    def _emit_run_end(self, ctx: RunCtx, *, status: RunStatus, error: str | None = None) -> None:
        self._on_event(
            RunEnd(
                run=ctx.id,
                status=status,
                error=error,
                finished_at=_utc_now(),
            )
        )

    def _emit_child_step_begin(
        self,
        parent: RunCtx,
        *,
        step_index: int,
        started_at: str,
        metadata: Mapping[str, object],
    ) -> None:
        self._on_event(
            StepBegin(
                step=trace_child_path(parent.id, step_index),
                kind="run",
                input=(InputRef(),),
                started_at=started_at,
                context=dict(metadata),
            )
        )

    def _emit_child_step_end(
        self,
        parent: RunCtx,
        *,
        step_index: int,
        started_at: str,
        child: RunCtx,
        target_kind: str,
        target: str | None,
        call: CallKind,
        output: Value,
        status: StepStatus = "finished",
        error: str | None = None,
        meta: Mapping[str, object],
    ) -> None:
        self._on_event(
            StepEnd(
                step=trace_child_path(parent.id, step_index),
                kind="run",
                status=status,
                output=(TextPart(text=_value_to_text(output)),),
                detail={
                    "call": call,
                    "target": {"kind": target_kind, "name": target},
                    "child_runs": [child.id],
                    "batch_index": _meta_int(meta, "batch_index"),
                    "lane": {"index": _meta_int(meta, "lane_index"), "count": _meta_int(meta, "parallelism")},
                    "item": {"index": _meta_int(meta, "item_index")},
                    "source": dict(meta),
                },
                started_at=started_at,
                finished_at=_utc_now(),
                error=error,
            )
        )


async def _run_script_loop(
    runner: Callable[[RunContext], RunResult],
    context: RunContext,
    *,
    run_id: str,
) -> RunResult:
    del run_id
    return await asyncio.to_thread(runner, context)


def _allocate_run_id(context: UptimeContext) -> str:
    value = allocate_id(
        agents.agent_id_state_path(context.root, context.name),
        family=RUN_ID_FAMILY,
    ).value
    return f"run_{value}"


def _child_parent_path(ctx: RunCtx) -> str | None:
    if ctx.parent is None:
        return None
    if ctx.parent_step is None:
        return ctx.parent
    return trace_child_path(ctx.parent, ctx.parent_step)


def _value_to_text(value: Value) -> str:
    if value is _UNSET or value is _DEFAULT_PARENT_CURRENT or value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, Message):
        return message_text(value.parts)
    return str(value)


def _meta_int(meta: Mapping[str, object], key: str) -> int | None:
    value = meta.get(key)
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value:
        return int(value)
    return None


def _child_meta(executable: AgicDecl | FlowDecl, meta: Mapping[str, object], *, live_program: Any) -> dict[str, object]:
    del executable, live_program
    return dict(meta)


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
