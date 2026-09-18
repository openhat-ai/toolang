"""Thread titles read back for the tmux pane marks."""

from __future__ import annotations

import httpx
import pytest

from toolang.cli.toolang.commands.chat import remote

_HOST_DESCRIPTION = "macOS 27.0 arm64"


def _profile() -> dict[str, object]:
    return {
        "runtime": {
            "version": "v0.3.9",
            "sandbox": {
                "driver": "host",
                "selector": "host",
                "instance": None,
                "description": _HOST_DESCRIPTION,
            },
        }
    }


def _session(thread_payload: object, *, status: int = 200) -> remote.RemoteChatSession:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"ok": True})
        if request.url.path == "/api/v1/profile":
            return httpx.Response(200, json=_profile())
        if request.url.path == "/api/v1/runs/defaults":
            return httpx.Response(
                200,
                json={
                    "model": {
                        "ref": "test/model",
                    },
                    "runnable": "agic:chat",
                    "policy": {"allow": [], "limits": {}},
                },
            )
        if request.url.path.startswith("/api/v1/threads/"):
            return httpx.Response(status, json=thread_payload)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    return remote.RemoteChatSession(
        "http://runtime.test:7001",
        expected_sandbox="host",
        transport=httpx.MockTransport(handler),
    )


def test_remote_thread_title_reads_a_thread_with_runs() -> None:
    session = _session({"title": "hello world", "run_count": 2})
    try:
        assert session.thread_title("term_x") == "hello world"
    finally:
        session.close()


@pytest.mark.parametrize(
    "payload",
    (
        {"title": "hello world", "run_count": 0},
        {"title": "", "run_count": 3},
        {"run_count": 3},
        {},
    ),
)
def test_remote_thread_title_ignores_threads_without_a_title(
    payload: dict[str, object],
) -> None:
    session = _session(payload)
    try:
        assert session.thread_title("term_x") is None
    finally:
        session.close()


def test_remote_thread_title_survives_a_missing_thread() -> None:
    session = _session({"detail": "thread not found"}, status=404)
    try:
        assert session.thread_title("term_missing") is None
    finally:
        session.close()


def test_remote_thread_title_survives_a_transport_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"ok": True})
        if request.url.path == "/api/v1/profile":
            return httpx.Response(200, json=_profile())
        if request.url.path == "/api/v1/runs/defaults":
            return httpx.Response(
                200,
                json={
                    "model": {
                        "ref": "test/model",
                    },
                    "runnable": "agic:chat",
                    "policy": {"allow": [], "limits": {}},
                },
            )
        raise httpx.ConnectError("runtime is gone")

    session = remote.RemoteChatSession(
        "http://runtime.test:7001",
        expected_sandbox="host",
        transport=httpx.MockTransport(handler),
    )
    try:
        assert session.thread_title("term_x") is None
    finally:
        session.close()
