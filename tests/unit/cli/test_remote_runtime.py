"""Shared remote AgentServer health and identity validation."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from toolang.cli.common.remote_runtime import (
    RemoteRuntimeError,
    inspect_remote_runtime,
)


def test_remote_runtime_rejects_a_profile_sandbox_mismatch() -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"ok": True})
        if request.url.path == "/api/v1/profile":
            return httpx.Response(
                200,
                json={
                    "runtime": {
                        "version": "v0.3.0",
                        "sandbox": {
                            "driver": "docker",
                            "selector": "docker:python:3.13-slim",
                            "instance": "a1b2c3d4e5f6",
                            "description": None,
                        },
                    }
                },
            )
        raise AssertionError(f"unexpected request: {request.url}")

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(
                RemoteRuntimeError,
                match="sandbox does not match its runtime status",
            ):
                await inspect_remote_runtime(
                    client,
                    "http://runtime.test:7001",
                    expected_sandbox="host",
                )

    asyncio.run(scenario())

    assert requests == ["/healthz", "/api/v1/profile"]
