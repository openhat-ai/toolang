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
from .input import assemble_run_input, bind_run_request
from .model import resolve_model
from .runner import RunOutcome, RunSubmission
from ..strategies import load_run_strategy

if TYPE_CHECKING:
    from ..up import UptimeContext
    from .input import RunBinding, RunInput
    from .response import ResponseSink

_LOGGER = logging.getLogger("toolang.runner")


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
    try:
        bound = bind_run_request(context, request, live=submission.live)
        run_input = assemble_run_input(context, bound)
        _emit_event(
            persist,
            response,
            RunStart(
                run_id=bound.run_id,
                origin=bound.origin,
                thread_id=bound.thread_id,
                input=run_input.input,
                created_at=bound.created_at,
                started_at=bound.created_at,
            ),
        )
        model = resolve_model(
            context,
            selector=run_input.model,
            default_selector=_activation_default_model_selector(context),
            allowed_selectors=_activation_allowed_model_selectors(context),
        )
        strategy = load_run_strategy(bound.run_strategy)
        run_context = RunContext(
            run_input,
            model,
            on_event=_event_handler(persist, response),
            stream=bool(response is not None and response.wants_stream),
        )
        execution = await asyncio.to_thread(strategy.run, run_context)
    except Exception as exc:
        error = str(exc)
        if bound is not None:
            _emit_event(
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
        raise

    _emit_event(
        persist,
        response,
        RunEnd(
            run_id=bound.run_id,
            thread_id=bound.thread_id,
            status="finished",
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
        status="finished",
        output_text=execution.output_text,
        live_fingerprint=bound.live.fingerprint,
    )


def _activation_default_model_selector(context: UptimeContext) -> str | None:
    value = context.config.get("models.default_selector")
    if not isinstance(value, str):
        return None
    selector = value.strip()
    return selector or None


def _activation_allowed_model_selectors(context: UptimeContext) -> tuple[str, ...]:
    value = context.config.get("models.allowed_selectors")
    if isinstance(value, tuple):
        return tuple(item for item in value if isinstance(item, str) and item.strip())
    if isinstance(value, list):
        return tuple(item for item in value if isinstance(item, str) and item.strip())
    return ()


def _event_handler(
    persist: PersistSink,
    response: ResponseSink | None,
) -> TraceEventHandler | None:
    if response is None:

        def handler(event: TraceEvent) -> None:
            _emit_event(persist, None, event)

        return handler

    def handler(event: TraceEvent) -> None:
        _emit_event(persist, response, event)

    return handler


def _emit_event(
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


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
