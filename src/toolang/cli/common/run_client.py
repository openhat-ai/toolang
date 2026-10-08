"""CLI acquisition of one AgentServer-backed run client."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx

from toolang.execution.remote import RemoteRunClient
from toolang.up.types import AgentServerRef

from .remote_runtime import inspect_remote_runtime


@asynccontextmanager
async def acquire_run_client(server: AgentServerRef) -> AsyncIterator[RemoteRunClient]:
    """Connect to the acquired agent without constructing an embedded executor."""

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
