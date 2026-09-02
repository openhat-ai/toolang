"""CLI acquisition of one local or AgentServer-backed run client."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import os
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
from toolang.up.types import AgentServerRef

from .remote_runtime import inspect_remote_runtime
from .context import load_runtime_environ
from .policy import (
    resolve_ceiling_overrides,
    resolve_default_overrides,
    resolve_limit_overrides,
)


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
    environ = load_runtime_environ(layout, base_environ=os.environ)
    allow_overrides = resolve_ceiling_overrides(environ)
    setup = SetupWatcher(
        layout,
        sandbox="host",
        model_catalog=model_catalog,
        allow_overrides={
            name: value
            for name, value in allow_overrides.items()
            if name in {"models", "tools"}
        },
        default_overrides=resolve_default_overrides(environ),
        limit_overrides=resolve_limit_overrides(environ),
    )
    state = StateWatcher(
        layout,
        allow_overrides={
            name: value
            for name, value in allow_overrides.items()
            if name in {"psyches", "skills", "services", "prompts"}
        },
    )
    try:
        await asyncio.gather(state.refresh(), setup.refresh())
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
