"""Framework-owned compaction execution, validation, and publication."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
import fcntl
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

from toolang.base.errors import ToolangError
from toolang.base.types.compaction import CompactionResult
from toolang.plugin.toolsets.collections import ToolCollection
from toolang.plugin.toolsets.loading import load_tools
from toolang.state.builtin import prepare_builtin_state
from toolang.state.state import AgentState
from .assembly import prompts
from .types import RunRef, ThreadRef, validate_compaction_coverage

if TYPE_CHECKING:
    from .store import RunStore
    from .executor.executor import RunExecutor, RunSpec
    from .schemas import CompactionOutput
    from .events import RunTracer


def available_horizon(store: RunStore, thread: str) -> RunRef | None:
    """Freeze applicable history at root creation, never while replaying a call."""
    if thread.startswith("compact_"):
        return None
    from .inspection.history import RunHistory

    history = RunHistory(store)
    output = history.get_compaction(thread)
    if output is None:
        return None
    return output.ref


@asynccontextmanager
async def permit(path: Path, *, wait: bool = True) -> AsyncIterator[None]:
    """A cancellable cross-process wait; never hold a SQLite transaction here."""
    with path.open("a+b") as lock:
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if not wait:
                    raise ToolangError("compaction already running") from None
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


def assemble_compaction(
    summary: object,
    *,
    thread: ThreadRef,
    roots: Sequence[RunRef],
    start: RunRef,
    begin: RunRef,
    end: RunRef,
) -> CompactionResult:
    if not isinstance(summary, str):
        raise ValueError("compact summary must be text")
    result = CompactionResult(str(thread), str(start), str(end), summary)
    validate_compaction_coverage(result, thread, roots)
    if begin not in roots or not roots.index(start) <= roots.index(begin) < roots.index(
        end
    ):
        raise ValueError("compact read range must be a nonempty suffix of its coverage")
    return result


@lru_cache(maxsize=1)
def forget_state() -> AgentState:
    return prepare_builtin_state(prompts.load("defaults/forget.too"))


async def execute_algorithm(
    executor: RunExecutor,
    spec: RunSpec,
    *,
    roots: Sequence[RunRef],
    tracer: RunTracer | None = None,
) -> tuple[str, CompactionOutput]:
    from .inspection.history import RunHistory

    handle = executor.run(spec, tracer=tracer)
    try:
        record = await handle
    except asyncio.CancelledError:
        if not handle.task.done():
            try:
                handle.cancel(reason="compaction interrupted")
            except ValueError:
                record = executor.store.get_run(run_id=handle.run_id)
                if record is None or record.status in {"pending", "running"}:
                    raise
            await asyncio.shield(handle.task)
        raise
    if record.status != "succeeded":
        error = (
            executor.store.resolve_error(record.error)
            if record.error is not None
            else record.status
        )
        raise ToolangError(f"compact Run {record.id}: {error}")
    reader = RunHistory(executor.store)
    output = reader.read_compaction(
        RunRef(record.id), ThreadRef.parse(str(spec.input["thread"])), roots
    )
    return record.id, output
