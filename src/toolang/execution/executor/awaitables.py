"""Execution-owned admission, observation, and cleanup of awaitable targets."""

from __future__ import annotations

import asyncio
from contextvars import Context
from typing import TYPE_CHECKING

from toolang.base.types.message import ToolResultPart
from toolang.common.errors import ToolangError
from toolang.lang.ast import AgicDecl, FlowDecl, RunStmt
from toolang.lang.contracts import OutputContract
from toolang.state.state import state_program

from ..records import LaunchContext, RunControlPayload, RunRecord
from ..types import (
    AwaitableHandle,
    ErrorRef,
    FieldRef,
    Output,
    RunRef,
    ToolStepGiven,
    ToolStepNoted,
    TypedRef,
)
from .common import BoundRun, Local, _ExecutionFailed

if TYPE_CHECKING:
    from .executor import _Execution
    from ..compaction import CompactSpec


def admission(
    execution: _Execution,
    binding: BoundRun,
    runnable: AgicDecl | FlowDecl | CompactSpec,
    *,
    asynchronous: bool,
) -> None:
    """Commit accepted work and, for async delivery, its launch output atomically."""
    from .executor import _bound_runnable

    assert binding.resources is not None
    context = None
    handle = None
    if asynchronous:
        assert isinstance(runnable, AgicDecl | FlowDecl)
        result_type = runnable.output or (
            "Part[]" if isinstance(runnable, AgicDecl) else None
        )
        handle = AwaitableHandle(binding.run_id, binding.thread, result_type)
        context = LaunchContext(
            binding.settings,
            binding.workspaces,
            binding.captured_iterations,
            OutputContract.resolve(
                result_type,
                structs={
                    s.name: s
                    for s in state_program(binding.state, binding.module).structs
                },
            )
            if result_type is not None
            else None,
        )
    with execution.executor.stream.publication() as publication:
        with execution.store.write_transaction():
            execution.store.accept_run(
                run_id=binding.run_id,
                parent=binding.parent,
                thread=binding.thread,
                resources=binding.resources,
                limits=binding.limits,
                state=binding.state.revision,
                runnable=_bound_runnable(binding),
                model_request=binding.model_request,
                input=binding.control_input,
                sandbox=None,
                cwd=binding.cwd,
                occurrence=binding.occurrence,
                request_id=None,
                created_at=binding.created_at,
                horizon=binding.horizon,
                launch_context=context,
                triggered_by=binding.parent if asynchronous else None,
            )
            if handle is None:
                return
            assert binding.parent is not None
            source = execution.store.get_step(ref=binding.parent)
            if source is None:
                raise RuntimeError("async admission requires its source Step")
            if isinstance(source.given, RunStmt) and source.given.asynchronous:
                output = Output(handle, source.given.binding)
                noted = None
            elif (
                isinstance(source.given, ToolStepGiven)
                and source.given.call.input.get("async") is True
            ):
                call = source.given.call
                output = Output(
                    ToolResultPart(
                        tool_call_id=call.tool_call_id,
                        call_id=call.call_id,
                        tool_name=call.name,
                        tool_family=call.name,
                        output={
                            "id": handle.id,
                            "thread": handle.thread,
                            "status": "pending",
                        },
                    )
                )
                noted = ToolStepNoted(summary=f"Started {handle.id} in {handle.thread}")
            else:
                raise RuntimeError("async admission requires an async run Step")
            execution.store.finish_step(
                ref=source.ref,
                kind=source.kind,
                status="succeeded",
                output=output,
                noted=noted,
                error=None,
                finished_at=binding.created_at,
            )
            execution.executor._publish_step_ends(
                publication,
                (source.ref,),
                thread_id=binding.thread,
                root_run_id=binding.root_run_id,
            )

    if execution._active is not None:
        execution._active.early_ends.add(source.ref)


