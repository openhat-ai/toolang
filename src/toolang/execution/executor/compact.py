"""Coordinate independent compact Runs and adopt only validated durable output."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
import fcntl
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from toolang.base.errors import ToolangError
from toolang.base.types.model import ModelOverride
from toolang.base.types.policy import RunBindings, RunLimits
from toolang.common.time import utc_now
from toolang.common.files import file_write_lock
from toolang.lang.input import RunnableInput
from toolang.plugin.toolsets.collections import ToolCollection
from toolang.plugin.toolsets.loading import load_tools
from toolang.setup.models import select_compact_model
from toolang.state.builtin import prepare_builtin_state
from toolang.state.state import AgentState

from ..history import RunHistory
from ..events import RunTracer
from ..schemas import CompactionOutput
from ..records import CompactControlPayload
from ..tool_results import control_summary
from ..types import FieldRef, Output, RunRef, StepRef, ThreadRef
from . import prompts

if TYPE_CHECKING:
    from toolang.setup import AgentSetup
    from ..store import RunStore
    from .executor import LocalRunHandle, RunExecutor
    from .runs.agic import _AgicState


def valid_output(
    output: CompactionOutput,
    thread: str,
    roots: Sequence[RunRef],
    expected: Mapping[str, object] | None = None,
) -> bool:
    raw: object = output.output.local.value
    if not isinstance(raw, Mapping):
        return False
    value = cast(Mapping[str, object], raw)
    summary = value.get("summary")
    return (
        value.get("thread") == thread
        and bool(roots)
        and value.get("begin") in (None, str(roots[0]))
        and value.get("end") in tuple(str(root) for root in roots[1:])
        and isinstance(summary, str)
        and bool(summary.strip())
        and (
            expected is None
            or all(value.get(key) == item for key, item in expected.items())
        )
    )


def available_horizon(store: RunStore, thread: str) -> FieldRef | None:
    """Freeze applicable history at root creation, never while replaying a call."""
    if thread.startswith("compact_"):
        return None
    history = RunHistory(store)
    output = history.get_compaction(thread)
    if output is None:
        return None
    roots = tuple(
        RunRef(run.id)
        for run in history.thread_view(thread, include_children=False).roots
    )
    return output.ref if valid_output(output, thread, roots) else None


@asynccontextmanager
async def permit(path: Path) -> AsyncIterator[None]:
    """A cancellable cross-process wait; never hold a SQLite transaction here."""
    with path.open("a+b") as lock:
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                await asyncio.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


@lru_cache(maxsize=1)
def compact_state() -> AgentState:
    return prepare_builtin_state(prompts.load("compact.too"))


@lru_cache(maxsize=1)
def compact_tools() -> ToolCollection:
    """The internal program's read-only tools, independent of user selectors."""
    return ToolCollection.from_tools(load_tools(toolsets=("history",)))


@dataclass(frozen=True)
class CompactRun:
    """Frozen prefix and execution guard for one independent compact Run."""

    thread: str
    roots: tuple[RunRef, ...]
    begin: str | None = None
    permit_held: bool = False

    @property
    def input(self) -> dict[str, str | None]:
        return {"thread": self.thread, "begin": self.begin, "end": str(self.roots[-1])}

    def validate_range(self, store: RunStore) -> None:
        current = (
            RunHistory(store).thread_view(self.thread, include_children=False).roots
        )
        if tuple(RunRef(run.id) for run in current[: len(self.roots)]) != self.roots:
            raise ToolangError("compact range changed; submit a new request")

    @asynccontextmanager
    async def guard(self, store: RunStore) -> AsyncIterator[None]:
        if self.permit_held:
            self.validate_range(store)
            yield
        else:
            lock = store.db_path.with_name(
                f"{store.db_path.name}.{self.thread}.compact.lock"
            )
            async with permit(lock):
                self.validate_range(store)
                yield

    def validate_output(self, store: RunStore, output: Output | None) -> None:
        self.validate_range(store)
        if output is None:
            raise ToolangError("compact Run returned no output")
        raw: object = store.resolve_output(output).local.value
        value = cast(Mapping[str, object], raw) if isinstance(raw, Mapping) else {}
        summary = value.get("summary")
        if (
            any(value.get(key) != item for key, item in self.input.items())
            or not isinstance(summary, str)
            or not summary.strip()
        ):
            raise ToolangError(
                "compact output must echo its full range and contain a nonempty summary"
            )


