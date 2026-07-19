from __future__ import annotations

from email.message import Message
from io import BytesIO
import json
from pathlib import Path
import threading
from types import SimpleNamespace
from typing import Any, cast
from urllib.error import HTTPError
from urllib.request import Request

import pytest
import click

import toolang.cli.common.client as client_module
from toolang.cli.common.client import (
    RuntimeClient,
    RuntimeClientError,
    message_payload,
    owned_runtime_client,
)
from toolang.up import process as agents


def test_runtime_client_get_requires_json_object(monkeypatch: pytest.MonkeyPatch) -> None:
    responses = iter((b"not json", b"[]"))
    monkeypatch.setattr(
        client_module,
        "urlopen",
        lambda *_args, **_kwargs: BytesIO(next(responses)),
    )
    client = RuntimeClient("http://runtime/")

    with pytest.raises(RuntimeClientError, match="invalid JSON"):
        client.get("/invalid")
    with pytest.raises(RuntimeClientError, match="non-object"):
        client.get("/list")


def test_runtime_client_post_sends_json_request(monkeypatch: pytest.MonkeyPatch) -> None:
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
    response = BytesIO(
        b'data: {"type":"first"}\n\n'
        b'data: {"type":\n'
        b'data: "last"}'
    )
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


def test_runtime_client_invoke_returns_record_and_forwards_trace_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = [
        {
            "type": "run_starting",
            "payload": {
                "run": "run_1",
                "cmd": 0,
                "parent": None,
                "thread": "script_1",
                "input": {"role": "user", "parts": [{"type": "text", "text": "hello"}]},
                "context": {"origin": "script"},
                "created_at": "2026-01-01T00:00:00Z",
            },
        },
        {
            "type": "run_end",
            "payload": {
                "run": "run_1",
                "status": "finished",
                "input": {"cmd": 0},
                "output": {"step": "run_1/1"},
                "finished_at": "2026-01-01T00:00:01Z",
            },
        },
    ]
    monkeypatch.setattr(
        RuntimeClient,
        "events",
        lambda *_args, **_kwargs: iter(events),
    )
    forwarded = []

    record = RuntimeClient("http://runtime").invoke(
        {"executable_name": "demo"}, on_event=forwarded.append
    )

    assert record.run_id == "run_1"
    assert record.status == "finished"
    assert record.output is not None and record.output.step == "run_1/1"
    assert [event.type for event in forwarded] == ["run_starting", "run_end"]


def test_owned_runtime_client_cleans_up_failed_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stopped: dict[str, object] = {}
    sandbox_plugin = object()
    startup = SimpleNamespace(hosting=SimpleNamespace(plugin=sandbox_plugin))
    monkeypatch.setattr(
        "toolang.up.server.build_run_argv",
        lambda *_args, **_kwargs: ("run", "alice"),
    )
    monkeypatch.setattr(
        agents.AgentProcess,
        "start",
        lambda *_args, **_kwargs: agents.AgentStatus(
            name="alice",
            status="failed",
            endpoint="http://localhost:7001",
            api_url=None,
            webui_url=None,
            sandbox="docker",
        ),
    )
    monkeypatch.setattr(
        agents.AgentProcess,
        "stop",
        lambda _self, **kwargs: stopped.update(kwargs) or True,
    )

    with pytest.raises(click.ClickException, match="agent API failed to start"):
        with owned_runtime_client(
            root=tmp_path,
            name="alice",
            startup=cast(Any, startup),
            environ={},
            log_path=tmp_path / "agent.log",
        ):
            raise AssertionError("failed runtime must not be yielded")

    assert stopped == {"sandbox_plugin": sandbox_plugin, "force": True}


def test_owned_runtime_client_cleans_up_when_start_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stopped: dict[str, object] = {}
    sandbox_plugin = object()
    startup = SimpleNamespace(hosting=SimpleNamespace(plugin=sandbox_plugin))
    monkeypatch.setattr(
        "toolang.up.server.build_run_argv",
        lambda *_args, **_kwargs: ("run", "alice"),
    )
    monkeypatch.setattr(
        agents.AgentProcess,
        "start",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(TimeoutError("timed out")),
    )
    monkeypatch.setattr(
        agents.AgentProcess,
        "stop",
        lambda _self, **kwargs: stopped.update(kwargs) or True,
    )

    with pytest.raises(TimeoutError, match="timed out"):
        with owned_runtime_client(
            root=tmp_path,
            name="alice",
            startup=cast(Any, startup),
            environ={},
            log_path=tmp_path / "agent.log",
        ):
            raise AssertionError("failed runtime must not be yielded")

    assert stopped == {"sandbox_plugin": sandbox_plugin, "force": True}


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
