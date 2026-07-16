"""Single-run execution pipeline."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import json
import logging
import time
from typing import TYPE_CHECKING

from toolang.base.types.message import Message
from toolang.base.types.message import message_summary
from .events import RunBegin, RunEnd, RunStarting, TraceEvent, TraceEventHandler
from .executor import Executor, Local
from .input import bind_run_request, select_origin_thunk
from .runner import RunOutcome, RunOutcomeStatus, RunRequest, RunSubmission
from .records import trace_run
from ..plugin import load_loop

if TYPE_CHECKING:
    from ..up import UptimeContext
    from .input import RunBinding
    from .response import ResponseSink

_LOGGER = logging.getLogger("toolang.run")


async def execute_run(
    context: UptimeContext,
    submission: RunSubmission,
    *,
    delay_sec: float,
    sleep: Callable[[float], Awaitable[None]],
) -> RunOutcome:
    """Execute one run request through bind/assemble/resolve/loop/persist."""

    await sleep(delay_sec)
    request = submission.request
    response = submission.response
    bound: RunBinding | None = None
    output_text = ""
    started = False
    run_started_at = time.perf_counter()
    try:
        bound = submission.binding or bind_run_request(
            context, request, live=submission.live
        )
        if context.store.get_run(run_id=bound.run_id) is None:
            _emit_event(
                context,
                response,
                RunStarting(
                    run=bound.run_id,
                    cmd=0,
                    parent=None,
                    thread=bound.thread_id,
                    input=bound.message or Message.user(bound.input_text),
                    context={
                        **dict(bound.metadata),
                        "origin": bound.origin,
                        "group": bound.group,
                        "root": bound.run_id,
                        "request_id": _request_id(bound.metadata),
                        "executable": {
                            "kind": _request_executable_kind(bound.metadata),
                            "name": bound.thunk_name,
                        },
                        "call": "top",
                    },
                    created_at=bound.created_at,
                ),
            )
        executable_kind, executable = _select_executable(bound)
        _log_run_begin(request=request, bound=bound)
        started = True
        executor = Executor(
            context,
            emit=_event_handler(context, response) or (lambda _event: None),
            consume_commands=lambda run_id, kind: context.store.pending_commands(
                run_id=run_id, kind=kind
            ),
            load_loop=load_loop,
            stream=bool(response is not None and response.wants_stream),
        )
        result = await executor.run(bound, executable)
        output_text = _local_text(result)
    except asyncio.CancelledError:
        if bound is None:
            raise
        stop = next(
            (
                command
                for command in reversed(context.store.list_commands(run_id=bound.run_id))
                if command.kind == "stop" and command.status == "pending"
            ),
            None,
        )
        stored = context.store.get_run(run_id=bound.run_id)
        error = (
            stored.error
            if stored is not None and stored.error
            else message_summary(stop.input.parts)
            if stop is not None and stop.input is not None
            else "canceled"
        )
        return _finish_outcome(
            run_id=bound.run_id,
            group=bound.group,
            origin=bound.origin,
            input_text=bound.input_text,
            thunk_name=bound.thunk_name,
            thread_id=bound.thread_id,
            delay_sec=delay_sec,
            duration_ms=_elapsed_ms(run_started_at),
            status="canceled",
            error=error,
            live_fingerprint=bound.live.fingerprint,
        )
    except Exception as exc:
        error = str(exc)
        if bound is not None and started:
            return _finish_outcome(
                run_id=bound.run_id,
                group=bound.group,
                origin=bound.origin,
                input_text=bound.input_text,
                thunk_name=bound.thunk_name,
                thread_id=bound.thread_id,
                delay_sec=delay_sec,
                duration_ms=_elapsed_ms(run_started_at),
                status="failed",
                output_text="",
                error=error,
                live_fingerprint=bound.live.fingerprint,
            )
        if bound is not None:
            _emit_pre_start_failure(context, response, bound=bound, error=error)
            return _finish_outcome(
                run_id=bound.run_id,
                group=bound.group,
                origin=bound.origin,
                input_text=bound.input_text,
                thunk_name=bound.thunk_name,
                thread_id=bound.thread_id,
                delay_sec=delay_sec,
                duration_ms=_elapsed_ms(run_started_at),
                status="failed",
                output_text="",
                error=error,
                live_fingerprint=bound.live.fingerprint,
            )
        raise

    return _finish_outcome(
        run_id=bound.run_id,
        group=bound.group,
        origin=bound.origin,
        input_text=bound.input_text,
        thunk_name=bound.thunk_name,
        thread_id=bound.thread_id,
        delay_sec=delay_sec,
        duration_ms=_elapsed_ms(run_started_at),
        status="finished",
        output_text=output_text,
        live_fingerprint=bound.live.fingerprint,
    )


def _event_handler(
    context: UptimeContext,
    response: ResponseSink | None,
) -> TraceEventHandler | None:
    if response is None:

        def handler(event: TraceEvent) -> None:
            _emit_event(context, None, event)

        return handler

    def handler(event: TraceEvent) -> None:
        _emit_event(context, response, event)

    return handler


def _log_run_begin(*, request: RunRequest, bound: RunBinding) -> None:
    input_summary = request.thunk
    if request.message is not None:
        input_summary = message_summary(request.message.parts) or input_summary
    _LOGGER.info(
        "Run started thread=%s run=%s input=%r",
        bound.thread_id,
        bound.run_id,
        input_summary,
    )


def _select_executable(bound: RunBinding):
    program = bound.live.program
    requested_kind = bound.metadata.get("executable_kind")
    if requested_kind == "flow":
        return "flow", program.get_flow(bound.thunk_name)
    if requested_kind in {"agic", "thunk"}:
        return "agic", program.get_thunk(bound.thunk_name)
    return "agic", select_origin_thunk(
        program,
        origin=bound.origin,
        thunk_name=bound.thunk_name,
    )


def _emit_pre_start_failure(
    context: UptimeContext,
    response: ResponseSink | None,
    *,
    bound: RunBinding,
    error: str,
) -> None:
    _emit_event(
        context,
        response,
        RunEnd(
            run=bound.run_id,
            status="failed",
            error=error,
            finished_at=_utc_now(),
        ),
    )


def _finish_outcome(
    *,
    run_id: str,
    group: str,
    origin: str,
    input_text: str,
    thunk_name: str | None,
    thread_id: str | None,
    delay_sec: float,
    duration_ms: int,
    status: RunOutcomeStatus,
    output_text: str = "",
    error: str | None = None,
    live_fingerprint: str | None = None,
) -> RunOutcome:
    outcome = RunOutcome(
        run_id=run_id,
        group=group,
        origin=origin,
        input_text=input_text,
        thunk_name=thunk_name,
        thread_id=thread_id,
        delay_sec=delay_sec,
        status=status,
        output_text=output_text,
        error=error,
        live_fingerprint=live_fingerprint,
    )
    _LOGGER.info(
        "Run finished thread=%s run=%s status=%s duration_ms=%s",
        outcome.thread_id or "-",
        outcome.run_id,
        outcome.status,
        duration_ms,
    )
    return outcome


def _emit_event(
    context: UptimeContext,
    response: ResponseSink | None,
    event: TraceEvent,
) -> None:
    if _event_is_after_canceled_run(context, event):
        return
    context.events.publish_trace(event)
    if response is not None:
        try:
            response.on_event(event)
        except Exception:
            _LOGGER.exception("response sink event handling failed")


def _event_is_after_canceled_run(context: UptimeContext, event: TraceEvent) -> bool:
    if isinstance(event, RunBegin):
        return False
    event_run = event.run if isinstance(event, RunEnd) else trace_run(getattr(event, "step", ""))
    if not event_run:
        event_run = getattr(event, "run", "")
    stored = context.store.get_run(run_id=event_run)
    return stored is not None and stored.status == "canceled"


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _elapsed_ms(started_at: float) -> int:
    return max(0, round((time.perf_counter() - started_at) * 1000))


def _request_id(metadata: dict[str, object]) -> str | None:
    value = metadata.get("request_id")
    return str(value) if value is not None else None


def _request_executable_kind(metadata: dict[str, object]) -> str:
    value = metadata.get("executable_kind")
    kind = str(value) if value is not None else "agic"
    return "agic" if kind == "thunk" else kind


def _local_text(local: Local) -> str:
    if local.shape == "none" or local.value is None:
        return ""
    if isinstance(local.value, str):
        return local.value
    return json.dumps(local.value, ensure_ascii=False, separators=(",", ":"))
