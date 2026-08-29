"""CLI acquisition of one local or AgentServer-backed run client."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx

from toolang.common.ids import IdIssuer
from toolang.common.layout import AgentLayout
from toolang.execution.client import LocalRunClient, RunClient
from toolang.execution.executor import RunExecutor
from toolang.execution.remote import RemoteRunClient
from toolang.execution.store import RunStore
from toolang.setup import SetupWatcher
from toolang.state.watcher import StateWatcher

from .agent_server import AgentServerRef
from .remote_runtime import inspect_remote_runtime


@asynccontextmanager
async def acquire_run_client(
    layout: AgentLayout,
    server: AgentServerRef | None,
    *,
    model_catalog: Path | None = None,
) -> AsyncIterator[RunClient]:
    """Acquire a run client for host embedding or one acquired AgentServer."""

    if server is not None:
        async with httpx.AsyncClient(timeout=httpx.Timeout(3.0)) as http:
            client = RemoteRunClient(server.endpoint, client=http)
            await client.connect()
            try:
                await inspect_remote_runtime(
                    http,
                    client.endpoint,
                    expected_sandbox=server.sandbox,
                )
                yield client
            finally:
                await client.disconnect()
        return

    store = RunStore(layout.run_store)
    setup = SetupWatcher(
        layout,
        sandbox="host",
        model_catalog=model_catalog,
    )
    state = StateWatcher(layout)
    try:
        await state.refresh()
        await setup.refresh()
        executor = RunExecutor(
            store,
            IdIssuer(layout.id_state),
            setup=setup.current,
            state=state.current,
            load_state=state.load,
            refresh_state=state.refresh_result,
        )
        client = LocalRunClient(executor)
        executor.start()
        await client.connect()
        try:
            yield client
        finally:
            await client.disconnect()
            await executor.stop()
    finally:
        store.close()
