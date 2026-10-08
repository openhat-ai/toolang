"""CLI RunClient acquisition tests."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from toolang.cli.common import run_client
from toolang.execution.remote import RemoteRunClient
from toolang.up.types import AgentServerRef


def test_acquire_run_client_connects_to_an_agent_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = AgentServerRef(
        sandbox="docker:python:3.13-slim",
        endpoint="http://runtime.test:7001",
    )
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
                            "selector": server.sandbox,
                            "instance": "a1b2c3d4e5f6",
                            "description": None,
                        },
                    }
                },
            )
        raise AssertionError(f"unexpected request: {request.url}")

    async_client = httpx.AsyncClient

    def client_factory(*, timeout: httpx.Timeout) -> httpx.AsyncClient:
        return async_client(
            timeout=timeout,
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr(run_client.httpx, "AsyncClient", client_factory)

    async def scenario() -> None:
        async with run_client.acquire_run_client(server) as client:
            assert isinstance(client, RemoteRunClient)
            assert client.connected
        assert not client.connected

    asyncio.run(scenario())

    assert requests == ["/healthz", "/api/v1/profile"]
