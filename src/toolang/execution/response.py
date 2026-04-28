"""Response sinks for caller-facing run output."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
import json
import threading
import time
from typing import Any, Protocol

from toolang.base.protocols.channel import ChannelPlugin
from toolang.base.types.channel import ChannelContext, OutboundMessage, ReplyTarget
from toolang.base.types.message import TextDelta, TextPart, ToolCallDelta, ToolCallPart, ToolResultPart, message_text
from .events import RunEnd, RunStart, StepEnd, StepStart, PartStart, PartDelta, PartEnd, TraceEvent, message_data_for_step


class ResponseSink(Protocol):
    """One caller-facing response sink."""

    wants_stream: bool

    def on_event(self, event: TraceEvent) -> None:
        """Consume one execution response event."""


class BufferedResponseSink:
    """Collect one final assistant message for buffered callers."""

    wants_stream = False

    def __init__(self) -> None:
        self._assistant = None
        self._run_id: str | None = None
        self._thread_id: str | None = None
        self._last_step_index = 0

    @property
    def assistant(self):
        return self._assistant

    def on_event(self, event: TraceEvent) -> None:
        if isinstance(event, RunStart):
            self._run_id = event.run_id
            self._thread_id = event.thread_id
            return
        if isinstance(event, StepEnd) and event.kind == "model_call":
            self._last_step_index = event.step_index
            self._assistant = message_data_for_step(
                run_id=event.run_id,
                thread_id=event.thread_id,
                step_index=event.step_index,
                kind=event.kind,
                output=event.output,
                created_at=event.finished_at,
                error=event.error,
            )
            return
        if (
            isinstance(event, RunEnd)
            and event.status != "finished"
            and event.error
            and self._assistant is None
        ):
            self._assistant = message_data_for_step(
                run_id=self._run_id or event.run_id,
                thread_id=self._thread_id or event.thread_id,
                step_index=max(self._last_step_index, 1),
                kind="model_call",
                output=(TextPart(text=event.error),),
                created_at=event.finished_at,
                error=event.error,
            )


class SseResponseSink:
    """Emit one AI SDK data-stream subset for one chat caller."""

    wants_stream = True

    def __init__(self, *, thread_id: str | None) -> None:
        self._thread_id = thread_id
        self._loop = asyncio.get_running_loop()
        self._queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._run_id: str | None = None
        self._text_started = False
        self._text_ended = False
        self._started_tool_inputs: set[str] = set()
        self._closed = False

    async def stream(self) -> AsyncIterator[str]:
        while True:
            item = await self._queue.get()
            if item is None:
                return
            yield item

    def on_event(self, event: TraceEvent) -> None:
        if self._closed:
            return
        if isinstance(event, RunStart):
            self._run_id = event.run_id
            self._thread_id = event.thread_id
            message_metadata = _message_metadata(
                run_id=event.run_id,
                thread_id=self._metadata_thread_id(event.thread_id),
            )
            self._enqueue_payload(
                {
                    "type": "start",
                    "messageId": event.run_id,
                    "messageMetadata": message_metadata,
                }
            )
            self._enqueue_payload(
                {
                    "type": "message-metadata",
                    "messageMetadata": message_metadata,
                }
            )
            return
        if isinstance(event, StepStart) and event.kind == "model_call":
            self._enqueue_payload({"type": "start-step"})
            return
        if isinstance(event, PartStart) and event.kind == "text":
            self._text_started = True
            self._text_ended = False
            self._enqueue_payload({"type": "text-start", "id": _message_id(event.run_id)})
            return
        if isinstance(event, PartDelta):
            if isinstance(event.delta, TextDelta):
                self._enqueue_payload(
                    {
                        "type": "text-delta",
                        "id": _message_id(event.run_id),
                        "delta": event.delta.text,
                    }
                )
                return
            if isinstance(event.delta, ToolCallDelta):
                self._emit_tool_input_start(event.run_id, event.delta.tool_call_id)
                self._enqueue_payload(
                    {
                        "type": "tool-input-delta",
                        "id": _message_id(event.run_id),
                        "toolCallId": event.delta.tool_call_id,
                        "inputTextDelta": event.delta.text,
                    }
                )
                return
        if isinstance(event, PartEnd):
            if isinstance(event.data, TextPart):
                if not self._text_started:
                    self._text_started = True
                    self._enqueue_payload({"type": "text-start", "id": _message_id(event.run_id)})
                self._text_ended = True
                self._enqueue_payload({"type": "text-end", "id": _message_id(event.run_id)})
                return
            if isinstance(event.data, ToolCallPart):
                self._emit_tool_input_start(event.run_id, event.data.tool_call_id)
                self._enqueue_payload(
                    {
                        "type": "tool-input-available",
                        "id": _message_id(event.run_id),
                        "toolCallId": event.data.tool_call_id,
                        "toolName": event.data.tool_name,
                        "input": event.data.input,
                        "providerMetadata": provider_metadata(
                            tool_family=event.data.tool_family,
                            tool_name=event.data.tool_name,
                        ),
                    }
                )
                return
            if isinstance(event.data, ToolResultPart):
                self._enqueue_payload(
                    {
                        "type": "tool-output-available",
                        "id": _message_id(event.run_id),
                        "toolCallId": event.data.tool_call_id,
                        "toolName": event.data.tool_name,
                        "output": event.data.output,
                        "providerMetadata": provider_metadata(
                            tool_family=event.data.tool_family,
                            tool_name=event.data.tool_name,
                        ),
                    }
                )
                return
        if isinstance(event, StepEnd) and event.kind == "model_call":
            self._enqueue_payload({"type": "finish-step"})
            return
        if isinstance(event, RunEnd):
            if event.status != "finished":
                if event.error:
                    self._enqueue_payload({"type": "error", "errorText": event.error})
                self._enqueue_payload({"type": "finish"})
                self._enqueue_done()
                return
            if self._text_started and not self._text_ended:
                self._enqueue_payload({"type": "text-end", "id": _message_id(event.run_id)})
            self._enqueue_payload(
                {
                    "type": "finish",
                    "finishReason": "stop",
                    "messageMetadata": _message_metadata(
                        run_id=event.run_id,
                        thread_id=self._metadata_thread_id(event.thread_id),
                    ),
                }
            )
            self._enqueue_done()

    def _metadata_thread_id(self, event_thread_id: str | None) -> str:
        return event_thread_id or self._thread_id or ""

    def _emit_tool_input_start(self, run_id: str, tool_call_id: str) -> None:
        if tool_call_id in self._started_tool_inputs:
            return
        self._started_tool_inputs.add(tool_call_id)
        self._enqueue_payload(
            {
                "type": "tool-input-start",
                "id": _message_id(run_id),
                "toolCallId": tool_call_id,
            }
        )

    def _enqueue_payload(self, payload: dict[str, object]) -> None:
        self._loop.call_soon_threadsafe(
            self._queue.put_nowait,
            _sse(payload),
        )

    def _enqueue_done(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._loop.call_soon_threadsafe(self._queue.put_nowait, "data: [DONE]\n\n")
        self._loop.call_soon_threadsafe(self._queue.put_nowait, None)


class ChannelResponseSink:
    """Progressively deliver one response through one channel binding."""

    wants_stream = True
    _TYPING_INTERVAL_SEC = 4.0
    _SENDER_POLL_INTERVAL_SEC = 0.1

    def __init__(
        self,
        *,
        plugin: ChannelPlugin | None,
        target: ReplyTarget,
        channel_context: ChannelContext,
        binding_name: str,
    ) -> None:
        self._plugin = plugin
        self._target = target
        self._channel_context = channel_context
        self._binding_name = binding_name
        self._remote_id: str | None = None
        self._text = ""
        self._delivered_text = ""
        self._last_typing_at = 0.0
        self._typing_requested = False
        self._finished = False
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._sender: threading.Thread | None = None

    def on_event(self, event: TraceEvent) -> None:
        if isinstance(event, RunStart):
            self._start_sender()
            self._request_typing(force=True)
            return
        if isinstance(event, PartDelta) and isinstance(event.delta, TextDelta):
            with self._lock:
                self._text += event.delta.text
            self._wake.set()
            return
        if isinstance(event, StepEnd) and event.kind == "model_call":
            message = message_data_for_step(
                run_id=event.run_id,
                thread_id=event.thread_id,
                step_index=event.step_index,
                kind=event.kind,
                output=event.output,
                created_at=event.finished_at,
                error=event.error,
            )
            if message is not None:
                with self._lock:
                    text = message_text(message.parts).strip()
                    if text:
                        self._text = text
                self._wake.set()
            return
        if isinstance(event, (PartStart, PartDelta, PartEnd)):
            is_tool_event = (
                (isinstance(event, PartStart) and event.kind in {"tool_call", "tool_result"})
                or (isinstance(event, PartDelta) and isinstance(event.delta, ToolCallDelta))
                or (
                    isinstance(event, PartEnd)
                    and isinstance(event.data, (ToolCallPart, ToolResultPart))
                )
            )
            if is_tool_event:
                self._request_typing()
            return
        if isinstance(event, RunEnd):
            with self._lock:
                if event.status != "finished" and event.error and not self._text.strip():
                    self._text = event.error
                self._finished = True
            self._wake.set()
            if self._sender is not None:
                self._sender.join()

    def _start_sender(self) -> None:
        if self._sender is not None:
            return
        self._sender = threading.Thread(
            target=self._run_sender,
            name=f"toolang-response-{self._binding_name}",
            daemon=True,
        )
        self._sender.start()

    def _run_sender(self) -> None:
        while True:
            self._wake.wait(self._SENDER_POLL_INTERVAL_SEC)
            self._wake.clear()
            with self._lock:
                current_text = self._text.strip()
                finished = self._finished
                typing_requested = self._typing_requested
                delivered_text = self._delivered_text
            if typing_requested or (not finished and not delivered_text):
                self._send_typing(force=typing_requested)
                with self._lock:
                    self._typing_requested = False
            if current_text and current_text != delivered_text:
                self._deliver_text(current_text)
            if finished and current_text == self._delivered_text:
                return

    def _deliver_text(self, current_text: str) -> None:
        message_meta: dict[str, Any] = {}
        if self._remote_id is not None:
            message_meta["replace_remote_id"] = self._remote_id
        self._deliver(OutboundMessage(text=current_text, meta=message_meta))
        self._delivered_text = current_text

    def _send_typing(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_typing_at < self._TYPING_INTERVAL_SEC:
            return
        self._last_typing_at = now
        self._deliver(OutboundMessage(text="", meta={"action": "typing"}))

    def _deliver(self, message: OutboundMessage) -> None:
        if self._plugin is None:
            return
        try:
            result = self._plugin.deliver(
                self._target,
                message,
                self._channel_context,
            )
        except Exception:
            return
        if result.ok and result.remote_id is not None and message.meta.get("action") != "typing":
            self._remote_id = result.remote_id

    def _request_typing(self, *, force: bool = False) -> None:
        with self._lock:
            self._typing_requested = True
            if force:
                self._last_typing_at = 0.0
        self._wake.set()


def build_channel_response_sink(
    context,
    *,
    binding_name: str,
    target: ReplyTarget | None,
) -> ChannelResponseSink | None:
    """Build one channel response sink from one resolved reply target."""

    if target is None:
        return None
    return ChannelResponseSink(
        plugin=context.channel_plugins.get(binding_name),
        target=target,
        channel_context=context.channel_context(binding_name),
        binding_name=binding_name,
    )


def _message_metadata(*, run_id: str, thread_id: str) -> dict[str, str]:
    return {
        "id": run_id,
        "threadId": thread_id,
        "runId": run_id,
        "role": "assistant",
    }


def _message_id(run_id: str) -> str:
    return run_id


def provider_metadata(*, tool_family: str, tool_name: str) -> dict[str, Any]:
    return {
        "toolang": {
            "toolFamily": tool_family,
            "toolName": tool_name,
        }
    }


def _sse(payload: dict[str, object]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n\n"