def start(
    execution: _Execution,
    binding: BoundRun,
    runnable: AgicDecl | FlowDecl | CompactSpec,
) -> None:
    """Register owned work before publishing its launch; observe every exception."""
    assert binding.parent is not None

    async def execute() -> None:
        try:
            await execution.execute(binding, runnable, begun=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            # execute persists the terminal outcome; it is surfaced by await.
            pass

    work = execute()
    try:
        task = asyncio.create_task(
            work, name=f"async run {binding.run_id}", context=Context()
        )
    except BaseException:
        work.close()
        raise
    execution._background_horizons[binding.run_id] = binding.horizon
    execution._background_tasks[binding.run_id] = task
    execution._background_owners[binding.run_id] = binding.parent.run_id


async def drain(execution: _Execution, owner: str) -> None:
    """End immediate ownership, including when cleanup is interrupted again."""
    targets = {
        rid: task
        for rid, task in execution._background_tasks.items()
        if execution._background_owners[rid] == owner
    }
    if not targets:
        return
    for task in targets.values():
        if not task.done():
            task.cancel()

    async def cleanup() -> None:
        await asyncio.gather(*targets.values(), return_exceptions=True)
        for rid in targets:
            # Cancellation before a task's first turn cannot run its finally.
            await execution.executor._ensure_terminal(
                rid, emit=execution.emit, status="canceled"
            )
            execution._background_tasks.pop(rid, None)
            execution._background_owners.pop(rid, None)
            execution._background_horizons.pop(rid, None)

    task = asyncio.create_task(cleanup(), name=f"drain async children of {owner}")
    interruption = None
    while True:
        try:
            await asyncio.shield(task)
            break
        except asyncio.CancelledError as exc:
            interruption = exc
            if task.done():
                task.result()
                break
    if interruption is not None:
        raise interruption


def resolve(execution: _Execution, caller: BoundRun, target: str) -> AwaitableHandle:
    """Resolve only targets admitted by this caller; identity is not authority."""
    RunRef(target)
    control = execution.store.get_run_control(run_id=target, index=0)
    run = execution.store.get_run(run_id=target)
    if (
        run is None
        or control is None
        or control.triggered_by is None
        or control.triggered_by.run_id != caller.run_id
        or not isinstance(control.payload, RunControlPayload)
        or control.payload.launch_context is None
        or target == caller.run_id
    ):
        raise ToolangError(f"await target is unavailable or inaccessible: {target}")
    contract = control.payload.launch_context.result
    return AwaitableHandle(
        target, str(run.thread), contract.type_name if contract else None
    )


async def wait(
    execution: _Execution, caller: BoundRun, handle: AwaitableHandle
) -> RunRecord:
    """Observe a durable outcome without transferring cancellation or ownership."""
    if resolve(execution, caller, handle.id) != handle:
        raise ToolangError(f"awaitable contract mismatch: {handle.id}")
    run = execution.store.get_run(run_id=handle.id)
    assert run is not None
    if run.status in {"pending", "running"}:
        with execution.executor._active_lock:
            active = execution.executor._active.get(handle.id)
            task = (
                active.execution._background_tasks.get(handle.id)
                if active is not None and active.execution is not None
                else None
            )
            if task is None and active is not None and run.parent is None:
                task = active.task
        if task is None:
            raise ToolangError(f"await target has no live execution owner: {handle.id}")
        current = asyncio.current_task()
        cancellations = current.cancelling() if current is not None else 0
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if (
                not task.cancelled()
                or current is None
                or current.cancelling() > cancellations
            ):
                raise
        except Exception:
            pass  # Inspect the persisted outcome, not a task's wrapper error.
        run = execution.store.get_run(run_id=handle.id)
        if run is None or run.status in {"pending", "running"}:
            raise ToolangError(f"await target has no terminal outcome: {handle.id}")
    return run


def result(execution: _Execution, run: RunRecord) -> Local:
    if run.status != "succeeded":
        error = execution.store.resolve_error(run.error) if run.error else run.status
        if run.error is not None:
            raise _ExecutionFailed(
                ErrorRef(FieldRef.from_path(RunRef(run.id), "error")),
                ToolangError(error),
            )
        raise ToolangError(error)
    if run.output is None:
        return Local()
    ref = FieldRef.from_path(RunRef(run.id), "output", "value")
    return Local(
        execution.store.resolve_value(run.output.value),
        ref,
        run.output.type,
        TypedRef(ref, run.output.type),
    )
