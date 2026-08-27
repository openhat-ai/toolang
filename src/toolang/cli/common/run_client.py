"""CLI ownership of one local or remote transport-neutral run client."""

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

from .execution_runtime import ExecutionRuntime
from .remote_runtime import inspect_remote_runtime


@asynccontextmanager
async def open_run_client(
    layout: AgentLayout,
    runtime: ExecutionRuntime,
    *,
    model_catalog: Path | None = None,
) -> AsyncIterator[RunClient]:
    """Open the RunClient implementation selected by one execution runtime."""

    if runtime.mode == "remote":
        if runtime.endpoint is None:  # pragma: no cover - runtime value invariant
            raise RuntimeError("remote execution runtime has no endpoint")
        async with httpx.AsyncClient(timeout=httpx.Timeout(3.0)) as http:
            client = RemoteRunClient(runtime.endpoint, client=http)
            await client.connect()
            try:
                await inspect_remote_runtime(
                    http,
                    client.endpoint,
                    expected_sandbox=runtime.sandbox,
                )
                yield client
            finally:
                await client.disconnect()
        return

    store = RunStore(layout.run_store)
    setup = SetupWatcher(
        layout,
        sandbox=runtime.sandbox,
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
