"""Coordinate independent compact Runs and adopt only validated durable output."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
import fcntl
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

from toolang.base.errors import ToolangError
from toolang.base.types.policy import RunBindings
from toolang.common.time import utc_now
from toolang.lang.input import RunnableInput
from toolang.plugin.toolsets.collections import ToolCollection
from toolang.plugin.toolsets.loading import load_tools
from toolang.setup.models import select_compact_model
from toolang.state.builtin import prepare_builtin_state
from toolang.state.state import AgentState

from ..inspection.history import RunHistory
from ..records import CompactControlPayload
from ..assembly.tool_replies import control_summary
from ..types import FieldRef, RunRef, StepRef, ThreadRef
from ..assembly import prompts

if TYPE_CHECKING:
    from ..store import RunStore
    from .runs.agic import _AgicState


def available_horizon(store: RunStore, thread: str) -> FieldRef | None:
    """Freeze applicable history at root creation, never while replaying a call."""
    if thread.startswith("compact_"):
        return None
    history = RunHistory(store)
    output = history.get_compaction(thread)
    if output is None:
        return None
    return output.ref


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
    return prepare_builtin_state(prompts.load("defaults/compact.too"))


@lru_cache(maxsize=1)
def compact_tools() -> ToolCollection:
    """The internal program's read-only tools, independent of user selectors."""
    return ToolCollection.from_tools(load_tools(toolsets=("history",)))


async def execute(
    state: _AgicState, step: StepRef, thread: str, begin: str | None, end: str
) -> dict[str, Any]:
    from .executor import RunSpec
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
        if output is None or output.result.end != end_ref:
            reuse = (
                output is not None
                and output.result.end in history.roots
                and history.roots.index(output.result.end)
                < history.roots.index(end_ref)
            )
            resolved: dict[str, str | bool] = {
                "thread": str(target),
                "begin": str(history.roots[0]),
                "end": end,
                "bare": not reuse,
            }
            if reuse:
                assert output is not None
                resolved.update(begin=str(output.result.end), previous=str(output.ref))
            frame = state.frame_for_step(*execution.state_snapshot())
            resources = frame.run.agent_resources
            if resources is None:
                raise RuntimeError(f"agent resources missing: {frame.run.run_id}")
            models = frame.run.setup.models.subset(resources.models)
            request = select_compact_model(models, frame.run.setup.compact_model)
            compact_thread = f"compact_{target}"
            if store.get_thread(thread_id=compact_thread) is None:
                store.create_thread(
                    thread_id=compact_thread, origin="script", created_at=utc_now()
                )
            # This isolated program has only read-only history tools. In particular
            # it cannot reload into the human's State or transfer out of compact.
            setup = replace(frame.run.setup, models=models, tools=compact_tools())
            handle = execution.executor.run(
                RunSpec(
                    setup=setup,
                    state=compact_state(),
                    thread=compact_thread,
                    bindings=RunBindings(model=request.ref, runnable="agic:compact"),
                    limits=frame.run.limits,
                    model_request=request,
                    input=RunnableInput(resolved),
                )
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
            try:
                output = reader.read_compaction(
                    FieldRef.from_path(RunRef(record.id), "output"),
                    target,
                    history.roots,
                )
            except (ValueError, KeyError, TypeError) as exc:
                raise ToolangError(f"invalid compact output: {exc}") from exc
        controls = execution.compact(step, output.ref)
        return {
            "controls": [
                control_summary(ref, CompactControlPayload(output.ref))
                for ref in controls
            ]
        }
