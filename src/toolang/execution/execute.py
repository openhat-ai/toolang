"""Single-run execution pipeline."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import logging
import time
from typing import TYPE_CHECKING

from .context import RunContext
from .db import PersistSink
from .events import RunEnd, RunStart, TraceEvent, TraceEventHandler
from .input import RunInput, bind_run_request
from .model import resolve_model
from .runner import RunOutcome, RunSubmission
from ..strategies import load_run_strategy

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
    """Execute one run request through bind/assemble/resolve/strategy/persist."""

    await sleep(delay_sec)
    request = submission.request
    response = submission.response
    persist = PersistSink(context.store)
    bound: RunBinding | None = None
    run_input: RunInput | None = None
    started = False
    try:
        bound = bind_run_request(context, request, live=submission.live)
        run_input = RunInput.from_binding(context, bound)
        _emit_event(
            context,
            persist,
            response,
            RunStart(
                run_id=bound.run_id,
                origin=bound.origin,
                thread_id=bound.thread_id,
                input=run_input.message,
                created_at=bound.created_at,
                started_at=bound.created_at,
            ),
        )
        started = True
        allowed_model_selectors = run_input.effective_model_selectors(context)
        model = resolve_model(
            context,
            selector=run_input.model_selector(context),
            allowed_selectors=allowed_model_selectors,
        )
        provider = context.model_providers[model.provider]
        strategy = load_run_strategy(bound.run_strategy)
        run_context = RunContext(
            run_input,
            model,
            provider,
            on_event=_event_handler(context, persist, response),
            consume_controls=lambda run_id, step_index: context.store.consume_pending_run_controls(
                run_id=run_id,
                step_index=step_index,
                kind="steer",
            ),
            stream=bool(response is not None and response.wants_stream),
        )
        execution = await asyncio.to_thread(strategy.run, run_context)
    except Exception as exc:
        error = str(exc)
        if bound is not None and started:
            _emit_event(
                context,
                persist,
                response,
                RunEnd(
                    run_id=bound.run_id,
                    thread_id=bound.thread_id,
                    status="failed",
                    error=error,
                    finished_at=_utc_now(),
                ),
            )
            return RunOutcome(
                run_id=bound.run_id,
                group=bound.group,
                origin=bound.origin,
                input_text=bound.input_text,
                thunk_name=bound.thunk_name,
                thread_id=bound.thread_id,
                delay_sec=delay_sec,
                status="failed",
                output_text="",
                error=error,
                live_fingerprint=bound.live.fingerprint,
            )
        if bound is not None:
            return RunOutcome(
                run_id=bound.run_id,
                group=bound.group,
                origin=bound.origin,
                input_text=bound.input_text,
                thunk_name=bound.thunk_name,
                thread_id=bound.thread_id,
                delay_sec=delay_sec,
                status="failed",
                output_text="",
                error=error,
                live_fingerprint=bound.live.fingerprint,
            )
        raise

    stored_run = context.store.get_run(run_id=bound.run_id)
    final_status = "canceled" if stored_run is not None and stored_run.status == "canceled" else "finished"
    final_error = stored_run.error if final_status == "canceled" and stored_run is not None else None
    _emit_event(
        context,
        persist,
        response,
        RunEnd(
            run_id=bound.run_id,
            thread_id=bound.thread_id,
            status=final_status,
            error=final_error,
            finished_at=_utc_now(),
        ),
    )
    return RunOutcome(
        run_id=bound.run_id,
        group=bound.group,
        origin=bound.origin,
        input_text=bound.input_text,
        thunk_name=bound.thunk_name,
        thread_id=bound.thread_id,
        delay_sec=delay_sec,
        status="failed" if final_status == "canceled" else "finished",
        output_text=execution.output_text,
        error=final_error,
        live_fingerprint=bound.live.fingerprint,
    )


def _event_handler(
    context: UptimeContext,
    persist: PersistSink,
    response: ResponseSink | None,
) -> TraceEventHandler | None:
    if response is None:

        def handler(event: TraceEvent) -> None:
            _emit_event(context, persist, None, event)

        return handler

    def handler(event: TraceEvent) -> None:
        _emit_event(context, persist, response, event)

    return handler


def _emit_event(
    context: UptimeContext,
    persist: PersistSink | None,
    response: ResponseSink | None,
    event: TraceEvent,
) -> None:
    if persist is not None:
        try:
            persist.on_event(event)
        except Exception:
            _LOGGER.exception("persist sink event handling failed")
    if response is not None:
        try:
            response.on_event(event)
        except Exception:
            _LOGGER.exception("response sink event handling failed")
    try:
        context.events.publish_trace(event)
    except Exception:
        _LOGGER.exception("runtime event publish failed")


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
