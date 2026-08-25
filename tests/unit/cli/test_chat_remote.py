"""Resident remote Chat session composition and recovery."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from decimal import Decimal
import json

import httpx
import pytest
from pydantic import TypeAdapter

from toolang.base.types.message import TextPart
from toolang.cli.toolang.commands.chat import remote
from toolang.cli.toolang.commands.chat.base import (
    RunAccepted,
    RunBlocked,
    RunDisconnected,
    RunRecovered,
)
from toolang.execution.events import RunBegin, RunEnd, RunEvent, run_event_to_data
from toolang.execution.schemas import (
    RunControlRefData,
    RunDetail,
    ThreadControlRefData,
    ThreadInfo,
    ThreadPeerInfo,
)
from toolang.execution.types import ControlRef, Local, RunOverride


class _Bytes(httpx.AsyncByteStream):
    def __init__(self, *chunks: bytes) -> None:
        self._chunks = chunks

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            yield chunk


def _profile(
    *,
    driver: str = "host",
    instance: str | None = None,
) -> dict[str, object]:
    return {
        "runtime": {
            "version": "0.3.9",
            "sandbox": {"driver": driver, "instance": instance},
        }
    }


def _thread(thread_id: str = "term_remote") -> ThreadInfo:
    return ThreadInfo(
        id=thread_id,
        title="chat",
        created_at="2026-08-25T00:00:00Z",
        updated_at="2026-08-25T00:00:00Z",
        origin="chat",
        channel="terminal",
        status="idle",
        peer=ThreadPeerInfo(),
        created_by=ThreadControlRefData(thread=thread_id, index=0),
        head=ThreadControlRefData(thread=thread_id, index=0),
        run_count=0,
        latest_run=None,
        active_run=None,
    )


def _detail(
    run_id: str = "run_remote",
    *,
    status: str = "succeeded",
    output: Local | None = None,
) -> RunDetail:
    terminal = status not in {"pending", "running"}
    return RunDetail(
        id=run_id,
        parent=None,
        thread_id="term_remote",
        root_run_id=run_id,
        runnable_kind="agic",
        runnable_name="chat",
        call_kind="top",
        occurrence=None,
        input_text="hello",
        summary="remote answer",
        status=status,  # type: ignore[arg-type]
        error=None,
        ejected=None,
        created_at="2026-08-25T00:00:00Z",
        started_at="2026-08-25T00:00:00Z",
        finished_at="2026-08-25T00:00:01Z" if terminal else None,
        updated_at=("2026-08-25T00:00:01Z" if terminal else "2026-08-25T00:00:00Z"),
        control=RunControlRefData(run=run_id, index=0),
        output=output,
        controls=[],
        steps=[],
    )


def _begin(run_id: str = "run_remote") -> RunBegin:
    return RunBegin(
        run=run_id,
        control=ControlRef(run_id, 0),
        runnable="agic:chat",
        started_at="2026-08-25T00:00:00Z",
    )


def _end(run_id: str = "run_remote") -> RunEnd:
    return RunEnd(
        run=run_id,
        status="succeeded",
        finished_at="2026-08-25T00:00:01Z",
    )


def _stream(*events: RunEvent, run_id: str = "run_remote") -> httpx.Response:
    chunks = tuple(
        (
            f"event: {event.type}\ndata: {json.dumps(run_event_to_data(event))}\n\n"
        ).encode()
        for event in events
    )
    return httpx.Response(
        200,
        headers={
            "content-type": "text/event-stream",
            "X-Toolang-Run-ID": run_id,
        },
        stream=_Bytes(*chunks),
    )


def _json(value: object) -> object:
    return TypeAdapter(type(value)).dump_python(value, mode="json")


def test_remote_chat_non_run_operations_and_executor_label() -> None:
    requests: list[tuple[str, str, object | None]] = []
    result = _detail(
        output=Local.typed("Part[]", (TextPart("remote answer"),), "_"),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        requests.append((request.method, request.url.path, body))
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"ok": True})
        if request.url.path == "/api/v1/profile":
            return httpx.Response(
                200,
                json=_profile(driver="docker", instance="a1b2c3"),
            )
        if request.url.path == "/api/v1/models":
            return httpx.Response(
                200,
                json={
                    "default": "test/model",
                    "items": [{"selector": "test/model", "name": "test"}],
                },
            )
        if request.url.path == "/api/v1/agics":
            return httpx.Response(
                200,
                json={"default": "chat", "items": [{"name": "chat"}]},
            )
        if request.url.path == "/api/v1/flows":
            return httpx.Response(200, json={"default": None, "items": []})
        if request.url.path == "/api/v1/threads" and request.method == "POST":
            return httpx.Response(201, json={"thread": _json(_thread())})
        if request.url.path == "/api/v1/runs/authored/validate":
            return httpx.Response(204)
        if request.url.path in {
            "/api/v1/runs/run_remote",
            "/api/v1/threads/term_remote/result",
        }:
            return httpx.Response(200, json=_json(result))
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    session = remote.RemoteChatSession(
        "HTTP://runtime.test:7001/",
        expected_sandbox="docker:python:3.13-slim",
        transport=httpx.MockTransport(handler),
    )
    try:
        assert session.executor_label == "v0.3.9, :7001, docker(a1b2c3)"
        assert session.run_client is not None
        assert session.run_client.endpoint == "http://runtime.test:7001"
        assert session.list_models()["default"] == "test/model"
        assert session.list_executables("agic")["default"] == "chat"
        assert session.list_executables("runnable") == {
            "default": "agic:chat",
            "items": [{"kind": "agic", "name": "chat"}],
        }
        assert session.create_thread() == "term_remote"
        selects = session.apply_settings(
            (
                RunOverride("allow", "models", ("test/*",)),
                RunOverride("default", "model", "test/model"),
                RunOverride("limit", "cost", Decimal("2.50")),
            ),
            {},
        )
        assert session.get_result("run_remote", thread_id=None).output == (
            TextPart("remote answer"),
        )
        assert session.get_result(None, thread_id="term_remote").run_id == (
            "run_remote"
        )
        stored_overrides = selects["run_overrides"]
        assert isinstance(stored_overrides, tuple)
        assert stored_overrides == (
            RunOverride("allow", "models", ("test/*",)),
            RunOverride("default", "model", "test/model"),
            RunOverride("limit", "cost", Decimal("2.50")),
        )
    finally:
        session.close()

    validation = next(
        body
        for method, path, body in requests
        if method == "POST" and path == "/api/v1/runs/authored/validate"
    )
    assert validation == {
        "session_commands": [
            {"group": "allow", "field": "models", "value": ["test/*"]},
            {"group": "default", "field": "model", "value": "test/model"},
            {"group": "limit", "field": "cost", "value": "2.50"},
        ],
        "runnable_fallbacks": ["agic:chat", "default"],
    }


def test_remote_chat_uses_remote_run_client_native_events() -> None:
    detail = _detail()
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"ok": True})
        if request.url.path == "/api/v1/profile":
            return httpx.Response(200, json=_profile())
        if request.url.path == "/api/v1/runs/authored/stream":
            return _stream(_begin(), _end())
        if request.url.path == "/api/v1/runs/run_remote":
            return httpx.Response(200, json=_json(detail))
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    session = remote.RemoteChatSession(
        "http://runtime.test:7001",
        expected_sandbox="host",
        transport=httpx.MockTransport(handler),
    )
    events: list[RunEvent] = []
    states: list[object] = []
    errors: list[str] = []
    try:
        session.start_run(
            "term_remote",
            "hello",
            {},
            events.append,
            errors.append,
            states.append,
        )
    finally:
        session.close()

    assert session.executor_label == "v0.3.9, :7001"
    assert [type(item) for item in events] == [RunBegin, RunEnd]
    assert states == [RunAccepted("run_remote")]
    assert errors == []
    assert requests.count("/api/v1/runs/authored/stream") == 1


def test_remote_chat_recovers_without_replaying_or_retrying(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(remote, "_RECOVERY_DELAYS", (0.0, 0.0, 0.0))
    monkeypatch.setattr(remote, "_RECOVERY_INTERVAL", 0.0)
    terminal = _detail(
        output=Local.typed("Part[]", (TextPart("durable"),), "_"),
    )
    details = iter(
        (
            replace(terminal, status="running", finished_at=None),
            terminal,
        )
    )
    submissions = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal submissions
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"ok": True})
        if request.url.path == "/api/v1/profile":
            return httpx.Response(200, json=_profile())
        if request.url.path == "/api/v1/runs/authored/stream":
            submissions += 1
            return _stream(_begin())
        if request.url.path == "/api/v1/runs/run_remote":
            return httpx.Response(200, json=_json(next(details)))
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    session = remote.RemoteChatSession(
        "http://runtime.test:7001",
        expected_sandbox="host",
        transport=httpx.MockTransport(handler),
    )
    events: list[RunEvent] = []
    states: list[object] = []
    errors: list[str] = []
    try:
        session.start_run(
            "term_remote",
            "hello",
            {},
            events.append,
            errors.append,
            states.append,
        )
    finally:
        session.close()

    assert submissions == 1
    assert [type(item) for item in events] == [RunBegin]
    assert [type(item) for item in states] == [
        RunAccepted,
        RunDisconnected,
        RunRecovered,
    ]
    assert isinstance(states[-1], RunRecovered)
    assert states[-1].detail == terminal
    assert errors == []


def test_remote_chat_blocks_ambiguous_pre_acceptance_failure() -> None:
    submissions = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal submissions
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"ok": True})
        if request.url.path == "/api/v1/profile":
            return httpx.Response(200, json=_profile())
        if request.url.path == "/api/v1/runs/authored/stream":
            submissions += 1
            raise httpx.ReadError("private transport detail", request=request)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    session = remote.RemoteChatSession(
        "http://runtime.test:7001",
        expected_sandbox="host",
        transport=httpx.MockTransport(handler),
    )
    states: list[object] = []
    errors: list[str] = []
    try:
        for _ in range(2):
            session.start_run(
                "term_remote",
                "hello",
                {},
                lambda _event: None,
                errors.append,
                states.append,
            )
    finally:
        session.close()

    assert submissions == 1
    assert len(states) == 2
    assert all(isinstance(item, RunBlocked) for item in states)
    assert errors == []
    assert "private transport detail" not in states[0].message
