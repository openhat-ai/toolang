"""Single-run execution pipeline."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import logging
import threading
import time
from typing import TYPE_CHECKING, Any

from toolang.base.error import ToolangError
from toolang.base.types.message import message_summary
from .context import RunContext
from .db import PersistSink
from .events import RunEnd, RunStart, TraceEvent, TraceEventHandler
from .input import RunInput, bind_run_request
from .model import resolve_model
from .runner import RunOutcome, RunOutcomeStatus, RunRequest, RunSubmission
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
    persist = PersistSink(context.store)
    bound: RunBinding | None = None
    run_input: RunInput | None = None
    started = False
    try:
        bound = bind_run_request(context, request, live=submission.live)
        _log_run_start(request=request, bound=bound)
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
                request_id=_request_id(bound.metadata),
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
        model = provider.prepare_target(model)
        adapter = context.model_adapters.get(model.adapter)
        if adapter is None:
            raise ToolangError(f"unknown model adapter: {model.adapter}")
        loop = load_loop(bound.run_loop)
        run_context = RunContext(
            run_input,
            model,
            adapter,
            on_event=_event_handler(context, persist, response),
            consume_inputs=lambda run_id: context.store.pending_inputs(run_id=run_id, action="steer"),
            stream=bool(response is not None and response.wants_stream),
        )
        if bound.origin == "script":
            execution = await _run_script_loop(loop.run, run_context, run_id=bound.run_id)
        else:
            execution = await asyncio.to_thread(loop.run, run_context)
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
            return _finish_outcome(
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
            return _finish_outcome(
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
    return _finish_outcome(
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


def _log_run_start(*, request: RunRequest, bound: RunBinding) -> None:
    input_summary = request.thunk
    if request.message is not None:
        input_summary = message_summary(request.message.parts) or input_summary
    _LOGGER.info(
        "starting run group=%s origin=%s thread_id=%s input=%r",
        bound.group,
        bound.origin,
        bound.thread_id or "-",
        input_summary,
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
        "finished run run_id=%s group=%s origin=%s thread_id=%s status=%s",
        outcome.run_id[:12],
        outcome.group,
        outcome.origin,
        outcome.thread_id or "-",
        outcome.status,
    )
    return outcome


async def _run_script_loop(
    run: Callable[[RunContext], Any],
    run_context: RunContext,
    *,
    run_id: str,
) -> Any:
    """Run a blocking script loop without making Ctrl+C wait for threadpool shutdown."""

    loop = asyncio.get_running_loop()
    future: asyncio.Future[Any] = loop.create_future()

    def set_result(result: Any) -> None:
        if not future.done():
            future.set_result(result)

    def set_exception(exc: BaseException) -> None:
        if not future.done():
            future.set_exception(exc)

    def complete(callback: Callable[[], None]) -> None:
        try:
            loop.call_soon_threadsafe(callback)
        except RuntimeError:
            return

    def worker() -> None:
        try:
            result = run(run_context)
        except BaseException as exc:
            error = exc
            complete(lambda: set_exception(error))
            return
        complete(lambda: set_result(result))

    thread = threading.Thread(
        target=worker,
        name=f"toolang-script-run-{run_id[:12]}",
        daemon=True,
    )
    thread.start()
    return await future


def _emit_event(
    context: UptimeContext,
    persist: PersistSink | None,
    response: ResponseSink | None,
    event: TraceEvent,
) -> None:
    if _event_is_after_canceled_run(context, event):
        return
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


def _event_is_after_canceled_run(context: UptimeContext, event: TraceEvent) -> bool:
    if isinstance(event, RunStart):
        return False
    stored = context.store.get_run(run_id=event.run_id)
    return stored is not None and stored.status == "canceled"


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _request_id(metadata: dict[str, object]) -> str | None:
    value = metadata.get("request_id")
    return str(value) if value is not None else None
