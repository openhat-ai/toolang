"""Core run executor for thunk and flow executions."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
import time
from typing import TYPE_CHECKING, Any, Literal

from toolang.base.error import ToolangError
from toolang.base.types.message import Message, TextPart, message_text
from toolang.base.types.run import ModelCall, RunResult

from ..common.ids import RUN_ID_FAMILY, allocate_id
from .. import agents
from ..lang.ast import Flow, FlowStage, MessageBlock, ParamDecl, Thunk
from ..plugin import load_loop as _default_load_loop
from .assembly import RunInput
from .binding import RunBinding
from .context import RunContext
from .events import PartEnd, PartStart, RunEnd, RunStart, StepEnd, StepStart, TraceEventHandler
from .input import effective_origin_model_selectors
from .model import resolve_model
from .records import (
    ChildCallStepPayload,
    FlowOpStepPayload,
    ModelCallStepPayload,
    RunCommandRef,
    RunStatus,
    StepKind,
    StepStatus,
)

if TYPE_CHECKING:
    from ..up import UptimeContext

Value = Any
CallKind = Literal["top", "hand", "handoff", "stage"]

_UNSET = object()
_DEFAULT_PARENT_CURRENT = object()
MAX_TOOL_ROUNDS = 8


@dataclass(slots=True)
class Frame:
    """Per-run variable environment."""

    vars: dict[str, Value] = field(default_factory=dict)
    current: Value = _UNSET

    @classmethod
    def from_invocation(
        cls,
        *,
        input_param: ParamDecl | None,
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


@dataclass(frozen=True, slots=True)
class ChildThunkCall:
    thunk: Thunk
    input_value: Value = _DEFAULT_PARENT_CURRENT
    params: Mapping[str, Value] = field(default_factory=dict)
    meta: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ChildFlowCall:
    flow: Flow
    input_value: Value = _DEFAULT_PARENT_CURRENT
    params: Mapping[str, Value] = field(default_factory=dict)
    meta: Mapping[str, object] = field(default_factory=dict)


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

    async def execute_thunk(self, ctx: RunCtx, thunk: Thunk) -> Value:
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

    async def _execute_inline_thunk(self, ctx: RunCtx, thunk: Thunk) -> Value:
        selectors = effective_origin_model_selectors(self._context, origin=ctx.binding.origin)
        model = resolve_model(
            self._context,
            selector=ctx.binding.model_selector,
            allowed_selectors=selectors,
        )
        provider = self._context.model_providers[model.provider]
        model = provider.prepare_target(model)
        adapter = self._context.model_adapters.get(model.adapter)
        if adapter is None:
            raise ToolangError(f"unknown model adapter: {model.adapter}")
        step_index = ctx.next_step()
        started_at = _utc_now()
        prompt = _render_inline_thunk(thunk, ctx)
        self._on_event(
            StepStart(
                run_id=ctx.id,
                thread_id=ctx.thread,
                step_index=step_index,
                kind="model",
                input=(RunCommandRef(),),
                started_at=started_at,
            )
        )
        result = await asyncio.to_thread(
            adapter.invoke,
            model,
            ModelCall(
                instructions="",
                messages=[Message.user(prompt)],
                tools=(),
                state=ctx.model_state,
            ),
        )
        output_text = message_text(result.message.parts) if result.message is not None else ""
        part = TextPart(text=output_text)
        self._on_event(
            PartStart(
                run_id=ctx.id,
                thread_id=ctx.thread,
                step_index=step_index,
                part_index=0,
                kind="text",
            )
        )
        self._on_event(
            PartEnd(
                run_id=ctx.id,
                thread_id=ctx.thread,
                step_index=step_index,
                part_index=0,
                data=part,
            )
        )
        ctx.model_state = result.state
        ctx.frame.set_current(output_text)
        self._on_event(
            StepEnd(
                run_id=ctx.id,
                thread_id=ctx.thread,
                step_index=step_index,
                kind="model",
                status="finished",
                output=(part,),
                payload=ModelCallStepPayload(
                    model_ref=model.ref,
                    input_tokens=result.usage.input_tokens if result.usage is not None else 0,
                    output_tokens=result.usage.output_tokens if result.usage is not None else 0,
                    provider=model.provider,
                    model=model.model,
                    adapter=model.adapter,
                    base_url=model.base_url,
                ),
                started_at=started_at,
                finished_at=_utc_now(),
            )
        )
        return output_text

    async def execute_flow(self, ctx: RunCtx, flow: Flow) -> Value:
        total = len(flow.stages)
        for index, stage in enumerate(flow.stages):
            if stage.kind in {"do", "bare"}:
                await self._run_do_stage(ctx, stage, index=index, total=total)
                continue
            if stage.kind == "each":
                await self._run_each_stage(ctx, stage, index=index, total=total)
                continue
            if stage.kind == "rank":
                await self._run_rank_stage(ctx, stage, index=index, total=total)
                continue
            if stage.kind in {"keep", "drop"}:
                await self._run_filter_stage(ctx, stage, index=index, total=total)
                continue
            raise ToolangError(f"Flow stage {stage.kind!r} is not supported yet.")
        return ctx.frame.require_current()

    async def execute_child_thunk(
        self,
        parent: RunCtx,
        thunk: Thunk,
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
        self._emit_child_step_start(
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
        self._emit_run_start(child, executable_name=thunk.name, executable_kind="thunk")
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

    async def execute_child_flow(
        self,
        parent: RunCtx,
        flow: Flow,
        *,
        input_value: Value = _DEFAULT_PARENT_CURRENT,
        params: Mapping[str, Value] | None = None,
        call: CallKind,
        meta: Mapping[str, object] | None = None,
    ) -> ChildResult:
        step_index = parent.next_step()
        started_at = _utc_now()
        actual_input = parent.frame.require_current() if input_value is _DEFAULT_PARENT_CURRENT else input_value
        child_meta = _child_meta(flow, meta or {}, live_program=parent.binding.live.program)
        child = self._create_child_ctx(
            parent,
            flow,
            input_value=actual_input,
            params=params or {},
            call=call,
            parent_step_index=step_index,
            meta=child_meta,
        )
        self._emit_child_step_start(
            parent,
            step_index=step_index,
            started_at=started_at,
            metadata={
                **child_meta,
                "target_kind": "flow",
                "target": flow.name,
                "call": call,
                "child_run_ids": (child.id,),
            },
        )
        self._emit_run_start(child, executable_name=flow.name, executable_kind="flow")
        try:
            value = await self.execute_flow(child, flow)
        except Exception as exc:
            error = str(exc)
            self._emit_run_end(child, status="failed", error=error)
            self._emit_child_step_end(
                parent,
                step_index=step_index,
                started_at=started_at,
                child=child,
                target_kind="flow",
                target=flow.name,
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
                target_kind="flow",
                target=flow.name,
                call=call,
                output=value,
                meta=child_meta,
            )
        return ChildResult(run_id=child.id, value=value)

    async def execute_child_thunk_batch(
        self,
        parent: RunCtx,
        calls: Sequence[ChildThunkCall],
        *,
        call: CallKind,
        batch_size: int,
    ) -> tuple[ChildResult, ...]:
        item_count = len(calls)
        parallelism = max(batch_size, 1)
        queue: asyncio.Queue[int] = asyncio.Queue()
        for index in range(item_count):
            queue.put_nowait(index)
        results: list[ChildResult | None] = [None] * item_count

        async def run_lane(lane_index: int) -> None:
            while True:
                try:
                    item_index = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                item = calls[item_index]
                try:
                    results[item_index] = await self.execute_child_thunk(
                        parent,
                        item.thunk,
                        input_value=item.input_value,
                        params=item.params,
                        call=call,
                        meta=_lane_meta(
                            item.meta,
                            parallelism=parallelism,
                            lane_index=lane_index,
                            item_count=item_count,
                        ),
                    )
                finally:
                    queue.task_done()

        await asyncio.gather(*(run_lane(lane_index) for lane_index in range(min(parallelism, max(item_count, 1)))))
        return tuple(item for item in results if item is not None)

    async def _run_do_stage(self, ctx: RunCtx, stage: FlowStage, *, index: int, total: int) -> Value:
        input_value = ctx.frame.require_current()
        self._record_flow_op(ctx, "prepare_do", stage=stage, index=index, total=total, input_value=input_value)
        result: ChildResult | None = None
        if stage.targets:
            for target in stage.targets:
                result = await self._execute_stage_target(
                    ctx,
                    stage,
                    index=index,
                    input_value=_DEFAULT_PARENT_CURRENT,
                    target=target,
                )
                ctx.frame.set_current(result.value)
        else:
            result = await self._execute_stage_target(
                ctx,
                stage,
                index=index,
                input_value=_DEFAULT_PARENT_CURRENT,
            )
            ctx.frame.set_current(result.value)
        if result is None:
            raise ToolangError("Flow 'do' stage requires a target or body.")
        self._record_flow_op(ctx, "set_current", stage=stage, index=index, total=total, input_value=input_value, output=result.value)
        return result.value

    async def _run_each_stage(self, ctx: RunCtx, stage: FlowStage, *, index: int, total: int) -> Value:
        input_value = ctx.frame.require_current()
        items = _as_items(ctx.frame.require_current())
        self._record_flow_op(ctx, "prepare_each", stage=stage, index=index, total=total, input_value=input_value, output={"count": len(items)})
        if stage.targets and len(stage.targets) > 1:
            raise ToolangError("Flow 'each' stage accepts only one target.")
        thunk = self._stage_thunk(ctx, stage)
        calls = [
            ChildThunkCall(
                thunk=thunk,
                input_value=item,
                meta=_stage_meta(ctx, stage, index, total=total, input_value=input_value, item_index=item_index),
            )
            for item_index, item in enumerate(items)
        ]
        results = await self.execute_child_thunk_batch(
            ctx,
            calls,
            call="stage",
            batch_size=stage.parallelism or 1,
        )
        output = [item.value for item in results]
        ctx.frame.set_current(output)
        self._record_flow_op(ctx, "set_current", stage=stage, index=index, total=total, input_value=input_value, output={"count": len(output)})
        return output

    async def _run_rank_stage(self, ctx: RunCtx, stage: FlowStage, *, index: int, total: int) -> Value:
        input_value = ctx.frame.require_current()
        items = _as_items(ctx.frame.require_current())
        self._record_flow_op(ctx, "prepare_rank", stage=stage, index=index, total=total, input_value=input_value, output={"count": len(items)})
        if stage.targets and len(stage.targets) > 1:
            raise ToolangError("Flow 'rank' stage accepts only one target.")
        thunk = self._stage_thunk(ctx, stage)
        calls = [
            ChildThunkCall(
                thunk=thunk,
                input_value=item,
                meta=_stage_meta(ctx, stage, index, total=total, input_value=input_value, item_index=item_index),
            )
            for item_index, item in enumerate(items)
        ]
        results = await self.execute_child_thunk_batch(
            ctx,
            calls,
            call="stage",
            batch_size=stage.parallelism or 1,
        )
        scored = sorted(
            zip(items, results, strict=True),
            key=lambda item: _score_value(item[1].value),
            reverse=True,
        )
        limit = stage.limit if stage.limit is not None else len(scored)
        output = [item for item, _result in scored[:limit]]
        ctx.frame.set_current(output)
        self._record_flow_op(ctx, "set_current", stage=stage, index=index, total=total, input_value=input_value, output={"count": len(output)})
        return output

    async def _run_filter_stage(self, ctx: RunCtx, stage: FlowStage, *, index: int, total: int) -> Value:
        input_value = ctx.frame.require_current()
        items = _as_items(ctx.frame.require_current())
        self._record_flow_op(ctx, f"prepare_{stage.kind}", stage=stage, index=index, total=total, input_value=input_value, output={"count": len(items)})
        if stage.targets and len(stage.targets) > 1:
            raise ToolangError(f"Flow '{stage.kind}' stage accepts only one target.")
        thunk = self._stage_thunk(ctx, stage)
        calls = [
            ChildThunkCall(
                thunk=thunk,
                input_value=item,
                meta=_stage_meta(ctx, stage, index, total=total, input_value=input_value, item_index=item_index),
            )
            for item_index, item in enumerate(items)
        ]
        results = await self.execute_child_thunk_batch(
            ctx,
            calls,
            call="stage",
            batch_size=stage.parallelism or 1,
        )
        kept = [
            item
            for item, result in zip(items, results, strict=True)
            if _truthy_value(result.value) == (stage.kind == "keep")
        ]
        ctx.frame.set_current(kept)
        self._record_flow_op(ctx, "set_current", stage=stage, index=index, total=total, input_value=input_value, output={"count": len(kept)})
        return kept

    async def _execute_stage_target(
        self,
        ctx: RunCtx,
        stage: FlowStage,
        *,
        index: int,
        input_value: Value,
        target: str | None = None,
    ) -> ChildResult:
        target_name = target or stage.target
        if target_name is not None:
            flow = ctx.binding.live.program.parsed.get_flow(target_name)
            if flow is not None:
                return await self.execute_child_flow(
                    ctx,
                    flow,
                    input_value=input_value,
                    call="stage",
                    meta=_stage_meta(ctx, stage, index, total=None, input_value=ctx.frame.current),
                )
            thunk = ctx.binding.live.program.get_thunk(target_name)
            return await self.execute_child_thunk(
                ctx,
                thunk,
                input_value=input_value,
                call="stage",
                meta=_stage_meta(ctx, stage, index, total=None, input_value=ctx.frame.current),
            )
        return await self.execute_child_thunk(
            ctx,
            self._stage_thunk(ctx, stage),
            input_value=input_value,
            call="stage",
            meta=_stage_meta(ctx, stage, index, total=None, input_value=ctx.frame.current),
        )

    def _stage_thunk(self, ctx: RunCtx, stage: FlowStage) -> Thunk:
        if stage.target is not None:
            return ctx.binding.live.program.get_thunk(stage.target)
        body = stage.body
        if body is None:
            raise ToolangError(f"Flow stage {stage.kind!r} requires a target or body.")
        return Thunk(
            name=None,
            input=ParamDecl(name="in", type_name="Text"),
            messages=(MessageBlock(kind="user", text=body, span=stage.span),),
            span=stage.span,
        )

    def _pending_inputs_consumer(self):
        consume_inputs = self._consume_inputs
        if consume_inputs is None:
            return None
        return lambda run_id: consume_inputs(run_id)

    def _create_child_ctx(
        self,
        parent: RunCtx,
        executable: Thunk | Flow,
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

    def _emit_run_start(self, ctx: RunCtx, *, executable_name: str | None, executable_kind: str) -> None:
        self._on_event(
            RunStart(
                run_id=ctx.id,
                origin=ctx.binding.origin,
                thread_id=ctx.thread,
                input=Message.user(ctx.binding.input_text),
                created_at=ctx.binding.created_at,
                started_at=ctx.binding.created_at,
                root_run_id=ctx.root,
                parent_run_id=ctx.parent,
                parent_step_index=ctx.parent_step,
                executable_kind=executable_kind,
                executable_name=executable_name,
                call_kind=ctx.call,
                metadata=dict(ctx.binding.metadata),
            )
        )

    def _emit_run_end(self, ctx: RunCtx, *, status: RunStatus, error: str | None = None) -> None:
        self._on_event(
            RunEnd(
                run_id=ctx.id,
                thread_id=ctx.thread,
                status=status,
                error=error,
                finished_at=_utc_now(),
            )
        )

    def _emit_child_step_start(
        self,
        parent: RunCtx,
        *,
        step_index: int,
        started_at: str,
        metadata: Mapping[str, object],
    ) -> None:
        self._on_event(
            StepStart(
                run_id=parent.id,
                thread_id=parent.thread,
                step_index=step_index,
                kind="run",
                input=(RunCommandRef(),),
                started_at=started_at,
                metadata=dict(metadata),
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
                run_id=parent.id,
                thread_id=parent.thread,
                step_index=step_index,
                kind="run",
                status=status,
                output=(TextPart(text=_value_to_text(output)),),
                payload=ChildCallStepPayload(
                    call=call,
                    target_kind=target_kind,
                    target=target,
                    child_run_ids=(child.id,),
                    batch_index=_meta_int(meta, "batch_index"),
                    parallelism=_meta_int(meta, "parallelism"),
                    lane_index=_meta_int(meta, "lane_index"),
                    stage_index=_meta_int(meta, "stage_index"),
                    stage_kind=_meta_str(meta, "stage_kind"),
                    item_indexes=(() if _meta_int(meta, "item_index") is None else (_meta_int(meta, "item_index") or 0,)),
                    metadata=dict(meta),
                ),
                started_at=started_at,
                finished_at=_utc_now(),
                error=error,
            )
        )

    def _record_flow_op(
        self,
        ctx: RunCtx,
        op: str,
        *,
        stage: FlowStage,
        index: int,
        total: int | None,
        input_value: Value,
        output: Value | None = None,
    ) -> None:
        step_index = ctx.next_step()
        now = _utc_now()
        metadata = {"op": op, **_stage_meta(ctx, stage, index, total=total, input_value=input_value)}
        kind = _flow_op_step_kind(op=op, stage=stage)
        self._on_event(
            StepStart(
                run_id=ctx.id,
                thread_id=ctx.thread,
                step_index=step_index,
                kind=kind,
                input=(RunCommandRef(),),
                started_at=now,
                metadata=metadata,
            )
        )
        self._on_event(
            StepEnd(
                run_id=ctx.id,
                thread_id=ctx.thread,
                step_index=step_index,
                kind=kind,
                status="finished",
                output=(TextPart(text=_value_to_text(output)),) if output is not None else (),
                payload=FlowOpStepPayload(
                    op=op,
                    stage_index=index,
                    stage_kind=stage.kind,
                    output_preview=_json_preview(output),
                    metadata=metadata,
                ),
                started_at=now,
                finished_at=_utc_now(),
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


async def _run_bounded_ordered(
    items: Sequence[Any],
    *,
    limit: int,
    fn: Callable[[Any], Awaitable[ChildResult]],
) -> tuple[ChildResult, ...]:
    if not items:
        return ()
    semaphore = asyncio.Semaphore(max(limit, 1))
    results: list[ChildResult | None] = [None] * len(items)

    async def run_one(index: int, item: Any) -> None:
        async with semaphore:
            results[index] = await fn(item)

    await asyncio.gather(*(run_one(index, item) for index, item in enumerate(items)))
    return tuple(item for item in results if item is not None)


def _allocate_run_id(context: UptimeContext) -> str:
    value = allocate_id(
        agents.agent_id_state_path(context.root, context.name),
        family=RUN_ID_FAMILY,
    ).value
    return f"run_{value}"


def _as_items(value: Value) -> list[Value]:
    if isinstance(value, list):
        return value
    text = _value_to_text(value)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines or [value]


def _score_value(value: Value) -> float:
    text = _value_to_text(value).strip()
    try:
        return float(text.split()[0])
    except (ValueError, IndexError):
        return 0.0


def _flow_op_step_kind(*, op: str, stage: FlowStage) -> StepKind:
    if op == "set_current":
        return "bind"
    if stage.kind in {"each", "rank", "keep", "drop"}:
        return "parallel"
    return "step"


def _truthy_value(value: Value) -> bool:
    text = _value_to_text(value).strip().lower()
    if text in {"true", "yes", "y", "1", "keep"}:
        return True
    if text in {"false", "no", "n", "0", "drop"}:
        return False
    return bool(text)


def _value_to_text(value: Value) -> str:
    if value is _UNSET or value is _DEFAULT_PARENT_CURRENT or value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, Message):
        return message_text(value.parts)
    return str(value)


def _render_inline_thunk(thunk: Thunk, ctx: RunCtx) -> str:
    text = thunk.messages_text() or ctx.binding.input_text
    current = "" if ctx.frame.current is _UNSET else _value_to_text(ctx.frame.current)
    values = {key: _value_to_text(value) for key, value in ctx.frame.vars.items()}
    values["_"] = current
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", value)
        text = text.replace("{{ " + key + " }}", value)
    if current and current not in text:
        return f"{text}\n\n{current}".strip()
    return text.strip()


def _json_preview(value: Value) -> Any:
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return {"type": "list", "count": len(value)}
    if isinstance(value, dict):
        count = value.get("count")
        if isinstance(count, int):
            return {"count": count}
        return {"type": "object", "keys": sorted(str(key) for key in value)[:20]}
    return _value_to_text(value)


def _meta_int(meta: Mapping[str, object], key: str) -> int | None:
    value = meta.get(key)
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value:
        return int(value)
    return None


def _meta_str(meta: Mapping[str, object], key: str) -> str | None:
    value = meta.get(key)
    return str(value) if value is not None else None


def _child_meta(executable: Thunk | Flow, meta: Mapping[str, object], *, live_program: Any) -> dict[str, object]:
    child_meta = dict(meta)
    if executable.name is None and "source_line" not in child_meta:
        child_meta["source_line"] = _source_line(live_program, executable.span.line)
    return child_meta


def _stage_meta(
    ctx: RunCtx,
    stage: FlowStage,
    index: int,
    *,
    total: int | None,
    input_value: Value,
    item_index: int | None = None,
) -> dict[str, object]:
    title = _stage_title(ctx, stage)
    data: dict[str, object] = {
        "stage_index": index,
        "stage_kind": stage.kind,
        "stage_line": _source_line(ctx.binding.live.program, stage.span.line),
        "stage_label": _stage_label(ctx, stage),
        "stage_title": title,
        "input_preview": _json_preview(input_value),
    }
    if total is not None:
        data["stage_total"] = total
    if stage.doc is not None:
        data["stage_doc"] = stage.doc
    target = _stage_target_label(ctx, stage)
    if target:
        data["stage_target"] = target
    if stage.parallelism is not None:
        data["parallelism"] = stage.parallelism
    if item_index is not None:
        data["item_index"] = item_index
    return data


def _lane_meta(
    meta: Mapping[str, object],
    *,
    parallelism: int,
    lane_index: int,
    item_count: int,
) -> dict[str, object]:
    data = dict(meta)
    item_index = _meta_int(data, "item_index")
    if item_index is not None:
        data["item_count"] = item_count
    if parallelism > 1:
        data["parallelism"] = parallelism
        data["lane_index"] = lane_index
    return data


def _stage_label(ctx: RunCtx, stage: FlowStage) -> str:
    if stage.doc is not None and stage.doc.strip():
        return f"{stage.kind}: {_stage_title(ctx, stage)}"
    parts: list[str] = [stage.kind]
    if stage.limit is not None:
        parts.append(f"top={stage.limit}")
    target = _stage_target_label(ctx, stage)
    if target:
        parts.append(target)
    if stage.parallelism is not None:
        parts.append(f"par={stage.parallelism}")
    if stage.count is not None:
        parts.append(f"count={stage.count}")
    return " ".join(parts)


def _stage_title(ctx: RunCtx, stage: FlowStage) -> str:
    if stage.doc is not None and stage.doc.strip():
        return _one_line(stage.doc)
    target = _stage_target_label(ctx, stage)
    return target or stage.kind


def _stage_target_label(ctx: RunCtx, stage: FlowStage) -> str:
    if stage.targets:
        return ",".join(stage.targets)
    if stage.target is not None:
        return stage.target
    if stage.body is not None:
        return f"thunk:<L{_source_line(ctx.binding.live.program, stage.span.line)}>"
    return ""


def _one_line(text: str) -> str:
    return " ".join(part.strip() for part in text.splitlines() if part.strip())


def _source_line(live_program: Any, line: int) -> int:
    return line + _program_body_line_offset(
        source_text=live_program.source_text,
        body_text=live_program.body_text,
    )


def _program_body_line_offset(*, source_text: str, body_text: str) -> int:
    body_lines = body_text.splitlines()
    if not body_lines:
        return 0
    source_lines = source_text.splitlines()
    body_len = len(body_lines)
    for index in range(0, len(source_lines) - body_len + 1):
        if source_lines[index : index + body_len] == body_lines:
            return index
    return 0


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
