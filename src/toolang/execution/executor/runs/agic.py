"""Agic run execution."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
import threading
from typing import TYPE_CHECKING, Any, TypeVar

from toolang.base.types.message import Message
from toolang.common.errors import ToolangError
from toolang.lang.ast import AgicDecl

from ..common import BoundRun, Local, decode_agic_output, program_structs, value_text
from ..prepare import PreparedAgic, prepare_agic
from ..steps import model as model_step
from ..steps import tool as tool_step
from ...events import RunEvent
from ...records import RunControlRecord

if TYPE_CHECKING:
    from ..executor import _Execution

_MAX_TOOL_ROUNDS = 8
_T = TypeVar("_T")


@dataclass(slots=True)
class _AgicState:
    """Mutable state shared by one agic's model and tool steps."""

    prepared: PreparedAgic
    home: Path
    emit: Callable[[RunEvent], None]
    pending_inputs: Callable[[], tuple[RunControlRecord, ...]]
    before_call: Callable[[], None]
    messages: list[Message]
    model_state: dict[str, Any] | None = None
    next_step: int = 0
    last_step: int | None = None
    tool_call_sources: dict[str, tuple[int, int]] = field(default_factory=dict)


async def execute(
    execution: _Execution,
    binding: BoundRun,
    agic: AgicDecl,
    locals: Mapping[str, Local],
) -> Local:
    """Execute one complete agic model-tool cycle."""

    invoke = {name: local.value for name, local in locals.items() if name != "_"}
    primary = locals.get("_", Local())
    context = {**binding.context, "invoke_params": invoke}
    bound = replace(
        binding,
        input_text=value_text(primary.value) if primary.shape != "none" else "",
        context=context,
    )
    prepared = prepare_agic(execution, bound, agic)
    state = _AgicState(
        prepared,
        home=execution.setup.home,
        emit=execution.emit,
        pending_inputs=lambda: execution.pending_controls(binding.run_id, "steer"),
        before_call=lambda: execution.raise_if_stopping(binding.run_id, call=True),
        messages=list(prepared.messages),
    )
    message = await _run_in_thread(
        lambda: _execute(state),
        run_id=binding.run_id,
    )
    return Local(
        decode_agic_output(
            message,
            agic.output,
            structs=program_structs(binding),
        ),
        "item",
    )


def _execute(state: _AgicState) -> Message | None:
    for _ in range(_MAX_TOOL_ROUNDS):
        result = model_step.execute(state)
        if result.tool_calls:
            for call in result.tool_calls:
                tool_step.execute(state, call)
            continue
        if state.pending_inputs():
            continue
        return result.message
    raise ToolangError("Model tool loop exceeded the maximum number of rounds.")


async def _run_in_thread(
    operation: Callable[[], _T],
    *,
    run_id: str,
) -> _T:
    """Run blocking agic calls without delaying async task cancellation."""

    event_loop = asyncio.get_running_loop()
    future: asyncio.Future[_T] = event_loop.create_future()

    def worker() -> None:
        try:
            result = operation()
        except BaseException as exc:
            _notify_event_loop(event_loop, _set_exception, future, exc)
            return
        _notify_event_loop(event_loop, _set_result, future, result)

    threading.Thread(
        target=worker,
        name=f"toolang-run-{run_id[:12]}",
        daemon=True,
    ).start()
    return await future


def _set_result(future: asyncio.Future[_T], result: _T) -> None:
    if not future.done():
        future.set_result(result)


def _set_exception(future: asyncio.Future[_T], error: BaseException) -> None:
    if not future.done():
        future.set_exception(error)


def _notify_event_loop(
    event_loop: asyncio.AbstractEventLoop,
    callback: Callable[..., None],
    *args: object,
) -> None:
    try:
        event_loop.call_soon_threadsafe(callback, *args)
    except RuntimeError:
        pass
