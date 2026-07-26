from __future__ import annotations

from email.message import Message
from io import BytesIO
import json
import threading
from typing import Any, cast
from urllib.error import HTTPError
from urllib.request import Request

import pytest
import toolang.cli.common.client as client_module
from toolang.cli.common.client import (
    RuntimeClient,
    message_payload,
)
from toolang.cli.common.errors import RuntimeClientError


def test_runtime_client_get_accepts_json_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = iter((b"not json", b"[]"))
    monkeypatch.setattr(
        client_module,
        "urlopen",
        lambda *_args, **_kwargs: BytesIO(next(responses)),
    )
    client = RuntimeClient("http://runtime/")

    with pytest.raises(RuntimeClientError, match="invalid JSON"):
        client.get("/invalid")
    assert client.get("/list") == []


def test_runtime_client_post_sends_json_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def open_request(request: Request, *, timeout: float | None) -> BytesIO:
        captured.update(
            url=request.full_url,
            method=request.method,
            content_type=request.headers.get("Content-type"),
            payload=json.loads(cast(bytes, request.data)),
            timeout=timeout,
        )
        return BytesIO(b'{"ok":true}')

    monkeypatch.setattr(client_module, "urlopen", open_request)

    result = RuntimeClient("http://runtime/").post(
        "/runs", payload={"message": "hello"}, timeout=12
    )

    assert result == {"ok": True}
    assert captured == {
        "url": "http://runtime/runs",
        "method": "POST",
        "content_type": "application/json",
        "payload": {"message": "hello"},
        "timeout": 12,
    }


def test_runtime_client_events_flushes_final_sse_event_at_eof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = BytesIO(b'data: {"type":"first"}\n\ndata: {"type":\ndata: "last"}')
    monkeypatch.setattr(client_module, "urlopen", lambda *_args, **_kwargs: response)

    events = list(RuntimeClient("http://runtime").events("/events"))

    assert events == [{"type": "first"}, {"type": "last"}]


def test_runtime_client_events_honors_stop_and_ignores_invalid_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop = threading.Event()
    stop.set()
    monkeypatch.setattr(
        client_module,
        "urlopen",
        lambda *_args, **_kwargs: BytesIO(b"data: invalid\n\n"),
    )

    assert list(RuntimeClient("http://runtime").events("/events", stop=stop)) == []


def test_runtime_client_extracts_http_error_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*_args: object, **_kwargs: object) -> BytesIO:
        raise HTTPError(
            "http://runtime/missing",
            404,
            "Not Found",
            Message(),
            BytesIO(b'{"detail":"run not found"}'),
        )

    monkeypatch.setattr(client_module, "urlopen", fail)

    with pytest.raises(
        RuntimeClientError,
        match="runtime request failed: 404 run not found",
    ):
        RuntimeClient("http://runtime").get("/missing")


def test_message_payload_uses_canonical_message_shape() -> None:
    assert message_payload("hello") == {
        "role": "user",
        "parts": [{"type": "text", "text": "hello"}],
    }
