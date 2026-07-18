"""HTTP and SSE client shared by Toolang CLI surfaces."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping
import json
import threading
from typing import Any, cast
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

import click
import typer

from toolang.agent import local as agents
from toolang.base.types.message import Message
from ...execution.events import TraceEvent, trace_event_from_data
from .context import context_root, require_prefix_agent, ui_base_url


class RuntimeClientError(RuntimeError):
    pass


class RuntimeClient:
    def __init__(self, endpoint: str) -> None:
        self.endpoint = endpoint.rstrip("/")

    def get(self, path: str, *, timeout: float | None = 30) -> dict[str, Any]:
        try:
            with urlopen(f"{self.endpoint}{path}", timeout=timeout) as response:
                return _json_object(response.read())
        except HTTPError as exc:
            raise _http_error(exc) from exc
        except URLError as exc:
            raise RuntimeClientError(f"runtime request failed: {exc.reason}") from exc

    def post(
        self,
        path: str,
        *,
        payload: Mapping[str, object],
        timeout: float | None = 60,
    ) -> dict[str, Any]:
        request = self._request(path, payload)
        try:
            with urlopen(request, timeout=timeout) as response:
                return _json_object(response.read())
        except HTTPError as exc:
            raise _http_error(exc) from exc
        except URLError as exc:
            raise RuntimeClientError(f"runtime request failed: {exc.reason}") from exc

    def lines(
        self,
        path: str,
        *,
        payload: Mapping[str, object] | None = None,
    ) -> Iterator[str]:
        request: str | Request = (
            self._request(path, payload)
            if payload is not None
            else f"{self.endpoint}{path}"
        )
        try:
            with urlopen(request, timeout=60) as response:
                for raw_line in response:
                    line = raw_line.decode("utf-8", errors="replace").rstrip()
                    if line:
                        yield line
        except HTTPError as exc:
            raise _http_error(exc) from exc
        except URLError as exc:
            raise RuntimeClientError(f"runtime request failed: {exc.reason}") from exc

    def events(
        self,
        path: str,
        *,
        payload: Mapping[str, object] | None = None,
        stop: threading.Event | None = None,
    ) -> Iterator[dict[str, Any]]:
        request: str | Request = (
            self._request(path, payload)
            if payload is not None
            else f"{self.endpoint}{path}"
        )
        stop_event = stop or threading.Event()
        try:
            with urlopen(request, timeout=None) as response:
                yield from _sse_events(response, stop=stop_event)
        except HTTPError as exc:
            raise _http_error(exc) from exc
        except URLError as exc:
            raise RuntimeClientError(f"runtime request failed: {exc.reason}") from exc

    def consume(
        self,
        path: str,
        *,
        payload: Mapping[str, object],
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        for event in self.events(path, payload=payload):
            if on_event is not None:
                on_event(event)

    def list_models(self) -> Mapping[str, Any]:
        return self.get("/api/v1/chat/models")

    def list_executables(self, kind: str) -> Mapping[str, Any]:
        return self.get(f"/api/v1/chat/{kind}s")

    def create_thread(self) -> str:
        result = self.post("/api/v1/threads", payload={"client": "tui"})
        thread_id = result.get("thread_id")
        if not isinstance(thread_id, str):
            raise RuntimeClientError("runtime did not return a thread id")
        return thread_id

    def start_run(
        self,
        thread_id: str,
        message: str,
        selects: Mapping[str, object],
        on_event: Callable[[TraceEvent], None],
        on_error: Callable[[str], None],
    ) -> None:
        try:
            self.consume(
                "/api/v1/chat/stream",
                payload={
                    "thread": thread_id,
                    "client": "tui",
                    "request_id": f"term_{uuid4().hex}",
                    "message": message_payload(message),
                    **selects,
                },
                on_event=lambda event: on_event(trace_event_from_data(event)),
            )
        except Exception as exc:
            on_error(_error_message(exc))

    def stop_run(
        self,
        run_id: str,
        on_event: Callable[[TraceEvent], None],
        on_error: Callable[[str], None],
    ) -> None:
        try:
            result = self.post(
                f"/api/v1/runs/{run_id}/cancel",
                payload={"request_id": f"req_{uuid4().hex}"},
            )
            if event := _command_trace_event(result.get("input")):
                on_event(event)
        except Exception as exc:
            on_error(_error_message(exc))

    def steer_run(
        self,
        run_id: str,
        message: str,
        on_event: Callable[[TraceEvent], None],
        on_error: Callable[[str], None],
    ) -> None:
        try:
            result = self.post(
                f"/api/v1/runs/{run_id}/steer",
                payload={
                    "request_id": f"req_{uuid4().hex}",
                    "message": message_payload(message),
                },
            )
            if event := _command_trace_event(result.get("input")):
                on_event(event)
        except Exception as exc:
            on_error(_error_message(exc))

    def _request(self, path: str, payload: Mapping[str, object]) -> Request:
        return Request(
            f"{self.endpoint}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )


def runtime_client(ctx: typer.Context) -> RuntimeClient:
    client = running_runtime_client(ctx)
    if client is None:
        raise click.ClickException(f"agent is not running: {require_prefix_agent(ctx)}")
    return client


def running_runtime_client(ctx: typer.Context) -> RuntimeClient | None:
    """Return a client only when the selected agent has a running HTTP runtime."""

    agent = require_prefix_agent(ctx)
    status = agents.AgentProcess(context_root(ctx), agent).status(
        ui_base_url=ui_base_url()
    )
    if status is None or status.status != "running" or status.endpoint is None:
        return None
    return RuntimeClient(status.endpoint)


def runtime_get(ctx: typer.Context, path: str) -> dict[str, Any]:
    try:
        return runtime_client(ctx).get(path)
    except RuntimeClientError as exc:
        raise click.ClickException(str(exc)) from exc


def runtime_post(
    ctx: typer.Context, path: str, *, payload: Mapping[str, object]
) -> dict[str, Any]:
    try:
        return runtime_client(ctx).post(path, payload=payload)
    except RuntimeClientError as exc:
        raise click.ClickException(str(exc)) from exc


def message_payload(text: str) -> dict[str, object]:
    return Message.user(text).to_data()


def _sse_events(
    response: Iterable[bytes], *, stop: threading.Event
) -> Iterator[dict[str, Any]]:
    data_lines: list[str] = []
    for raw_line in response:
        if stop.is_set():
            break
        line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
        if line == "":
            if data_lines:
                if event := _decode_sse_data(data_lines):
                    yield event
                data_lines = []
            continue
        if line.startswith("data:"):
            data_lines.append(line.removeprefix("data:").removeprefix(" "))
    if data_lines and not stop.is_set():
        if event := _decode_sse_data(data_lines):
            yield event


def _decode_sse_data(data_lines: list[str]) -> dict[str, Any] | None:
    try:
        event = json.loads("\n".join(data_lines))
    except json.JSONDecodeError:
        return None
    return cast(dict[str, Any], event) if isinstance(event, dict) else None


def _json_object(payload: bytes) -> dict[str, Any]:
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeClientError("runtime returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise RuntimeClientError("runtime returned a non-object JSON response")
    return cast(dict[str, Any], value)


def _command_trace_event(payload: object) -> TraceEvent | None:
    if not isinstance(payload, Mapping):
        return None
    event_payload = cast(Mapping[str, object], payload)
    event_type = event_payload.get("type")
    if not isinstance(event_type, str):
        return None
    return trace_event_from_data({"type": event_type, "payload": event_payload})


def _http_error(exc: HTTPError) -> RuntimeClientError:
    body = exc.read().decode("utf-8", errors="replace").strip()
    detail = body or str(exc.reason)
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        pass
    else:
        if isinstance(payload, Mapping):
            value = payload.get("detail")
            if isinstance(value, str):
                detail = value
    return RuntimeClientError(f"runtime request failed: {exc.code} {detail}")


def _error_message(exc: Exception) -> str:
    return (
        str(exc)
        if isinstance(exc, RuntimeClientError)
        else f"{type(exc).__name__}: {exc}"
    )
