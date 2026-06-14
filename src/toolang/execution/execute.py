"""Single-run execution pipeline."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import logging
import threading
import time
from typing import TYPE_CHECKING, Any

from toolang.base.error import ToolangError
from toolang.base.types.message import Message
from toolang.base.types.message import message_summary
from .context import RunContext
from .db import PersistSink
from .events import RunEnd, RunStart, TraceEvent, TraceEventHandler
from .executor import Executor, Frame, RunCtx
from .input import RunInput, bind_run_request, select_origin_thunk
from .model import resolve_model
from .runner import RunOutcome, RunOutcomeStatus, RunRequest, RunSubmission
from .binding import invoke_params
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
    output_text = ""
    started = False
    run_started_at = time.perf_counter()
    try:
        bound = bind_run_request(context, request, live=submission.live)
        executable_kind, executable = _select_executable(bound)
        _log_run_start(request=request, bound=bound)
        if executable_kind == "thunk":
            _preflight_thunk_run(context, bound, executable)
        frame = Frame.from_invocation(
            input_param=executable.input,
            input_value=bound.input_text,
            params=invoke_params(bound),
        )
        run_ctx = RunCtx(
            binding=bound,
            root=bound.run_id,
            parent=None,
            parent_step=None,
            thread=bound.thread_id,
            call="top",
            frame=frame,
        )
        _emit_event(
            context,
            persist,
            response,
            RunStart(
                run_id=bound.run_id,
                origin=bound.origin,
                thread_id=bound.thread_id,
                input=bound.message or Message.user(bound.input_text),
                created_at=bound.created_at,
                started_at=bound.created_at,
                request_id=_request_id(bound.metadata),
                root_run_id=bound.run_id,
                executable_kind=executable_kind,
                executable_name=(
                    executable.thunk_name() if executable_kind == "thunk" else executable.flow_name()
                ),
                call_kind="top",
                metadata=dict(bound.metadata),
            ),
        )
        started = True
        executor = Executor(
            context,
            on_event=_event_handler(context, persist, response) or (lambda _event: None),
            consume_inputs=lambda run_id: context.store.pending_commands(run_id=run_id, kind="steer"),
            load_loop_func=load_loop,
            stream=bool(response is not None and response.wants_stream),
        )
        if executable_kind == "thunk":
            value = await executor.execute_thunk(run_ctx, executable)
        else:
            value = await executor.execute_flow(run_ctx, executable)
        output_text = "" if value is None else str(value)
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
        duration_ms=_elapsed_ms(run_started_at),
        status="failed" if final_status == "canceled" else "finished",
        output_text=output_text,
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
        "Run started thread=%s run=%s input=%r",
        bound.thread_id,
        bound.run_id,
        input_summary,
    )


def _log_run_prepared(*, run: RunBinding, run_input: RunInput, model: object) -> None:
    _LOGGER.info(
        "Run prepared thread=%s run=%s thunk=%s model=%s tools=%s psyches=%s skills=%s services=%s",
        run.thread_id,
        run.run_id,
        _thunk_name(run_input),
        getattr(model, "ref", "-"),
        len(run_input.tools()),
        _entry_count(run_input, "psyches"),
        _entry_count(run_input, "skills"),
        _entry_count(run_input, "services"),
    )


def _thunk_name(run_input: RunInput) -> str:
    thunk = getattr(run_input, "thunk", None)
    thunk_name = getattr(thunk, "thunk_name", None)
    if callable(thunk_name):
        return str(thunk_name())
    return "-"


def _entry_count(run_input: RunInput, method_name: str) -> int:
    method = getattr(run_input, method_name, None)
    if not callable(method):
        return 0
    return len(method())


def _preflight_thunk_run(context: UptimeContext, bound: RunBinding, thunk: Any) -> None:
    run_input = RunInput.from_thunk(context, bound, thunk)
    allowed_model_selectors = run_input.effective_model_selectors(context)
    model = resolve_model(
        context,
        selector=run_input.model_selector(context),
        allowed_selectors=allowed_model_selectors,
    )
    provider = context.model_providers[model.provider]
    model = provider.prepare_target(model)
    _log_run_prepared(run=bound, run_input=run_input, model=model)
    if model.adapter not in context.model_adapters:
        raise ToolangError(f"unknown model adapter: {model.adapter}")


def _select_executable(bound: RunBinding):
    program = bound.live.program
    requested_kind = bound.metadata.get("executable_kind")
    if requested_kind == "flow":
        return "flow", program.get_flow(bound.thunk_name)
    if requested_kind == "thunk":
        return "thunk", program.get_thunk(bound.thunk_name)
    return "thunk", select_origin_thunk(
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
    if response is None:
        return
    _emit_event(
        context,
        None,
        response,
        RunEnd(
            run_id=bound.run_id,
            thread_id=bound.thread_id,
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


def _elapsed_ms(started_at: float) -> int:
    return max(0, round((time.perf_counter() - started_at) * 1000))


def _request_id(metadata: dict[str, object]) -> str | None:
    value = metadata.get("request_id")
    return str(value) if value is not None else None
