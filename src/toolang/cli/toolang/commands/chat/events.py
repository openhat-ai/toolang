"""Run-event handling for terminal chat blocks."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from toolang.execution.events import (
    PartBegin,
    PartDelta,
    PartEnd,
    RunBegin,
    RunEnd,
    RunEvent,
    StepBegin,
    StepEnd,
)
from toolang.execution.records import trace_run

from .base import AppContext
from .blocks import (
    ChildRunStepBlock,
    DefaultStepBlock,
    FlowStepBlock,
    ModelStepBlock,
    MutableBlock,
    RunStartBlock,
    RunSteerBlock,
    RunStopBlock,
    ToolStepBlock,
)

BlockFamily = Literal["command", "step", "run"]
KeyedRunEvent = RunBegin | StepBegin | PartDelta | StepEnd | RunEnd
ChatUIEventType = Literal[
    "submit",
    "run_event",
    "run_error",
    "cancel_error",
    "steer_error",
    "interrupt",
    "eof",
    "cancel",
    "clear",
    "quit",
]


@dataclass(frozen=True, slots=True)
class ChatUIEvent:
    """One input or execution event consumed by the chat UI."""

    type: ChatUIEventType
    value: str | RunEvent | None = None


@dataclass(frozen=True, slots=True)
class MutableBlockKey:
    run_id: str
    family: BlockFamily
    identity: str


def handle_run_event(event: RunEvent, app: AppContext) -> None:
    """Apply one ordered run event to live mutable blocks."""

    if isinstance(event, RunBegin):
        if _is_root_begin(event) and app.get_active_run() in {None, event.run}:
            app.set_active_run(event.run)
        _update_and_finalize_blocks(event, app, families={"command"})
        if event.run == app.get_active_run():
            _ensure_tail_block(event, app, RunStopBlock.create)
        return

    if isinstance(event, StepBegin):
        _update_and_finalize_blocks(
            event,
            app,
            families={"command"},
            all_matching=True,
        )
        _ensure_block(event, app, step_block)
        return

    if isinstance(event, PartDelta):
        if block := _find_block(event, app):
            if isinstance(block, ModelStepBlock):
                block.update(event)
        return

    if isinstance(event, (PartBegin, PartEnd)):
        return

    if isinstance(event, StepEnd):
        _update_and_finalize_blocks(event, app, families={"step"})
        return

    run_end = event
    _update_and_finalize_blocks(
        run_end,
        app,
        families={"command", "step"},
        all_matching=True,
    )
    if run_end.run != app.get_active_run():
        return
    if block := _find_block(run_end, app):
        _finalize_block(block, app, run_end)
    else:
        app.finalize_block(RunStopBlock.create(run_end))
    app.finish_run()


def _flush_live_blocks(app: AppContext) -> None:
    """Move all currently live blocks into scrollback without a run update."""

    for block in list(app.get_live_blocks()):
        app.finalize_block(block)


def handle_run_error(app: AppContext, message: str) -> bool:
    """Finalize live blocks and the active run after an execution error."""

    active_run_id = app.get_active_run()
    if active_run_id is None and not app.get_live_blocks():
        return False
    _flush_live_blocks(app)
    app.finalize_block(
        RunStopBlock(
            run_id=active_run_id or "run",
            status="failed",
            error=message,
        )
    )
    app.finish_run()
    return True


def _ensure_block(
    event: StepBegin,
    app: AppContext,
    create: Callable[[StepBegin], MutableBlock],
) -> MutableBlock:
    if block := _find_block(event, app):
        return block
    block = create(event)
    _insert_live_block(app, block)
    return block


def _ensure_tail_block(
    event: RunBegin,
    app: AppContext,
    create: Callable[[RunBegin], MutableBlock],
) -> MutableBlock:
    if block := _find_block(event, app):
        return block
    block = create(event)
    app.get_live_blocks().append(block)
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


def _find_block(event: KeyedRunEvent, app: AppContext) -> MutableBlock | None:
    active_run_id = app.get_active_run() or ""
    key = _event_key(event)
    for block in reversed(app.get_live_blocks()):
        if _block_key(block, active_run_id=active_run_id) == key:
            return block
    return None


def _update_and_finalize_blocks(
    event: KeyedRunEvent,
    app: AppContext,
    *,
    families: set[BlockFamily],
    all_matching: bool = False,
) -> None:
    active_run_id = app.get_active_run() or ""
    key = _event_key(event)
    target_run_id = _event_run_id(event)
    live_blocks = app.get_live_blocks()
    candidates = list(live_blocks) if all_matching else list(reversed(live_blocks))
    for block in candidates:
        block_key = _block_key(block, active_run_id=active_run_id)
        if block_key is None or block_key.family not in families:
            continue
        if all_matching and block_key.run_id != target_run_id:
            continue
        if all_matching or block_key == key:
            _finalize_block(block, app, event)


def _finalize_block(
    block: MutableBlock,
    app: AppContext,
    event: KeyedRunEvent,
) -> None:
    block.update(event)
    app.finalize_block(block)


def _block_key(block: MutableBlock, *, active_run_id: str) -> MutableBlockKey | None:
    if isinstance(block, RunStartBlock):
        return MutableBlockKey(block.run_id or active_run_id, "command", "start")
    if isinstance(block, RunSteerBlock):
        return MutableBlockKey(block.run_id or active_run_id, "command", "steer")
    if isinstance(
        block,
        (
            ChildRunStepBlock,
            DefaultStepBlock,
            FlowStepBlock,
            ModelStepBlock,
            ToolStepBlock,
        ),
    ):
        return MutableBlockKey(trace_run(block.step), "step", block.step)
    if isinstance(block, RunStopBlock):
        return MutableBlockKey(block.run_id or active_run_id, "run", "end")
    return None


def step_block(event: StepBegin) -> MutableBlock:
    if event.kind == "model":
        return ModelStepBlock.create(event)
    if event.kind == "tool":
        return ToolStepBlock.create(event)
    if event.kind == "run":
        return ChildRunStepBlock.create(event)
    if event.given.get("statement"):
        return FlowStepBlock.create(event)
    return DefaultStepBlock.create(event)


def _event_key(event: KeyedRunEvent) -> MutableBlockKey:
    event_run = _event_run_id(event)
    if isinstance(event, RunBegin):
        return MutableBlockKey(event_run, "command", "start")
    if isinstance(event, (StepBegin, PartDelta, StepEnd)):
        return MutableBlockKey(event_run, "step", event.step)
    return MutableBlockKey(event_run, "run", "end")


def _event_run_id(event: KeyedRunEvent) -> str:
    if isinstance(event, (RunBegin, RunEnd)):
        return event.run
    return trace_run(event.step)


def _is_root_begin(event: RunBegin) -> bool:
    return event.context.get("root") in {None, event.run}