def start_compact(
    executor: RunExecutor,
    *,
    setup: AgentSetup,
    thread: str,
    end: RunRef | None,
    limits: RunLimits,
    model: ModelOverride | None = None,
    begin: str | None = None,
    request_id: str | None = None,
    tracer: RunTracer | None = None,
    permit_held: bool = False,
) -> LocalRunHandle:
    """Accept a guarded compact Run; both callers use ordinary Run lifecycle."""
    from .executor import RunSpec

    if thread.startswith("compact_"):
        raise ToolangError("cannot compact a compact Thread")
    roots = RunHistory(executor.store).thread_view(thread, include_children=False).roots
    terminal = [run for run in roots if run.status not in {"pending", "running"}]
    if len(terminal) < 2:
        raise ToolangError(
            "compact requires a nonempty prefix and a retained terminal root"
        )
    end = end or RunRef(terminal[-1].id)
    ids = tuple(RunRef(run.id) for run in roots)
    if end not in ids or ids.index(end) == 0:
        raise ToolangError("compact end must be a visible root after the first root")
    index = ids.index(end)
    if any(run.status in {"pending", "running"} for run in roots[:index]) or not any(
        run.status not in {"pending", "running"} for run in roots[index:]
    ):
        raise ToolangError(
            "compact must exclude active roots and retain a terminal root"
        )
    if begin not in (None, str(ids[0])):
        raise ToolangError("compact must summarize a full prefix")
    work = CompactRun(thread, ids[: index + 1], begin, permit_held)
    request = select_compact_model(
        setup.models, model if model is not None else setup.compact_model
    )
    compact_thread = f"compact_{thread}"
    with file_write_lock(executor.store.thread_lock_path):
        if executor.store.get_thread(thread_id=compact_thread) is None:
            executor.store.create_thread(
                thread_id=compact_thread, origin="script", created_at=utc_now()
            )
    return executor.run(
        RunSpec(
            setup=replace(setup, tools=compact_tools()),
            state=compact_state(),
            thread=compact_thread,
            bindings=RunBindings(model=request.ref, runnable="flow:compact"),
            limits=limits,
            model_request=request,
            input=RunnableInput(work.input),
        ),
        request_id=request_id,
        tracer=tracer,
        _compact=work,
    )


async def execute(
    state: _AgicState, step: StepRef, thread: str, begin: str | None, end: str
) -> dict[str, Any]:
    from .steps.model import compaction_boundary

    target = ThreadRef.parse(thread)
    begin_ref = RunRef.parse(begin) if begin is not None else None
    end_ref = RunRef.parse(end)
    execution = state.execution
    if execution is None:
        raise RuntimeError("Agic runtime execution is unavailable")
    history = execution.message_history()
    if str(target) != history.thread or str(target).startswith("compact_"):
        raise ToolangError("compact must target the calling Run's normal Thread")
    if (
        not history.roots
        or begin_ref not in {None, history.roots[0]}
        or end_ref not in history.roots[1:]
    ):
        raise ToolangError(
            "compact must cover a nonempty prefix and retain a historical root"
        )
    store = execution.store
    lock = store.db_path.with_name(f"{store.db_path.name}.{target}.compact.lock")
    async with permit(lock):
        execution.raise_if_canceling(step.run_id, call=True)
        boundary = compaction_boundary(state)
        if boundary is None:
            return {"controls": []}
        if boundary != end_ref:
            raise ToolangError("compact range changed while waiting; retry required")
        reader = RunHistory(store)
        output = reader.get_compaction(target)
        expected = {"thread": str(target), "begin": begin, "end": end}
        if not (
            output is not None
            and valid_output(output, str(target), history.roots, expected)
        ):
            frame = state.frame_for_step(*execution.state_snapshot())
            resources = frame.run.agent_resources
            if resources is None:
                raise RuntimeError(f"agent resources missing: {frame.run.run_id}")
            models = frame.run.setup.models.subset(resources.models)
            handle = start_compact(
                execution.executor,
                setup=replace(frame.run.setup, models=models),
                thread=str(target),
                begin=begin,
                end=end_ref,
                limits=frame.run.limits,
                permit_held=True,
            )
            try:
                record = await handle
            except asyncio.CancelledError:
                # This request owns the admitted work; other waiters own no Run.
                handle.cancel()
                await handle
                raise
            if record.status != "succeeded":
                raise ToolangError(f"compact Run {record.id} {record.status}")
            result = reader.get_output(RunRef(record.id))
            if result is None:
                raise ToolangError("compact Run returned no output")
            output = CompactionOutput(
                FieldRef.from_path(RunRef(record.id), "output"), result
            )
        if not valid_output(output, str(target), history.roots, expected):
            raise ToolangError(
                "compact output must echo its full range and contain a nonempty summary"
            )
        controls = execution.compact(step, output.ref)
        return {
            "controls": [
                control_summary(ref, CompactControlPayload(output.ref))
                for ref in controls
            ]
        }
