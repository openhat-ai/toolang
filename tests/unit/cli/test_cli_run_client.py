"""CLI RunClient acquisition tests."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from toolang.cli.common import run_client
from toolang.common.layout import AgentLayout
from toolang.execution.remote import RemoteRunClient
from toolang.up.types import AgentServerRef


def test_acquire_run_client_uses_local_embedding_without_a_server(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = AgentLayout.resident(tmp_path, "alice")
    catalog = tmp_path / "models.json"
    store = Mock()
    setup = Mock(refresh=AsyncMock())
    state = Mock(refresh=AsyncMock())
    executor = Mock(stop=AsyncMock())
    local_client = Mock(connect=AsyncMock(), disconnect=AsyncMock())

    def open_store(path: Path) -> Mock:
        assert path == layout.run_store
        return store

    def open_setup(
        selected: AgentLayout,
        *,
        sandbox: str,
        model_catalog: Path | None,
        allow_overrides: object,
        default_overrides: object,
        limit_overrides: object,
    ) -> Mock:
        assert selected == layout
        assert sandbox == "host"
        assert model_catalog == catalog
        assert allow_overrides == {}
        assert default_overrides == {}
        assert limit_overrides == {}
        return setup

    def open_state(selected: AgentLayout, *, allow_overrides: object) -> Mock:
        assert selected == layout
        assert allow_overrides == {}
        return state

    def open_executor(
        selected_store: object,
        _ids: object,
        **_kwargs: object,
    ) -> Mock:
        assert selected_store is store
        return executor

    def open_client(selected: object) -> Mock:
        assert selected is executor
        return local_client

    monkeypatch.setattr(run_client, "RunStore", open_store)
    monkeypatch.setattr(run_client, "SetupWatcher", open_setup)
    monkeypatch.setattr(run_client, "StateWatcher", open_state)
    monkeypatch.setattr(run_client, "RunExecutor", open_executor)
    monkeypatch.setattr(run_client, "LocalRunClient", open_client)
    monkeypatch.setattr(
        run_client,
        "load_runtime_environ",
        lambda *_args, **_kwargs: {},
    )

    async def scenario() -> None:
        async with run_client.acquire_run_client(
            layout,
            None,
            model_catalog=catalog,
        ) as selected:
            assert selected is local_client
            store.close.assert_not_called()
            executor.stop.assert_not_awaited()

    asyncio.run(scenario())

    state.refresh.assert_awaited_once_with()
    setup.refresh.assert_awaited_once_with()
    executor.start.assert_called_once_with()
    local_client.connect.assert_awaited_once_with()
    local_client.disconnect.assert_awaited_once_with()
    executor.stop.assert_awaited_once_with()
    store.close.assert_called_once_with()


def test_acquire_run_client_connects_to_an_agent_server(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = AgentLayout.resident(tmp_path, "alice")
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
        async with run_client.acquire_run_client(layout, server) as client:
            assert isinstance(client, RemoteRunClient)
            assert client.connected
        assert not client.connected

    asyncio.run(scenario())

    assert requests == ["/healthz", "/api/v1/profile"]
