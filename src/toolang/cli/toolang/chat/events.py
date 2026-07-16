"""Trace-event handling for chat mutable blocks."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, cast

from toolang.execution.events import (
    PartBegin,
    PartDelta,
    PartEnd,
    RunBegin,
    RunEnd,
    RunSteering,
    RunStopping,
    RunWaiting,
    RunStarting,
    StepBegin,
    StepEnd,
    TraceEvent,
)
from toolang.execution.records import trace_run

from .base import AppContext
from .blocks import (
    ChildRunStepBlock,
    DefaultStepBlock,
    FlowStepBlock,
    GenericCommandBlock,
    ModelStepBlock,
    MutableBlock,
    RunStartBlock,
    RunSteerBlock,
    RunStopBlock,
    ToolStepBlock,
)


@dataclass(frozen=True, slots=True)
class MutableBlockKey:
    run_id: str
    family: Literal["command", "step", "run"]
    identity: str


def handle_trace_event(event: TraceEvent, app: AppContext) -> None:
    """Apply one trace event to live mutable blocks."""

    if _should_set_active_run(event, app) and (run_id := event_run_id(event)):
        app.set_active_run(run_id)

    if event.type in {"run_waiting", "run_starting"}:
        _create_or_update_block(event, app, command_block)
    elif event.type == "run_steering":
        _create_or_update_block(event, app, command_block)
    elif event.type == "run_stopping":
        _create_or_update_block(event, app, run_stop_block)
    elif event.type == "run_begin":
        _update_and_finalize_blocks(event, app, families={"command"})
        if cast(RunBegin, event).run == app.get_active_run():
            _ensure_tail_block(event, app, run_stop_block)
    elif event.type == "step_begin":
        _update_and_finalize_blocks(event, app, families={"command"}, all_matching=True)
        _ensure_block(event, app, step_block)
    elif event.type in {"part_begin", "part_end", "part_delta"}:
        if block := _find_block(event, app):
            block.update(event)
    elif event.type == "step_end":
        _update_and_finalize_blocks(event, app, families={"step"})
    elif event.type == "run_end":
        run_end = cast(RunEnd, event)
        _update_and_finalize_blocks(
            run_end, app, families={"command", "step"}, all_matching=True
        )
        if run_end.run == app.get_active_run():
            if block := _find_block(run_end, app):
                _finalize_block(block, app, run_end)
            else:
                app.finalize_block(run_stop_block(run_end))
            app.finish_run()


def flush_live_blocks(app: AppContext) -> None:
    """Move all currently live blocks into scrollback without a trace update."""

    for block in list(app.get_live_blocks()):
        app.finalize_block(block)


def handle_runtime_error(app: AppContext, message: str) -> bool:
    """Finalize live blocks and the active run after a runtime request error."""

    active_run_id = app.get_active_run()
    if active_run_id is None and not app.get_live_blocks():
        return False
    flush_live_blocks(app)
    app.finalize_block(
        run_stop_block(
            RunEnd(
                run=active_run_id or "run",
                status="failed",
                finished_at="",
                error=message,
            )
        )
    )
    app.finish_run()
    return True


def _ensure_block(
    event: TraceEvent,
    app: AppContext,
    create: Callable[[TraceEvent], MutableBlock],
) -> MutableBlock:
    if block := _find_block(event, app):
        return block
    block = create(event)
    _insert_live_block(app, block)
    return block


def _ensure_tail_block(
    event: TraceEvent,
    app: AppContext,
    create: Callable[[TraceEvent], MutableBlock],
) -> MutableBlock:
    if block := _find_block(event, app):
        return block
    block = create(event)
    app.get_live_blocks().append(block)
    return block


def _create_or_update_block(
    event: TraceEvent,
    app: AppContext,
    create: Callable[[TraceEvent], MutableBlock],
) -> MutableBlock:
    if block := _find_block(event, app):
        block.update(event)
        return block
    block = create(event)
    _insert_live_block(app, block)
    return block


def _insert_live_block(app: AppContext, block: MutableBlock) -> None:
    live_blocks = app.get_live_blocks()
    active_run_id = app.get_active_run() or ""
    key = _block_key(block, active_run_id=active_run_id)
    if key is not None and key.family == "run":
        live_blocks.append(block)
        return
    for index in range(len(live_blocks) - 1, -1, -1):
        block_key = _block_key(live_blocks[index], active_run_id=active_run_id)
        if block_key is not None and block_key.family == "run":
            live_blocks.insert(index, block)
            return
    live_blocks.append(block)


def _find_block(event: TraceEvent, app: AppContext) -> MutableBlock | None:
    active_run_id = app.get_active_run() or ""
    key = event_key(event, run_id=active_run_id)
    if key is None:
        return None
    for block in reversed(app.get_live_blocks()):
        if _block_key(block, active_run_id=active_run_id) == key:
            return block
    return None


def _update_and_finalize_blocks(
    event: TraceEvent,
    app: AppContext,
    *,
    families: set[str],
    all_matching: bool = False,
) -> None:
    active_run_id = app.get_active_run() or ""
    key = event_key(event, run_id=active_run_id)
    target_run_id = event_run_id(event) or active_run_id
    live_blocks = app.get_live_blocks()
    blocks = list(live_blocks) if all_matching else list(reversed(live_blocks))
    for block in blocks:
        block_key = _block_key(block, active_run_id=active_run_id)
        if block_key is None or block_key.family not in families:
            continue
        if all_matching and block_key.run_id != target_run_id:
            continue
        if all_matching or key is None or block_key == key:
            _finalize_block(block, app, event)


def _finalize_block(block: MutableBlock, app: AppContext, event: TraceEvent) -> None:
    block.update(event)
    app.finalize_block(block)


def _block_key(block: MutableBlock, *, active_run_id: str) -> MutableBlockKey | None:
    run_id = getattr(block, "run_id", None) or active_run_id
    if block.type == "RunStartBlock":
        return MutableBlockKey(run_id, "command", "0")
    if block.type == "RunSteerBlock":
        return MutableBlockKey(run_id, "command", str(getattr(block, "index", 0)))
    if block.type in {
        "ChildRunStepBlock",
        "DefaultStepBlock",
        "FlowStepBlock",
        "ModelStepBlock",
        "ToolStepBlock",
    }:
        step = str(getattr(block, "step", ""))
        return MutableBlockKey(trace_run(step), "step", step)
    if block.type == "RunStopBlock":
        return MutableBlockKey(run_id, "run", "end")
    return None


def command_block(event: TraceEvent) -> MutableBlock:
    if event.type in {"run_starting", "run_waiting"}:
        return RunStartBlock.create(cast(RunStarting | RunWaiting, event))
    if event.type == "run_steering":
        return RunSteerBlock.create(cast(RunSteering, event))
    return GenericCommandBlock.create(event)


def step_block(event: TraceEvent) -> MutableBlock:
    step_begin = cast(StepBegin, event)
    if step_begin.kind == "model":
        return ModelStepBlock.create(step_begin)
    if step_begin.kind == "tool":
        return ToolStepBlock.create(step_begin)
    if step_begin.kind == "run":
        return ChildRunStepBlock.create(step_begin)
    if step_begin.context.get("statement"):
        return FlowStepBlock.create(step_begin)
    return DefaultStepBlock.create(step_begin)


def run_stop_block(event: TraceEvent) -> RunStopBlock:
    return RunStopBlock.create(cast(RunBegin | RunStopping | RunEnd, event))


def event_key(event: TraceEvent, *, run_id: str = "") -> MutableBlockKey | None:
    event_run = event_run_id(event) or run_id
    if event.type in {"run_waiting", "run_starting", "run_begin"}:
        return MutableBlockKey(event_run, "command", "0")
    if event.type == "run_steering":
        return MutableBlockKey(
            event_run,
            "command",
            str(cast(RunSteering, event).cmd),
        )
    if event.type == "run_stopping":
        return MutableBlockKey(event_run, "run", "end")
    if event.type in {
        "step_begin",
        "part_begin",
        "part_delta",
        "part_end",
        "step_end",
    }:
        step = cast(StepBegin | PartBegin | PartDelta | PartEnd | StepEnd, event).step
        return MutableBlockKey(event_run, "step", step)
    if event.type == "run_end":
        return MutableBlockKey(event_run, "run", "end")
    return None


def event_run_id(event: TraceEvent) -> str | None:
    if event.type in {
        "run_waiting",
        "run_starting",
        "run_steering",
        "run_stopping",
        "run_begin",
        "run_end",
    }:
        return cast(
            RunWaiting | RunStarting | RunSteering | RunStopping | RunBegin | RunEnd,
            event,
        ).run
    if event.type in {"step_begin", "part_begin", "part_delta", "part_end", "step_end"}:
        return trace_run(
            cast(StepBegin | PartBegin | PartDelta | PartEnd | StepEnd, event).step
        )
    return None


def _should_set_active_run(event: TraceEvent, app: AppContext) -> bool:
    if event.type in {"run_waiting", "run_starting"}:
        return cast(RunWaiting | RunStarting, event).parent is None
    if event.type == "run_begin":
        return app.get_active_run() in {None, cast(RunBegin, event).run}
    return app.get_active_run() is None
